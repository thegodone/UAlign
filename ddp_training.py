from tqdm import tqdm
import numpy as np
import torch
from torch.nn.functional import cross_entropy
from data_utils import (
    generate_tgt_mask, correct_trans_output,
    convert_log_into_label
)

from data_utils import eval_trans as data_eval_trans
from training import calc_trans_loss
import torch.distributed as torch_dist
from enum import Enum


class Summary(Enum):
    NONE, SUM, AVERAGE, COUNT = 0, 1, 2, 3


class MetricCollector(object):
    def __init__(self, name, type_fmt=':f', summary_type=Summary.AVERAGE):
        super(MetricCollector, self).__init__()
        self.name, self.type_fmt = name, type_fmt
        self.summary_type = summary_type
        self.reset()

    def reset(self):
        self.val, self.sum, self.cnt, self.avg = [0] * 4

    def update(self, val, num=1):
        self.val = val
        self.sum += val
        self.cnt += num
        self.avg = self.sum / self.cnt

    def all_reduce(self, device):
        infos = torch.FloatTensor([self.sum, self.cnt]).to(device)
        torch_dist.all_reduce(infos, torch_dist.ReduceOp.SUM)
        self.sum, self.cnt = infos.tolist()
        self.avg = self.sum / self.cnt

    def __str__(self):
        return ''.join([
            '{name}: {val', self.type_fmt, '} avg: {avg', self.type_fmt, '}'
        ]).format(**self.__dict__)

    def summary(self):
        if self.summary_type is Summary.NONE:
            fmtstr = ''
        elif self.summary_type is Summary.AVERAGE:
            fmtstr = '{name} {avg:.3f}'
        elif self.summary_type is Summary.SUM:
            fmtstr = '{name} {sum:.3f}'
        elif self.summary_type is Summary.COUNT:
            fmtstr = '{name} {cnt:.3f}'
        else:
            raise ValueError(f'Invaild summary type {self.summary_type} found')

        return fmtstr.format(**self.__dict__)

    def get_value(self):
        if self.summary_type is Summary.AVERAGE:
            return self.avg
        elif self.summary_type is Summary.SUM:
            return self.sum
        elif self.summary_type is Summary.COUNT:
            return self.cnt
        else:
            raise ValueError(
                f'Invaild summary type {self.summary_type} '
                'for get_value()'
            )


class MetricManager(object):
    def __init__(self, metrics):
        super(MetricManager, self).__init__()
        self.metrics = metrics

    def all_reduct(self, device):
        for idx in range(len(self.metrics)):
            self.metrics[idx].all_reduce(device)

    def summary_all(self, split_string='  '):
        return split_string.join(x.summary() for x in self.metrics)

    def get_all_value_dict(self):
        return {x.name: x.get_value() for x in self.metrics}


def warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor):
    def f(x):
        if x >= warmup_iters:
            return 1
        alpha = float(x) / warmup_iters
        return warmup_factor * (1 - alpha) + alpha

    return torch.optim.lr_scheduler.LambdaLR(optimizer, f)


def ddp_pretrain(
    loader, model, optimizer, device, tokenizer, pad_token,
    warmup, accu=1, verbose=False, label_smoothing=0
):
    model = model.train()
    losses = MetricCollector('loss', type_fmt=':.3f')
    manager = MetricManager([losses])
    ignore_idx = tokenizer.token2idx[pad_token]
    its, total_len = 1, len(loader)
    if warmup:
        warmup_iters = len(loader) - 1
        warmup_sher = warmup_lr_scheduler(optimizer, warmup_iters, 5e-2)

    iterx = tqdm(loader, desc='train') if verbose else loader
    for graph, tran in iterx:
        graph = graph.to(device, non_blocking=True)
        tops = torch.LongTensor(tokenizer.encode2d(tran))
        tops = tops.to(device, non_blocking=True)
        trans_dec_ip = tops[:, :-1]
        trans_dec_op = tops[:, 1:]

        trans_op_mask, diag_mask = generate_tgt_mask(
            trans_dec_ip, tokenizer, pad_token, 'cpu'
        )

        trans_op_mask = trans_op_mask.to(device, non_blocking=True)
        diag_mask = diag_mask.to(device, non_blocking=True)

        trans_logs = model(
            graphs=graph, tgt=trans_dec_ip, tgt_mask=diag_mask,
            tgt_pad_mask=trans_op_mask
        )

        loss = calc_trans_loss(
            trans_logs, trans_dec_op, ignore_idx,
            lbsm=label_smoothing
        )

        if not warmup and accu > 1:
            loss = loss / accu
        loss.backward()

        if its % accu == 0 or its == total_len or warmup:
            optimizer.step()
            optimizer.zero_grad()
        its += 1

        losses.update(loss.item())

        if warmup:
            warmup_sher.step()

        if verbose:
            iterx.set_postfix_str(manager.summary_all())

    return manager


def ddp_preeval(
    model, loader, device, tokenizer, pad_token, end_token,
    verbose=False
):
    model = model.eval()

    trans_accs = MetricCollector('trans_acc', type_fmt=':.3f')
    manager = MetricManager([trans_accs])

    end_idx = tokenizer.token2idx[end_token]
    pad_idx = tokenizer.token2idx[pad_token]

    iterx = tqdm(loader, desc='eval') if verbose else loader

    for graph, tran in iterx:
        graph = graph.to(device, non_blocking=True)
        tops = torch.LongTensor(tokenizer.encode2d(tran))
        tops = tops.to(device, non_blocking=True)
        trans_dec_ip = tops[:, :-1]
        trans_dec_op = tops[:, 1:]

        trans_op_mask, diag_mask = generate_tgt_mask(
            trans_dec_ip, tokenizer, pad_token, 'cpu'
        )

        trans_op_mask = trans_op_mask.to(device, non_blocking=True)
        diag_mask = diag_mask.to(device, non_blocking=True)

        with torch.no_grad():
            trans_logs = model(
                graphs=graph, tgt=trans_dec_ip, tgt_mask=diag_mask,
                tgt_pad_mask=trans_op_mask
            )
            trans_pred = convert_log_into_label(trans_logs, mod='softmax')
            trans_pred = correct_trans_output(trans_pred, end_idx, pad_idx)
        A, B = data_eval_trans(trans_pred, trans_dec_op, False)
        trans_accs.update(val=A, num=B)
        if verbose:
            iterx.set_postfix_str(manager.summary_all())

    return manager


if __name__ == '__main__':
    X = MetricCollector(name='test')
    X.update(2)
    print(X)
