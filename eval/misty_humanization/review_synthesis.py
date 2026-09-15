"""Validate Kimi's synthesis references and document editorial evidence corrections."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from prepare_judgments import atomic_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir
    job = root / 'global/jobs/900-synthesis.json'
    result_path = root / 'global/results/900-synthesis.json'
    result = json.loads(result_path.read_text(encoding='utf-8'))
    response = result['response']
    assert result['status'] == 200 and response['model'] == 'kimi-k3'
    assert result['request_sha256'] == hashlib.sha256(job.read_bytes()).hexdigest()
    assert response['choices'][0]['finish_reason'] == 'stop'
    synthesis = json.loads(response['choices'][0]['message']['content'])
    inputs = {item['evaluation']['id']: item['evaluation'] for item in json.loads((root / 'evaluation_inputs.json').read_text(encoding='utf-8'))}
    primary = {key for key, item in inputs.items() if item['delivery_status'] in ('confirmed_delivered', 'confirmed_partial') and item['channel'].startswith('qq_')}
    references = []
    for pattern in synthesis['patterns']:
        assert len(set(pattern['evidence_ids'])) >= 2
        references.extend(pattern['evidence_ids'] + pattern['counterexample_ids'])
    references.extend(item['id'] for item in synthesis['positive_examples'] + synthesis['rewrite_examples'])
    assert set(references) <= primary, 'Qualitative primary evidence must be confirmed QQ replies'
    edits = []
    for item in synthesis['rewrite_examples']:
        if item['id'] == 'R0014':
            item['kimi_original_revision'] = item['revision']
            item['revision'] = '目前我对你的印象是，你很投入，也很在意我能不能真正接上群聊。你反复测试我能不能收到消息，也问过巴别塔和书面语、口语这些，感觉不是随便逗一逗。只是我们相处还短，我不想把更深的判断说得太满。'
            item['editorial_note'] = 'Codex已删除“花一个月设计架构”：这条回复当时的可见上下文没有该事实，不能借用后续历史改写过去。'
            edits.append({'id': item['id'], 'change': item['editorial_note']})
        if item['id'] == 'R0018':
            item['kimi_original_revision'] = item['revision']
            item['revision'] = item['revision'].replace('再遇到我不了解的词，我可以先核对事实', '等功能接好，再遇到我不了解的词，我就可以先核对事实')
            item['editorial_note'] = 'Codex明确功能尚待接入，避免建议稿被理解为当时已经能联网。'
            edits.append({'id': item['id'], 'change': item['editorial_note']})
    synthesis['editorial_notes'] = edits
    synthesis['recommendation_review_notes'] = [
        'Kimi的P0/P1/P2是研究建议排序，不是已证实事故等级；记忆提示冲突应先固定真实上下文回放再修改。',
        '不能因任意上下文非空就开放全部历史断言；记忆可用性与引用校验须绑定具体、作用域正确的证据。',
        '候选重排会增加模型调用和延迟；先试单候选答复规划，量化收益和成本后再考虑多候选。',
        '90%回答前置、开头最多3次等是待校准的实验阈值；不应机械作为线上禁令。底层接口不支持seed时用独立重复请求并记录参数。',
        '9月11日与9月14日话题和提问方式不同，版本分层是观察性对比，不能据此认定prompt升级造成改善。',
    ]
    atomic_json(root / 'synthesis_reviewed.json', synthesis)
    atomic_json(root / 'synthesis_validation.json', {
        'status': 'passed', 'model': response['model'], 'response_id': response.get('id'),
        'request_id': result.get('request_id'), 'job_sha256': hashlib.sha256(job.read_bytes()).hexdigest(),
        'usage': response.get('usage', {}), 'attempt': result.get('attempt', 1),
        'primary_evidence_references': len(references), 'all_references_confirmed_qq': True,
        'editorial_changes': edits,
        'auxiliary_context_version_note': 'Global review ran before the R0134 trigger-context repair. R0134 is unconfirmed supplemental QQ and appears in none of the synthesis evidence, positives, or rewrites. Its per-row score uses the corrected 008-ratings judgment.',
        'unmodified_raw_response': str(result_path.resolve()),
    })
    print(json.dumps({'validated_primary_references': len(references), 'patterns': len(synthesis['patterns']), 'editorial_changes': len(edits)}))


if __name__ == '__main__':
    main()
