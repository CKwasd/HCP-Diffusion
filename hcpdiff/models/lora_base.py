"""
lora.py
====================
    :Name:        lora tools
    :Author:      Dong Ziyi
    :Affiliation: HCP Lab, SYSU
    :Created:     10/03/2023
    :Licence:     Apache-2.0
"""

import torch
from torch import nn

from hcpdiff.utils.utils import make_mask, low_rank_approximate
from .plugin import SinglePluginBlock, PluginGroup, BasePluginBlock

from typing import Union, Tuple, Dict, Type

class LoraBlock(SinglePluginBlock):
    def __init__(self, host:Union[nn.Linear, nn.Conv2d], rank, dropout=0.1, scale=1.0, bias=False, inplace=True,
                 hook_param=None, **kwargs):
        super().__init__(host, hook_param)
        if hasattr(host, 'lora_block'):
            self.id = len(host.lora_block)
            host.lora_block.append(self)
        else:
            self.id = 0
            host.lora_block=nn.ModuleList([self])

        self.mask_range = None
        self.inplace=inplace
        self.bias=bias

        if isinstance(host, nn.Linear):
            self.host_type = 'linear'
            self.layer = self.LinearLayer(host, rank, bias, dropout, self)
        elif isinstance(host, nn.Conv2d):
            self.host_type = 'conv'
            self.layer = self.Conv2dLayer(host, rank, bias, dropout, self)
        else:
            raise NotImplementedError(f'No lora for {type(host)}')
        self.rank = self.layer.rank

        self.register_buffer('scale', torch.tensor(1.0 if scale == 0 else scale / self.rank))

    def set_mask(self, mask_range):
        self.mask_range = mask_range

    def init_weights(self, svd_init=False):
        host = self.host()
        if svd_init:
            U, V = low_rank_approximate(host.weight, self.rank)
            self.feed_svd(U, V, host.weight)
        else:
            self.layer.lora_down.reset_parameters()
            nn.init.zeros_(self.layer.lora_up.weight)

    def forward(self, fea_in:Tuple[torch.Tensor], fea_out:torch.Tensor):
        if self.mask_range is None:
            return fea_out + self.layer(fea_in[0]) * self.scale
        else:
            # for DreamArtist-lora
            batch_mask = make_mask(self.mask_range[0], self.mask_range[1], fea_out.shape[0])
            if self.inplace:
                fea_out[batch_mask, ...] = fea_out[batch_mask, ...] + self.layer(fea_in[0][batch_mask, ...]) * self.scale
                return fea_out
            else: # colossal-AI dose not support inplace+view
                new_out = fea_out.clone()
                new_out[batch_mask, ...] = fea_out[batch_mask, ...] + self.layer(fea_in[0][batch_mask, ...]) * self.scale
                return new_out

    def remove(self):
        super().remove()
        host = self.host()
        for i in range(len(host.lora_block)):
            if host.lora_block[i] == self:
                del host.lora_block[i]
                break
        if len(host.lora_block)==0:
            del host.lora_block

    def collapse_to_host(self, alpha=None, base_alpha=1.0):
        if alpha is None:
            alpha = self.scale

        host = self.host()
        re_w, re_b = self.get_collapsed_param()
        host.weight = nn.Parameter(
            host.weight.data * base_alpha + alpha * re_w.to(host.weight.device, dtype=host.weight.dtype)
        )

        if self.lora_up.bias is not None:
            if host.bias is None:
                host.bias = nn.Parameter(re_b.to(host.weight.device, dtype=host.weight.dtype))
            else:
                host.bias = nn.Parameter(
                    host.bias.data * base_alpha + alpha * re_b.to(host.weight.device, dtype=host.weight.dtype))

    class LinearLayer(nn.Module):
        def __init__(self, host, rank, bias, dropout, block):
            super().__init__()
            self.rank=rank
            if isinstance(self.rank, float):
                self.rank = max(round(host.out_features * self.rank), 1)
            self.dropout = nn.Dropout(dropout)

        def feed_svd(self, U, V, weight):
            self.lora_up.weight.data = U.to(device=weight.device, dtype=weight.dtype)
            self.lora_down.weight.data = V.to(device=weight.device, dtype=weight.dtype)

        def forward(self, x):
            return self.dropout(self.lora_up(self.lora_down(x)))

        def get_collapsed_param(self) -> Tuple[torch.Tensor, torch.Tensor]:
            pass

    class Conv2dLayer(nn.Module):
        def __init__(self, host, rank, bias, dropout, block):
            super().__init__()
            self.rank = rank
            if isinstance(self.rank, float):
                self.rank = max(round(host.out_channels * self.rank), 1)
            self.dropout = nn.Dropout(dropout)

        def feed_svd(self, U, V, weight):
            self.lora_up.weight.data = U.to(device=weight.device, dtype=weight.dtype)
            self.lora_down.weight.data = V.to(device=weight.device, dtype=weight.dtype)

        def forward(self, x):
            return self.dropout(self.lora_up(self.lora_down(x)))

        def get_collapsed_param(self) -> Tuple[torch.Tensor, torch.Tensor]:
            pass

    @classmethod
    def warp_layer(cls, layer: Union[nn.Linear, nn.Conv2d], rank=1, dropout=0.1, scale=1.0, svd_init=False, bias=False, mask=None, **kwargs):# -> LoraBlock:
        lora_block = cls(layer, rank, dropout, scale, bias=bias, **kwargs)
        lora_block.init_weights(svd_init)
        lora_block.set_mask(mask)
        return lora_block

    @classmethod
    def warp_model(cls, model: nn.Module, rank=1, dropout=0.0, scale=1.0, svd_init=False, bias=False, mask=None, **kwargs):# -> Dict[str, LoraBlock]:
        lora_block_dict = {}
        if isinstance(model, nn.Linear) or isinstance(model, nn.Conv2d):
            lora_block_dict['lora_block'] = cls.warp_layer(model, rank, dropout, scale, svd_init, bias=bias, mask=mask, **kwargs)
        else:
            # there maybe multiple lora block, avoid insert lora into lora_block
            named_modules = {name: layer for name, layer in model.named_modules() if 'lora_block' not in name}
            for name, layer in named_modules.items():
                if isinstance(layer, nn.Linear) or isinstance(layer, nn.Conv2d):
                    lora_block_dict[f'{name}.lora_block'] = cls.warp_layer(layer, rank, dropout, scale, svd_init,
                                                                bias=bias, mask=mask, **kwargs)
        return lora_block_dict

    @staticmethod
    def extract_lora_state(model:nn.Module):
        return {k:v for k,v in model.state_dict().items() if 'lora_block.' in k}

    @staticmethod
    def extract_state_without_lora(model:nn.Module):
        return {k:v for k,v in model.state_dict().items() if 'lora_block.' not in k}

    @staticmethod
    def extract_param_without_lora(model:nn.Module):
        return {k:v for k,v in model.named_parameters() if 'lora_block.' not in k}

    @staticmethod
    def extract_trainable_state_without_lora(model:nn.Module):
        trainable_keys = {k for k,v in model.named_parameters() if ('lora_block.' not in k) and v.requires_grad}
        return {k: v for k, v in model.state_dict().items() if k in trainable_keys}

class LoraGroup(PluginGroup):
    def set_mask(self, batch_mask):
        for item in self.plugin_dict.values():
            item.set_mask(batch_mask)

    def collapse_to_host(self, alpha=None, base_alpha=1.0):
        for item in self.plugin_dict.values():
            item.collapse_to_host(alpha, base_alpha)

    def set_inplace(self, inplace):
        for item in self.plugin_dict.values():
            item.inplace = inplace

def split_state(state_dict):
    sd_base, sd_lora={}, {}
    for k, v in state_dict.items():
        if 'lora_block.' in k:
            sd_lora[k]=v
        else:
            sd_base[k]=v
    return sd_base, sd_lora