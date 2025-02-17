import os
import math
import torch
import warnings
import torch.nn as nn
from functools import partial
import torch.nn.functional as F
from timm.models.registry import register_model
from mmengine.model import constant_init, kaiming_init
from timm.models.layers import DropPath, to_2tuple, make_divisible, trunc_normal_
warnings.filterwarnings('ignore')


class OverlapPatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=7, stride=4, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)

        self.img_size = img_size
        self.patch_size = patch_size
        self.H, self.W = img_size[0] // patch_size[0], img_size[1] // patch_size[1]
        self.num_patches = self.H * self.W
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                              padding=(patch_size[0] // 2, patch_size[1] // 2))
        self.norm = nn.LayerNorm(embed_dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        x = self.proj(x)
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)

        return x, H, W

class DWConv(nn.Module):
    def __init__(self, dim=768):
        super(DWConv, self).__init__()
        self.dwconv = nn.Conv2d(dim, dim, 3, 1, 1, bias=True, groups=dim)

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(1, 2).view(B, C, H, W)
        x = self.dwconv(x)
        x = x.flatten(2).transpose(1, 2)

        return x

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=1):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio)
            self.norm = nn.LayerNorm(dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        B, N, C = x.shape
        q = self.q(x).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_).reshape(B, C, -1).permute(0, 2, 1)
            x_ = self.norm(x_)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        else:
            kv = self.kv(x).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1]

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.dwconv = DWConv(hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        x = self.fc1(x)
        x = self.dwconv(x, H, W)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, sr_ratio=1):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        x = x + self.drop_path(self.attn(self.norm1(x), H, W))
        x = x + self.drop_path(self.mlp(self.norm2(x), H, W))

        return x

class PyramidVisionTransformerImpr(nn.Module):
    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dims=[64, 128, 256, 512],
                 num_heads=[1, 2, 4, 8], mlp_ratios=[4, 4, 4, 4], qkv_bias=False, qk_scale=None, drop_rate=0.,
                 attn_drop_rate=0., drop_path_rate=0., norm_layer=nn.LayerNorm,
                 depths=[3, 4, 6, 3], sr_ratios=[8, 4, 2, 1]):
        super().__init__()
        self.num_classes = num_classes
        self.depths = depths

        # patch_embed
        self.patch_embed1 = OverlapPatchEmbed(img_size=img_size, patch_size=7, stride=4, in_chans=in_chans,
                                              embed_dim=embed_dims[0])
        self.patch_embed2 = OverlapPatchEmbed(img_size=img_size // 4, patch_size=3, stride=2, in_chans=embed_dims[0],
                                              embed_dim=embed_dims[1])
        self.patch_embed3 = OverlapPatchEmbed(img_size=img_size // 8, patch_size=3, stride=2, in_chans=embed_dims[1],
                                              embed_dim=embed_dims[2])
        self.patch_embed4 = OverlapPatchEmbed(img_size=img_size // 16, patch_size=3, stride=2, in_chans=embed_dims[2],
                                              embed_dim=embed_dims[3])

        # transformer encoder
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0
        self.block1 = nn.ModuleList([Block(
            dim=embed_dims[0], num_heads=num_heads[0], mlp_ratio=mlp_ratios[0], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[0])
            for i in range(depths[0])])
        self.norm1 = norm_layer(embed_dims[0])

        cur += depths[0]
        self.block2 = nn.ModuleList([Block(
            dim=embed_dims[1], num_heads=num_heads[1], mlp_ratio=mlp_ratios[1], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[1])
            for i in range(depths[1])])
        self.norm2 = norm_layer(embed_dims[1])

        cur += depths[1]
        self.block3 = nn.ModuleList([Block(
            dim=embed_dims[2], num_heads=num_heads[2], mlp_ratio=mlp_ratios[2], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[2])
            for i in range(depths[2])])
        self.norm3 = norm_layer(embed_dims[2])

        cur += depths[2]
        self.block4 = nn.ModuleList([Block(
            dim=embed_dims[3], num_heads=num_heads[3], mlp_ratio=mlp_ratios[3], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[3])
            for i in range(depths[3])])
        self.norm4 = norm_layer(embed_dims[3])

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def init_weights(self, pretrained=None):
        if isinstance(pretrained, str):
            logger = 1
            #load_checkpoint(self, pretrained, map_location='cpu', strict=False, logger=logger)

    def reset_drop_path(self, drop_path_rate):
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(self.depths))]
        cur = 0
        for i in range(self.depths[0]):
            self.block1[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[0]
        for i in range(self.depths[1]):
            self.block2[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[1]
        for i in range(self.depths[2]):
            self.block3[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[2]
        for i in range(self.depths[3]):
            self.block4[i].drop_path.drop_prob = dpr[cur + i]

    def freeze_patch_emb(self):
        self.patch_embed1.requires_grad = False

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed1', 'pos_embed2', 'pos_embed3', 'pos_embed4', 'cls_token'}  # has pos_embed may be better

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x):
        B = x.shape[0]
        outs = []

        # stage 1
        x, H, W = self.patch_embed1(x)
        for i, blk in enumerate(self.block1):
            x = blk(x, H, W)
        x = self.norm1(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 2
        x, H, W = self.patch_embed2(x)
        for i, blk in enumerate(self.block2):
            x = blk(x, H, W)
        x = self.norm2(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 3
        x, H, W = self.patch_embed3(x)
        for i, blk in enumerate(self.block3):
            x = blk(x, H, W)
        x = self.norm3(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 4
        x, H, W = self.patch_embed4(x)
        for i, blk in enumerate(self.block4):
            x = blk(x, H, W)
        x = self.norm4(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        return outs

    def forward(self, x):
        x = self.forward_features(x)

        return x

@register_model
class pvt_v2_b2(PyramidVisionTransformerImpr):
    def __init__(self,in_chans=3, embed_dims= [64, 128, 320, 512], **kwargs):
        super(pvt_v2_b2, self).__init__(
            in_chans=in_chans,patch_size=4, embed_dims=embed_dims, num_heads=[1, 2, 5, 8], mlp_ratios=[8, 8, 4, 4], 
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 4, 6, 3], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, i):
        ctx.save_for_backward(i)
        return i * torch.sigmoid(i)

    @staticmethod
    def backward(ctx, grad_output):
        sigmoid_i = torch.sigmoid(ctx.saved_variables[0])
        return grad_output * (sigmoid_i * (1 + ctx.saved_variables[0] * (1 - sigmoid_i)))

# Swish激活函数
class Swish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)

class MLP(nn.Module):
    def __init__(self, dim, mlp_ratio, act):
        super(MLP, self).__init__()
        self.line_conv_0 = nn.Conv2d(dim, dim * mlp_ratio, kernel_size=1, bias=False)
        self.act = act
        self.line_conv_1 = nn.Conv2d(dim * mlp_ratio, dim, kernel_size=1, bias=False)

    def forward(self, x):
        x = self.line_conv_0(x)
        x = self.act(x)
        x = self.line_conv_1(x)
        return x

class BasicConv2d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, act=nn.ReLU(inplace=True)):
        super(BasicConv2d, self).__init__()
        self.conv = nn.Conv2d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        self.act = act

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.act(x)
        return x
   

class SEModule(nn.Module):
    def __init__(
        self,
        channels,
        rd_ratio=1.0 / 16,
        rd_channels=None,
        rd_divisor=8,
        add_maxpool=False,
        bias=True,
        act=nn.GELU(),
        norm_layer=None,
        gate_layer=nn.Sigmoid,
    ):
        super(SEModule, self).__init__()
        self.add_maxpool = add_maxpool
        if not rd_channels:
            rd_channels = make_divisible(
                channels * rd_ratio, rd_divisor, round_limit=0.0
            )
        self.fc1 = nn.Conv2d(channels, rd_channels, kernel_size=1, bias=bias)
        self.bn = norm_layer(rd_channels) if norm_layer else nn.Identity()
        self.act = act
        self.fc2 = nn.Conv2d(rd_channels, channels, kernel_size=1, bias=bias)
        self.gate = gate_layer()

    def forward(self, x):
        x_se = x.mean((2, 3), keepdim=True)
        if self.add_maxpool:
            # experimental codepath, may remove or change
            x_se = 0.5 * x_se + 0.5 * x.amax((2, 3), keepdim=True)
        x_se = self.fc1(x_se)
        x_se = self.act(self.bn(x_se))
        x_se = self.fc2(x_se)
        return x * self.gate(x_se)



class GobleAttention(nn.Module):
    def __init__(self, in_dim=1, out_dim=32, kernel_size=3, mlp_ratio=4, act=nn.GELU()):
        super(GobleAttention, self).__init__()
        # 调整通道Conv
        self.conv = nn.Conv2d(in_dim, out_dim, 3, 1, 1)   
        # 特征Norm
        self.norm = nn.GroupNorm(out_dim // 2, out_dim)
        # 激活函数
        self.act = act
        # 多重计算Rep
        self.base_conv = nn.Conv2d(out_dim, out_dim, kernel_size, 1, (kernel_size - 1) // 2, 1, out_dim, bias=False)
        self.base_norm = nn.BatchNorm2d(out_dim)
        
        self.add_conv = nn.Conv2d(out_dim, out_dim, 1, 1, 0, 1, out_dim, bias=False)
        self.add_norm = nn.BatchNorm2d(out_dim)
        # # SE
        # self.se = SEModule(out_dim, 0.25, act=self.act) 
        # MLP
        self.mlp = MLP(out_dim, mlp_ratio, act=self.act)
    
    def forward(self, x):
        # 调整通道
        x = self.conv(x)
        # 特征Norm
        x = self.norm(x)
        # 特征激活
        x = self.act(x)
        # keep input
        identity = x
        # 多重计算
        x = self.base_norm(self.base_conv(x)) + self.add_norm(self.add_conv(x)) + x
        # # SE
        # x = self.se(x)
        x = self.mlp(x)
        return x + identity
    
    @torch.no_grad()
    def fuse(self):
        conv = self.conv.fuse()
        conv1 = self.conv1.fuse()

        conv_w = conv.weight
        conv_b = conv.bias
        conv1_w = conv1.weight
        conv1_b = conv1.bias

        conv1_w = nn.functional.pad(conv1_w, [1, 1, 1, 1])

        identity = nn.functional.pad(
            torch.ones(conv1_w.shape[0], conv1_w.shape[1], 1, 1, device=conv1_w.device),
            [1, 1, 1, 1],
        )

        final_conv_w = conv_w + conv1_w + identity
        final_conv_b = conv_b + conv1_b

        conv.weight.data.copy_(final_conv_w)
        conv.bias.data.copy_(final_conv_b)
        return conv


class LocalAttention(nn.Module):
    def __init__(self, in_dim=32, out_dim=32):
        super(LocalAttention, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_dim)
        self.pointwise_conv_0 = nn.Conv2d(in_dim, in_dim, kernel_size=1, bias=False)
        self.depthwise_conv = nn.Conv2d(in_dim, in_dim, padding=1, kernel_size=3, groups=in_dim, bias=False)
        self.bn2 = nn.BatchNorm2d(in_dim)
        self.pointwise_conv_1 = nn.Conv2d(in_dim, out_dim, kernel_size=1, bias=False)

    def forward(self, x):
        x = self.bn1(x)
        x = self.pointwise_conv_0(x)
        x = self.depthwise_conv(x)
        x = self.bn2(x)
        x = self.pointwise_conv_1(x)
        return x

class AttentionBlock(nn.Module):
    def __init__(self, in_dim=3, out_dim=32, kernel_size=3, mlp_ratio=4, shallow=True):
        super(AttentionBlock, self).__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        if shallow == True:
            self.act = nn.GELU()
        else:
            self.act = Swish()
        self.gobel_attention = GobleAttention(in_dim=in_dim//2, out_dim=out_dim, kernel_size=kernel_size, mlp_ratio=mlp_ratio, act=self.act)
        self.local_attention = LocalAttention(in_dim=in_dim//2, out_dim=out_dim)
        self.downsample = BasicConv2d(out_dim*2, out_dim, 1, act=self.act)

    def forward(self, x):
        x_0, x_1 = x.chunk(2,dim = 1)
        x_0 = self.gobel_attention(x_0)
        x_1 = self.local_attention(x_1)
        x = torch.cat([x_0, x_1], dim=1)
        x = self.downsample(x)
        return x

class GlobalSparseTransformer(nn.Module):
    def __init__(self, channels, r, heads):
        super(GlobalSparseTransformer, self).__init__()
        self.head_dim = channels // heads
        self.scale = self.head_dim ** -0.5
        self.num_heads = heads
        self.sparse_sampler = nn.AvgPool2d(kernel_size=1, stride=r)
        # qkv
        self.qkv = nn.Conv2d(channels, channels * 3, kernel_size=1, bias=False)

    def forward(self, x):
        x = self.sparse_sampler(x)
        B, C, H, W = x.shape
        q, k, v = self.qkv(x).view(B, self.num_heads, -1, H * W ).split(
            [self.head_dim, self.head_dim, self.head_dim],
            dim=2)
        attn = (q.transpose(-2, -1) @ k).softmax(-1)
        x = (v @ attn.transpose(-2, -1)).view(B, -1, H, W)
        return x

class LocalReverseDiffusion(nn.Module):
    def __init__(self, in_channels, out_channels, r):
        super(LocalReverseDiffusion, self).__init__()
        self.norm = nn.GroupNorm(num_groups=1, num_channels=in_channels)
        self.conv_trans = nn.ConvTranspose2d(in_channels,
                                             in_channels,
                                             kernel_size=r,
                                             stride=r,
                                             groups=in_channels)
        self.pointwise_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=False)

    def forward(self, x):
        x = self.conv_trans(x)
        x = self.norm(x)
        x = self.pointwise_conv(x)
        return x

class VerbClassifier(nn.Module):
    def __init__(self, in_channels, verb_channels=6):
        super(VerbClassifier, self).__init__()
        # 假设 out_channels 是图像编码 tensor 的通道数
        # 使用一个线性层将全局池化后的特征映射到分类置信度
        self.fc = nn.Linear(in_channels, verb_channels)

    def forward(self, x):
        # x 的形状是 (batch_size, out_channels, 32, 56)
        # 使用全局平均池化来降低空间维度
        x = F.adaptive_avg_pool2d(x, (1, 1))
        # 展平 tensor 以匹配线性层的输入
        x = x.view(x.size(0), -1)
        # 使用线性层生成分类置信度
        x = self.fc(x)
        return x

class CrossAttention(nn.Module):
    def __init__(self, feature_dim):
        super(CrossAttention, self).__init__()
        self.query_conv = nn.Conv2d(feature_dim, feature_dim // 8, kernel_size=1)
        self.key_conv = nn.Conv2d(feature_dim, feature_dim // 8, kernel_size=1)
        self.value_conv = nn.Conv2d(feature_dim, feature_dim, kernel_size=1)
        self.gamma = nn.Parameter(torch.zeros(1))

    def forward(self, x1, x2):
        # 计算query、key、value
        query = self.query_conv(x1)
        key = self.key_conv(x2)
        value = self.value_conv(x2)
        
        # 计算注意力分数
        attention = torch.matmul(query.permute(0, 2, 3, 1), key.permute(0, 2, 3, 1).transpose(-1, -2))
        attention = F.softmax(attention, dim=-1)
        
        # 应用注意力权重
        out = torch.matmul(attention, value.permute(0, 2, 3, 1))
        out = out.permute(0, 3, 1, 2)
        
        # 缩放和残差连接
        out = self.gamma * out + x1
        return out
    
class SiameseNetworkWithCrossAttention(nn.Module):
    def __init__(self, input_channels, output_dim1, output_dim2):
        super(SiameseNetworkWithCrossAttention, self).__init__()
        self.shared_conv = nn.Sequential(
            nn.Conv2d(input_channels, 64, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=2, stride=2),
            nn.Conv2d(64, 128, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=2, stride=2)
        )
        self.cross_attention = CrossAttention(128)
        self.fc1 = nn.Linear(128, output_dim1)
        self.fc2 = nn.Linear(128, output_dim2)

    def forward(self, img1, img2):
        shared_features1 = self.shared_conv(img1)
        shared_features2 = self.shared_conv(img2)
        
        # 应用交叉注意力
        attended_features1 = self.cross_attention(shared_features1, shared_features2)
        attended_features2 = self.cross_attention(shared_features2, shared_features1)
        
        # 全局平均池化
        pooled_features1 = F.adaptive_avg_pool2d(attended_features1, (1, 1))
        pooled_features2 = F.adaptive_avg_pool2d(attended_features2, (1, 1))
        
        # 展平并生成分类特征
        class1_features = self.fc1(pooled_features1.view(-1, 128 * 1 * 1))
        class2_features = self.fc2(pooled_features2.view(-1, 128 * 1 * 1))
        
        return class1_features, class2_features

class AttentionClassifier(nn.Module):
    def __init__(self, query_dim, key_dim, value_dim, num_heads=4, num_class=100, i_num=6, t_num=15, v_num=10):
        super(AttentionClassifier, self).__init__() 
        
        self.query_proj = nn.Linear(query_dim, key_dim)
        self.key_proj = nn.Linear(key_dim, key_dim)
        self.value_proj = nn.Linear(value_dim, key_dim)
        self.num_heads = num_heads
        self.head_dim = key_dim // num_heads

        assert self.head_dim * num_heads == key_dim, "key_dim must be divisible by num_heads"
        
        # self.instrument_shape_layer = nn.Linear(i_num * v_num, key_dim)
        # self.target_shape_layer = nn.Linear(t_num * v_num, key_dim)
        # self.verb_shape_layer = nn.Linear(v_num * i_num, key_dim)
        
        # self.gmp = nn.AdaptiveMaxPool2d((1,1)) 
        self.mlp = nn.Linear(in_features=key_dim, out_features=num_class)    
        
    def forward(self, inputs):
        instrument, target, verb = inputs
        i_num = instrument.size()[-1] # 6
        t_num = target.size()[-1] # 15
        v_num = verb.size()[-1] # 10
        
        query = instrument.repeat(1, 1, v_num).view(-1, i_num * v_num)
        # query = self.instrument_shape_layer(query)
        key = target.repeat(1, 1, v_num).view(-1, t_num * v_num)
        # key = self.target_shape_layer(key)
        value = verb.repeat(1, i_num, 1).view(-1, v_num * i_num)
        # value = self.verb_shape_layer(value)
        
        batch_size = query.size(0)

        # Project the query, key, and value matrices
        query = self.query_proj(query).view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        key = self.key_proj(key).view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        value = self.value_proj(value).view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)

        # Compute the attention scores
        scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(self.head_dim)
        
        attention = F.softmax(scores, dim=-1)

        # Aggregate the values
        out = torch.matmul(attention, value)
        out = out.transpose(1, 2).contiguous().view(batch_size, -1, self.num_heads * self.head_dim)
        
        out = self.mlp(out.squeeze(1))
        return out

   
class EndoForm(nn.Module):
    def __init__(self, in_channels=3, total_channels = 100, i_channels=6, t_channels=15, v_channels=10, num_heads=10, dims=[64, 128, 320, 512], out_dim=32, kernel_size=3, mlp_ratio=4, model_dir = '/workspace/Encs/src/CVCUNETR/pvt_v2_b2.pth'):
        super(EndoForm, self).__init__()
        self.backbone = pvt_v2_b2(in_chans=in_channels,embed_dims=dims)
        if os.path.isfile(model_dir):
            model_dir = '/root/.cache/huggingface/forget/pvt_v2_b3.pth'
            save_model = torch.load(model_dir)
            model_dict = self.backbone.state_dict()
            state_dict = {k: v for k, v in save_model.items() if k in model_dict.keys()}
            model_dict.update(state_dict)
            self.backbone.load_state_dict(model_dict)
        
        c1_in_channels, c2_in_channels, c3_in_channels, c4_in_channels = dims[0], dims[1], dims[2], dims[3]
        
        # self.block1 = AttentionBlock(in_dim=c1_in_channels, out_dim=out_dim, kernel_size=kernel_size, mlp_ratio=mlp_ratio, shallow=True)
        self.block2 = AttentionBlock(in_dim=c2_in_channels, out_dim=out_dim, kernel_size=kernel_size, mlp_ratio=mlp_ratio, shallow=True)
        self.block3 = AttentionBlock(in_dim=c3_in_channels, out_dim=out_dim, kernel_size=kernel_size, mlp_ratio=mlp_ratio, shallow=False)
        self.block4 = AttentionBlock(in_dim=c4_in_channels, out_dim=out_dim, kernel_size=kernel_size, mlp_ratio=mlp_ratio, shallow=False)

        self.fuse2 = nn.Sequential(BasicConv2d(out_dim*2, out_dim, 1,1),nn.Conv2d(out_dim, v_channels, kernel_size=1, bias=False))
        self.verb_classifier = VerbClassifier(v_channels,v_channels)
        
        
        self.L_feature = BasicConv2d(c1_in_channels, out_dim, 3,1,1)
        self.fuse = BasicConv2d(out_dim, out_dim, 1)
        
        self.two_classifier = SiameseNetworkWithCrossAttention(input_channels = out_dim, output_dim1 = i_channels, output_dim2 = t_channels)
        
        self.classifier = AttentionClassifier(query_dim= i_channels * v_channels, key_dim= t_channels * v_channels, value_dim= v_channels * i_channels, num_heads=num_heads, num_class=total_channels, i_num=i_channels, t_num=t_channels, v_num=v_channels)

        
        # self.SBA = SBA(input_dim = out_dim,out_channels=out_channels)
        # self.g = GlobalSparseTransformer(out_dim*2, r=4, heads=2)
        # self.l = LocalReverseDiffusion(in_channels=out_dim*2, out_channels=out_channels, r=4)
        
    def Upsample(self, x, size, align_corners = False):
        """
        Wrapper Around the Upsample Call
        """
        return nn.functional.interpolate(x, size=size, mode='bilinear', align_corners=align_corners)
        
    def forward(self, x):
        pvt = self.backbone(x)
        c1, c2, c3, c4 = pvt
        
        _c4 = self.block4(c4) # [1, 64, 11, 11]
        _c4 = self.Upsample(_c4, c3.size()[2:])
        # _c4 = F.interpolate(_c4, size=c3.size()[2:], mode='trilinear', align_corners=False)
        _c3 = self.block3(c3) # [1, 64, 22, 22]
        _c2 = self.block2(c2) # [1, 64, 44, 44]
        # verb
        output = self.fuse2(torch.cat([self.Upsample(_c4, c2.size()[2:]), self.Upsample(_c3, c2.size()[2:])], dim=1))
        
        verb_output = self.verb_classifier(output)
        
        L_feature = self.L_feature(c1)  # [1, 64, 88, 88]
        H_feature = self.fuse(_c2)
        H_feature = self.Upsample(H_feature,L_feature.size()[2:])

        instrument_output, target_output = self.two_classifier(L_feature, H_feature)
        
        triplet_output = self.classifier((instrument_output, target_output, verb_output))
        
        return instrument_output, verb_output, target_output, triplet_output



if __name__ == '__main__':
    device = 'cuda:0'
    
    x = torch.randn(size=(1, 3, 256, 448)).to(device)
    # test_x = torch.randn(size=(2, 64, 88, 88)).to(device)
    
    model = EndoForm(in_channels=3).to(device)
    # module = AttentionBlock(in_dim=64, out_dim=32, kernel_size=3, mlp_ratio=4, shallow=True).to(device)
    tool, verb, target, triplet = model(x)
    print(tool.size())
    print(verb.size())
    print(target.size())
    print(triplet.size())

    
    