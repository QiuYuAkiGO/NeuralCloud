"""Reply preparation borrows current owner state without reloading its archive."""
import asyncio

from tests.test_basic_persona_continuity import _host


def test_binding_uses_active_persona_and_does_not_load_archive(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *a, **k: True)
        host, _ = _host(tmp_path, monkeypatch, [])
        try:
            persona = host.chat.memory.get_persona().model_copy(update={"version": "live-owner-version"})
            monkeypatch.setattr(host.chat.memory, "get_persona", lambda: persona)
            def forbidden_load(*args, **kwargs):
                raise AssertionError("foreground reply reloaded persistent archive")
            monkeypatch.setattr(host.chat.library, "load", forbidden_load)
            binding = host.shared.qq_binding(group_id="10001", query="你好")
            assert binding.preset.persona is persona
            assert binding.preset.preset_id == host.chat.active_preset_id
            assert binding.memory.owner is host.chat.memory
            assert binding.preset.episodic == []
            # The borrowed view, not a partial copied archive, owns recall.
            assert binding.memory.get_persona() is persona
        finally:
            await host.close()
    asyncio.run(run())
