"""A capped review round of receipt-confirmed historical production messages."""
import hashlib
import json
from pathlib import Path
from review import clean_context
from compact_review import RULE

PREFIXES = ['d210c7dd','ace5e7c8','257523aa','88ffeb84','4ea1fdc7',
            '50d2958e','9ac155a8','fca77cd3','3a6ca2df','229f3f9a',
            'e60e53ab','ed39523b','63dd9ed6','87991fa2','30fc5779',
            'b158bee2','b69a7035','b8a2e813','ba7f42d6','35c4e264']


def build(root):
    root = Path(root)
    source = root / 'projected-cases.json'
    cases = json.loads(source.read_text(encoding='utf8'))
    selected = {}
    eligible = set()
    for case in cases:
        messages = case['messages']
        receipts = {m.get('toolCallId'):m for m in messages if m['role'] == 'tool'}
        users = [m for m in messages if m.get('source', {}).get('kind') == 'user']
        cleaned = iter(clean_context(users))
        history = []
        for message in messages:
            if message.get('source', {}).get('kind') == 'user':
                history.append(next(cleaned))
            if message['role'] != 'assistant':
                continue
            sent = []
            for block in message['content']:
                if block.get('type') != 'tool-call' or block.get('name') != 'misty_send':
                    continue
                receipt = receipts.get(block['id'])
                if not receipt or receipt.get('isError'):
                    continue
                confirmed = False
                for content in receipt['content']:
                    try:
                        confirmed |= json.loads(content.get('text', '{}')).get('status') == 'SENT'
                    except (ValueError, AttributeError):
                        pass
                if not confirmed:
                    continue
                args = block['arguments']
                args = json.loads(args) if isinstance(args, str) else args
                sent.append({'text':args['text'], 'call_id':block['id'], 'receipt_id':receipt['id']})
                eligible.add((message['id'], block['id']))
            if sent:
                prefix = message['id'][:8]
                if prefix in PREFIXES and prefix not in selected:
                    selected[prefix] = dict(id=message['id'], context=history[-6:],
                        reply='\n\n'.join(s['text'] for s in sent), receipt_status='SENT',
                        provenance={'case_id':case['id'], 'session':case['session'],
                                    'source_sha256':case['source_sha256'], 'sends':sent})
                history.append({'speaker':'Misty', 'text':'\n\n'.join(s['text'] for s in sent), 'images':[]})
    if set(selected) != set(PREFIXES):
        raise ValueError('Selected production messages not all confirmed')
    rows = [selected[p] for p in PREFIXES]
    assert len(rows) <= 30 and len({r['id'] for r in rows}) == len(rows)
    manifest = dict(round_id='production-20261028-r1', cap=30, count=len(rows),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        confirmed_send_candidates=len(eligible), rating_rule=RULE,
        selection='Manual topic-diverse selection from receipt-confirmed messages; repeated message IDs and adjacent same-topic replies omitted. Not random or representative.',
        limitations='Historical captured production records, not fresh logs or candidate-model outputs. Context is a recent excerpt; unavailable attachments remain unavailable. No preference win-rate against A/C can be inferred.', rows=rows)
    (root/'production-review-r1.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    template = Path(__file__).with_name('production_review.html').read_text(encoding='utf8')
    # UI includes no reasoning blocks, tool payloads, destination IDs or internal provenance.
    public = {k:v for k,v in manifest.items() if k != 'rows'}
    public['rows'] = [{k:r[k] for k in ('id','context','reply')} for r in rows]
    payload = json.dumps(public,ensure_ascii=False).replace('<','\\u003c')
    (root/'production-review-r1.html').write_text(template.replace('/*DATA*/{}',payload),encoding='utf8')
    print(json.dumps({'selected':len(rows),'confirmed_candidates':len(eligible),'cap':30}))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    build(parser.parse_args().root)
