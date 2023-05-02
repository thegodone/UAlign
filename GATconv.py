from torch_geometric.nn.inits import glorot, zeros
from torch_geometric.utils import softmax as sp_softmax
from torch_geometric.utils import add_self_loops, remove_self_loops
import torch
import torch.nn.functional as F
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.dense.linear import Linear
from torch.nn import Parameter


class MyGATConv(MessagePassing):
    def __init__(
        self, in_channels, out_channels, edge_dim, heads=1,
        negative_slope=0.2, dropout=0.1, **kwargs,
    ):
        kwargs.setdefault('aggr', 'add')
        super(MyGATConv, self).__init__(node_dim=0, **kwargs)
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.edge_dim = edge_dim
        self.negative_slope = negative_slope
        self.dropout = dropout
        self.dropout_fun = torch.nn.Dropout(dropout)

        self.lin_src = self.lin_dst = Linear(
            in_channels, out_channels * heads,
            bias=False, weight_initializer='glorot'
        )

        self.att_src = Parameter(torch.zeros(1, heads, out_channels))
        self.att_dst = Parameter(torch.zeros(1, heads, out_channels))
        self.att_edge = Parameter(torch.zeros(1, heads, out_channels))

        self.bias = Parameter(torch.zeros(heads * out_channels))
        self.lin_edge = Linear(
            edge_dim, out_channels * heads,
            bias=False, weight_initializer='glorot'
        )
        self.lin_message = Linear(
            out_channels * 2, out_channels,
            bias=True, weight_initializer='glorot'
        )

        self.reset_parameters()

    def reset_parameters(self):
        super(MyGATConv, self).reset_parameters()
        self.lin_src.reset_parameters()
        self.lin_dst.reset_parameters()
        self.lin_edge.reset_parameters()
        self.lin_message.reset_parameters()
        glorot(self.att_src)
        glorot(self.att_dst)
        glorot(self.att_edge)
        zeros(self.bias)

    def forward(self, x, edge_index, edge_attr, size=None):

        num_nodes = x.shape[0]
        edge_index, edge_attr = remove_self_loops(edge_index, edge_attr)
        edge_index, edge_attr = add_self_loops(
            edge_index, edge_attr, fill_value='mean', num_nodes=num_nodes
        )

        H, C = self.heads, self.out_channels
        x_src = self.lin_src(x).view(-1, H, C)
        x_dst = self.lin_dst(x).view(-1, H, C)

        x = (x_src, x_dst)
        alpha_src = (x_src * self.att_src).sum(dim=-1)
        alpha_dst = (x_dst * self.att_dst).sum(dim=-1)
        alpha = (alpha_src, alpha_dst)

        edge_attr = self.lin_edge(edge_attr)
        edge_attr = edge_attr.view(-1, self.heads, self.out_channels)

        alpha = self.edge_updater(edge_index, alpha=alpha, edge_attr=edge_attr)

        out = self.propagate(
            edge_index, x=x, alpha=alpha,
            size=size, edge_attr=edge_attr
        )
        out = out.view(-1, H * C) + self.bias
        return out

    def edge_update(self, alpha_j, alpha_i, edge_attr, index, ptr, size_i):
        alpha_edge = (edge_attr * self.att_edge).sum(dim=-1)
        alpha = alpha_i + alpha_j + alpha_edge

        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = self.dropout_fun(sp_softmax(alpha, index, ptr, size_i))
        return alpha

    def message(self, x_j, alpha, edge_attr):
        message = self.lin_message(torch.cat([x_j, edge_attr], dim=-1))
        return alpha.unsqueeze(-1) * message

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.in_channels}, '
            f'{self.out_channels}, heads={self.heads})'
        )