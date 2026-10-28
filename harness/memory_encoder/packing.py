"""Whole-record admission, exact token budgets and auditable chunk manifests."""
from __future__ import annotations

from dataclasses import dataclass
from .config import EncoderConfig
from .contracts import EncodingManifest, ManifestChunk, MemoryBundle, canonical, digest


@dataclass
class PackedMemory:
    input_ids: list[list[int]]
    manifest: EncodingManifest


def pack(bundle: MemoryBundle, tokenizer, config: EncoderConfig) -> PackedMemory:
    ids, chunks, omitted = [], [], []
    for record in bundle.records:
        if not record.valid_at(bundle.read_at):
            omitted.append({"record_id": record.record_id, "reason": "outside_validity"})
            continue
        subject = ",".join(record.subject_ids) or record.subject_mention or "未知主体"
        prefix = f"主体:{subject};类型:{record.kind};"
        serialized = canonical(record.model_dump(mode="json"))
        prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
        available = config.chunk_tokens - len(prefix_ids) - 3
        if available < 8:
            omitted.append({"record_id": record.record_id, "reason": "metadata_budget"})
            continue
        tokenized = tokenizer(serialized, add_special_tokens=False, return_offsets_mapping=True)
        body_ids, offsets = tokenized["input_ids"], tokenized["offset_mapping"]
        needed = (len(body_ids) + available - 1) // available
        if not needed or len(ids) + needed > config.max_chunks:
            omitted.append({"record_id": record.record_id, "reason": "whole_record_budget", "required_chunks": needed})
            continue
        for start in range(0, len(body_ids), available):
            stop = min(start + available, len(body_ids))
            row = [tokenizer.cls_token_id, *prefix_ids, tokenizer.sep_token_id,
                   *body_ids[start:stop], tokenizer.sep_token_id]
            index = len(ids)
            char_start, char_end = offsets[start][0], offsets[stop - 1][1]
            chunks.append(ManifestChunk(record_id=record.record_id, revision=record.revision,
                chunk_index=index, text=serialized[char_start:char_end], prefix=prefix,
                start=char_start, end=char_end, token_ids_hash=digest(row), token_count=len(row),
                slot_start=index * config.slots_per_chunk, slot_end=(index + 1) * config.slots_per_chunk))
            ids.append(row)
    return PackedMemory(ids, EncodingManifest(provider=bundle.provider, audience=bundle.audience,
        bundle_hash=digest(bundle.model_dump(mode="json")), chunks=tuple(chunks), omitted=tuple(omitted)))
