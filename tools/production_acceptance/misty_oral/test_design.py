import unittest
from design import variant,schedule,gate
class DesignTests(unittest.TestCase):
 def test_baseline_identical(self):self.assertEqual(variant('original','A')['text'],'original')
 def test_schedule(self):
  s=schedule([str(i) for i in range(36)]);self.assertEqual(len(s),540);self.assertEqual(len({r['run_id'] for r in s}),540);self.assertEqual(s,schedule([str(i) for i in range(36)]))
 def test_unapproved_removal_rejected(self):
  with self.assertRaises(ValueError):variant('fact','D',[{'before':'fact','after':''}])
 def test_incomplete_capture_blocks_paid_calls(self):self.assertFalse(gate([],[])['passed'])
 def test_examples_not_in_baseline(self):self.assertNotIn('被窝',variant('original','B')['text']);self.assertIn('被窝',variant('original','C')['text'])
if __name__=='__main__':unittest.main()
