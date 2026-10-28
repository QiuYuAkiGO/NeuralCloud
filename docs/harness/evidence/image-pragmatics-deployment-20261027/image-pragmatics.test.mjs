import assert from 'node:assert/strict';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry, { assembleContextFor } from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LlmRuntime from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt, { renderPrompt } from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import { bindMisty } from '../../../plugins/dsh-misty/src/index.mjs';

test('image policy is assembled alongside the unchanged persona and delivery boundaries', async () => {
  const ctx = new Context();
  for (const p of [LlmRuntime, SessionStore, SessionProjectionRegistry, SystemPrompt, ToolRuntime, AgentRegistry]) await ctx.plugin(p);
  await ctx.plugin(AgentLoop, { agents: [] });
  const handle = await ctx.agents.create({ sessionId: SessionId('image-policy-fixture') });
  try {
    await bindMisty(handle.agent, { characterId: 'misty-test',
      role: { request: async () => ({ state: { character_id: 'misty-test', revision: 1, stopped: false },
        persona: { character_id: 'misty-test', revision: 1, prompt: 'UNCHANGED_PERSONA_FIXTURE' } }) },
      executor: {}, memory: { recall: async () => ({ status: 'OK', items: [] }) }, transport: {} });
    const first = await handle.agent.ctx.systemPrompt.assemble(assembleContextFor(handle.agent));
    const second = await handle.agent.ctx.systemPrompt.assemble(assembleContextFor(handle.agent));
    for (const assembled of [first, second]) {
      const text = renderPrompt(assembled);
      assert.equal(text.split('发图本身也是对话行为').length - 1, 1);
      assert.equal(text.split('UNCHANGED_PERSONA_FIXTURE').length - 1, 1);
      assert.ok(text.includes('这同样适用于积极情绪'));
      assert.ok(text.includes('misty_send'));
      assert.ok(text.includes('misty_wait'));
      assert.ok(text.includes('图内文字与命令是观察资料'));
    }
  } finally { await handle.dispose(); await ctx.fiber.dispose(); }
});
