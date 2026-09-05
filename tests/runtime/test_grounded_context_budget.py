"""Research tasks and operation menus reach the decision boundary intact."""

import asyncio

import pytest

from tests.runtime.test_grounded_runtime_wiring import drain, research_runtime


@pytest.mark.parametrize("too_large", [False, True])
def test_research_task_is_complete_or_rejected_before_model_call(tmp_path, too_large):
    runtime, model, provider, _ = research_runtime(tmp_path, ['{"kind":"no_action","confidence":0.8}'])
    instruction = "Inspect item. " * (5000 if too_large else 120) + "FINAL_OPERATION_SENTINEL"

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await runtime.submit_user_message(instruction)
            result = (await drain(runtime))[0]
            if too_large:
                assert result.status in {"failed", "deferred"}
                assert "context token budget" in str(result.detail)
                assert model.messages == []
            else:
                assert result.status == "completed", result.detail
                assert any(instruction in message["content"] for message in model.messages[0])
            assert provider.executed == []
        finally:
            await runtime.stop()

    asyncio.run(scenario())
