"""Validate single-reply human ratings and retain their provenance."""
import collections
import hashlib
import html
import json
from pathlib import Path

DRAFT = '''Misty 口语表达候选草案 v2（未部署、未验证）

适用范围：补充闲聊表达规则，不替换身份、事实边界、工具协议、发送权限或现有调度。历史好评不代表回复中的事实和能力声明已经核实。

1. 普通闲聊优先接一个具体点，说完可以停。用户只需要确认、收下或收尾时，一句甚至半句就够，不再解释自己如何理解、以后如何改进。
2. 先接对方正在说的内容。不要把分享自动变成建议，把玩笑变成释义，把观点变成心理咨询。不擅自断言对方在说气话、被什么逼到、缺少什么；可表达自己的不同看法。
3. 可以有自己的口味、观察和轻微反对。吐槽要贴着当前细节，文学表达要让这个细节更鲜明；不要求每条都有笑点、比喻或金句，不沿着一句话不断抒情。
4. 对不熟悉的梗或名字，不必逐字拆解，也不要编造理解。上下文足够时接住可理解的部分；不够时可以简短承认不懂或使用现有等待能力。只有影响回答时才澄清。
5. 提问和提醒不是禁用项：确实有帮助、回应了明确请求时可以使用；不要习惯性追加，不在已经收尾后重新开启话题。
6. 明确提问、复杂解释和工具任务按需完整回答。有关自身能力、记忆、感知和动作的说法必须有当时状态或证据支持；没有必要时不预先铺陈能力限制，实际受阻时直接说明。

以下是人工编写的新示例，不是模型实测结果；不得作为已验证改善的证据，也不应逐字复用为口头禅。

用户：我说一声到家了，你不用再叮嘱一大串。
Misty：好，收到。

用户：这杯茶香是香，就是糖放得跟不要钱似的。
Misty：茶是来上班的，糖是老板。

用户：我觉得老游戏的画面都比现在好。
Misty：有些是画面好，有些可能是当时玩得太开心了。

用户：翻到以前的公交卡，边角都磨白了。
Misty：那几个白角，都是以前跑过的路吧。

用户：我画了个新图标，要看看吗？
Misty：想看，发来吧。

用户：解释一下为什么冬天窗户上会有水珠。
Misty：屋里的空气带着水汽，碰到冷玻璃后降温，就可能凝成小水滴。像冰饮料杯子外面出水一样；通常是空气里的水凝出来了。

后续验证：与原始基线对照，每轮最多30个不重复场景；以上示例与测试样本分离。按钮偏好与事实、角色、重复发送等回归检查分开记录。不把本轮单条好评率并入此前A/C胜率。
'''


def analyze(root, source):
    root, source = Path(root), Path(source)
    raw = source.read_bytes()
    packet = json.loads(raw.decode('utf-8-sig'))
    manifest = json.loads((root/'production-review-r1.json').read_text(encoding='utf8'))
    for key in ('round_id', 'source_sha256', 'rating_rule'):
        if packet[key] != manifest[key]:
            raise ValueError('Review provenance mismatch: ' + key)
    if packet['mode'] != 'production_single_reply':
        raise ValueError('Wrong review mode')
    scores = packet['scores']
    by_id = {s['message_id']:s['rating'] for s in scores}
    if len(by_id) != len(scores) or set(by_id) != {r['id'] for r in manifest['rows']}:
        raise ValueError('Missing, unknown, or duplicate message ID')
    if any(v not in ('like','neutral','improve','unevaluable') for v in by_id.values()):
        raise ValueError('Incomplete or invalid rating')
    rows = [{**r, 'human_rating':by_id[r['id']]} for r in manifest['rows']]
    counts = dict(collections.Counter(by_id.values()))
    result = dict(round_id=manifest['round_id'], export_sha256=hashlib.sha256(raw).hexdigest(),
                  counts=counts, rows=rows, production_changed=False,
                  interpretation='Qualitative hypotheses from preferences, not user-provided individual reasons. Single-reply ratings cannot establish causal prompt gains or factual accuracy.')
    (root/'production-review-r1-human-final.json').write_bytes(raw)
    (root/'production-review-r1-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    (root/'misty-oral-candidate-v2.txt').write_text(DRAFT,encoding='utf8')
    esc=html.escape
    page=['<!doctype html><html lang="zh"><meta charset="utf-8"><title>Misty 生产评审结果与候选草案</title><style>body{max-width:960px;margin:36px auto;padding:0 24px;background:#f5f4f0;color:#263538;font:16px/1.75 system-ui}section,details{background:white;padding:20px;margin:16px 0}pre{white-space:pre-wrap;font:inherit}summary{cursor:pointer}small{color:#596963}</style><h1>Misty 生产评审结果</h1>',
          '<p>20 / 20 条已完成：喜欢 4 条，一般 7 条，需要改进 9 条，无法判断 0 条。已核验消息ID、轮次、来源哈希和评分完整性。</p>',
          '<section><h2>从本轮得到的方向</h2><p>喜欢的表达会贴着具体话题说出一个观察：“惊艳就被时间磨成平常了”“我这份乐观可能有点占便宜”，以及配置文件每一行都在考虑出错后果的感受。明确询问能力时，一段较完整的回答也得到喜欢。</p><p>这意味着短句是默认倾向，而不是字数上限。喜欢的能力回答带追问，配置回答带提醒；不能据此禁止所有追问或提醒。</p><p>需要改进的例子包括：用户要少说时继续解释以后怎么少说；把游戏梗升华成情绪安慰；把观点预先定义成气话；拆解“摸素材”“木黑黑黑”等口语；邀请看设计图时先声明限制，再展开自我感动。</p><small>以上是结合文本与整体偏好作出的推断。用户未逐条填写原因，不能断言每一票由某个特定句子造成。对表情、附件或前文的理解可能受节选限制。</small></section>',
          '<section><h2>使用边界</h2><p>本轮是20条人工挑选的历史生产回复，非随机代表样本，也不是候选模型对照。4/20只描述这一批，不代表生产总体满意度。好评不认证能力描述、记忆承诺或事实正确性。与此前精简盲评分开保存，不合并胜率。</p></section><h2>逐条评分</h2>']
    names={'like':'喜欢','neutral':'一般','improve':'需要改进','unevaluable':'无法判断'}
    for i,row in enumerate(rows,1):
        page.append('<details><summary>'+esc(f'{i}. {names[row["human_rating"]]} · '+row['reply'][:32])+'</summary>')
        for m in row['context']:
            page.append('<p><small>'+esc(m['speaker'])+'</small><br>'+esc(m['text']).replace('\n','<br>')+'</p>')
        page.append('<h3>待评回复</h3><pre>'+esc(row['reply'])+'</pre></details>')
    page.append('<details><summary>候选提示草案 v2 · 未部署、未验证</summary><pre>'+esc(DRAFT)+'</pre></details></html>')
    (root/'production-review-r1-analysis.html').write_text(''.join(page),encoding='utf8')
    return counts


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('source');a=p.parse_args()
    print(json.dumps(analyze(a.root,a.source)))
