""" EfficientViT (by MIT Song Han's Lab)

Paper: `Efficientvit: Enhanced linear attention for high-resolution low-computation visual recognition`
    - https://arxiv.org/abs/2205.14756

Adapted from official impl at https://github.com/mit-han-lab/efficientvit
"""

__all__ = ['EfficientVit']
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from ._registry import register_model, generate_default_cfgs
from ._builder import build_model_with_cfg
from ._manipulate import checkpoint_seq
from timm.layers import SelectAdaptivePool2d, create_conv2d


def val2list(x: list or tuple or any, repeat_time=1):
    if isinstance(x, (list, tuple)):
        return list(x)
    return [x for _ in range(repeat_time)]


def val2tuple(x: list or tuple or any, min_len: int = 1, idx_repeat: int = -1):
    # repeat elements if necessary
    x = val2list(x)
    if len(x) > 0:
        x[idx_repeat:idx_repeat] = [x[idx_repeat] for _ in range(min_len - len(x))]

    return tuple(x)


def get_same_padding(kernel_size: int or tuple[int, ...]) -> int or tuple[int, ...]:
    if isinstance(kernel_size, tuple):
        return tuple([get_same_padding(ks) for ks in kernel_size])
    else:
        assert kernel_size % 2 > 0, "kernel size should be odd number"
        return kernel_size // 2


class ConvNormAct(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size=3,
        stride=1,
        dilation=1,
        groups=1,
        bias=False,
        dropout=0,
        norm_layer=nn.BatchNorm2d,
        act_layer=nn.ReLU,
    ):
        super(ConvNormAct, self).__init__()
        self.dropout = nn.Dropout(dropout, inplace=False)
        self.conv = create_conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            dilation=dilation,
            groups=groups,
            bias=bias,
        )
        self.norm = norm_layer(num_features=out_channels) if norm_layer else nn.Identity()
        self.act = act_layer(inplace=True) if act_layer else nn.Identity()

    def forward(self, x):
        x = self.dropout(x)
        x = self.conv(x)
        x = self.norm(x)
        x = self.act(x)
        return x


class DSConv(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size=3,
        stride=1,
        use_bias=False,
        norm_layer=(nn.BatchNorm2d, nn.BatchNorm2d),
        act_layer=(nn.ReLU6, None),
    ):
        super(DSConv, self).__init__()
        use_bias = val2tuple(use_bias, 2)
        norm_layer = val2tuple(norm_layer, 2)
        act_layer = val2tuple(act_layer, 2)

        self.depth_conv = ConvNormAct(
            in_channels,
            in_channels,
            kernel_size,
            stride,
            groups=in_channels,
            norm_layer=norm_layer[0],
            act_layer=act_layer[0],
            bias=use_bias[0],
        )
        self.point_conv = ConvNormAct(
            in_channels,
            out_channels,
            1,
            norm_layer=norm_layer[1],
            act_layer=act_layer[1],
            bias=use_bias[1],
        )

    def forward(self, x):
        x = self.depth_conv(x)
        x = self.point_conv(x)
        return x


class MBConv(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size=3,
        stride=1,
        mid_channels=None,
        expand_ratio=6,
        use_bias=False,
        norm_layer=(nn.BatchNorm2d, nn.BatchNorm2d, nn.BatchNorm2d),
        act_layer=(nn.ReLU6, nn.ReLU6, None),
    ):
        super(MBConv, self).__init__()
        use_bias = val2tuple(use_bias, 3)
        norm_layer = val2tuple(norm_layer, 3)
        act_layer = val2tuple(act_layer, 3)
        mid_channels = mid_channels or round(in_channels * expand_ratio)

        self.inverted_conv = ConvNormAct(
            in_channels,
            mid_channels,
            1,
            stride=1,
            norm_layer=norm_layer[0],
            act_layer=act_layer[0],
            bias=use_bias[0],
        )
        self.depth_conv = ConvNormAct(
            mid_channels,
            mid_channels,
            kernel_size,
            stride=stride,
            groups=mid_channels,
            norm_layer=norm_layer[1],
            act_layer=act_layer[1],
            bias=use_bias[1],
        )
        self.point_conv = ConvNormAct(
            mid_channels,
            out_channels,
            1,
            norm_layer=norm_layer[2],
            act_layer=act_layer[2],
            bias=use_bias[2],
        )

    def forward(self, x):
        x = self.inverted_conv(x)
        x = self.depth_conv(x)
        x = self.point_conv(x)
        return x


class LiteMSA(nn.Module):
    """Lightweight multi-scale attention"""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        heads: int or None = None,
        heads_ratio: float = 1.0,
        dim=8,
        use_bias=False,
        norm_layer=(None, nn.BatchNorm2d),
        act_layer=(None, None),
        kernel_func=nn.ReLU,
        scales=(5,),
        eps=1e-5,
    ):
        super(LiteMSA, self).__init__()
        self.eps = eps
        heads = heads or int(in_channels // dim * heads_ratio)
        total_dim = heads * dim
        use_bias = val2tuple(use_bias, 2)
        norm_layer = val2tuple(norm_layer, 2)
        act_layer = val2tuple(act_layer, 2)

        self.dim = dim
        self.qkv = ConvNormAct(
            in_channels,
            3 * total_dim,
            1,
            bias=use_bias[0],
            norm_layer=norm_layer[0],
            act_layer=act_layer[0],
        )
        self.aggreg = nn.ModuleList([
            nn.Sequential(
                nn.Conv2d(
                    3 * total_dim,
                    3 * total_dim,
                    scale,
                    padding=get_same_padding(scale),
                    groups=3 * total_dim,
                    bias=use_bias[0],
                ),
                nn.Conv2d(3 * total_dim, 3 * total_dim, 1, groups=3 * heads, bias=use_bias[0]),
            )
            for scale in scales
        ])
        self.kernel_func = kernel_func(inplace=False)

        self.proj = ConvNormAct(
            total_dim * (1 + len(scales)),
            out_channels,
            1,
            bias=use_bias[1],
            norm_layer=norm_layer[1],
            act_layer=act_layer[1],
        )

    def forward(self, x):
        B, _, H, W = x.shape

        # generate multi-scale q, k, v
        qkv = self.qkv(x)
        multi_scale_qkv = [qkv]
        for op in self.aggreg:
            multi_scale_qkv.append(op(qkv))
        multi_scale_qkv = torch.cat(multi_scale_qkv, dim=1)
        multi_scale_qkv = multi_scale_qkv.reshape(B, -1, 3 * self.dim, H * W).transpose(-1, -2)
        q, k, v = multi_scale_qkv.tensor_split(3, dim=-1)

        # lightweight global attention
        q = self.kernel_func(q)
        k = self.kernel_func(k)
        v = F.pad(v, (0, 1), mode="constant", value=1)

        kv = k.transpose(-1, -2) @ v
        out = q @ kv
        out = out[..., :-1] / (out[..., -1:] + self.eps)

        # final projection
        out = out.transpose(-1, -2).reshape(B, -1, H, W)
        out = self.proj(out)
        return out


class EfficientVitBlock(nn.Module):
    def __init__(
        self,
        in_channels,
        heads_ratio=1.0,
        head_dim=32,
        expand_ratio=4,
        norm_layer=nn.BatchNorm2d,
        act_layer=nn.Hardswish,
    ):
        super(EfficientVitBlock, self).__init__()
        self.context_module = ResidualBlock(
            LiteMSA(
                in_channels=in_channels,
                out_channels=in_channels,
                heads_ratio=heads_ratio,
                dim=head_dim,
                norm_layer=(None, norm_layer),
            ),
            nn.Identity(),
        )
        self.local_module = ResidualBlock(
            MBConv(
                in_channels=in_channels,
                out_channels=in_channels,
                expand_ratio=expand_ratio,
                use_bias=(True, True, False),
                norm_layer=(None, None, norm_layer),
                act_layer=(act_layer, act_layer, None),
            ),
            nn.Identity(),
        )

    def forward(self, x):
        x = self.context_module(x)
        x = self.local_module(x)
        return x


class ResidualBlock(nn.Module):
    def __init__(
        self,
        main: Optional[nn.Module],
        shortcut: Optional[nn.Module] = None,
        pre_norm: Optional[nn.Module] = None,
    ):
        super(ResidualBlock, self).__init__()
        self.pre_norm = pre_norm if pre_norm is not None else nn.Identity()
        self.main = main
        self.shortcut = shortcut

    def forward(self, x):
        res = self.main(self.pre_norm(x))
        if self.shortcut is not None:
            res = res + self.shortcut(x)
        return res


def build_local_block(        
        in_channels: int,
        out_channels: int,
        stride: int,
        expand_ratio: float,
        norm_layer: str,
        act_layer: str,
        fewer_norm: bool = False,
):
    if expand_ratio == 1:
        block = DSConv(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
            use_bias=(True, False) if fewer_norm else False,
            norm_layer=(None, norm_layer) if fewer_norm else norm_layer,
            act_layer=(act_layer, None),
        )
    else:
        block = MBConv(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
            expand_ratio=expand_ratio,
            use_bias=(True, True, False) if fewer_norm else False,
            norm_layer=(None, None, norm_layer) if fewer_norm else norm_layer,
            act_layer=(act_layer, act_layer, None),
        )
    return block


class Stem(nn.Sequential):
    def __init__(self, in_chs, out_chs, depth, norm_layer, act_layer):
        super().__init__()
        self.stride = 2

        self.add_module(
            'in_conv',
            ConvNormAct(
                in_chs, out_chs,
                kernel_size=3, stride=2, norm_layer=norm_layer, act_layer=act_layer,
            )
        )
        stem_block = 0
        for _ in range(depth):
            self.add_module(f'res{stem_block}', ResidualBlock(
                build_local_block(
                    in_channels=out_chs,
                    out_channels=out_chs,
                    stride=1,
                    expand_ratio=1,
                    norm_layer=norm_layer,
                    act_layer=act_layer,
                ),
                nn.Identity(),
            ))
            stem_block += 1


class EfficientVitStage(nn.Module):
    def __init__(
            self,
            in_chs,
            out_chs,
            depth,
            norm_layer,
            act_layer,
            expand_ratio,
            head_dim,
            vit_stage=False,
    ):
        super(EfficientVitStage, self).__init__()
        blocks = [ResidualBlock(
            build_local_block(
                in_channels=in_chs,
                out_channels=out_chs,
                stride=2,
                expand_ratio=expand_ratio,
                norm_layer=norm_layer,
                act_layer=act_layer,
                fewer_norm=vit_stage,
            ),
            None,
        )]
        in_chs = out_chs

        if vit_stage:
            # for stage 3, 4
            for _ in range(depth):
                blocks.append(
                    EfficientVitBlock(
                        in_channels=in_chs,
                        head_dim=head_dim,
                        expand_ratio=expand_ratio,
                        norm_layer=norm_layer,
                        act_layer=act_layer,
                    )
                )
        else:
            # for stage 1, 2
            for i in range(1, depth):
                blocks.append(ResidualBlock(
                    build_local_block(
                        in_channels=in_chs,
                        out_channels=out_chs,
                        stride=1,
                        expand_ratio=expand_ratio,
                        norm_layer=norm_layer,
                        act_layer=act_layer
                    ),
                    nn.Identity(),
                ))

        self.blocks = nn.Sequential(*blocks)

    def forward(self, x):
        return self.blocks(x)


class ClassifierHead(nn.Module):
    def __init__(
        self,
        in_channels,
        widths,
        n_classes=1000,
        dropout=0,
        norm_layer=nn.BatchNorm2d,
        act_layer=nn.Hardswish,
        global_pool='avg',
    ):
        super(ClassifierHead, self).__init__()
        self.in_conv = ConvNormAct(in_channels, widths[0], 1, norm_layer=norm_layer, act_layer=act_layer)
        self.global_pool = SelectAdaptivePool2d(pool_type=global_pool, flatten=True, input_fmt='NCHW')
        self.classifier = nn.Sequential(
            nn.Linear(widths[0], widths[1], bias=False),
            nn.LayerNorm(widths[1]),
            act_layer(inplace=True),
            nn.Dropout(dropout, inplace=False),
            nn.Linear(widths[1], n_classes, bias=True),
        )

    def forward(self, x, pre_logits: bool = False):
        x = self.in_conv(x)
        x = self.global_pool(x)
        if pre_logits:
            return x
        x = self.classifier(x)
        return x


class EfficientVit(nn.Module):
    def __init__(
        self,
        in_chans=3,
        widths=(),
        depths=(),
        head_dim=32,
        expand_ratio=4,
        norm_layer=nn.BatchNorm2d,
        act_layer=nn.Hardswish,
        global_pool='avg',
        head_widths=(),
        drop_rate=0.0,
        num_classes=1000,
    ):
        super(EfficientVit, self).__init__()
        self.grad_checkpointing = False
        self.global_pool = global_pool
        self.num_classes = num_classes

        # input stem
        self.stem = Stem(in_chans, widths[0], depths[0], norm_layer, act_layer)
        stride = self.stem.stride

        # stages
        self.feature_info = []
        stages = []
        stage_idx = 0
        in_channels = widths[0]
        for i, (w, d) in enumerate(zip(widths[1:], depths[1:])):
            stages.append(EfficientVitStage(
                in_channels,
                w,
                depth=d,
                norm_layer=norm_layer,
                act_layer=act_layer,
                expand_ratio=expand_ratio,
                head_dim=head_dim,
                vit_stage=i >= 2,
            ))
            stride *= 2
            in_channels = w
            self.feature_info += [dict(num_chs=in_channels, reduction=stride, module=f'stages.{stage_idx}')]
            stage_idx += 1

        self.stages = nn.Sequential(*stages)
        self.num_features = in_channels
        self.head_widths = head_widths
        self.head_dropout = drop_rate
        if num_classes > 0:
            self.head = ClassifierHead(
                self.num_features,
                self.head_widths,
                n_classes=num_classes,
                dropout=self.head_dropout,
                global_pool=self.global_pool,
            )
        else:
            if self.global_pool == 'avg':
                self.head = SelectAdaptivePool2d(pool_type=global_pool, flatten=True)
            else:
                self.head = nn.Identity()

    @torch.jit.ignore
    def group_matcher(self, coarse=False):
        matcher = dict(
            stem=r'^stem',  # stem and embed
            blocks=[(r'^stages\.(\d+)', None)]
        )
        return matcher

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.grad_checkpointing = enable

    @torch.jit.ignore
    def get_classifier(self):
        return self.head.classifier[-1]

    def reset_classifier(self, num_classes, global_pool=None, dropout=0):
        self.num_classes = num_classes
        if global_pool is not None:
            self.global_pool = global_pool
        if num_classes > 0:
            self.head = ClassifierHead(
                self.num_features,
                self.head_widths,
                n_classes=num_classes,
                dropout=self.head_dropout,
                global_pool=self.global_pool,
            )
        else:
            if self.global_pool == 'avg':
                self.head = SelectAdaptivePool2d(pool_type=global_pool, flatten=True)
            else:
                self.head = nn.Identity()

    def forward_features(self, x):
        x = self.stem(x)
        if self.grad_checkpointing and not torch.jit.is_scripting():
            x = checkpoint_seq(self.stages, x)
        else:
            x = self.stages(x)
        return x

    def forward_head(self, x, pre_logits: bool = False):
        return self.head(x, pre_logits=pre_logits) if pre_logits else self.head(x)

    def forward(self, x):
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000,
        'mean': IMAGENET_DEFAULT_MEAN,
        'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'stem.in_conv.conv',
        'classifier': 'head.classifier.4',
        'crop_pct': 0.95,
        'input_size': (3, 224, 224),
        **kwargs,
    }


default_cfgs = generate_default_cfgs({
    'efficientvit_b0.r224_in1k': _cfg(
        hf_hub_id='timm/',
    ),
    'efficientvit_b1.r224_in1k': _cfg(
        hf_hub_id='timm/',
    ),
    'efficientvit_b1.r256_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 256, 256), crop_pct=1.0,
    ),
    'efficientvit_b1.r288_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 288, 288), crop_pct=1.0,
    ),
    'efficientvit_b2.r224_in1k': _cfg(
        hf_hub_id='timm/',
    ),
    'efficientvit_b2.r256_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 256, 256), crop_pct=1.0,
    ),
    'efficientvit_b2.r288_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 288, 288), crop_pct=1.0,
    ),
    'efficientvit_b3.r224_in1k': _cfg(
        hf_hub_id='timm/',
    ),
    'efficientvit_b3.r256_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 256, 256), crop_pct=1.0,
    ),
    'efficientvit_b3.r288_in1k': _cfg(
        hf_hub_id='timm/',
        input_size=(3, 288, 288), crop_pct=1.0,
    ),
})


def _create_efficientvit(variant, pretrained=False, **kwargs):
    out_indices = kwargs.pop('out_indices', (0, 1, 2, 3))
    model = build_model_with_cfg(
        EfficientVit,
        variant,
        pretrained,
        feature_cfg=dict(flatten_sequential=True, out_indices=out_indices),
        **kwargs
    )
    return model


@register_model
def efficientvit_b0(pretrained=False, **kwargs):
    model_args = dict(
        widths=(8, 16, 32, 64, 128), depths=(1, 2, 2, 2, 2), head_dim=16, head_widths=(1024, 1280))
    return _create_efficientvit('efficientvit_b0', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def efficientvit_b1(pretrained=False, **kwargs):
    model_args = dict(
        widths=(16, 32, 64, 128, 256), depths=(1, 2, 3, 3, 4), head_dim=16, head_widths=(1536, 1600))
    return _create_efficientvit('efficientvit_b1', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def efficientvit_b2(pretrained=False, **kwargs):
    model_args = dict(
        widths=(24, 48, 96, 192, 384), depths=(1, 3, 4, 4, 6), head_dim=32, head_widths=(2304, 2560))
    return _create_efficientvit('efficientvit_b2', pretrained=pretrained, **dict(model_args, **kwargs))


@register_model
def efficientvit_b3(pretrained=False, **kwargs):
    model_args = dict(
        widths=(32, 64, 128, 256, 512), depths=(1, 4, 6, 6, 9), head_dim=32, head_widths=(2304, 2560))
    return _create_efficientvit('efficientvit_b3', pretrained=pretrained, **dict(model_args, **kwargs))
