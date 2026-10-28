"""Trainable encoder and gated readers; base model parameters stay frozen."""
from __future__ import annotations

from dataclasses import dataclass
import torch
from torch import nn
from torch.nn import functional as F

from .config import EncoderConfig
from .contracts import EncodingManifest, MemoryBundle
from .packing import pack


@dataclass
class EncodedMemory:
    states: torch.Tensor
    mask: torch.Tensor
    manifest: EncodingManifest


class ResamplerBlock(nn.Module):
    def __init__(self, dim, heads):
        super().__init__()
        self.norm_q, self.norm_kv = nn.LayerNorm(dim), nn.LayerNorm(dim)
        self.attention = nn.MultiheadAttention(dim, heads, batch_first=True)
        self.norm_ff = nn.LayerNorm(dim)
        self.ff = nn.Sequential(nn.Linear(dim, dim * 4), nn.GELU(), nn.Linear(dim * 4, dim))

    def forward(self, slots, tokens, mask):
        keys = self.norm_kv(tokens)
        update, _ = self.attention(self.norm_q(slots), keys, keys, key_padding_mask=~mask, need_weights=False)
        slots = slots + update
        return slots + self.ff(self.norm_ff(slots))


class MemoryEncoder(nn.Module):
    def __init__(self, bert, tokenizer, config: EncoderConfig):
        super().__init__()
        self.bert, self.tokenizer, self.config = bert, tokenizer, config
        if bert.config.hidden_size != config.encoder_hidden_size:
            raise ValueError("encoder width mismatch")
        bert.requires_grad_(False)
        for layer in bert.encoder.layer[config.encoder_frozen_layers:]:
            layer.requires_grad_(True)
        self.project = nn.Linear(config.encoder_hidden_size, config.memory_dim)
        self.queries = nn.Parameter(torch.randn(config.slots_per_chunk, config.memory_dim) * 0.02)
        self.resampler = nn.ModuleList([
            ResamplerBlock(config.memory_dim, config.heads) for _ in range(config.resampler_layers)])
        self.chunk_positions = nn.Embedding(config.max_chunks, config.memory_dim)

    def forward(self, input_ids, attention_mask):
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=input_ids.device.type == "cuda"):
            return self._encode_tokens(input_ids, attention_mask)

    def _encode_tokens(self, input_ids, attention_mask):
        tokens = self.bert(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        tokens = self.project(tokens)
        slots = self.queries.unsqueeze(0).expand(input_ids.shape[0], -1, -1)
        for block in self.resampler:
            slots = block(slots, tokens, attention_mask.bool())
        positions = torch.arange(slots.shape[0], device=slots.device)
        slots = slots + self.chunk_positions(positions).unsqueeze(1)
        return slots.flatten(0, 1).unsqueeze(0)

    def encode(self, bundle: MemoryBundle) -> EncodedMemory:
        packed = pack(bundle, self.tokenizer, self.config)
        device = self.queries.device
        if not packed.input_ids:
            states = self.queries.new_empty((1, 0, self.config.memory_dim))
        else:
            width = max(map(len, packed.input_ids))
            ids = torch.full((len(packed.input_ids), width), self.tokenizer.pad_token_id,
                             dtype=torch.long, device=device)
            mask = torch.zeros_like(ids, dtype=torch.bool)
            for index, row in enumerate(packed.input_ids):
                ids[index, :len(row)] = torch.tensor(row, device=device)
                mask[index, :len(row)] = True
            states = self(ids, mask)
        return EncodedMemory(states, torch.ones(states.shape[:2], dtype=torch.bool, device=device), packed.manifest)


class MemoryReader(nn.Module):
    def __init__(self, hidden_size, dim, heads):
        super().__init__()
        self.heads, self.head_dim = heads, dim // heads
        self.norm = nn.LayerNorm(hidden_size)
        self.q = nn.Linear(hidden_size, dim, bias=False)
        self.k, self.v = nn.Linear(dim, dim, bias=False), nn.Linear(dim, dim, bias=False)
        self.out = nn.Linear(dim, hidden_size, bias=False)
        self.gate = nn.Parameter(torch.zeros(()))

    def forward(self, hidden, memory, mask):
        if memory is None or memory.shape[1] == 0:
            return hidden
        if mask is None or mask.shape != memory.shape[:2] or not mask.any(dim=1).all():
            raise ValueError("nonempty memory requires a valid mask")
        def split(x):
            return x.view(x.shape[0], x.shape[1], self.heads, self.head_dim).transpose(1, 2)
        update = F.scaled_dot_product_attention(split(self.q(self.norm(hidden))),
            split(self.k(memory)), split(self.v(memory)), attn_mask=mask[:, None, None, :])
        update = update.transpose(1, 2).contiguous().flatten(2)
        return hidden + torch.tanh(self.gate) * self.out(update)


class MemoryDecoderLayer(nn.Module):
    """Explicit argument flow survives activation checkpoint recomputation.

    Do not use a mutable global, temporary forward hook, or ContextVar here:
    backward recomputes layers after the outer forward has returned.
    """
    def __init__(self, original, reader=None):
        super().__init__()
        self.original, self.reader = original, reader

    def forward(self, *args, memory_states=None, memory_mask=None, **kwargs):
        hidden = self.original(*args, **kwargs)
        return self.reader(hidden, memory_states, memory_mask) if self.reader is not None else hidden


class MemoryAugmentedModel(nn.Module):
    def __init__(self, backbone, encoder: MemoryEncoder, config: EncoderConfig):
        super().__init__()
        self.backbone, self.encoder, self.config = backbone, encoder, config
        layers = backbone.model.layers
        if len(layers) != config.backbone_layers or backbone.config.hidden_size != config.hidden_size:
            raise ValueError("backbone architecture mismatch")
        backbone.requires_grad_(False)
        for index, original in enumerate(layers):
            reader = MemoryReader(config.hidden_size, config.memory_dim, config.heads) if index + 1 in config.reader_layers else None
            if reader is not None:
                reference = next(original.parameters())
                reader.to(device=reference.device, dtype=encoder.queries.dtype)
            layers[index] = MemoryDecoderLayer(original, reader)

    def forward(self, input_ids, *, memory_states=None, memory_mask=None, **kwargs):
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=input_ids.device.type == "cuda"):
            return self.backbone(input_ids=input_ids, memory_states=memory_states, memory_mask=memory_mask, **kwargs)

    def trainable_parameters(self):
        return [p for p in self.parameters() if p.requires_grad]

    def trainable_state(self):
        return {name: p.detach().cpu().contiguous() for name, p in self.named_parameters() if p.requires_grad}
