"""Isolated official C2C integration tests. No network or production writes."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pydantic import TypeAdapter
from harness.qq.conversation import private_scope_id, conversation_scope, conversation_audience, QQConversationID
from harness.qq.official_identity import identity, message_id
from harness.qq.official_events import normalize_official
from harness.qq.direct_config import DirectQQSettings
from harness.qq.session import QQSessionSettings
from harness.qq.official_actor import OfficialQQActor
from harness.qq.official_enrollment import capture_enrollment
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.direct_capabilities import QQDirectReadScope
from harness.qq.telemetry import QQTurnTrace, QQDeadline
from harness.qq.memory import protocol_message_evidence_ref
from harness.core.models import MemoryScope
from harness.memory.shared import Audience, SharedMemoryStore
from harness.qq.policy import QQLatencyOptions
from harness.memory.qq_assimilation import QQSource

APP='123'; SELF=identity(APP,'bot',APP); PEER=identity(APP,'user','userA'); GROUP=identity(APP,'group','groupA')
SCOPE=private_scope_id(SELF,PEER)
def payload(text='hello'):
 return {'transport':'qqbot','app_id':APP,'event_type':'C2C_MESSAGE_CREATE','data':{
  'id':'c2c-test','timestamp':datetime.now(timezone.utc).isoformat(),'author':{'user_openid':'userA'},'content':text}}

class PrivateTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  session=QQSessionSettings(memory_db=self.root/'memory.db',token_file=self.root/'session.token',
   allowed_group_ids=[GROUP],allowed_private_peer_ids=[PEER],self_member_id=SELF)
  (self.root/'session.json').write_text(session.model_dump_json())
  paths={k:self.root/k for k in ['token_file','state_db','status_file','control_file','evidence_dir','image_cache_dir','desktop_status_file','shared_control_token_file']}
  self.settings=DirectQQSettings(**paths,transport='qqbot',qqbot_app_id=APP,qqbot_credentials_file=self.root/'credentials.json',
   enabled=True,self_member_id=SELF,group_roster=[{'group_id':GROUP,'observe':True,'send':True}],
   private_contacts=[{'peer_id':PEER,'label':'2285027817','observe':True,'send':True}],session_config_path=self.root/'session.json',qq_image_mode='off')
  self.settings.control_file.write_text('{"paused":false}')
  self.settings.desktop_status_file.write_text(json.dumps({'running':False,'heartbeat_at':datetime.now(timezone.utc).isoformat()}))
 def tearDown(self): self.tmp.cleanup()

 def test_scope_boundaries(self):
  self.assertEqual(conversation_scope(SCOPE)['peer_id'],PEER)
  self.assertEqual(private_scope_id('123','456'),'private:123:456')
  TypeAdapter(QQConversationID).validate_python(SCOPE)
  TypeAdapter(Audience).validate_python(conversation_audience(SCOPE))
  MemoryScope(channel='qq_private',conversation_id=conversation_audience(SCOPE),visibility=[conversation_audience(SCOPE)])
  with self.assertRaises(ValueError): private_scope_id(SELF,identity('456','user','userA'))
  with self.assertRaises(ValueError): private_scope_id(SELF,identity(APP,'member','userA'))
  with self.assertRaises(ValueError): MemoryScope(channel='qq_private',conversation_id=conversation_audience(SCOPE),visibility=['qq:group:'+GROUP])
  with self.assertRaises(ValueError): protocol_message_evidence_ref(SCOPE,identity(APP,'user','outsider'),message_id(APP,'m'))

 def test_admission_and_shared_request(self):
  self.assertIsNone(normalize_official(payload(),self_id=SELF,groups=[GROUP]))
  event=normalize_official(payload(),self_id=SELF,groups=[GROUP],private_peers=[PEER])
  self.assertEqual(event['protocol_addressing'],'private')
  trace=QQTurnTrace(event_id=event['event_id']); trace.bind_conversation(group_id=SCOPE,self_member_id=SELF)
  data=dict(transport='qqbot',group_id=SCOPE,self_member_id=SELF,trigger_member_id=PEER,
   deadline_at=datetime.now(timezone.utc)+timedelta(seconds=30),trace=dict(trace_id=trace.trace_id,event_id=event['event_id'],first_seen_at=trace.first_seen_at,first_seen_mono=trace.first_seen_mono))
  QQSharedPrepareRequest(**data)
  with self.assertRaises(ValueError): QQSharedPrepareRequest(**(data|{'trigger_member_id':identity(APP,'user','outsider')}))
  QQDirectReadScope(transport='qqbot',group_id=SCOPE,self_member_id=SELF)
  QQLatencyOptions.validate_conversations([SELF+'/'+SCOPE])

 def test_private_observation_and_memory(self):
  async def run():
   actor=OfficialQQActor(self.settings,cognition=SimpleNamespace())
   actor.client=SimpleNamespace(connected=True); actor.connected=True
   await actor.on_event(payload())
   event=normalize_official(payload(),self_id=SELF,groups=[GROUP],private_peers=[PEER])
   row=actor.store.get(event['event_id']); self.assertIsNotNone(row)
   observation=await actor.observe(row['event'],QQDeadline.after(30))
   self.assertEqual(observation.member_id,PEER)
   mirror=SharedMemoryStore(self.root/'shared.db')
   mirror.import_qq_database(self.settings.session_config_path.parent/'memory.db',allowed_group_ids=[GROUP],
    self_member_id=SELF,allowed_private_peer_ids=[PEER])
   with closing(mirror._connect(write=False)) as db:
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    self.assertTrue(tables)
   await actor.on_lifecycle({'event_type':'FRIEND_DEL','data':{'openid':'userA'}})
   self.assertNotIn(SCOPE,actor.current_conversations(send=True))
   await actor.on_lifecycle({'event_type':'FRIEND_ADD','data':{'openid':'userA'}})
   self.assertNotIn(SCOPE,actor.current_conversations(send=True))
  asyncio.run(run())

 def test_reply_route_and_receipt_binding(self):
  import sqlite3
  async def run():
   actor=OfficialQQActor(self.settings,cognition=SimpleNamespace())
   event=normalize_official(payload(),self_id=SELF,groups=[GROUP],private_peers=[PEER])
   dbpath=self.root/'reservation.db'
   with sqlite3.connect(dbpath) as db:
    db.execute('CREATE TABLE qq_direct_sends(event_id TEXT,status TEXT,segment_index INTEGER)')
    db.execute('INSERT INTO qq_direct_sends VALUES(?,?,?)',(event['event_id'],'attempted',0))
   def connect():
    db=sqlite3.connect(dbpath); db.row_factory=sqlite3.Row; return db
   calls=[]
   async def reply(**kw): calls.append(kw); return {'id':'c2c-ack'}
   actor.store=SimpleNamespace(connect=connect,evidence=lambda _: 'test-proof')
   actor.client=SimpleNamespace(reply=reply)
   result=await actor.transmit_text(event,'hello',timeout=10)
   self.assertEqual(calls,[{'user_openid':'userA','message_id':'c2c-test','seq':1,'text':'hello','timeout':10}])
   self.assertEqual(await actor.verify_sent(event,result['message_id'],'hello'),'test-proof')
   with self.assertRaises(ValueError):
    await actor.transmit_text(event|{'group_id':private_scope_id(identity('456','bot','456'),identity('456','user','userA'))},'hello',timeout=10)
  asyncio.run(run())

 def test_enrollment_requires_challenge(self):
  self.settings.evidence_dir.mkdir()
  now=datetime.now(timezone.utc).timestamp(); challenge='Misty-bind-test-random'
  pending={'app_id':APP,'requested_qq_label':'2285027817','created_at':now,'expires_at':now+60,
   'challenge_sha256':hashlib.sha256(challenge.encode()).hexdigest()}
  path=self.root/'qqbot-private-enrollment.json'; path.write_text(json.dumps(pending)); path.chmod(0o600)
  self.assertFalse(capture_enrollment(self.settings,payload('I am 2285027817')))
  self.assertTrue(capture_enrollment(self.settings,payload(challenge)))
  self.assertFalse(capture_enrollment(self.settings,payload(challenge)))
  result=json.loads((self.settings.evidence_dir/'private-enrollment-candidate.json').read_text())
  self.assertEqual(result['peer_id'],PEER)

if __name__=='__main__': unittest.main(verbosity=2)
