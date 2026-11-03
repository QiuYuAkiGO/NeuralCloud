import json,tempfile,unittest
from pathlib import Path
from review import build
class ReviewTests(unittest.TestCase):
 def test_anonymous_pairs_and_escape(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);(r/'results').mkdir();(r/'cases.json').write_text(json.dumps([{'id':'synthetic-test','messages':[{'role':'user','source':{'kind':'user'},'content':[{'type':'text','text':'</script><script>alert(1)</script>'}]}]}]))
   for g in 'AB':
    for i in range(3):
     row={'case_id':'synthetic-test','group':g,'repetition':i,'status':'completed','actions':[{'name':'misty_send','args':{'text':'hello'}},{'name':'misty_wait','args':{}}],'missing_evidence':[]}
     (r/'results'/f'{g}{i}.json').write_text(json.dumps(row))
   s=build(r);pairs=json.loads((r/'blind-review.json').read_text(encoding='utf8'))
   self.assertEqual(s['paired_cases'],1);self.assertIsNone(s['winner']);self.assertEqual(s['human_review'],'pending')
   self.assertFalse(pairs[0]['trials'][0]['left']['wait']);self.assertNotIn('candidate_group',pairs[0]);self.assertEqual((r/'blind-review.html').read_text(encoding='utf8').count('</script>'),1)
if __name__=='__main__':unittest.main()
