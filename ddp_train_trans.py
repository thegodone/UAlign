import torch
import argparse
import json
import os
import time
import pickle


from tokenlizer import DEFAULT_SP, Tokenizer
from torch.utils.data import DataLoader
from model import PositionalEncoding, PretrainModel
from Dataset import RetroDataset, col_fn_retro

from ddp_training import ddp_pretrain, ddp_preeval
from data_utils import load_data, fix_seed, check_early_stop
from torch.nn import TransformerDecoderLayer, TransformerDecoder
from torch.optim.lr_scheduler import ExponentialLR
from sparse_backBone import GATBase


import torch.distributed as torch_dist
import torch.multiprocessing as torch_mp
from torch.utils.data.distributed import DistributedSampler


def create_log_model(args):
    timestamp = time.time()
    if not os.path.exists(args.base_log):
        os.makedirs(args.base_log)
    detail_log_dir = os.path.join(args.base_log, f'log-{timestamp}.json')
    detail_model_dir = os.path.join(args.base_log, f'mod-{timestamp}.pth')
    token_path = os.path.join(args.base_log, f'token-{timestamp}.pkl')
    return detail_log_dir, detail_model_dir, token_path


def main_worker(worker_idx, args, tokenizer, log_dir, model_dir):
    print(f'[INFO] Process {worker_idx} start')
    torch_dist.init_process_group(
        backend='nccl', init_method=f'tcp://127.0.0.1:{args.port}',
        world_size=args.num_gpus, rank=worker_idx
    )

    device = torch.device(f'cuda:{worker_idx}')
    verbose = (worker_idx == 0)

    train_rec, train_prod, train_rxn = load_data(args.data_path, 'train')
    val_rec, val_prod, val_rxn = load_data(args.data_path, 'val')
    test_rec, test_prod, test_rxn = load_data(args.data_path, 'test')

    print(f'[INFO] worker {worker_idx} Data Loaded')

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

    train_sampler = DistributedSampler(train_set, shuffle=True)
    valid_sampler = DistributedSampler(valid_set, shuffle=False)
    test_sampler = DistributedSampler(test_set, shuffle=False)

    train_loader = DataLoader(
        train_set, collate_fn=col_fn_retro, sampler=train_sampler,
        batch_size=args.bs, shuffle=False, pin_memory=True,
        num_workers=args.num_workers
    )
    valid_loader = DataLoader(
        valid_set, collate_fn=col_fn_retro, sampler=valid_sampler,
        batch_size=args.bs, shuffle=False, pin_memory=True,
        num_workers=args.num_workers
    )
    test_loader = DataLoader(
        test_set, collate_fn=col_fn_retro, sampler=test_sampler,
        batch_size=args.bs, shuffle=False, pin_memory=True,
        num_workers=args.num_workers
    )

    GNN = GATBase(
        num_layers=args.n_layer, dropout=args.dropout, num_heads=args.heads,
        embedding_dim=args.dim, negative_slope=args.negative_slope,
        n_class=11 if args.use_class else None
    )

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

    if args.checkpoint != '':
        assert args.token_ckpt != '', 'Missing Tokenizer Information'
        print(f'[INFO {worker_idx}] Loading model weight in {args.checkpoint}')
        weight = torch.load(args.checkpoint, map_location=device)
        model.load_state_dict(weight, strict=True)

    model = torch.nn.parallel.DistributedDataParallel(
        model, device_ids=[worker_idx], output_device=worker_idx,
        find_unused_parameters=True
    )

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    lr_sh = ExponentialLR(optimizer, gamma=args.gamma, verbose=verbose)
    best_perf, best_ep = None, None

    print(f'[INFO {worker_idx}] padding index', tokenizer.token2idx['<PAD>'])

    log_info = {
        'args': args.__dict__, 'train_loss': [],
        'valid_metric': [], 'test_metric': []
    }

    with open(log_dir, 'w') as Fout:
        json.dump(log_info, Fout, indent=4)

    for ep in range(args.epoch):
        if verbose:
            print(f'[INFO] traing at epoch {ep + 1}')

        train_sampler.set_epoch(ep)
        train_loss = ddp_pretrain(
            loader=train_loader, model=model, optimizer=optimizer,
            tokenizer=tokenizer, device=device, pad_token='<PAD>',
            warmup=(ep < args.warmup), accu=args.accu, verbose=verbose,
            label_smoothing=args.label_smoothing
        )

        valid_result = ddp_preeval(
            loader=valid_loader, model=model, tokenizer=tokenizer,
            pad_token='<PAD>', end_token='<END>', device=device,
            verbose=verbose
        )

        test_result = ddp_preeval(
            loader=test_loader, model=model, tokenizer=tokenizer,
            pad_token='<PAD>', end_token='<END>', device=device,
            verbose=verbose
        )

        torch_dist.barrier()
        train_loss.all_reduct(device)
        valid_result.all_reduct(device)
        test_result.all_reduct(device)

        log_info['train_loss'].append(train_loss.get_all_value_dict())
        log_info['valid_metric'].append(valid_result.get_all_value_dict())
        log_info['test_metric'].append(test_result.get_all_value_dict())

        if verbose:
            print('[TRAIN]', log_info['train_loss'][-1])
            print('[VALID]', log_info['valid_metric'][-1])
            print('[TEST]', log_info['test_metric'][-1])
            this_valid = log_info['valid_metric'][-1]

            with open(log_dir, 'w') as Fout:
                json.dump(log_info, Fout, indent=4)

            if best_perf is None or this_valid['trans_acc'] > best_perf:
                best_perf, best_ep = this_valid['trans_acc'], ep
                torch.save(model.module.state_dict(), model_dir)

        if ep >= args.warmup and ep >= args.step_start:
            lr_sh.step()

        if args.early_stop > 3 and ep > max(10, args.early_stop):
            tx = log_info['valid_metric'][-args.early_stop:]
            tx = [x['trans_acc'] for x in tx]
            if check_early_stop(tx):
                print(f'[INFO] worker {worker_idx} early stop')
                break

    if not verbose:
        return

    print(f'[INFO] best acc epoch: {best_ep}')
    print(f'[INFO] best valid loss: {log_info["valid_metric"][best_ep]}')
    print(f'[INFO] best test loss: {log_info["test_metric"][best_ep]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Graph Edit Exp, Sparse Model')
    parser.add_argument(
        '--dim', default=256, type=int,
        help='the hidden dim of model'
    )
    parser.add_argument(
        '--aug_prob', default=0.5, type=float,
        help='the probability of performing data augumentation '
        "should be between 0 and 1"
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
        '--negative_slope', type=float, default=0.2,
        help='negative slope for attention, only useful for gat'
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
        '--num_gpus', default=1, type=int,
        help='the number of gpus to run ddp'
    )
    parser.add_argument(
        '--lr', default='1e-3', type=float,
        help='the learning rate for training'
    )
    parser.add_argument(
        '--base_log', default='dpp_overall', type=str,
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
        '--checkpoint', type=str, default='',
        help='the path of checkpoint to restart the exp'
    )
    parser.add_argument(
        '--token_ckpt', type=str, default='',
        help='the path of tokenizer, when ckpt is loaded, necessary'
    )
    parser.add_argument(
        '--use_class', action='store_true',
        help='use class for model or not'
    )
    parser.add_argument(
        '--label_smoothing', type=float, default=0.0,
        help='the label smoothing for transformer training'
    )
    parser.add_argument(
        '--num_workers', type=int, default=0,
        help='the number of workers per dataset for data loading'
    )
    parser.add_argument(
        '--port', type=int, default=12225,
        help='the port for ddp communation'
    )

    args = parser.parse_args()
    print(args)
    log_dir, model_dir, token_dir = create_log_model(args)
    fix_seed(args.seed)

    if args.checkpoint != '':
        assert args.token_ckpt != '', \
            'require token_ckpt when checkpoint is given'
        with open(args.token_ckpt, 'rb') as Fin:
            tokenizer = pickle.load(Fin)
    else:
        assert args.token_path != '', 'file containing all tokens are required'
        SP_TOKEN = DEFAULT_SP | set([f"<RXN>_{i}" for i in range(11)])

        with open(args.token_path) as Fin:
            tokenizer = Tokenizer(json.load(Fin), SP_TOKEN)

    with open(token_dir, 'wb') as Fout:
        pickle.dump(tokenizer, Fout)

    torch_mp.spawn(
        main_worker, nprocs=args.num_gpus,
        args=(args, tokenizer, log_dir, model_dir)
    )
