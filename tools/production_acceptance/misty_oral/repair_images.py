import json
from pathlib import Path
r=Path('/var/tmp/misty-oral-20261028');cases={c['id']:c for c in json.loads((r/'cases.json').read_text())};d=json.loads((r/'design.json').read_text());checks=[]
for i in range(6):
 row=json.loads((r/f'results/pilot-image-{i}.json').read_text());c=cases[row['case_id']]
 expected=json.loads(json.dumps(c['messages']));next(m for m in expected if m['role']=='system')['content']=[{'type':'text','text':d['prompts'][c['id']][row['group']]['text']}]
 assert row['status'] in ('completed','unevaluable') and row['calls'][0]['request']['messages']==expected and row['calls'][0]['request']['tools']==c['header']['tools']
 checks.append({'id':row['run_id'],'passed':True})
(r/'image-gate.json').write_text(json.dumps(checks,indent=2))
archive=r/'attempts/missing-attachment-service';archive.mkdir(parents=True,exist_ok=True);n=0
for p in (r/'results').glob('*.json'):
 if p.name.startswith('pilot-'):continue
 row=json.loads(p.read_text())
 if row['status']=='model_error' and any(x.get('finish',{}).get('failure',{}).get('code')=='UNSUPPORTED_CONTENT' for x in row['calls']):
  assert not (archive/p.name).exists();p.rename(archive/p.name);n+=1
print(json.dumps({'image_contracts':6,'archived_for_uniform_repair':n}))
