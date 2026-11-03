"""Versioned prompts and deterministic experiment schedule; no credentials."""
import hashlib,json,random
RULES='普通闲聊优先用一句或半句表达，只接一个具体点。可以省略主语和铺垫，不复述分析对方的话，不固定追加问题。没有具体想说的内容可以等待。明确问题、纠正和交办任务按实际需要完整回应；需要解释时不强行缩短。保持原有人格与事实边界。'
EXAMPLES=[
 ('这部电影我最喜欢配乐。','我更喜欢结尾突然安静下来的那一下。'),
 ('阴天拍照肯定不好看。','我倒喜欢阴天，光线软一点。'),
 ('我的闹钟今天又输给被窝了。','被窝这次准备得挺充分。'),
 ('终于把那首曲子练顺了，之前总卡在转调。','那个转调过了，后面应该顺手多了。'),
 ('好，那我先去忙。','好，你先忙。'),
 ('为什么逆光拍出来脸那么暗？说清楚一点。','相机把亮背景当成了曝光参考，脸就暗了。可以点脸测光，或者补一点光；背景可能会更亮，要看你想保留哪边的细节。')]
ECHO='接住此刻真正想说的那一点，普通闲聊一句就好；明确请求仍要回应完整。'
def variant(system,group,edits=()):
 if group not in 'ABCDE':raise ValueError('UNKNOWN_GROUP')
 text=system; applied=[]
 if group in 'DE':
  for edit in edits:
   if not edit.get('approved') or not edit.get('reason') or text.count(edit['before'])!=1:raise ValueError('UNVERIFIED_PROMPT_EDIT')
   text=text.replace(edit['before'],edit['after']);applied.append(edit)
 if group!='A':text+='\n\n'+RULES
 if group in 'CDE':text+='\n以下是语气示例，不是真实经历，不照抄台词：\n'+'\n'.join(f'对方：{a}\nMisty：{b}' for a,b in EXAMPLES)
 if group=='E':text+='\n'+ECHO
 return {'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),'edits':applied}
def schedule(ids,seed=281028):
 rng=random.Random(seed);rows=[]
 for repetition in range(3):
  ordered=list(ids);rng.shuffle(ordered)
  for case in ordered:
   groups=list('ABCDE');rng.shuffle(groups)
   rows.extend({'case_id':case,'group':g,'repetition':repetition,'run_id':f'{case}-{g}-{repetition}'} for g in groups)
 return rows

def gate(cases,checks):
 errors=[]
 if len(cases)!=36:errors.append('CASE_COUNT_NOT_36')
 if sum(c['kind'] in ('real','reconstructed') for c in cases)!=24:errors.append('REAL_COUNT_NOT_24')
 if sum(c['kind']=='synthetic' for c in cases)!=12:errors.append('SYNTHETIC_COUNT_NOT_12')
 if len({c['id'] for c in cases})!=len(cases):errors.append('DUPLICATE_CASE_ID')
 for c in cases:
  for field in ('category','scope','messages','header','source_sha256','projection_verified','tools_frozen'):
   if field not in c:errors.append(f"{c['id']}:MISSING_{field}")
  if not c.get('projection_verified'):errors.append(f"{c['id']}:PROJECTION_UNVERIFIED")
  if not c.get('tools_frozen'):errors.append(f"{c['id']}:TOOLS_NOT_FROZEN")
 if len(checks)!=6 or any(not x.get('passed') for x in checks):errors.append('SIX_CONTRACT_CASES_NOT_PASSED')
 return {'passed':not errors,'errors':errors,'formal_rounds':540 if not errors else 0}
