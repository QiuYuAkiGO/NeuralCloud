"""Run as root on the production host: stage first, then activate.

Upload index.mjs, oral-expression.mjs, oral-expression.test.mjs and hashes.json
to /tmp/misty-oral-v2 before staging. No credentials or chat data are included.
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

OLD = Path('/opt/misty/releases/resident-20261028-context-budget-v1')
NEW = Path('/opt/misty/releases/resident-20261028-oral-v2')
REL = Path('MistyAI/compatibility/harness')
PLUGIN = Path('MistyAI/plugins/dsh-misty/src')
UPLOAD = Path('/tmp/misty-oral-v2')
NODE = '/opt/misty/node-v24.15.0/bin/node'
SERVICE = 'misty-harness.service'
DROP = Path('/etc/systemd/system/misty-harness.service.d/60-oral-v2.conf')
BACKUP = Path('/var/lib/misty/backups/oral-v2-20261028')


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage():
    assert Path(run(['systemctl','show',SERVICE,'--value','-p','WorkingDirectory']).strip()) == OLD / REL
    hashes = json.loads((UPLOAD/'hashes.json').read_text())
    assert sha(OLD/PLUGIN/'index.mjs') == hashes['before']
    assert sha(UPLOAD/'index.mjs') == hashes['after']
    assert sha(UPLOAD/'oral-expression.mjs') == hashes['policy']
    assert not NEW.exists()
    run(['cp','-a','--reflink=auto',str(OLD),str(NEW)],timeout=120)
    for name in ('index.mjs','oral-expression.mjs'):
        shutil.copyfile(UPLOAD/name,NEW/PLUGIN/name)
    shutil.copyfile(UPLOAD/'oral-expression.test.mjs',NEW/REL/'test/oral-expression.test.mjs')
    tests = sorted(str(p) for p in (NEW/REL/'test').glob('*.test.mjs'))
    result = subprocess.run([NODE,'--test',*tests],cwd=NEW/REL,text=True,capture_output=True,timeout=180)
    (UPLOAD/'tests.log').write_text(result.stdout+result.stderr)
    assert result.returncode == 0, 'Harness suite failed; see tests.log'
    preflight = run(['sudo','-u','misty-harness',NODE,'resident/launch.mjs','check','/var/lib/misty/config/harness.json'],cwd=NEW/REL,timeout=70)
    record = dict(hashes=hashes,preflight=json.loads(preflight),tests_passed=True,
                  old_release=str(OLD),new_release=str(NEW))
    (NEW/'oral-v2-stage.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))


def activate():
    record = json.loads((NEW/'oral-v2-stage.json').read_text())
    assert record['tests_passed']
    assert sha(NEW/PLUGIN/'index.mjs') == record['hashes']['after']
    assert sha(NEW/PLUGIN/'oral-expression.mjs') == record['hashes']['policy']
    assert Path(run(['systemctl','show',SERVICE,'--value','-p','WorkingDirectory']).strip()) == OLD/REL
    assert not DROP.exists() and not BACKUP.exists()
    config = json.loads(Path('/var/lib/misty/config/harness.json').read_text())
    root = Path(config['data_root'])/'harness'
    stopped = False
    try:
        run(['systemctl','stop',SERVICE],timeout=75)
        stopped = True
        props = run(['systemctl','show',SERVICE,'-p','ActiveState','-p','ExecMainStatus','-p','Result'])
        assert all(s in props for s in ('ActiveState=inactive','ExecMainStatus=0','Result=success')), 'Unclean shutdown'
        BACKUP.mkdir(parents=True,mode=0o700)
        for name in ('runtime.sqlite3','ingress.sqlite3','outbox.sqlite3','owner.sqlite3'):
            with sqlite3.connect(f'file:{root/name}?mode=ro',uri=True) as src, sqlite3.connect(str(BACKUP/name)) as dst:
                src.backup(dst)
                assert dst.execute('pragma quick_check').fetchone()[0] == 'ok'
        shutil.copytree(root/'sessions',BACKUP/'sessions')
        shutil.copy2('/var/lib/misty/config/harness.json',BACKUP/'harness.json')
        with sqlite3.connect(f'file:{root}/runtime.sqlite3?mode=ro',uri=True) as db:
            before = db.execute('select key,id,status from active order by key').fetchall()
        DROP.write_text(f'[Service]\nWorkingDirectory={NEW/REL}\nExecStart=\nExecStart={NODE} {NEW/REL}/resident/launch.mjs run /var/lib/misty/config/harness.json\n')
        run(['systemctl','daemon-reload'])
        started = int(time.time()*1000)
        run(['systemctl','start',SERVICE],timeout=25)
        for _ in range(45):
            time.sleep(1)
            status = json.loads((root/'status.json').read_text())
            if status.get('observed_at',0)>started and status.get('status')=='READY':
                break
        else:
            raise RuntimeError('New runtime did not become READY')
        pid = int(run(['systemctl','show',SERVICE,'--value','-p','MainPID']).strip())
        assert Path(f'/proc/{pid}/cwd').resolve() == NEW/REL
        with sqlite3.connect(f'file:{root}/runtime.sqlite3?mode=ro',uri=True) as db:
            after = db.execute('select key,id,status from active order by key').fetchall()
        assert before == after, 'Session bindings changed during deployment'
        record.update(status='DEPLOYED',runtime_status=status['status'],main_pid=pid,
            backup=str(BACKUP),service_dropin=str(DROP),session_bindings_preserved=True,
            input_pending=status.get('input_pending'),
            channels={k:{'ready':v.get('ready'),'connected':v.get('connected')} for k,v in status.get('channels',{}).items()},
            real_qq_test_sent=False,verified_at_ms=int(time.time()*1000))
        (BACKUP/'deployment.json').write_text(json.dumps(record,indent=2))
        print(json.dumps(record))
    except Exception:
        if stopped:
            run(['systemctl','stop',SERVICE],timeout=75)
            if DROP.exists():
                DROP.rename(BACKUP/'60-oral-v2.failed.conf')
            run(['systemctl','daemon-reload'])
            run(['systemctl','start',SERVICE],timeout=25)
        raise


if __name__ == '__main__':
    {'stage':stage,'activate':activate}[sys.argv[1]]()
