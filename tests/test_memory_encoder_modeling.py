"""Real small BERT/Qwen hybrid graphs, not mocks of gradient flow."""
# ruff: noqa: E402
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

from transformers import BertConfig, BertModel, Qwen3_5ForCausalLM, Qwen3_5TextConfig
from harness.memory_encoder.checkpoints import load_checkpoint, save_checkpoint
from harness.memory_encoder.config import EncoderConfig
from harness.memory_encoder.modeling import MemoryAugmentedModel, MemoryEncoder
from tests.test_memory_encoder_contracts import CharacterTokenizer, bundle


@pytest.fixture
def model():
    torch.set_num_threads(1)
    torch.manual_seed(7)
    config = EncoderConfig(hidden_size=32, backbone_layers=4, encoder_hidden_size=16,
        encoder_frozen_layers=1, reader_layers=(2, 4), memory_dim=16, heads=2,
        slots_per_chunk=2, resampler_layers=2)
    bert = BertModel(BertConfig(vocab_size=128, hidden_size=16, num_hidden_layers=2,
        num_attention_heads=2, intermediate_size=32), add_pooling_layer=False)
    qwen = Qwen3_5ForCausalLM(Qwen3_5TextConfig(vocab_size=128, hidden_size=32,
        num_hidden_layers=4, num_attention_heads=4, num_key_value_heads=2, head_dim=8,
        intermediate_size=64, linear_num_key_heads=2, linear_num_value_heads=2,
        linear_key_head_dim=8, linear_value_head_dim=8, max_position_embeddings=256,
        rope_parameters={"rope_type": "default", "rope_theta": 10000.0,
                         "partial_rotary_factor": 1.0, "mrope_section": [1, 1, 2]},
        pad_token_id=0, eos_token_id=2))
    return MemoryAugmentedModel(qwen, MemoryEncoder(bert, CharacterTokenizer(), config), config)


def test_zero_gate_preserves_base_and_checkpointed_gradients_reach_encoder(model):
    model.eval()
    ids = torch.tensor([[3, 4, 5, 6, 7, 8]])
    encoded = model.encoder.encode(bundle())
    with torch.no_grad():
        original = model(ids, use_cache=False).logits
        with_memory = model(ids, memory_states=encoded.states, memory_mask=encoded.mask, use_cache=False).logits
    torch.testing.assert_close(original, with_memory, rtol=0, atol=0)
    model.backbone.gradient_checkpointing_enable({"use_reentrant": False})
    model.train()
    optimizer = torch.optim.AdamW(model.trainable_parameters(), lr=0.01)
    for _ in range(2):  # zero gate first learns to open; then encoder gets gradients
        optimizer.zero_grad()
        encoded = model.encoder.encode(bundle())
        output = model(ids, memory_states=encoded.states, memory_mask=encoded.mask, use_cache=False).logits
        loss = torch.nn.functional.cross_entropy(output[0], torch.tensor([4, 5, 6, 7, 8, 9]))
        loss.backward()
        assert torch.isfinite(loss)
        optimizer.step()
    assert any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.encoder.parameters() if p.requires_grad)
    assert all(p.grad is None for p in model.parameters() if not p.requires_grad)


def test_hybrid_cache_matches_full_sequence_and_memory_is_request_local(model):
    model.eval()
    for layer in model.backbone.model.layers:
        if layer.reader is not None:
            layer.reader.gate.data.fill_(0.2)
    memory = model.encoder.encode(bundle())
    ids = torch.tensor([[3, 4, 5, 6, 7, 8]])
    with torch.no_grad():
        full = model(ids, memory_states=memory.states, memory_mask=memory.mask, use_cache=False).logits
        prefix = model(ids[:, :-1], memory_states=memory.states, memory_mask=memory.mask, use_cache=True)
        last = model(ids[:, -1:], memory_states=memory.states, memory_mask=memory.mask,
                     past_key_values=prefix.past_key_values, use_cache=True).logits
        torch.testing.assert_close(full[:, -1], last[:, -1], rtol=1e-3, atol=1e-4)
        no_memory = model(ids, use_cache=False).logits
        again = model(ids, use_cache=False).logits
        torch.testing.assert_close(no_memory, again, rtol=0, atol=0)


def test_checkpoint_roundtrip_and_corruption_detection(model, tmp_path):
    optimizer = torch.optim.AdamW(model.trainable_parameters())
    sum(p.square().mean() for p in model.trainable_parameters()).backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    save_checkpoint(model, tmp_path, step=2, status="profile_only", optimizer=optimizer)
    expected_random = torch.rand(4)
    before = {name: p.clone() for name, p in model.named_parameters() if p.requires_grad}
    with torch.no_grad():
        for p in model.trainable_parameters():
            p.add_(1)
    result = load_checkpoint(model, tmp_path, optimizer=optimizer)
    assert result["training_status"] == "profile_only"
    torch.testing.assert_close(torch.rand(4), expected_random)
    assert all(state["step"].item() == 1 for state in optimizer.state.values())
    for name, p in model.named_parameters():
        if p.requires_grad:
            torch.testing.assert_close(p, before[name])
    with (tmp_path / "adapter.safetensors").open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        load_checkpoint(model, tmp_path)


def test_text_lora_keeps_encoder_and_native_readers_frozen(model):
    pytest.importorskip("peft")
    from harness.memory_encoder.experiments import text_lora
    text_lora(model)
    assert all("lora_" in name for name, p in model.named_parameters() if p.requires_grad)
    loss = model(torch.tensor([[3, 4, 5, 6]]), use_cache=False).logits.float().square().mean()
    loss.backward()
    assert any(p.grad is not None and p.grad.abs().sum() > 0 for p in model.trainable_parameters())


def test_answer_only_loss_and_template_boundary(model):
    from harness.memory_encoder.data import example
    from harness.memory_encoder.runtime import answer_loss
    class Tokenizer:
        bad = False
        def apply_chat_template(self, messages, *, add_generation_prompt, return_dict, **kwargs):
            assert return_dict is False
            prompt = [3, 4, 5, 6]
            if add_generation_prompt:
                return prompt
            return ([9] if self.bad else prompt) + [7, 8, 2]
    tokenizer = Tokenizer()
    loss = answer_loss(model, tokenizer, example("train", 0))
    assert torch.isfinite(loss)
    tokenizer.bad = True
    with pytest.raises(ValueError, match="answer boundary"):
        answer_loss(model, tokenizer, example("train", 0))
