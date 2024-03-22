import torch
import argparse
import json
import os
import time
import pickle


from tokenlizer import DEFAULT_SP, Tokenizer
from torch.utils.data import DataLoader
from data_utils import load_data, fix_seed, check_early_stop
from torch.nn import TransformerDecoderLayer, TransformerDecoder
from torch.nn import TransformerEncoderLayer, TransformerEncoder
from torch.optim.lr_scheduler import ExponentialLR
from model import PositionalEncoding


class Model(torch.nn.Module):
    def __init__(self, n_layer, n_heads, n_dim, tokens, dropout):
        super(Model, self).__init__()
        self.word_emb = torch.nn.Embedding(tokens, n_dim)
        self.pos_enc = PositionalEncoding(n_dim, dropout=dropout, maxlen=2000)
        enc_lay = TransformerEncoderLayer(
            n_dim, n_heads, batch_first=True,
            dim_feedforward=n_dim << 1, dropout=dropout
        )
        self.encoder = TransformerEncoder(enc_lay, n_layer)
        decode_layer = TransformerDecoderLayer(
            d_model=n_dim, nhead=n_heads, batch_first=True,
            dim_feedforward=n_dim << 1, dropout=dropout
        )
        self.decoder = TransformerDecoder(decode_layer, n_layer)

    


def create_log_model(args):
    timestamp = time.time()
    detail_log_folder = os.path.join(args.base_log, 'transformer')
    if not os.path.exists(detail_log_folder):
        os.makedirs(detail_log_folder)
    detail_log_dir = os.path.join(detail_log_folder, f'log-{timestamp}.json')
    detail_model_dir = os.path.join(detail_log_folder, f'mod-{timestamp}.pth')
    token_path = os.path.join(detail_log_folder, f'token-{timestamp}.pkl')
    return detail_log_dir, detail_model_dir, token_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Graph Edit Exp, Sparse Model')
    parser.add_argument(
        '--dim', default=256, type=int,
        help='the hidden dim of model'
    )
    parser.add_argument(
        '--n_layer', default=8, type=int,
        help='the layer of encoder gnn'
    )
    parser.add_argument(
        '--token_path', type=str, default='',
        help='the path of a json containing all tokens'
    )
    parser.add_argument(
        '--heads', default=4, type=int,
        help='the number of heads for attention, only useful for gat'
    )
    parser.add_argument(
        '--warmup', default=1, type=int,
        help='the epoch of warmup'
    )
    parser.add_argument(
        '--gamma', default=0.998, type=float,
        help='the gamma of lr scheduler'
    )
    parser.add_argument(
        '--dropout', type=float, default=0.3,
        help='the dropout rate, useful for all backbone'
    )
    parser.add_argument(
        '--data_path', required=True, type=str,
        help='the path containing dataset'
    )
    parser.add_argument(
        '--seed', type=int, default=2023,
        help='the seed for training'
    )
    parser.add_argument(
        '--bs', type=int, default=512,
        help='the batch size for training'
    )
    parser.add_argument(
        '--epoch', type=int, default=200,
        help='the max epoch for training'
    )
    parser.add_argument(
        '--early_stop', default=0, type=int,
        help='number of epochs to judger early stop '
        ', will be ignored when it\'s less than 5'
    )
    parser.add_argument(
        '--device', default=-1, type=int,
        help='the device for running exps'
    )
    parser.add_argument(
        '--lr', default='1e-3', type=float,
        help='the learning rate for training'
    )
    parser.add_argument(
        '--base_log', default='log_exp', type=str,
        help='the base dir of logging'
    )
    parser.add_argument(
        '--accu', type=int, default=1,
        help='the number of batch accu'
    )
    parser.add_argument(
        '--step_start', type=int, default=50,
        help='the step of starting lr decay'
    )
    parser.add_argument(
        '--label_smoothing', type=float, default=0.0,
        help='the label smoothing for transformer training'
    )
    parser.add_argument(
        '--num_workers', type=int, default=4,
        help='the num of worker for dataloader'
    )

    args = parser.parse_args()
    print(args)
    log_dir, model_dir, token_dir = create_log_model(args)

    if not torch.cuda.is_available() or args.device < 0:
        device = torch.device('cpu')
    else:
        device = torch.device(f'cuda:{args.device}')

    fix_seed(args.seed)

    assert args.token_path != '', 'file containing all tokens are required'
    SP_TOKEN = DEFAULT_SP | set([f"<RXN>_{i}" for i in range(11)])

    with open(args.token_path) as Fin:
        tokenizer = Tokenizer(json.load(Fin), SP_TOKEN)

    train_rec, train_prod, train_rxn = load_data(args.data_path, 'train')
    val_rec, val_prod, val_rxn = load_data(args.data_path, 'val')
    test_rec, test_prod, test_rxn = load_data(args.data_path, 'test')

    print('[INFO] Data Loaded')

    train_set = RetroDataset(
        prod_sm=train_prod, reat_sm=train_rec, aug_prob=args.aug_prob,
        rxn_cls=train_rxn if args.use_class else None
    )
    valid_set = RetroDataset(
        prod_sm=val_prod, reat_sm=val_rec, aug_prob=0,
        rxn_cls=val_rxn if args.use_class else None
    )
    test_set = RetroDataset(
        prod_sm=test_prod, reat_sm=test_rec, aug_prob=0,
        rxn_cls=test_rxn if args.use_class else None
    )

    train_loader = DataLoader(
        train_set, collate_fn=col_fn_retro, batch_size=args.bs,
        shuffle=True, num_workers=args.num_workers
    )
    valid_loader = DataLoader(
        valid_set, collate_fn=col_fn_retro, batch_size=args.bs,
        shuffle=False, num_workers=args.num_workers
    )
    test_loader = DataLoader(
        test_set, collate_fn=col_fn_retro, batch_size=args.bs,
        shuffle=False, num_workers=args.num_workers
    )

    if args.transformer:
        if args.gnn_type == 'gin':
            gnn_args = {
                'in_channels': args.dim, 'out_channels': args.dim,
                'edge_dim': args.dim
            }
        elif args.gnn_type == 'gat':
            assert args.dim % args.heads == 0, \
                'The model dim should be evenly divided by num_heads'
            gnn_args = {
                'in_channels': args.dim, 'dropout': args.dropout,
                'out_channels': args.dim // args.heads, 'edge_dim': args.dim,
                'negative_slope': args.negative_slope, 'heads': args.heads
            }
        else:
            raise ValueError(f'Invalid GNN type {args.backbone}')

        GNN = MixFormer(
            emb_dim=args.dim, n_layers=args.n_layer, gnn_args=gnn_args,
            dropout=args.dropout, heads=args.heads, gnn_type=args.gnn_type,
            n_class=11 if args.use_class else None,
            update_gate=args.update_gate
        )
    else:
        if args.gnn_type == 'gin':
            GNN = GINBase(
                num_layers=args.n_layer, dropout=args.dropout,
                embedding_dim=args.dim,
                n_class=11 if args.use_class else None
            )
        elif args.gnn_type == 'gat':
            GNN = GATBase(
                num_layers=args.n_layer, dropout=args.dropout,
                embedding_dim=args.dim, num_heads=args.heads,
                negative_slope=args.negative_slope,
                n_class=11 if args.use_class else None
            )
        else:
            raise ValueError(f'Invalid GNN type {args.backbone}')

    decode_layer = TransformerDecoderLayer(
        d_model=args.dim, nhead=args.heads, batch_first=True,
        dim_feedforward=args.dim * 2, dropout=args.dropout
    )
    Decoder = TransformerDecoder(decode_layer, args.n_layer)
    Pos_env = PositionalEncoding(args.dim, args.dropout, maxlen=2000)

    model = PretrainModel(
        token_size=tokenizer.get_token_size(), encoder=GNN,
        decoder=Decoder, d_model=args.dim, pos_enc=Pos_env
    ).to(device)

    if args.encoder != '':
        assert args.checkpoint == '', "encoder will be covered by total ckpt"
        print(f'[INFO] Loading encoder weight in {args.encoder}')
        weight = torch.load(args.encoder, map_location=device)
        weight = {k: v for k, v in weight.items() if k.startswith('encoder')}
        model.load_state_dict(weight, strict=False)
    if args.decoder != '':
        assert args.checkpoint == '', "encoder will be covered by total ckpt"
        assert args.token_ckpt != '', 'Missing Tokenizer Information'
        print(f'[INFO] Loading decoder weight in {args.decoder}')
        weight = torch.load(args.decoder, map_location=device)
        weight = {
            k: v for k, v in weight.items() if k.startswith('decoder') or
            k.startswith('word_emb') or k.startswith('pos_enc') or
            k.startswith('output_layer')
        }
        model.load_state_dict(weight, strict=False)
    if args.checkpoint != '':
        assert args.token_ckpt != '', 'Missing Tokenizer Information'
        print(f'[INFO] Loading model weight in {args.checkpoint}')
        weight = torch.load(args.checkpoint, map_location=device)
        model.load_state_dict(weight, strict=False)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    lr_sh = ExponentialLR(optimizer, gamma=args.gamma, verbose=True)
    best_perf, best_ep = None, None

    print('[INFO] padding index', tokenizer.token2idx['<PAD>'])

    log_info = {
        'args': args.__dict__, 'train_loss': [],
        'valid_metric': [], 'test_metric': []
    }

    with open(token_dir, 'wb') as Fout:
        pickle.dump(tokenizer, Fout)

    with open(log_dir, 'w') as Fout:
        json.dump(log_info, Fout, indent=4)

    for ep in range(args.epoch):
        print(f'[INFO] traing at epoch {ep + 1}')
        loss = pretrain(
            loader=train_loader, model=model, optimizer=optimizer,
            tokenizer=tokenizer, device=device, pad_token='<PAD>',
            warmup=(ep < args.warmup), accu=args.accu,
            label_smoothing=args.label_smoothing
        )
        log_info['train_loss'].append({'trans': loss})

        valid_result = preeval(
            loader=valid_loader, model=model, tokenizer=tokenizer,
            pad_token='<PAD>', end_token='<END>', device=device
        )
        log_info['valid_metric'].append({'trans': valid_result})

        test_result = preeval(
            loader=test_loader, model=model, tokenizer=tokenizer,
            pad_token='<PAD>', end_token='<END>', device=device
        )

        log_info['test_metric'].append({'trans': test_result})

        print('[TRAIN]', log_info['train_loss'][-1])
        print('[VALID]', log_info['valid_metric'][-1])
        print('[TEST]', log_info['test_metric'][-1])

        if ep >= args.warmup and ep >= args.step_start:
            lr_sh.step()

        with open(log_dir, 'w') as Fout:
            json.dump(log_info, Fout, indent=4)

        if best_perf is None or valid_result > best_perf:
            best_perf, best_ep = valid_result, ep
            torch.save(model.state_dict(), model_dir)

        if args.early_stop > 3 and ep > max(10, args.early_stop):
            tx = log_info['valid_metric'][-args.early_stop:]
            tx = [x['trans'] for x in tx]
            if check_early_stop(tx):
                break

    print(f'[INFO] best acc epoch: {best_ep}')
    print(f'[INFO] best valid loss: {log_info["valid_metric"][best_ep]}')
    print(f'[INFO] best test loss: {log_info["test_metric"][best_ep]}')
