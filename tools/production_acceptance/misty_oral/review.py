"""Create anonymous paired review packets. Never infer human scores."""
import argparse,collections,hashlib,json,random,statistics,html,base64
from pathlib import Path

def build(root):
 root=Path(root);rows=[json.loads(p.read_text(encoding='utf8')) for p in (root/'results').glob('*.json') if not p.name.startswith('pilot-')]
 cases={c['id']:c for c in json.loads((root/'cases.json').read_text(encoding='utf8'))}
 lookup={(r['case_id'],r['group'],r['repetition']):r for r in rows};rng=random.Random(281028)
 pairs=[];key={}
 def context(cid):
  rows=json.loads(json.dumps([m for m in cases[cid]['messages'] if m.get('source',{}).get('kind')=='user'][-6:]))
  for m in rows:
   for b in m['content']:
    if b['type']=='image':
     ref=b['attachment'];h=ref['attachmentId'].removeprefix('sha256:');path=root/'frozen-home/attachments/v1/objects'/h[:2]/h
     if path.exists():b['preview']='data:'+ref['mediaType']+';base64,'+base64.b64encode(path.read_bytes()).decode()
  return rows
 def visible(r):
  return {'status':r['status'],'messages':[a['args']['text'] for a in r['actions'] if a['name']=='misty_send'],'wait':not any(a['name']=='misty_send' for a in r['actions']) and any(a['name']=='misty_wait' for a in r['actions']),'missing_evidence':bool(r['missing_evidence'])}
 for cid in sorted({r['case_id'] for r in rows}):
  for group in 'BCDE':
   batches=[]
   for repeat in range(3):
    a=lookup.get((cid,'A',repeat));b=lookup.get((cid,group,repeat))
    if a and b:batches.append((a,b))
   if len(batches)!=3:continue
   swapped=rng.choice([True,False]);ident=hashlib.sha256(f'{cid}/{group}/281028'.encode()).hexdigest()[:12]
   pairs.append({'pair_id':ident,'case_id':cid,'context':context(cid),'trials':[{'left':visible(b if swapped else a),'right':visible(a if swapped else b)} for a,b in batches], 'review':{'preference':None,'naturalness':None,'specificity':None,'brevity':None,'persona':None,'appropriateness':None,'severe_regressions':None,'notes':None}})
   key[ident]={'candidate_group':group,'candidate_side':'left' if swapped else 'right'}
 rng.shuffle(pairs)
 chosen=[];seen=set()
 for prefix in ('real-','synthetic-'):
  for pair in pairs:
   if pair['case_id'].startswith(prefix) and pair['case_id'] not in seen:
    chosen.append(pair);seen.add(pair['case_id'])
    if sum(p['case_id'].startswith(prefix) for p in chosen)==6:break
 pairs=chosen+[p for p in pairs if p not in chosen]
 (root/'blind-review.json').write_text(json.dumps(pairs,ensure_ascii=False,indent=2),encoding='utf8')
 (root/'blind-key.json').write_text(json.dumps(key,indent=2),encoding='utf8')
 summary={'formal_rows':len(rows),'status_counts':dict(collections.Counter(r['status'] for r in rows)),'paired_cases':len(pairs),'human_review':'pending','winner':None,'production_changed':False}
 summary['groups']={}
 for group in 'ABCDE':
  rs=[r for r in rows if r['group']==group];texts=[''.join(a['args']['text'] for a in r['actions'] if a['name']=='misty_send') for r in rs if r['status']=='completed'];spoken=[t for t in texts if t]
  firsts=[[a['args']['text'] for a in r['actions'] if a['name']=='misty_send'] for r in rs]
  summary['groups'][group]={'first_message_median_characters':statistics.median(len(t[0]) for t in firsts if t) if any(firsts) else None,'duplicate_text_rounds':sum(len(t)!=len(set(t)) for t in firsts),'no_tool_action_rounds':sum(not r['actions'] for r in rs),'rounds':len(rs),'statuses':dict(collections.Counter(r['status'] for r in rs)),'speaking_rounds':len(spoken),'median_characters':statistics.median(map(len,spoken)) if spoken else None,'mean_characters':statistics.mean(map(len,spoken)) if spoken else None,'question_rounds':sum('？' in t or '?' in t for t in spoken)}
 render_html(root,pairs,summary)
 (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8');return summary

def clean_context(messages):
 speakers={}; cleaned=[]
 for m in messages:
  texts=[];images=[];speaker='用户'
  for b in m.get('content',[]):
   if b.get('preview'):images.append(b['preview'])
   if b.get('type')!='text':continue
   raw=b.get('text','').strip()
   try:
    envelope,end=json.JSONDecoder().raw_decode(raw)
   except (ValueError,TypeError):envelope=None
   if isinstance(envelope,dict) and isinstance(envelope.get('text'),str):
    ident=str(envelope.get('sender_id','user'))
    if ident not in speakers:speakers[ident]='用户'+str(len(speakers)+1)
    speaker=speakers[ident];texts.append(envelope['text'])
    tail=raw[end:].strip()
    if tail and tail not in ('图片 1：','图片1：'):texts.append(tail)
   elif raw:texts.append(raw)
  cleaned.append({'speaker':speaker,'text':'\n'.join(texts),'images':images})
 return cleaned

def render_html(root,pairs,summary):
 view=[{**p,'context':clean_context(p['context'])} for p in pairs]
 payload=json.dumps(view,ensure_ascii=False).replace('<',chr(92)+'u003c')
 template=Path(__file__).with_name('review.html').read_text(encoding='utf8')
 (root/'blind-review.html').write_text(template.replace('/*DATA*/[]',payload),encoding='utf8')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('root');print(json.dumps(build(p.parse_args().root)))
