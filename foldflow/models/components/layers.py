import math
from typing import Callable, List, Optional

import numpy as np
import torch
from einops import rearrange, repeat
from scipy.stats import truncnorm
from torch import Tensor, einsum
from torch.nn import LayerNorm, TransformerEncoderLayer
from torch.nn import functional as F
import torch.nn as nn


def _standardize(kernel):
    """
    Makes sure that Var(W) = 1 and E[W] = 0
    """
    eps = 1e-6

    if len(kernel.shape) == 3:
        axis = [0, 1]  # last dimension is output dimension
    else:
        axis = 1

    var, mean = torch.var_mean(kernel, dim=axis, unbiased=True, keepdim=True)
    kernel = (kernel - mean) / (var + eps) ** 0.5
    return kernel


def he_orthogonal_init(tensor):
    """
    Generate a weight matrix with variance according to He initialization.
    Based on a random (semi-)orthogonal matrix neural networks
    are expected to learn better when features are decorrelated
    (stated by eg. "Reducing overfitting in deep networks by decorrelating representations",
    "Dropout: a simple way to prevent neural networks from overfitting",
    "Exact solutions to the nonlinear dynamics of learning in deep linear neural networks")
    """
    tensor = torch.nn.init.orthogonal_(tensor)

    if len(tensor.shape) == 3:
        fan_in = tensor.shape[:-1].numel()
    else:
        fan_in = tensor.shape[1]

    with torch.no_grad():
        tensor.data = _standardize(tensor.data)
        tensor.data *= (1 / fan_in) ** 0.5

    return tensor


class Dense(torch.nn.Module):
    """
    Combines dense layer and scaling for swish activation.

    Parameters
    ----------
        units: int
            Output embedding size.
        activation: str
            Name of the activation function to use.
        bias: bool
            True if use bias.
    """

    def __init__(
        self, in_features, out_features, bias=False, activation=None, name=None
    ):
        super().__init__()

        self.linear = torch.nn.Linear(in_features, out_features, bias=bias)
        self.reset_parameters()
        self.weight = self.linear.weight
        self.bias = self.linear.bias

        if isinstance(activation, str):
            activation = activation.lower()
        if activation in ["swish", "silu"]:
            self._activation = ScaledSiLU()
        elif activation is None:
            self._activation = torch.nn.Identity()
        else:
            raise NotImplementedError(
                "Activation function not implemented for GemNet (yet)."
            )

    def reset_parameters(self):
        he_orthogonal_init(self.linear.weight)
        if self.linear.bias is not None:
            self.linear.bias.data.fill_(0)

    def forward(self, x):
        x = self.linear(x)
        x = self._activation(x)
        return x


class ScaledSiLU(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.scale_factor = 1 / 0.6
        self._activation = torch.nn.SiLU()

    def forward(self, x):
        return self._activation(x) * self.scale_factor


class ResidualLayer(torch.nn.Module):
    """
    Residual block with output scaled by 1/sqrt(2).

    Parameters
    ----------
        units: int
            Output embedding size.
        nLayers: int
            Number of dense layers.
        activation: str
            Name of the activation function to use.
    """

    def __init__(self, units: int, nLayers: int = 2, activation=None, name=None):
        super().__init__()
        self.dense_mlp = torch.nn.Sequential(
            *[
                Dense(units, units, activation=activation, bias=False)
                for i in range(nLayers)
            ]
        )
        self.inv_sqrt_2 = 1 / (2.0**0.5)

    def forward(self, inputs):
        x = self.dense_mlp(inputs)
        x = inputs + x
        x = x * self.inv_sqrt_2
        return x


class EfficientInteractionDownProjection(torch.nn.Module):
    """
    Down projection in the efficient reformulation.

    Parameters
    ----------
        num_spherical: int
            Same as the setting in the basis layers.
        num_radial: int
            Same as the setting in the basis layers.
        emb_size_interm: int
            Intermediate embedding size (down-projection size).
    """

    def __init__(
        self,
        num_spherical: int,
        num_radial: int,
        emb_size_interm: int,
        name="EfficientDownProj",
    ):
        super().__init__()

        self.num_spherical = num_spherical
        self.num_radial = num_radial
        self.emb_size_interm = emb_size_interm

        self.reset_parameters()

    def reset_parameters(self):
        self.weight = torch.nn.Parameter(
            torch.empty((self.num_spherical, self.num_radial, self.emb_size_interm)),
            requires_grad=True,
        )
        he_orthogonal_init(self.weight)

    def forward(self, tbf):
        """
        Returns
        -------
            (rbf_W1, sph): tuple
            - rbf_W1: Tensor, shape=(nEdges, emb_size_interm, num_spherical)
            - sph: Tensor, shape=(nEdges, Kmax, num_spherical)
        """
        rbf_env, sph = tbf
        # (num_spherical, nEdges, num_radial), (nEdges, Kmax, num_spherical) ;  Kmax = maximum number of neighbors of the edges

        # MatMul: mul + sum over num_radial
        rbf_W1 = torch.matmul(
            rbf_env, self.weight
        )  # (num_spherical, nEdges , emb_size_interm)
        rbf_W1 = rbf_W1.permute(1, 2, 0)  # (nEdges, emb_size_interm, num_spherical)

        sph = torch.transpose(sph, 1, 2)  # (nEdges, num_spherical, Kmax)
        return rbf_W1, sph


class ResidualLayer(torch.nn.Module):
    """
    Residual block with output scaled by 1/sqrt(2).

    Parameters
    ----------
        units: int
            Output embedding size.
        nLayers: int
            Number of dense layers.
        activation: str
            Name of the activation function to use.
    """

    def __init__(self, units: int, nLayers: int = 2, activation=None, name=None):
        super().__init__()
        self.dense_mlp = torch.nn.Sequential(
            *[
                Dense(units, units, activation=activation, bias=False)
                for i in range(nLayers)
            ]
        )
        self.inv_sqrt_2 = 1 / (2.0**0.5)

    def forward(self, inputs):
        x = self.dense_mlp(inputs)
        x = inputs + x
        x = x * self.inv_sqrt_2
        return x


def permute_final_dims(tensor: torch.Tensor, inds: List[int]):
    zero_index = -1 * len(inds)
    first_inds = list(range(len(tensor.shape[:zero_index])))
    return tensor.permute(first_inds + [zero_index + i for i in inds])


def flatten_final_dims(t: torch.Tensor, no_dims: int):
    return t.reshape(t.shape[:-no_dims] + (-1,))


def ipa_point_weights_init_(weights):
    with torch.no_grad():
        softplus_inverse_1 = 0.541324854612918
        weights.fill_(softplus_inverse_1)


def _prod(nums):
    out = 1
    for n in nums:
        out = out * n
    return out


def _calculate_fan(linear_weight_shape, fan="fan_in"):
    fan_out, fan_in = linear_weight_shape

    if fan == "fan_in":
        f = fan_in
    elif fan == "fan_out":
        f = fan_out
    elif fan == "fan_avg":
        f = (fan_in + fan_out) / 2
    else:
        raise ValueError("Invalid fan option")

    return f


def trunc_normal_init_(weights, scale=1.0, fan="fan_in"):
    shape = weights.shape
    f = _calculate_fan(shape, fan)
    scale = scale / max(1, f)
    a = -2
    b = 2
    std = math.sqrt(scale) / truncnorm.std(a=a, b=b, loc=0, scale=1)
    size = _prod(shape)
    samples = truncnorm.rvs(a=a, b=b, loc=0, scale=std, size=size)
    samples = np.reshape(samples, shape)
    with torch.no_grad():
        weights.copy_(torch.tensor(samples, device=weights.device))


def lecun_normal_init_(weights):
    trunc_normal_init_(weights, scale=1.0)


def he_normal_init_(weights):
    trunc_normal_init_(weights, scale=2.0)


def glorot_uniform_init_(weights):
    torch.nn.init.xavier_uniform_(weights, gain=1)


def final_init_(weights):
    with torch.no_grad():
        weights.fill_(0.0)


def gating_init_(weights):
    with torch.no_grad():
        weights.fill_(0.0)


def normal_init_(weights):
    torch.nn.init.kaiming_normal_(weights, nonlinearity="linear")


class Linear(torch.nn.Linear):
    """
    A Linear layer with built-in nonstandard initializations. Called just
    like torch.nn.Linear.

    Implements the initializers in 1.11.4, plus some additional ones found
    in the code.
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        bias: bool = True,
        init: str = "default",
        init_fn: Optional[Callable[[torch.Tensor, torch.Tensor], None]] = None,
    ):
        """
        Args:
            in_dim:
                The final dimension of inputs to the layer
            out_dim:
                The final dimension of layer outputs
            bias:
                Whether to learn an additive bias. True by default
            init:
                The initializer to use. Choose from:

                "default": LeCun fan-in truncated normal initialization
                "relu": He initialization w/ truncated normal distribution
                "glorot": Fan-average Glorot uniform initialization
                "gating": Weights=0, Bias=1
                "normal": Normal initialization with std=1/sqrt(fan_in)
                "final": Weights=0, Bias=0

                Overridden by init_fn if the latter is not None.
            init_fn:
                A custom initializer taking weight and bias as inputs.
                Overrides init if not None.
        """
        super(Linear, self).__init__(in_dim, out_dim, bias=bias)

        if bias:
            with torch.no_grad():
                self.bias.fill_(0)

        if init_fn is not None:
            init_fn(self.weight, self.bias)
        else:
            if init == "default":
                lecun_normal_init_(self.weight)
            elif init == "relu":
                he_normal_init_(self.weight)
            elif init == "glorot":
                glorot_uniform_init_(self.weight)
            elif init == "gating":
                gating_init_(self.weight)
                if bias:
                    with torch.no_grad():
                        self.bias.fill_(1.0)
            elif init == "normal":
                normal_init_(self.weight)
            elif init == "final":
                final_init_(self.weight)
            else:
                raise ValueError("Invalid init string.")


def exists(val):
    return val is not None


def uniq(arr):
    return {el: True for el in arr}.keys()


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


# Implement Cross Attention Layer
# Code Taken from https://github.com/CompVis/latent-diffusion/blob/main/ldm/modules/attention.py
class CrossAttention(nn.Module):
    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0.0):
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.scale = dim_head**-0.5
        self.heads = heads

        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        self.to_k = nn.Linear(context_dim, inner_dim, bias=False)
        self.to_v = nn.Linear(context_dim, inner_dim, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, query_dim), nn.Dropout(dropout)
        )

    def forward(self, x, context=None, mask=None):
        h = self.heads

        q = self.to_q(x)
        context = default(context, x)
        k = self.to_k(context)
        v = self.to_v(context)

        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> (b h) n d", h=h), (q, k, v))

        sim = einsum("b i d, b j d -> b i j", q, k) * self.scale

        if exists(mask):
            mask = rearrange(mask, "b ... -> b (...)")
            max_neg_value = -torch.finfo(sim.dtype).max
            mask = repeat(mask, "b j -> (b h) () j", h=h)
            sim.masked_fill_(~mask, max_neg_value)

        # attention, what we cannot get enough of
        attn = sim.softmax(dim=-1)

        out = einsum("b i j, b j d -> b i d", attn, v)
        out = rearrange(out, "(b h) n d -> b n (h d)", h=h)
        return self.to_out(out)


# Custom Pytorch Transformer Encoder Layer
class CrossAttentionTransformerEncoderLayer(TransformerEncoderLayer):
    def __init__(
        self, d_model, nhead, dim_feedforward=2048, dropout=0.1, activation="relu"
    ):
        super().__init__(d_model, nhead, dim_feedforward, dropout, activation)
        self.attn = CrossAttention(d_model, nhead, dropout=dropout)
        self.dropout = nn.Dropout(dropout)
        self.activation = _get_activation_fn(activation)
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

        if activation is F.relu or isinstance(activation, torch.nn.ReLU):
            self.activation_relu_or_gelu = 1
        elif activation is F.gelu or isinstance(activation, torch.nn.GELU):
            self.activation_relu_or_gelu = 2
        else:
            self.activation_relu_or_gelu = 0
        self.activation = activation

    def forward(
        self, src, src_mask=None, src_key_padding_mask=None, is_causal: bool = False
    ):
        src_key_padding_mask = F._canonical_mask(
            mask=src_key_padding_mask,
            mask_name="src_key_padding_mask",
            other_type=F._none_or_dtype(src_mask),
            other_name="src_mask",
            target_type=src.dtype,
        )

        src_mask = F._canonical_mask(
            mask=src_mask,
            mask_name="src_mask",
            other_type=None,
            other_name="",
            target_type=src.dtype,
            check_other=False,
        )

        x = src
        if self.norm_first:
            x = x + self._attn_block(
                self.norm1(x), src_mask, src_key_padding_mask, is_causal=is_causal
            )
            x = x + self._ff_block(self.norm2(x))
        else:
            x = self.norm1(
                x
                + self._sa_block(x, src_mask, src_key_padding_mask, is_causal=is_causal)
            )
            x = self.norm2(x + self._ff_block(x))

        return x

    # self-attention block
    def _attn_block(
        self,
        x: Tensor,
        attn_mask: Optional[Tensor],
        key_padding_mask: Optional[Tensor],
        is_causal: bool = False,
    ) -> Tensor:
        x = self.attn(
            x,
            x,
            x,
            attn_mask=attn_mask,
            key_padding_mask=key_padding_mask,
            need_weights=False,
            is_causal=is_causal,
        )[0]
        return self.dropout1(x)

    # feed forward block
    def _ff_block(self, x: Tensor) -> Tensor:
        x = self.linear2(self.dropout(self.activation(self.linear1(x))))
        return self.dropout2(x)
