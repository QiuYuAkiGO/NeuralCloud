"""Queue an independent all-history Kimi K3 pattern review, separate from per-row scores."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from prepare_judgments import SYSTEM, atomic_json, enqueue


def obj(props: dict) -> dict:
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


def arr(item: dict) -> dict:
    return {'type': 'array', 'items': item}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir
    work = root / 'global'
    (work / 'jobs').mkdir(parents=True, exist_ok=True)
    inputs = json.loads((root / 'evaluation_inputs.json').read_text(encoding='utf-8'))
    rows = []
    for item in inputs:
        data = item['evaluation']
        rows.append({k: data.get(k) for k in ('id', 'timestamp', 'channel', 'delivery_status', 'user', 'reply', 'context_limitations')}
                    | {'prompt_version': item['source_record'].get('prompt_version'),
                       'recent_context': data.get('context', [])[-6:]})
    primary_ids = [row['id'] for row in rows if row['channel'].startswith('qq_')
                   and row['delivery_status'] in ('confirmed_delivered', 'confirmed_partial')]
    text_type = {'type': 'string'}
    ids_type = arr(text_type)
    schema = obj({
        'overall_assessment': text_type,
        'patterns': arr(obj({'name': text_type, 'description': text_type,
                            'severity': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                            'evidence_ids': ids_type, 'counterexample_ids': ids_type,
                            'mechanism_hypothesis': text_type})),
        'positive_examples': arr(obj({'id': text_type, 'why': text_type})),
        'rewrite_examples': arr(obj({'id': text_type, 'issue': text_type, 'revision': text_type, 'why': text_type})),
        'recommendations': arr(obj({'priority': text_type, 'title': text_type, 'change': text_type, 'acceptance_test': text_type})),
        'limitations': arr(text_type),
    })
    user = f'''请对以下全部{len(rows)}条可归属Misty的历史文本做第二阶段跨历史会话自然度审计。第一阶段另行盲评逐条分数，此处不需打分。
主结论只针对有交付证据的QQ决策：{json.dumps(primary_ids)}，共{len(primary_ids)}轮。其他generated_unconfirmed是生成但交付未确认；web_chat都是真实运维验证，不是日常聊天；board是独白。不能混入主要重复率分母。
重要：语料非随机、同线程相关、代表历史不同prompt版本，不能当当前版本固定表现。只说明可取回留存历史，不保证删除/漏采样没有缺失。日期UTC，查询截止北京时间2026-09-15 14:20:55。
需要：
1. 给出不预设结论的总体判断。区分字面重复、句法套路、对话策略重复；高频省略号、第一人称或礼貌语本身不是违规。
2. 找4-7个最有证据的跨回复模式，每个至少2个主QQ id，描述影响并给存在时的反例。不要编造统计百分比。生成未确认只能作辅助观察且标明。
3. 选3-5个自然的主QQ正例解释为什么好；选6个主QQ代表问题给改写，保留Misty温柔克制但可有明确观点，不虚构经历/现实动作，尽量不套新模板。改写只是建议，不是历史原文。
4. 给工程改进优先级与可重复验收方法，避免只增加更多负面prompt规则。下面源码审计只说明当前工作区可验证机制和假设，不证明历史因果或当前部署相同。
5. 明说单一模型裁判偏差、上下文局部缺失与样本范围等限制，事实疑点不要写成已证实撒谎。

历史文本：
{json.dumps(rows, ensure_ascii=False)}

源码审计（独立资料，不是历史成因定论）：
{(root / 'source_audit.md').read_text(encoding='utf-8')}
'''
    enqueue(work, '900-synthesis', user, schema, SYSTEM + '\n本请求属于跨历史模式审计，应按新的结构schema回答，而不是输出逐条ratings。')
    atomic_json(work / 'scope.json', {'input_count': len(rows), 'primary_count': len(primary_ids), 'primary_ids': primary_ids})
    (work / 'STOP').write_text('Stop after queued synthesis completes.', encoding='utf-8')
    print(json.dumps({'records': len(rows), 'primary': len(primary_ids), 'user_characters': len(user)}))


if __name__ == '__main__':
    main()
