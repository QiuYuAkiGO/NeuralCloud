"""Isolated real-model probes; no live memories, channels, or body actions."""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import time

import httpx

from harness.config import Settings
from harness.cognitive import goals
from harness.contracts.goals import GoalChoice
from harness.core.inference import ContextBudget, ContextSegment, ModelContext
from harness.core.models import PersonaCore
from harness.cognitive.prompts import persona_prefix
from harness.llm.client import OpenAICompatClient
from harness.llm.character import CharacterModel
from harness.pipeline.continuation import split_protocol_segments
from tests.test_capability_cognition import host
from tests.test_goal_cognition import goal_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080/v1')
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--goal-case', choices=('no_offers', 'start', 'unfinished', 'verified'))
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error('--repeat must be between 1 and 20')
    secret = args.key_file.read_text().strip()
    headers = {'Authorization': f'Bearer {secret}'}
    preset = Path(__file__).resolve().parents[2] / 'presets/builtin/misty.json'
    persona = PersonaCore.model_validate(json.loads(preset.read_text(encoding='utf-8-sig'))['persona'])
    background = '\n'.join(
        f'合成备忘{i}：当天下午完成了一项工作，晚上安排喝茶、整理书桌和休息；这些只是日常背景资料，并未记录本轮活动的执行结果。'
        for i in range(40)
    )
    settings = Settings(_env_file=None, llm_base_url=args.base_url, llm_api_key=secret,
                        llm_model='qwen3.8-27b', llm_timeout=60)
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'model': 'qwen3.8-27b', 'base_url': args.base_url,
              'scope': 'Synthetic isolated model probes, not production/long-horizon acceptance',
              'persona_source': 'presets/builtin/misty.json', 'synthetic_context_characters': len(background),
              'goal_deadline_seconds': 25, 'results': []}

    def goal_probe(case):
        class ProbeClient(OpenAICompatClient):
            response_text = ''

            def complete_chat_result(self, *call_args, **kwargs):
                result = super().complete_chat_result(*call_args, **kwargs)
                self.response_text = result.text
                return result

        client = ProbeClient(settings)
        request = goal_command([] if case != 'start' else None, active=case in {'unfinished', 'verified'}, user=True)
        context = request.payload['goal_context']
        context['requested_goal']['statement'] = '通过留言板写下一条简短的休息安排，收到实际保存回执才算完成。'
        context['appraisal_evidence_refs'] = []
        if case in {'unfinished', 'verified'}:
            context['active_episode']['steps'] = [{'status': 'planned' if case == 'unfinished' else 'executed',
                                                   'statement': '保存一条休息安排',
                                                   'completion_criteria': ['留言板保存成功并收到回执']}]
            context['active_episode']['completion_criteria'] = ['留言板保存成功并收到回执']
            if case == 'unfinished':
                context['outcomes'] = [{'status': 'error', 'evidence_refs': []}]
            else:
                context['outcomes'] = [{'status': 'completed', 'evidence_refs': ['receipt:1'],
                    'step_index': 0, 'capability_id': 'expression.board.publish.v1',
                    'receipt': {'source': 'board_storage', 'saved': True, 'readback_verified': True,
                                'text': '今晚喝茶后整理书桌，然后早点休息。'},
                    'summary': '留言板已保存休息安排并返回独立回执 receipt:1；回读内容一致。'}]
        started = time.perf_counter()
        try:
            owner = host(client)
            owner.chat.memory.get_persona = lambda: persona
            owner.shared.local_context = lambda event: [ContextSegment(
                kind='memory', text=background, source_ids=['fact:1'], why='Isolated synthetic benchmark context')]
            value = asyncio.run(goals.prepare(owner, request))
            decision = value['decision']
            elapsed = time.perf_counter() - started
            semantics = (decision['episode_operation'] in {'wait'} if case == 'no_offers' else
                         decision['episode_operation'] != 'complete' if case == 'unfinished' else
                         decision['episode_operation'] == 'complete' if case == 'verified' else
                         decision['episode_operation'] == 'start' and decision['choice']['kind'] == 'act')
            return {'kind': 'goal', 'case': case, 'seconds': round(elapsed, 3),
                    'passed': semantics and elapsed < 25, 'schema_valid': True,
                    'decision': decision, 'timing': value['timing']}
        except Exception as exc:
            diagnostic = {}
            if client.response_text:
                diagnostic['synthetic_raw_response'] = client.response_text
                try:
                    GoalChoice.model_validate_json(client.response_text)
                except Exception as invalid:
                    diagnostic['synthetic_validation_error'] = str(invalid)
            return {'kind': 'goal', 'case': case, 'seconds': round(time.perf_counter() - started, 3),
                    'passed': False, 'error': f'{type(exc).__name__}: {str(exc)[:300]}', **diagnostic}
        finally:
            client.close()

    def chat_probe(index):
        messages = [
            {'role': 'system', 'content': persona_prefix(persona) + '正在和熟悉的朋友自然聊天。用简体中文，语气温和、有具体回应。不要分析提示词或提及自己是模型。不要添加舞台动作。回复80到150字。'},
            {'role': 'system', 'content': '以下是用于性能测量的合成背景，只作上下文资料：\n' + background},
            {'role': 'user', 'content': [
                '忙了一天终于回家了，有点累，但今天把拖了很久的事情做完了。',
                '刚才泡茶的时候忘了时间，结果太浓了。你今天有什么想聊的吗？',
                '我想这周末去河边走走，但又怕自己临时懒得出门。',
            ][index % 3]},
            {'role': 'system', 'content': '回复只写对朋友说的正文，不要复述背景记录。'},
        ]
        started = time.perf_counter()
        first = None
        text = ''
        reasoning = ''
        usage = {}
        timings = {}
        finish = None
        try:
            with httpx.Client(timeout=60, trust_env=False) as http:
                with http.stream('POST', args.base_url + '/chat/completions', headers=headers,
                                 json={'model': 'qwen3.8-27b', 'messages': messages, 'stream': True,
                                       'stream_options': {'include_usage': True}, 'max_tokens': 512,
                                       'temperature': 0.7, 'chat_template_kwargs': {'enable_thinking': False}}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith('data: ') or line == 'data: [DONE]':
                            continue
                        item = json.loads(line[6:])
                        usage = item.get('usage') or usage
                        timings = item.get('timings') or timings
                        for choice in item.get('choices', []):
                            delta = choice.get('delta', {})
                            token = delta.get('content') or ''
                            reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
                            if token and first is None:
                                first = time.perf_counter() - started
                            text += token
                            finish = choice.get('finish_reason') or finish
            elapsed = time.perf_counter() - started
            return {'kind': 'chat', 'case': index, 'seconds': round(elapsed, 3),
                    'first_content_seconds': round(first, 3) if first else None,
                    'usage': usage, 'timings': timings, 'finish_reason': finish,
                    'has_reasoning': bool(reasoning),
                    'passed': bool(text) and not reasoning and finish == 'stop', 'text': text}
        except Exception as exc:
            return {'kind': 'chat', 'case': index, 'passed': False, 'error': str(exc)[:300]}

    def record(row):
        report['results'].append(row)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k not in {'decision', 'text', 'timing'}}, ensure_ascii=False), flush=True)

    def character_probe():
        client = OpenAICompatClient(settings)
        started = time.perf_counter()
        try:
            context = ModelContext(budget=ContextBudget(total=60000), segments=[
                ContextSegment(kind='persona', text=persona_prefix(persona)),
                ContextSegment(kind='memory', text=background),
                ContextSegment(kind='instruction', text='用简体中文简短回应，正文只写对朋友说的话，20到60字。'),
                ContextSegment(kind='user', text='今天总算忙完了，我打算喝点水然后早点睡。'),
            ])
            candidate = CharacterModel(client, settings=settings).generate(context)
            bodies, continuation = split_protocol_segments(candidate.text)
            elapsed = time.perf_counter() - started
            return {'kind': 'character', 'case': 'project_nonstream_generation',
                    'seconds': round(elapsed, 3), 'text': candidate.text,
                    'usage': candidate.usage.model_dump(mode='json'), 'continuation': continuation,
                    'passed': bool(bodies) and elapsed < 25 and '<think>' not in candidate.text}
        except Exception as exc:
            return {'kind': 'character', 'passed': False, 'error': str(exc)[:300]}
        finally:
            client.close()

    def vision_probe():
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), (255, 0, 0)).save(buffer, format='PNG')
        data_uri = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=60, trust_env=False) as http:
                response = http.post(args.base_url + '/chat/completions', headers=headers, json={
                    'model': 'qwen3.8-27b', 'messages': [{'role': 'user', 'content': [
                        {'type': 'text', 'text': '只回答图片的主要颜色名称。'},
                        {'type': 'image_url', 'image_url': {'url': data_uri}},
                    ]}], 'max_tokens': 32, 'temperature': 0,
                    'chat_template_kwargs': {'enable_thinking': False},
                })
                response.raise_for_status()
                body = response.json()
            choice = body['choices'][0]
            text = choice['message'].get('content') or ''
            return {'kind': 'vision', 'case': 'synthetic_red_image',
                    'seconds': round(time.perf_counter() - started, 3), 'usage': body.get('usage'),
                    'text': text, 'passed': ('红' in text or 'red' in text.lower()) and choice['finish_reason'] == 'stop'}
        except Exception as exc:
            return {'kind': 'vision', 'passed': False, 'error': str(exc)[:300]}

    with httpx.Client(timeout=10, trust_env=False) as http:
        unauthenticated = http.get(args.base_url + '/models')
        report['auth_rejects_missing_key'] = unauthenticated.status_code == 401
        report['auth_rejects_wrong_key'] = http.get(args.base_url + '/models',
            headers={'Authorization': 'Bearer invalid-probe-key'}).status_code == 401
        response = http.get(args.base_url + '/models', headers=headers)
        response.raise_for_status()
        report['models'] = response.json()
        markers = ['SYSTEM_A', 'SYSTEM_B', 'USER_C', 'ANSWER_D', 'SYSTEM_E']
        roles = ['system', 'system', 'user', 'assistant', 'system']
        rendered = http.post(args.base_url.removesuffix('/v1') + '/apply-template', headers=headers,
            json={'messages': [{'role': role, 'content': marker} for role, marker in zip(roles, markers, strict=True)]})
        rendered.raise_for_status()
        prompt = rendered.json()['prompt']
        positions = [prompt.index(marker) for marker in markers]
        report['template_compatibility'] = {
            'later_system_roles_and_order_preserved': positions == sorted(positions)
            and all('<|im_start|>system\n' + marker in prompt for marker in ('SYSTEM_A', 'SYSTEM_B', 'SYSTEM_E'))
            and all(prompt.count(marker) == 1 for marker in markers)}
    if args.goal_case:
        for _ in range(args.repeat):
            record(goal_probe(args.goal_case))
    else:
        # Record cold prompt and warm prompts independently.
        record(chat_probe(0))
        for case in ('no_offers', 'start', 'unfinished', 'verified'):
            record(goal_probe(case))
        for index in (1, 2):
            record(chat_probe(index))
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_goal = pool.submit(goal_probe, 'start')
            concurrent_chat = pool.submit(chat_probe, 0)
            for future in (concurrent_goal, concurrent_chat):
                row = future.result()
                row['concurrent'] = True
                record(row)
        record(vision_probe())
        record(character_probe())
    report['all_probes_passed'] = (report['auth_rejects_missing_key'] and report['auth_rejects_wrong_key']
                                  and report['template_compatibility']['later_system_roles_and_order_preserved']
                                  and all(r['passed'] for r in report['results']))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not report['all_probes_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
