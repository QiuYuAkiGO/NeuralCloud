"""Read-only production capture. Run with access to logs; output stays private."""
import argparse, collections, hashlib, json, os
from pathlib import Path

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def capture(config_path, output):
 c=json.loads(Path(config_path).read_text()); root=Path(c['data_root'])/'harness'
 out=Path(output);out.mkdir(mode=0o700,parents=True,exist_ok=False)
 candidates=[]
 for p in sorted((root/'sessions').rglob('session.v4.jsonl'),key=lambda x:x.stat().st_mtime,reverse=True)[:12]:
  raw=p.read_bytes();rows=[]
  for line in raw.splitlines():
   try: rows.append(json.loads(line))
   except ValueError: break # live trailing line is not a complete event
  messages=[]; header=None; seen=set()
  for r in rows:
   t=r['type'];d=r.get('data',{});m=d.get('message',d)
   if t=='request/header':header=d['header']
   if t=='assistant/message' and header and d.get('turn') not in seen:
    seen.add(d.get('turn')); users=[x for x in messages if x.get('role')=='user']
    if users and any('sender_id' in json.dumps(x) for x in users):
     candidates.append({'id':f'real-{len(candidates):03}', 'kind':'reconstructed','session':p.parent.name,'anchor_seq':r['seq'],'source_sha256':hashlib.sha256(raw).hexdigest(),'header':header,'messages':list(messages),'note':'Event-projected prefix; request/context does not preserve full wire payload. No future events or current memory added.'})
   if t in ('system/message','user/message','assistant/message','tool/result') and isinstance(m,dict) and 'role' in m:
    messages.append(m)
 if len(candidates)<24:raise RuntimeError('INSUFFICIENT_REAL_CASES')
 # Spread selection across sessions rather than taking only one busy group.
 buckets=collections.defaultdict(list)
 for x in candidates:buckets[x['session']].append(x)
 selected=[]
 while len(selected)<24:
  for values in buckets.values():
   if values and len(selected)<24:selected.append(values.pop())
 (out/'real-cases.json').write_text(json.dumps(selected,ensure_ascii=False),encoding='utf-8')
 model={k:v for k,v in c['model'].items() if 'key' not in k}
 (out/'manifest.json').write_text(json.dumps({'model':model,'config_sha256':sha(config_path),'real_cases':24,'capture_kind':'reconstructed-event-prefix','production_writes':False},indent=2))
 print(json.dumps({'captured':len(selected),'sessions':len(set(x['session'] for x in selected)),'output':str(out)}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('config');p.add_argument('output');a=p.parse_args();capture(a.config,a.output)
