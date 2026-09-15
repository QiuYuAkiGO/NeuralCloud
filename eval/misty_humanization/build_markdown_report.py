"""Build the evidence-backed Chinese report from validated Kimi outputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


LABELS = {
    'context_specificity': '情境具体性', 'conversational_naturalness': '口语自然度',
    'non_template': '表达非模板性', 'emotional_calibration': '情绪适配',
    'continuity_and_persona': '连续性与人格',
}


def load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def link(label: str, path: Path) -> str:
    return f'[{label}]({path.resolve().as_posix()})'


def quote(text: str) -> str:
    return '\n'.join('> ' + line for line in text.splitlines())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, default=Path('data/evaluations/misty-humanization-20260915'))
    parser.add_argument('--output', type=Path, default=Path('eval/reports/misty-humanization-20260915.md'))
    args = parser.parse_args()
    root = args.run_dir
    inventory = load(root / 'inventory.json')
    stats = load(root / 'statistics.json')
    judgments = load(root / 'judgments.json')
    synthesis = load(root / 'synthesis_reviewed.json')
    verification = load(root / 'independent_verification.json')
    by_id = {row['id']: row for row in judgments}
    main_stats = stats['strata']['confirmed_qq']
    n = main_stats['n']
    ellipsis = main_stats['literal_indicators']['contains_ellipsis']
    hesitant = main_stats['literal_indicators']['starts_hesitation']
    lines = [
        '# Misty 历史回复拟人化与模板化评估报告', '',
        '评审模型：**Kimi K3（官方 API 实际返回 `kimi-k3`）**。数据查询截止：**2026-09-15 14:20:55，北京时间**。', '',
        'Kimi负责逐条评分与跨历史模式分析；程序负责去重、分层和统计，Codex负责来源核验、源码审计及建议稿的事实校正。这里的“拟人化”指自然、具体、连贯且符合Misty设定的表达。', '',
        '## 1. 结论', '',
        f'**单条回复总体合适，跨历史表达存在明显同质化。** 在{n}轮有交付证据的QQ回复中，Kimi给出的口语自然度为 **{main_stats["scores"]["conversational_naturalness"]["mean"]:.2f}/5**，非模板性为 **{main_stats["scores"]["non_template"]["mean"]:.2f}/5**，自定义五维综合分为 **{main_stats["complete_case_composite_100"]:.2f}/100**。', '',
        f'最清楚的改进目标是开场和回应策略：**{ellipsis}/{n}（{ellipsis/n:.1%}）出现省略号，{hesitant}/{n}（{hesitant/n:.1%}）以犹豫式措辞开头**。Kimi的跨历史复审还识别了反复的情感回礼、边界声明先行、条件化结论和刷屏下的重复防守。', '',
        '这些频率是程序统计的风格特征，并不意味着每次出现都不自然。单条评审未给任何主样本的自然度或非模板性打到2分及以下，也没有把客服式收尾标记为普遍问题。因此，本报告支持优先改善“多轮听起来总在用相似方式回应”，并保留已有的具体回答、温柔边界与人格一致性。', '',
        '## 2. 覆盖范围与分母', '',
        '**共取回134条留存记录，128条可归属Misty模型的文本全部完成有效评分；6条缺模型归属的账号输出单列。** 历史文本时间跨2026-09-04至2026-09-15。', '',
        '- QQ：119轮模型回复。其中54轮确认交付、1轮部分交付、64轮生成后交付状态未确认。主结论采用前55轮，包含53轮群聊、2轮私聊；部分交付仅评实际送达片段。',
        '- 网页：6条真实运维验证文本，包括接线、记忆核验、保存思考和迁移验证；不代表日常闲聊。其中4条有Runtime记忆交付记录、1条会话提交、1条仅生成。应用持久化证据不能证明浏览器已接收。',
        '- 看板：3条已持久化独白，单独评分；上下文不足的维度保留空值。',
        '- 账号归属不明：1条外部验证输出、5条同账号历史消息。没有足够模型来源证据，未混入Misty质量分母。', '',
        '以QQ decision_id聚合一轮，合并整轮、分段及协议回执，避免同一回复在多份日志中重复计数；相同文本的不同决策不会仅因字面相似而被删除。排除了436个mock运行和241次没有回复文本的等待决定；Linux增量另含17次等待决定。', '',
        '基础来源是正式迁移快照中的QQ决策/回执数据库、Cognitive保存状态、Runtime journal和Board记录，补充本地真实模型trace；Linux通过只读API分页补抓。迁移后journal的94847–97510共2664条连续无缺号，查询全部走到has_more=false。线上监控保留7天，因此本报告的“全量”限定为这些来源可取回的留存历史，不能证明曾删除、未采集或未暴露的记录不存在。', '',
        'Linux新增的两条QQ生成记录缺15/23个上下文引用，当前提问已从原始decision.effective_query恢复；交付回执未在开放API中取得，保留“未确认”，不能推断它们没有发送。', '',
        f'来源清单与准确文件哈希：{link("inventory.json", root / "inventory.json")}；增量说明：{link("Linux覆盖记录", root / "linux-api/coverage-summary.json")}。', '',
        '## 3. 评分方法与结果', '',
        '量表1–5分：1为明显机械且妨碍回应，3为基本合适但机械感可辨，5为高度贴合语境且灵活。缺信息填null并给置信度。评分不按篇幅、甜度、表情数量加分；平静、拒绝、结束话题和诚实说明AI身份都可以自然。首阶段按单条与可见上下文评分，第二阶段独立查看跨历史模式。', '',
    ]
    for key, label in LABELS.items():
        metric = main_stats['scores'][key]
        lines.append(f'- {label}：**{metric["mean"]:.2f}/5**（有效n={metric["n"]}）。')
    lines += ['', '综合分将每项按(score−1)/4映射到0–1，依次采用25%、20%、25%、15%、15%的预设权重，再乘100；聚合只使用五维齐全样本，本次主样本55轮均齐全。它是本次量表的汇总指标，尚未用真人判断校准。', '',
              '主样本回复长度中位数89字符；28轮为闲聊、9轮为记忆/身份、8轮为边界回应、4轮为任务回答，其余6轮为情绪支持、主动发言或其他。单一分数不能替代场景差异。', '',
              '### 历史变化', '',
              '按UTC日期分层：9月11日20轮中20轮都以犹豫词开头，回复中位长度129字符；9月14日35轮中12轮以犹豫词开头，中位长度76字符。留存v12群聊33轮的自然度4.15/5、非模板性4.00/5，29轮仍含省略号。历史后段更短、开场更多样，但话题与用户提问也发生了变化，不能据此认定代码升级带来了因果改善。私聊仅2轮，不能作稳定渠道排名。', '',
              '辅助切片：65条交付未确认生成文本的自然度4.05/5，其中包含64条QQ和1条网页；6条网页自然度4.00/5；3条看板的自然度4.00/5，情境具体性与连续性缺信息。这里的辅助切片有交集，不能相加当作互斥总数。', '',
              f'完整分层与指标定义：{link("statistics.json", root / "statistics.json")}。', '',
              '## 4. Kimi识别的六类跨回复模式', '']
    for index, pattern in enumerate(synthesis['patterns'], 1):
        lines += [f'### {index}. {pattern["name"]}', '', pattern['description'], '',
                  f'证据：{", ".join(pattern["evidence_ids"])}。反例：{", ".join(pattern["counterexample_ids"])}。', '']
    lines += ['这六类模式的81次案例引用全部校验为主样本中的已交付QQ回复。严重度由Kimi标为五类medium、一类low；这属于风格问题的模型判断，不能解释为线上事故等级。', '',
              '### 值得保留的能力', '']
    for item in synthesis['positive_examples']:
        lines.append(f'- **{item["id"]}**：{item["why"]}')
    lines += ['', '## 5. 代表案例与改写建议', '',
              '以下原文按已交付正文呈现；建议稿由Kimi提出，R0014和R0018经过原始上下文事实校正。建议尚未回放验证，不等于修改后的实际产品表现。', '']
    for item in synthesis['rewrite_examples']:
        row = by_id[item['id']]
        lines += [f'### {item["id"]} · {item["issue"]}', '', '**当前提问**', '',
                  quote(row.get('user') or '原始触发消息不完整，见逐条上下文'), '', '**历史原文**', '',
                  quote(row['reply']), '', '**建议稿**', '', quote(item['revision']), '', item['why'], '']
        if item.get('editorial_note'):
            lines += [item['editorial_note'], '']
    lines += ['## 6. 改进优先级', '',
              '以下是结合Kimi建议与源码审计的实施顺序；先进行离线回放，验证效果后再决定改动。', '',
              '1. **先核验记忆证据与“记忆为空”提示的冲突。** QQ的合法历史可能经附加资料传入，而通用检索返回空；审计快照中编译器和Validator的空记忆判断未充分区分这两条路径。这可能促使“我不记得/无法确认”先行，但尚无逐条生成—校验—修订证据证明历史归因。应把具体、作用域正确的证据传给可用性判断，保留对无依据往事的限制。', '',
              '2. **让当前回应意图决定第一句。** 直接问题先答；关系回应抓住用户本轮新增的事实；只有确有不确定时才解释边界。先试同一次生成中的简短答复规划，保留自然停顿，不强制每轮先描写害羞或自我感受。多候选重排需要额外调用和延迟，应在单候选基线上证明收益再采用。', '',
              '3. **把连续点名和同一问题当作一个话题段处理。** 完整说明一次边界后，重复内容允许等待或简短回应；夹入的新实质问题仍应回答。评估要区分用户重复触发与模型无必要地重复立场。', '',
              '4. **用多轮回归检验表达变化。** 建立未参与本次改写的冻结场景，覆盖记忆、技术、闲聊、安慰、拒绝与伦理问题；按线程拆分，重复运行并记录参数。除单条自然度，还统计开头分布、语义策略重复、事实依据和回应前置程度，并请至少两名盲评者比较成对版本。', '',
              '5. **后续再测内稳态与人格投影。** 固定同一问题，改变有意义的状态，检查回应策略是否适当地变化，同时防止虚构现实动作与身体感知。更激动、更亲密或更长不作为自动成功标准。', '',
              '当前审计快照已经包含反固定开场、反客服收尾及不强行追问的规则，也已将共享内稳态摘要接入QQ生成。继续单纯添加禁令的收益需要证明；旧文档中“QQ没有接内稳态”的说法不能作为本报告根因。', '',
              f'精确源码路径与待验证机制见 {link("source_audit.md", root / "source_audit.md")}。该文件记录审计时的工作区，其他任务可能并行修改代码，不能直接代表历史部署。Kimi完整建议与编辑说明见 {link("synthesis_reviewed.json", root / "synthesis_reviewed.json")}。', '',
              '## 7. 限制与质量控制', '',
              '- 单一LLM裁判可能偏好某种文风，分数集中在4分附近；单轮高分与长时间交互中的厌倦感可以同时出现。没有真人校准，不给统计显著性或拟人程度概率结论。',
              '- 语料多来自同一批用户、连续测试和刷屏场景，不是随机样本，条目之间相关；短期、少量私聊不能代表全部真实使用。',
              '- 上下文是留存记录而非每次生成的全部模型可见记忆；未显示的事实不能据此认定虚构。事实风险标签只作为复核线索。',
              '- 逐条输入包含可取回上下文，跨历史复审为控制篇幅只提供每条最近6则上下文。R0134在跨历史复审中仍是更早提取的辅助触发文本；其逐条评分已用正确提问重评，跨历史81次证据引用均不含它，但不能证明其他可见输入对模型输出完全无影响。',
              '- 脱敏移除了密钥、邮箱、URL并替换长数字标识；少量上下文微秒和纯数字别名也被替换。日期及秒级顺序保留，原始私有语料和实际提交版本均留存，便于复核。',
              '- 128个有效评分均校验模型身份、HTTP状态、完成原因、输入ID覆盖、数值范围和逐字证据。4条非逐字引文由Kimi重新摘取，保留原引文；2条补全触发语境的评分通过显式替换记录采用新结果，没有篡改旧响应。',
              '- 对Kimi改写稿额外做了事实校正：删除R0014借用后续历史的“花一个月设计架构”，并为R0018明确联网功能仍待接入。', '',
              f'独立核验：{link("independent_verification.json", root / "independent_verification.json")}。全部 **{verification["known_usage"]["item_and_quote_calls"] + verification["known_usage"]["global_calls"]}次**Chat Completions请求返回 `kimi-k3`；API报告已知总用量 **{verification["known_usage"]["all_known_total_tokens"]:,} tokens**（含推理token），未知重试次数为0。仅记录本次API usage，没有推算账单金额。', '',
              '## 8. 产物与复用', '',
              f'- {link("可筛选报告与全部逐条明细", args.output.with_suffix(".html"))}',
              f'- {link("128条评分及证据JSON", root / "judgments.json")}',
              f'- {link("冻结量表", root / "rubric.json")}；{link("实际提交与原始响应目录", root / "results")}',
              f'- {link("跨历史Kimi原始响应", root / "global/results/900-synthesis.json")}',
              f'- {link("可复用执行说明", Path("eval/misty_humanization/README.md"))}', '',
              f'语料实际文件SHA256：`{inventory["corpus_sha256"]}`。模型调用使用进程内环境变量读取密钥，评估文件与报告不含API key；原始会话和API证据目录已加入Git忽略。', '',
              '接口依据：[Kimi官方快速开始](https://platform.kimi.com/docs/get-api-key)；[Kimi K3模型文档](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)。本次使用官方中国区 `/v1/chat/completions`、`kimi-k3`、`reasoning_effort=high` 和结构化JSON返回。', '']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('\n'.join(lines), encoding='utf-8')
    print(str(args.output.resolve()))


if __name__ == '__main__':
    main()
