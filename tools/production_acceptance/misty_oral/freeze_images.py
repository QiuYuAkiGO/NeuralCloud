import hashlib,json,shutil
from pathlib import Path
r=Path('/var/tmp/misty-oral-20261028');cases=json.loads((r/'cases.json').read_text());copied=[];missing=[]
for c in cases:
 for m in c['messages']:
  for b in m['content']:
   if b['type']!='image':continue
   ref=b['attachment']['attachmentId'];h=ref.removeprefix('sha256:')
   if len(h)!=64 or any(x not in '0123456789abcdef' for x in h):raise ValueError('INVALID_IMAGE_HASH')
   p=Path('/var/lib/misty/harness/attachments/v1/objects')/h[:2]/h
   if not p.exists():missing.append(ref);continue
   data=p.read_bytes()
   if hashlib.sha256(data).hexdigest()!=h:raise ValueError('IMAGE_HASH_MISMATCH')
   target=r/'frozen-home/attachments/v1/objects'/h[:2]/h;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(0o600);copied.append(h)
(r/'attachments-manifest.json').write_text(json.dumps({'copied':sorted(set(copied)),'missing':sorted(set(missing))},indent=2));print(json.dumps({'copied':len(set(copied)),'missing':len(set(missing))}))
