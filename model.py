import torch
from sparse_backBone import (
    GINBase, GATBase, SparseAtomEncoder, SparseBondEncoder
)
from itertools import combinations, permutations
from torch_geometric.data import Data
from typing import Any, Dict, List, Tuple, Optional, Union
import numpy as np
from torch.nn.functional import binary_cross_entropy_with_logits
from torch.nn.functional import cross_entropy
from scipy.optimize import linear_sum_assignment
from data_utils import (
    convert_log_into_label, convert_edge_log_into_labels,
    seperate_dict, extend_label_by_edge, filter_label_by_node,
    seperate_encoder_graphs, seperate_pred
)
import math


def make_memory_from_feat(node_feat, batch_mask):
    batch_size, max_node = batch_mask.shape
    memory = torch.zeros(batch_size, max_node, node_feat.shape[-1])
    memory = memory.to(node_feat.device)
    memory[batch_mask] = node_feat
    return memory, ~batch_mask


class SynthonPredictionModel(torch.nn.Module):
    def __init__(self, base_model, node_dim, edge_dim, dropout=0.1):
        super(SynthonPredictionModel, self).__init__()
        self.base_model = base_model
        self.edge_predictor = torch.nn.Sequential(
            torch.nn.Linear(edge_dim, edge_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(edge_dim, 3)
        )

        self.node_predictor = torch.nn.Sequential(
            torch.nn.Linear(node_dim, node_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(node_dim, 7)
        )

    def calc_loss(
        self, node_logits, node_label, edge_logits, edge_label,
        node_batch, edge_batch,
    ):
        max_node_batch = node_batch.max().item() + 1
        node_loss = torch.zeros(max_node_batch).to(node_logits)
        node_loss_src = cross_entropy(
            node_logits, node_label, reduction='none'
        )
        node_loss.scatter_add_(0, node_batch, node_loss_src)

        max_edge_batch = edge_batch.max().item() + 1
        edge_loss = torch.zeros(max_edge_batch).to(edge_logits)
        edge_loss_src = cross_entropy(
            edge_logits, edge_label, reduction='none',
        )
        edge_loss.scatter_add_(0, edge_batch, edge_loss_src)

        return node_loss.mean(), edge_loss.mean()

    def forward(self, graph, ret_loss=True, ret_feat=False):
        node_feat, edge_feat = self.base_model(graph)

        node_logits = self.node_predictor(node_feat)
        node_logits = node_logits.squeeze(dim=-1)

        edge_logits = self.edge_predictor(edge_feat)
        edge_logits = edge_logits.squeeze(dim=-1)

        if ret_loss:
            n_loss, e_loss = self.calc_loss(
                node_logits=node_logits, edge_logits=edge_logits,
                node_label=graph.node_label, node_batch=graph.batch,
                edge_label=graph.edge_label, edge_batch=graph.e_batch
            )

        answer = (node_logits, edge_logits)
        if ret_loss:
            answer += (n_loss, e_loss)
        if ret_feat:
            answer += (node_feat, edge_feat)
        return answer


class PositionalEncoding(torch.nn.Module):
    def __init__(self, emb_size: int, dropout: float, maxlen: int = 2000):
        super(PositionalEncoding, self).__init__()
        den = torch.exp(
            - torch.arange(0, emb_size, 2) * math.log(10000) / emb_size
        )
        pos = torch.arange(0, maxlen).reshape(maxlen, 1)
        pos_embedding = torch.zeros((maxlen, emb_size))
        pos_embedding[:, 0::2] = torch.sin(pos * den)
        pos_embedding[:, 1::2] = torch.cos(pos * den)

        self.dropout = torch.nn.Dropout(dropout)
        self.register_buffer('pos_embedding', pos_embedding)

    def forward(self, token_embedding: torch.Tensor):
        token_len = token_embedding.shape[1]
        return self.dropout(token_embedding + self.pos_embedding[:token_len])


class OverallModel(torch.nn.Module):
    def __init__(
        self, GNN, trans_enc, trans_dec, node_dim, edge_dim, num_token,
        use_sim=True, pre_graph=True, heads=1, dropout=0.0, maxlen=2000,
        rxn_num=None
    ):
        super(OverallModel, self).__init__()
        self.GNN, self.trans_enc, self.trans_dec = GNN, trans_enc, trans_dec
        self.syn_e_pred = torch.nn.Sequential(
            torch.nn.Linear(edge_dim, edge_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(edge_dim, 3)
        )
        self.syn_n_pred = torch.nn.Sequential(
            torch.nn.Linear(node_dim, node_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(node_dim, 7)
        )
        self.lg_activate = torch.nn.Sequential(
            torch.nn.Linear(node_dim, node_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(node_dim, 1)
        )
        self.conn_pred = torch.nn.Sequential(
            torch.nn.Linear(edge_dim + edge_dim, edge_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(edge_dim, 1)
        )
        if rxn_num is None:
            self.reac_emb = torch.nn.Parameter(torch.randn(1, 1, node_dim))
        else:
            self.reac_emb = torch.nn.Embedding(rxn_num, node_dim)
        self.rxn_num = rxn_num
        self.prod_emb = torch.nn.Parameter(torch.randn(1, 1, node_dim))
        self.emb_trans = torch.nn.Linear(node_dim + node_dim, node_dim)
        self.use_sim, self.pre_graph = use_sim, pre_graph
        if self.use_sim:
            self.SIM_G = SIM(node_dim, node_dim, heads, dropout)
            self.SIM_L = SIM(node_dim, node_dim, heads, dropout)

        self.token_embeddings = torch.nn.Embedding(num_token, node_dim)
        self.PE = PositionalEncoding(node_dim, dropout, maxlen)
        self.trans_pred = torch.nn.Linear(node_dim, num_token)

    def add_type_emb(self, x, is_reac, graph_rxn=None):
        batch_size, max_len = x.shape[:2]
        if is_reac:
            if self.rxn_num is None:
                type_emb = self.reac_emb.repeat(batch_size, max_len, 1)
            else:
                type_emb = self.reac_emb(graph_rxn)
        else:
            type_emb = self.prod_emb.repeat(batch_size, max_len, 1)

        return self.emb_trans(torch.cat([x, type_emb], dim=-1)) + x

    def trans_enc_forward(
        self, word_emb, word_pad, graph_emb, graph_pad,
        graph_rxn=None
    ):
        word_emb = self.add_type_emb(
            word_emb, is_reac=True, graph_rxn=graph_rxn
        )
        word_emb = self.PE(word_emb)
        graph_emb = self.add_type_emb(graph_emb, is_reac=False)

        if self.pre_graph:
            trans_input = torch.cat([word_emb, graph_emb], dim=1)
            memory_pad = torch.cat([word_pad, graph_pad], dim=1)
            memory = self.trans_enc(
                trans_input, src_key_padding_mask=memory_pad)
        else:
            memory = self.trans_enc(word_emb, src_key_padding_mask=word_pad)
            memory = torch.cat([memory, graph_emb], dim=1)
            memory_pad = torch.cat([word_pad, graph_pad], dim=1)
        return memory, memory_pad

    def conn_forward(self, lg_emb, graph_emb, conn_edges, node_mask):
        useful_edges_mask = node_mask[conn_edges[:, 1]]
        useful_src, useful_dst = conn_edges[useful_edges_mask].T
        conn_embs = [graph_emb[useful_src], lg_emb[useful_dst]]
        conn_embs = torch.cat(conn_embs, dim=-1)
        conn_logits = self.conn_pred(conn_embs).squeeze(dim=-1)

        return conn_logits, useful_edges_mask

    def update_via_sim(self, graph_emb, graph_mask, lg_emb, lg_mask):
        graph_emb, g_pad_mask = make_memory_from_feat(graph_emb, graph_mask)
        lg_emb, l_pad_mask = make_memory_from_feat(lg_emb, lg_mask)

        new_graph_emb = self.SIM_G(graph_emb, lg_emb, l_pad_mask)
        new_lg_emb = self.SIM_L(lg_emb, graph_emb, g_pad_mask)
        return new_graph_emb[graph_mask], new_lg_emb[lg_mask]

    def forward(
        self, prod_graph, lg_graph, trans_ip, conn_edges, conn_batch,
        trans_op, graph_rxn=None, pad_idx=None, trans_ip_key_padding=None,
        trans_op_key_padding=None, trans_op_mask=None, trans_label=None,
        conn_label=None, mode='train', return_loss=False
    ):
        prod_n_emb, prod_e_emb = self.GNN(prod_graph)
        lg_n_emb, lg_e_emb = self.GNN(lg_graph)

        prod_n_logits = self.syn_n_pred(prod_n_emb)
        prod_e_logits = self.syn_e_pred(prod_e_emb)

        trans_ip = self.token_embeddings(trans_ip)
        trans_op = self.token_embeddings(trans_op)

        batched_prod_emb, prod_padding_mask = \
            make_memory_from_feat(prod_n_emb, prod_graph.batch_mask)
        memory, memory_pad = self.trans_enc_forward(
            trans_ip, trans_ip_key_padding, batched_prod_emb,
            prod_padding_mask, graph_rxn
        )

        trans_pred = self.trans_pred(self.trans_dec(
            tgt=trans_op, memory=memory, tgt_mask=trans_op_mask,
            memory_key_padding_mask=memory_pad,
            tgt_key_padding_mask=trans_op_key_padding
        ))

        lg_act_logits = self.lg_activate(lg_n_emb).squeeze(dim=-1)
        if mode == 'train':
            lg_useful = (lg_graph.node_label > 0) | (lg_act_logits > 0)
        else:
            lg_useful = lg_graph.node_label > 0

        if self.use_sim:
            n_prod_emb, n_lg_emb = self.update_via_sim(
                prod_n_emb, prod_graph.batch_mask,
                lg_n_emb, lg_graph.batch_mask
            )
        else:
            n_prod_emb, n_lg_emb = prod_n_emb, lg_n_emb
        conn_logits, conn_mask = self.conn_forward(
            n_lg_emb, n_prod_emb,  conn_edges, lg_useful
        )

        if mode == 'train' or return_loss:
            losses = self.loss_calc(
                prod_n_log=prod_n_logits,
                prod_e_log=prod_e_logits,
                prod_n_label=prod_graph.node_label,
                prod_e_label=prod_graph.edge_label,
                prod_n_batch=prod_graph.batch,
                prod_e_batch=prod_graph.e_batch,
                lg_n_log=lg_act_logits,
                lg_n_label=lg_graph.node_label,
                lg_n_batch=lg_graph.batch,
                conn_lg=conn_logits,
                conn_lb=conn_label[conn_mask],
                conn_batch=conn_batch[conn_mask],
                trans_pred=trans_pred,
                trans_lb=trans_label,
                pad_idx=pad_idx
            )
        if mode == 'train':
            return losses
        elif return_loss:
            return (prod_n_logits, prod_e_logits, lg_act_logits,
                    conn_logits, conn_mask, trans_pred), losses
        else:
            return prod_n_logits, prod_e_logits, lg_act_logits,\
                conn_logits, conn_mask, trans_pred

    def loss_calc(
        self, prod_n_log, prod_e_log, prod_n_label, prod_e_label,
        prod_n_batch, prod_e_batch, lg_n_log, lg_n_label, lg_n_batch,
        conn_lg, conn_lb, conn_batch, trans_pred, trans_lb, pad_idx
    ):
        syn_node_loss = self.scatter_loss_by_batch(
            prod_n_log, prod_n_label, prod_n_batch, cross_entropy
        )
        # print('syn_node', prod_n_log.shape, prod_n_label.shape)
        # print('syn_node', syn_node_loss.item())

        syn_edge_loss = self.scatter_loss_by_batch(
            prod_e_log, prod_e_label, prod_e_batch, cross_entropy
        )

        # print('syn_edge', prod_e_log.shape, prod_e_label.shape)
        # print('syn_edge', syn_edge_loss.item())

        lg_act_loss = self.scatter_loss_by_batch(
            lg_n_log, lg_n_label, lg_n_batch,
            binary_cross_entropy_with_logits
        )

        # print('lg_act', lg_n_log.shape, lg_n_label.shape)
        # print('lg_act', lg_act_loss.item())

        conn_loss = self.scatter_loss_by_batch(
            conn_lg, conn_lb, conn_batch,
            binary_cross_entropy_with_logits
        )

        # print('conn', conn_lg.shape, conn_lb.shape)
        # print('conn', conn_loss.item())

        trans_loss = self.calc_trans_loss(trans_pred, trans_lb, pad_idx)

        # print('trans', trans_pred.shape, trans_lb.shape)
        # print('trans', trans_loss.item())
        
        return syn_node_loss, syn_edge_loss, lg_act_loss, conn_loss, trans_loss

    def scatter_loss_by_batch(self, logits, label, batch, lfn):
        max_batch = batch.max().item() + 1
        losses = torch.zeros(max_batch).to(logits)
        org_loss = lfn(logits, label, reduction='none')
        losses.index_add_(0, batch, org_loss)
        return losses.mean()

    def calc_trans_loss(self, trans_pred, trans_lb, ignore_index):
        batch_size, maxl, num_c = trans_pred.shape
        trans_pred = trans_pred.reshape(-1, num_c)
        trans_lb = trans_lb.reshape(-1)

        losses = cross_entropy(
            trans_pred, trans_lb, reduction='none',
            ignore_index=ignore_index
        )
        losses = losses.reshape(batch_size, maxl)
        loss = torch.mean(torch.sum(losses, dim=-1))
        return loss


class SIM(torch.nn.Module):
    def __init__(self, q_dim, kv_dim, heads, dropout):
        super(SIM, self).__init__()
        self.Attn = torch.nn.MultiheadAttention(
            embed_dim=q_dim, kdim=kv_dim, vdim=kv_dim,
            num_heads=heads, dropout=dropout, batch_first=True
        )

    def forward(self, x, other, key_padding_mask=None):
        return x + self.Attn(
            query=x, key=other, value=other,
            key_padding_mask=key_padding_mask
        )
