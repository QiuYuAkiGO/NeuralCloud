"""Consume human scores; never manufacture preferences or select from partial reviews."""
import argparse,json
from pathlib import Path
DIMS=['自然度','具体性','简洁度','角色一致性','回应适当性']
def evaluate(root,score_file):
 root=Path(root);keys=json.loads((root/'blind-key.json').read_text());packet=json.loads(Path(score_file).read_text(encoding='utf8'));scores=packet['scores'];simple=packet.get('mode')=='preference_only';by={g:[] for g in 'BCDE'};seen=set()
 for s in scores:
  ident=s['pair_id']
  if ident in seen or ident not in keys:raise ValueError('UNKNOWN_OR_DUPLICATE_PAIR')
  seen.add(ident);k=keys[ident];by[k['candidate_group']].append((s,k['candidate_side']))
 result={'human_review':'partial','winner':None,'mode':'preference_only' if simple else 'full_rubric','quality_checks':'not_assessed' if simple else 'scored','groups':{}}
 for g,rows in by.items():
  valid=[]
  for s,side in rows:
   ratings=s.get('ratings',{});p=s.get('preference')
   if p=='unevaluable':continue
   if p not in ('left','right','tie') or (not simple and any(not isinstance(ratings.get(x+d),(int,float)) or not 1<=ratings[x+d]<=5 for x in ('left','right') for d in DIMS)):continue
   valid.append((s,side))
  non_ties=[(s,side) for s,side in valid if s['preference']!='tie'];wins=sum(s['preference']==side for s,side in non_ties)
  diffs={d:sum(s['ratings'][side+d]-s['ratings'][('right' if side=='left' else 'left')+d] for s,side in valid)/len(valid) if valid and not simple else None for d in ['具体性','角色一致性']}
  cleared=bool(valid) and all(s.get('regressions_left')=='clear' and s.get('regressions_right')=='clear' for s,_ in valid)
  complete=len(rows)==36 and len(valid)+sum(s.get('preference')=='unevaluable' for s,_ in rows)==36
  rate=wins/len(non_ties) if non_ties else None
  result['groups'][g]={'reviewed_cases':len(valid),'non_ties':len(non_ties),'win_rate':rate,'mean_paired_score_difference':diffs,'regressions_cleared':cleared,'complete':complete,'eligible':complete and len(valid)>=24 and cleared and rate is not None and rate>=.6 and all(x is not None and x>=0 for x in diffs.values())}
 if all(v['complete'] for v in result['groups'].values()):
  result['human_review']='complete';eligible=[g for g in 'BCDE' if result['groups'][g]['eligible']]
  # Five percentage points is the declared practical tie band; prefer fewer prompt changes.
  if eligible:
   best=max(result['groups'][g]['win_rate'] for g in eligible);result['winner']=next(g for g in eligible if result['groups'][g]['win_rate']>=best-.05)
 return result
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('scores');a=p.parse_args();print(json.dumps(evaluate(a.root,a.scores),ensure_ascii=False,indent=2))
