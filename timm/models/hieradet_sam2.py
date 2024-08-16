import math
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.jit import Final

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.layers import PatchEmbed, Mlp, DropPath, ClNormMlpClassifierHead, PatchDropout, \
    get_norm_layer, get_act_layer, init_weight_jax, init_weight_vit

from ._builder import build_model_with_cfg
from ._features import feature_take_indices
from ._manipulate import named_apply, checkpoint_seq, adapt_input_conv
from ._registry import generate_default_cfgs, register_model, register_model_deprecations


def do_pool(
        x: torch.Tensor,
        pool: nn.Module,
        norm: nn.Module = None,
) -> torch.Tensor:
    if pool is None:
        return x
    # (B, H, W, C) -> (B, C, H, W)
    x = x.permute(0, 3, 1, 2)
    x = pool(x)
    # (B, C, H', W') -> (B, H', W', C)
    x = x.permute(0, 2, 3, 1)
    if norm:
        x = norm(x)

    return x


def window_partition(x, window_size):
    """
    Partition into non-overlapping windows with padding if needed.
    Args:
        x (tensor): input tokens with [B, H, W, C].
        window_size (int): window size.
    Returns:
        windows: windows after partition with [B * num_windows, window_size, window_size, C].
        (Hp, Wp): padded height and width before partition
    """
    B, H, W, C = x.shape

    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size
    x = F.pad(x, (0, 0, 0, pad_w, 0, pad_h))
    Hp, Wp = H + pad_h, W + pad_w

    x = x.view(B, Hp // window_size, window_size, Wp // window_size, window_size, C)
    windows = (
        x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    )
    return windows, (Hp, Wp)


def window_unpartition(windows: torch.Tensor, window_size: int, pad_hw: List[int], hw: List[int]):
    """
    Window unpartition into original sequences and removing padding.
    Args:
        x (tensor): input tokens with [B * num_windows, window_size, window_size, C].
        window_size (int): window size.
        pad_hw (Tuple): padded height and width (Hp, Wp).
        hw (Tuple): original height and width (H, W) before padding.
    Returns:
        x: unpartitioned sequences with [B, H, W, C].
    """
    Hp, Wp = pad_hw
    H, W = hw
    B = windows.shape[0] // (Hp * Wp // window_size // window_size)
    x = windows.view(
        B, Hp // window_size, Wp // window_size, window_size, window_size, -1
    )
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, Hp, Wp, -1)
    x = x[:, :H, :W, :].contiguous()
    return x


class MultiScaleAttention(nn.Module):
    def __init__(
        self,
        dim: int,
        dim_out: int,
        num_heads: int,
        q_pool: nn.Module = None,
    ):
        super().__init__()

        self.dim = dim
        self.dim_out = dim_out

        self.num_heads = num_heads
        head_dim = dim_out // num_heads
        self.scale = head_dim**-0.5

        self.q_pool = q_pool
        self.qkv = nn.Linear(dim, dim_out * 3)
        self.proj = nn.Linear(dim_out, dim_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, H, W, _ = x.shape

        # qkv with shape (B, H * W, 3, nHead, C)
        qkv = self.qkv(x).reshape(B, H * W, 3, self.num_heads, -1)

        # q, k, v with shape (B, H * W, nheads, C)
        q, k, v = torch.unbind(qkv, 2)

        # Q pooling (for downsample at stage changes)
        if self.q_pool:
            q = do_pool(q.reshape(B, H, W, -1), self.q_pool)
            H, W = q.shape[1:3]  # downsampled shape
            q = q.reshape(B, H * W, self.num_heads, -1)

        # Torch's SDPA expects [B, nheads, H*W, C] so we transpose
        x = F.scaled_dot_product_attention(
            q.transpose(1, 2),
            k.transpose(1, 2),
            v.transpose(1, 2),
        )
        # Transpose back
        x = x.transpose(1, 2).reshape(B, H, W, -1)

        x = self.proj(x)
        return x


class MultiScaleBlock(nn.Module):
    def __init__(
        self,
        dim: int,
        dim_out: int,
        num_heads: int,
        mlp_ratio: float = 4.0,
        drop_path: float = 0.0,
        q_stride: Tuple[int, int] = None,
        norm_layer: Union[nn.Module, str] = "LayerNorm",
        act_layer: Union[nn.Module, str] = "GELU",
        window_size: int = 0,
    ):
        super().__init__()
        norm_layer = get_norm_layer(norm_layer)
        act_layer = get_act_layer(act_layer)
        self.window_size = window_size
        self.dim = dim
        self.dim_out = dim_out

        self.norm1 = norm_layer(dim)
        self.pool = None
        self.q_stride = q_stride
        if self.q_stride:
            self.pool = nn.MaxPool2d(
                kernel_size=q_stride,
                stride=q_stride,
                ceil_mode=False,
            )
        self.attn = MultiScaleAttention(
            dim,
            dim_out,
            num_heads=num_heads,
            q_pool=self.pool,
        )
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

        self.norm2 = norm_layer(dim_out)
        self.mlp = Mlp(
            dim_out,
            int(dim_out * mlp_ratio),
            act_layer=act_layer,
        )

        if dim != dim_out:
            self.proj = nn.Linear(dim, dim_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shortcut = x  # B, H, W, C
        x = self.norm1(x)

        # Skip connection
        if self.dim != self.dim_out:
            shortcut = do_pool(self.proj(x), self.pool)

        # Window partition
        window_size = self.window_size
        feat_size = x.shape[1:3]
        pad_hw = 0, 0
        if window_size > 0:
            x, pad_hw = window_partition(x, window_size)

        # Window Attention + Q Pooling (if stage change)
        x = self.attn(x)
        if self.q_stride:
            # Shapes have changed due to Q pooling
            window_size = self.window_size // self.q_stride[0]
            feat_size = shortcut.shape[1:3]

            pad_h = (window_size - feat_size[0] % window_size) % window_size
            pad_w = (window_size - feat_size[1] % window_size) % window_size
            pad_hw = (feat_size[0] + pad_h, feat_size[1] + pad_w)

        # Reverse window partition
        if self.window_size > 0:
            x = window_unpartition(x, window_size, pad_hw, feat_size)

        x = shortcut + self.drop_path(x)

        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class HieraPatchEmbed(nn.Module):
    """
    Image to Patch Embedding.
    """

    def __init__(
        self,
        kernel_size: Tuple[int, ...] = (7, 7),
        stride: Tuple[int, ...] = (4, 4),
        padding: Tuple[int, ...] = (3, 3),
        in_chans: int = 3,
        embed_dim: int = 768,
    ):
        """
        Args:
            kernel_size (Tuple): kernel size of the projection layer.
            stride (Tuple): stride of the projection layer.
            padding (Tuple): padding size of the projection layer.
            in_chans (int): Number of input image channels.
            embed_dim (int):  embed_dim (int): Patch embedding dimension.
        """
        super().__init__()
        self.proj = nn.Conv2d(
            in_chans, embed_dim, kernel_size=kernel_size, stride=stride, padding=padding
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.proj(x)
        # B C H W -> B H W C
        x = x.permute(0, 2, 3, 1)
        return x


class HieraDet(nn.Module):
    """
    Reference: https://arxiv.org/abs/2306.00989
    """

    def __init__(
            self,
            in_chans: int = 3,
            num_classes: int = 1000,
            global_pool: str = 'avg',
            embed_dim: int = 96,  # initial embed dim
            num_heads: int = 1,  # initial number of heads
            patch_kernel: Tuple[int, ...] = (7, 7),
            patch_stride: Tuple[int, ...] = (4, 4),
            patch_padding: Tuple[int, ...] = (3, 3),
            patch_size: Optional[Tuple[int, ...]] = None,
            q_pool: int = 3,  # number of q_pool stages
            q_stride: Tuple[int, int] = (2, 2),  # downsample stride bet. stages
            stages: Tuple[int, ...] = (2, 3, 16, 3),  # blocks per stage
            dim_mul: float = 2.0,  # dim_mul factor at stage shift
            head_mul: float = 2.0,  # head_mul factor at stage shift
            global_pos_size: Tuple[int, int] = (7, 7),
            # window size per stage, when not using global att.
            window_spec: Tuple[int, ...] = (
                8,
                4,
                14,
                7,
            ),
            # global attn in these blocks
            global_att_blocks: Tuple[int, ...] = (
                12,
                16,
                20,
            ),
            weight_init: str = '',
            fix_init: bool = True,
            head_init_scale: float = 0.001,
            drop_rate: float = 0.0,
            drop_path_rate: float = 0.0,  # stochastic depth
            norm_layer: Union[nn.Module, str] = "LayerNorm",
            act_layer: Union[nn.Module, str] = "GELU",
    ):
        super().__init__()
        norm_layer = get_norm_layer(norm_layer)
        act_layer = get_act_layer(act_layer)
        assert len(stages) == len(window_spec)
        self.num_classes = num_classes
        self.window_spec = window_spec

        depth = sum(stages)
        self.q_stride = q_stride
        self.stage_ends = [sum(stages[:i]) - 1 for i in range(1, len(stages) + 1)]
        assert 0 <= q_pool <= len(self.stage_ends[:-1])
        self.q_pool_blocks = [x + 1 for x in self.stage_ends[:-1]][:q_pool]

        if patch_size is not None:
            # use a non-overlapping vit style patch embed
            self.patch_embed = PatchEmbed(
                img_size=None,
                patch_size=patch_size,
                in_chans=in_chans,
                embed_dim=embed_dim,
                output_fmt='NHWC',
                dynamic_img_pad=True,
            )
        else:
            self.patch_embed = HieraPatchEmbed(
                kernel_size=patch_kernel,
                stride=patch_stride,
                padding=patch_padding,
                in_chans=in_chans,
                embed_dim=embed_dim,
            )
        # Which blocks have global att?
        self.global_att_blocks = global_att_blocks

        # Windowed positional embedding (https://arxiv.org/abs/2311.05613)
        self.global_pos_size = global_pos_size
        self.pos_embed = nn.Parameter(torch.zeros(1, embed_dim, *self.global_pos_size))
        self.pos_embed_window = nn.Parameter(torch.zeros(1, embed_dim, self.window_spec[0], self.window_spec[0]))

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        cur_stage = 0
        self.blocks = nn.Sequential()
        self.feature_info = []
        for i in range(depth):
            dim_out = embed_dim
            # lags by a block, so first block of
            # next stage uses an initial window size
            # of previous stage and final window size of current stage
            window_size = self.window_spec[cur_stage]

            if self.global_att_blocks is not None:
                window_size = 0 if i in self.global_att_blocks else window_size

            if i - 1 in self.stage_ends:
                dim_out = int(embed_dim * dim_mul)
                num_heads = int(num_heads * head_mul)
                cur_stage += 1

            block = MultiScaleBlock(
                dim=embed_dim,
                dim_out=dim_out,
                num_heads=num_heads,
                drop_path=dpr[i],
                q_stride=self.q_stride if i in self.q_pool_blocks else None,
                window_size=window_size,
                norm_layer=norm_layer,
                act_layer=act_layer,
            )

            embed_dim = dim_out
            self.blocks.append(block)
            if i in self.stage_ends:
                self.feature_info += [
                    dict(num_chs=dim_out, reduction=2**(cur_stage+2), module=f'blocks.{self.stage_ends[cur_stage]}')]

        self.channel_list = (
            [self.blocks[i].dim_out for i in self.stage_ends[::-1]]
            if True else
            [self.blocks[-1].dim_out]
        )

        self.num_features = self.head_hidden_size = embed_dim
        self.head = ClNormMlpClassifierHead(
            embed_dim,
            num_classes,
            pool_type=global_pool,
            drop_rate=drop_rate,
            norm_layer=norm_layer,
        )

        # Initialize everything
        if self.pos_embed is not None:
            nn.init.trunc_normal_(self.pos_embed, std=0.02)

        if self.pos_embed_window is not None:
            nn.init.trunc_normal_(self.pos_embed_window, std=0.02)

        if weight_init != 'skip':
            init_fn = init_weight_jax if weight_init == 'jax' else init_weight_vit
            init_fn = partial(init_fn, classifier_name='head.fc')
            named_apply(init_fn, self)

        if fix_init:
            self.fix_init_weight()

        if isinstance(self.head, ClNormMlpClassifierHead) and isinstance(self.head.fc, nn.Linear):
            self.head.fc.weight.data.mul_(head_init_scale)
            self.head.fc.bias.data.mul_(head_init_scale)

    def _pos_embed(self, x: torch.Tensor) -> torch.Tensor:
        h, w = x.shape[1:3]
        window_embed = self.pos_embed_window
        pos_embed = F.interpolate(self.pos_embed, size=(h, w), mode="bicubic")
        tile_h = pos_embed.shape[-2] // window_embed.shape[-2]
        tile_w = pos_embed.shape[-1] // window_embed.shape[-1]
        pos_embed = pos_embed + window_embed.tile((tile_h, tile_w))
        pos_embed = pos_embed.permute(0, 2, 3, 1)
        return x + pos_embed

    def fix_init_weight(self):
        def rescale(param, _layer_id):
            param.div_(math.sqrt(2.0 * _layer_id))

        for layer_id, layer in enumerate(self.blocks):
            rescale(layer.attn.proj.weight.data, layer_id + 1)
            rescale(layer.mlp.fc2.weight.data, layer_id + 1)

    @torch.jit.ignore
    def no_weight_decay(self):
        return ['pos_embed', 'pos_embed_window']

    @torch.jit.ignore
    def group_matcher(self, coarse: bool = False) -> Dict:
        return dict(
            stem=r'^pos_embed|pos_embed_window|patch_embed',
            blocks=[(r'^blocks\.(\d+)', None)]
        )

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable: bool = True) -> None:
        self.grad_checkpointing = enable

    @torch.jit.ignore
    def get_classifier(self):
        return self.head.fc

    def reset_classifier(self, num_classes: int, global_pool: Optional[str] = None, reset_other: bool = False):
        self.num_classes = num_classes
        self.head.reset(num_classes, pool_type=global_pool, reset_other=reset_other)

    def forward_intermediates(
            self,
            x: torch.Tensor,
            indices: Optional[Union[int, List[int]]] = None,
            norm: bool = False,
            stop_early: bool = True,
            output_fmt: str = 'NCHW',
            intermediates_only: bool = False,
            coarse: bool = True,
    ) -> Union[List[torch.Tensor], Tuple[torch.Tensor, List[torch.Tensor]]]:
        """ Forward features that returns intermediates.

        Args:
            x: Input image tensor
            indices: Take last n blocks if int, all if None, select matching indices if sequence
            norm: Apply norm layer to all intermediates
            stop_early: Stop iterating over blocks when last desired intermediate hit
            output_fmt: Shape of intermediate feature outputs
            intermediates_only: Only return intermediate features
            coarse: Take coarse features (stage ends) if true, otherwise all block featrures
        Returns:

        """
        assert not norm, 'normalization of features not supported'
        assert output_fmt in ('NCHW', 'NHWC'), 'Output format must be one of NCHW, NHWC.'
        if coarse:
            take_indices, max_index = feature_take_indices(len(self.stage_ends), indices)
            take_indices = [self.stage_ends[i] for i in take_indices]
            max_index = self.stage_ends[max_index]
        else:
            take_indices, max_index = feature_take_indices(len(self.blocks), indices)

        x = self.patch_embed(x)
        x = self._pos_embed(x)

        intermediates = []
        if torch.jit.is_scripting() or not stop_early:  # can't slice blocks in torchscript
            blocks = self.blocks
        else:
            blocks = self.blocks[:max_index + 1]
        for i, blk in enumerate(blocks):
            x = blk(x)
            if i in take_indices:
                x_out = x.permute(0, 3, 1, 2) if output_fmt == 'NCHW' else x
                intermediates.append(x_out)

        if intermediates_only:
            return intermediates

        return x, intermediates

    def prune_intermediate_layers(
            self,
            indices: Union[int, List[int]] = 1,
            prune_norm: bool = False,
            prune_head: bool = True,
            coarse: bool = True,
    ):
        """ Prune layers not required for specified intermediates.
        """
        if coarse:
            take_indices, max_index = feature_take_indices(len(self.stage_ends), indices)
            max_index = self.stage_ends[max_index]
        else:
            take_indices, max_index = feature_take_indices(len(self.blocks), indices)
        self.blocks = self.blocks[:max_index + 1]  # truncate blocks
        if prune_head:
            self.head.reset(0, reset_other=True)
        return take_indices

    def forward_features(self, x: torch.Tensor) -> List[torch.Tensor]:
        x = self.patch_embed(x)  # BHWC
        x = self._pos_embed(x)
        for i, blk in enumerate(self.blocks):
            x = blk(x)
        return x

    def forward_head(self, x, pre_logits: bool = False) -> torch.Tensor:
        x = self.head(x, pre_logits=pre_logits) if pre_logits else self.head(x)
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x


# NOTE sam2 appears to use 1024x1024 for all models, but T, S, & B+ have windows that fit multiples of 224.
def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 0, 'input_size': (3, 896, 896), 'pool_size': (28, 28),
        'crop_pct': 1.0, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.proj', 'classifier': 'head.fc',
        **kwargs
    }


default_cfgs = generate_default_cfgs({
    "sam2_hiera_tiny.r224": _cfg(
        hf_hub_id='facebook/sam2-hiera-tiny',
        hf_hub_filename='sam2_hiera_tiny.pt',
        input_size=(3, 224, 224), pool_size=(7, 7),
    ),  # FIXME reduced res for testing
    "sam2_hiera_tiny.r896": _cfg(
        hf_hub_id='facebook/sam2-hiera-tiny',
        hf_hub_filename='sam2_hiera_tiny.pt',
    ),
    "sam2_hiera_small": _cfg(
        hf_hub_id='facebook/sam2-hiera-small',
        hf_hub_filename='sam2_hiera_small.pt',
    ),
    "sam2_hiera_base_plus": _cfg(
        hf_hub_id='facebook/sam2-hiera-base-plus',
        hf_hub_filename='sam2_hiera_base_plus.pt',
    ),
    "sam2_hiera_large": _cfg(
        hf_hub_id='facebook/sam2-hiera-large',
        hf_hub_filename='sam2_hiera_large.pt',
        input_size=(3, 1024, 1024), pool_size=(32, 32),
    ),
})


def checkpoint_filter_fn(state_dict, model=None, prefix=''):
    state_dict = state_dict.get('model', state_dict)

    output = {}
    for k, v in state_dict.items():
        if k.startswith(prefix):
            k = k.replace(prefix, '')
        else:
            continue
        k = k.replace('mlp.layers.0', 'mlp.fc1')
        k = k.replace('mlp.layers.1', 'mlp.fc2')
        output[k] = v
    return output


def _create_hiera_det(variant: str, pretrained: bool = False, **kwargs) -> HieraDet:
    out_indices = kwargs.pop('out_indices', 4)
    if True:  # kwargs.get('pretrained_cfg', '') == '?':
        # SAM2 pretrained weights have no classifier or final norm-layer (`head.norm`)
        # This is workaround loading with num_classes=0 w/o removing norm-layer.
        kwargs.setdefault('pretrained_strict', False)
    checkpoint_prefix = 'image_encoder.trunk.' if 'sam2' in variant else ''
    return build_model_with_cfg(
        HieraDet,
        variant,
        pretrained,
        pretrained_filter_fn=partial(checkpoint_filter_fn, prefix=checkpoint_prefix),
        feature_cfg=dict(out_indices=out_indices, feature_cls='getter'),
        **kwargs,
    )


@register_model
def sam2_hiera_tiny(pretrained=False, **kwargs):
    model_args = dict(stages=(1, 2, 7, 2), global_att_blocks=(5, 7, 9))
    return _create_hiera_det('sam2_hiera_tiny', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def sam2_hiera_small(pretrained=False, **kwargs):
    model_args = dict(stages=(1, 2, 11, 2), global_att_blocks=(7, 10, 13))
    return _create_hiera_det('sam2_hiera_small', pretrained=pretrained, **dict(model_args, **kwargs))


# @register_model
# def sam2_hiera_base(pretrained=False, **kwargs):
#     model_args = dict()
#     return _create_hiera_det('sam2_hiera_base', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def sam2_hiera_base_plus(pretrained=False, **kwargs):
    model_args = dict(embed_dim=112, num_heads=2, global_pos_size=(14, 14))
    return _create_hiera_det('sam2_hiera_base_plus', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def sam2_hiera_large(pretrained=False, **kwargs):
    model_args = dict(
        embed_dim=144,
        num_heads=2,
        stages=(2, 6, 36, 4),
        global_att_blocks=(23, 33, 43),
        window_spec=(8, 4, 16, 8),
    )
    return _create_hiera_det('sam2_hiera_large', pretrained=pretrained, **dict(model_args, **kwargs))
