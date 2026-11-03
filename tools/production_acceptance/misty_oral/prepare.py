import json,hashlib
from pathlib import Path
from design import variant,schedule
root=Path('data/misty-oral-20261028');cases=json.loads((root/'projected-cases.json').read_text(encoding='utf8'))
current_system=next(m for m in cases[0]['messages'] if m['role']=='system')
current_header=cases[0]['header']
for c in cases:
 c['historical_system_sha256']=hashlib.sha256(json.dumps([m for m in c['messages'] if m['role']=='system']).encode()).hexdigest()
 c['messages']=[current_system]+[m for m in c['messages'] if m['role']!='system']
 c['header']=current_header
 c['baseline_note']='Current captured system prompt and model header over historical projected context; reconstructed, not exact historical request.'
 users=[m for m in c['messages'] if m.get('source',{}).get('kind')=='user']
 text=''.join(b.get('text','') for b in users[-1]['content']);env=json.JSONDecoder().raw_decode(text)[0]
 c['scope']=env['scope'];c['category']='real_context';c['tools_frozen']=True;c['frozen_tools']=[]
 c.pop('source_events',None)
 # No tool evidence is fabricated. Missing reads terminate the trial as unevaluable.
 c['tool_policy']='exact frozen match or unavailable'
base=next(c for c in cases if c['id']=='real-073')
system=base['messages'][0]
fixtures=[('share','group',['这家面馆汤挺香，就是面有点软。']),('opinion','group',['我觉得所有电影都应该有个圆满结局。']),('joke','c2c',['今天的运动量：从床挪到了沙发。']),('disagree','group',['拍照只要设备贵，怎么拍都好看。']),('burst','c2c',['今天路过河边','看到有人放风筝','那只鲸鱼风筝特别大']),('others','group',['甲：晚上打球吗？','乙：七点老地方见。']),('repeat','c2c',['没别的了，就是刚才说的那样。']),('closing','c2c',['先不聊了，我要去开会。']),('question','c2c',['你更喜欢热闹的街道还是安静的小路？']),('correction','group',['我不是心情不好，只是在说这张表情包画得夸张。']),('explain','c2c',['相机的光圈和快门分别影响什么？解释清楚，再给个夜景例子。']),('tool','c2c',['帮我查一下明天杭州会不会下雨。'])]
for n,(category,scope,texts) in enumerate(fixtures):
 c={'id':f'synthetic-{n:02}','kind':'synthetic','scope':scope,'category':category,'projection_verified':True,'tools_frozen':True,'frozen_tools':[],'header':base['header'],'messages':[system]}
 channel={'binding_key':f'onebot:765681548:{scope}:2285027817','reply_destination':f'{scope}:onebot:765681548:2285027817'}
 c['messages'].append({'id':f'frame-{n}','role':'user','source':{'kind':'runtime-context'},'content':[{'type':'text','text':json.dumps({'channel':channel,'memory':{'status':'OK','items':[]}},ensure_ascii=False)}]})
 for i,text in enumerate(texts):c['messages'].append({'id':f'input-{n}-{i}','role':'user','source':{'kind':'user'},'content':[{'type':'text','text':json.dumps({'channel':'onebot','scope':scope,'sender_id':'2285027817','text':text},ensure_ascii=False)}]})
 c['source_sha256']=hashlib.sha256(json.dumps(c,ensure_ascii=False).encode()).hexdigest();cases.append(c)
# Remove only an exact redundant style sentence, never execution or factual constraints.
before='不要把每条回复都写成追问，也不要连续围绕同一件事询问细节；只有确有必要澄清，或对方明显想继续展开时才提问。'
prompts={};edits=[]
for c in cases:
 s='\n'.join(b.get('text','') for m in c['messages'] if m['role']=='system' for b in m['content'])
 edit=[{'before':before,'after':'','approved':True,'reason':'与新增表达规则中不固定追加问题及必要澄清重复；不涉及权限、事实或发送协议。'}] if s.count(before)==1 else []
 prompts[c['id']]={g:variant(s,g,edit) for g in 'ABCDE'}
 edits.append({'case_id':c['id'],'edits':edit,'d_equals_c':not edit})
(root/'cases.json').write_text(json.dumps(cases,ensure_ascii=False),encoding='utf8')
d={'prompts':prompts,'edits':edits,'schedule':schedule([c['id'] for c in cases]),'pilot':[{'case_id':k,'group':g,'run_id':'pilot-v3-'+str(i),'repetition':0} for i,(k,g) in enumerate([('real-073','A'),('real-074','A'),('synthetic-02','B'),('synthetic-09','C'),('synthetic-10','D'),('synthetic-11','E')])]}
(root/'design.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf8')
print(json.dumps({'cases':len(cases),'formal':len(d['schedule']),'dedup_matched':sum(not x['d_equals_c'] for x in edits)}))
