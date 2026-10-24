"""Offline regression checks for official reply packing and receipt schemas."""
import unittest
from types import SimpleNamespace
from harness.qq.decision import _prepared_group_segments, _validate_reply_segments, QQDraftError, QQReplyDecision, QQSegmentDeliveryReceipt
class Trace:
    def add_counter(self, *args): pass
class Tests(unittest.TestCase):
    def pack(self, text):
        return _prepared_group_segments(SimpleNamespace(text=text, segments=[text]), Trace(), reply_style="conversational", official=True)
    def test_many_paragraphs(self):
        for n in (1,3,4,5,6,30):
            text="\n\n".join("paragraph " + str(i) for i in range(n))
            segments, reply=self.pack(text)
            self.assertEqual(reply, text)
            self.assertLessEqual(len(segments),5)
            self.assertEqual(_validate_reply_segments(segments,reply),segments)
    def test_long_text(self):
        for text in ("文"*8000, "🐷"*6000, "line\n"*1500):
            segments,reply=self.pack(text)
            self.assertEqual("".join(segments),text)
            self.assertTrue(all(len(x.encode("utf-16-le"))//2<=3000 for x in segments))
            self.assertLessEqual(len(segments),5)
    def test_invalid(self):
        for text in (" ","x"*8001,'{"action":"speak","reply":"hello"}'):
            with self.assertRaises(QQDraftError):self.pack(text)
    def test_five_schema(self):
        self.assertEqual(QQReplyDecision.model_json_schema()["properties"]["reply_segments"]["maxItems"],5)
        self.assertEqual(QQSegmentDeliveryReceipt.model_json_schema()["properties"]["segment_index"]["maximum"],4)
    def test_legacy_unchanged(self):
        text="\n\n".join(["a"]*4)
        with self.assertRaises(QQDraftError):
            _prepared_group_segments(SimpleNamespace(text=text,segments=[text]),Trace(),reply_style="formal")
if __name__=="__main__":unittest.main()
