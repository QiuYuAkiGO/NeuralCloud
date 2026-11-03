import json,tempfile,unittest
from pathlib import Path
from score import evaluate,DIMS
class ScoreTests(unittest.TestCase):
 def test_partial_never_wins(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);(r/'blind-key.json').write_text(json.dumps({'p':{'candidate_group':'B','candidate_side':'left'}}));(r/'scores.json').write_text(json.dumps({'scores':[{'pair_id':'p','preference':'left','ratings':{s+x:5 for s in ['left','right'] for x in DIMS},'regressions_left':'clear','regressions_right':'clear'}]}));self.assertIsNone(evaluate(r,r/'scores.json')['winner'])
 def test_duplicate_review_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);(r/'blind-key.json').write_text(json.dumps({'p':{'candidate_group':'B','candidate_side':'left'}}));(r/'scores.json').write_text(json.dumps({'scores':[{'pair_id':'p'},{'pair_id':'p'}]}))
   with self.assertRaises(ValueError):evaluate(r,r/'scores.json')
if __name__=='__main__':unittest.main()
