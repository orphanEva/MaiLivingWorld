from __future__ import annotations
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.bilibili import parse_popular
from MaiLivingWorld.explore import Story, parse_feed, choose_story, valid_feed_host
from MaiLivingWorld.policy import is_quiet,parse_decision
from MaiLivingWorld.store import Store


class CoreTests(unittest.TestCase):
    def test_parse_bilibili_metadata_only(self):
        payload={"code":0,"data":{"list":[
          {"bvid":"BV1234567890","title":"示例热门标题","desc":"测试视频简介","pubdate":1740000000},
          {"bvid":"not_a_bvid","title":"bad","desc":"wrong"}]}}
        out=parse_popular(json.dumps(payload).encode())
        self.assertEqual(len(out),1)
        self.assertEqual(out[0].url,"https://www.bilibili.com/video/BV1234567890")
        self.assertEqual(out[0].summary,"测试视频简介")
        self.assertEqual(out[0].source,"B站热门")

    def test_parse_bilibili_api_failure(self):
        with self.assertRaisesRegex(ValueError,"风控"):
            parse_popular(b'{"code":-412,"message":"risk"}')

    def test_rss_parsing(self):
        xml=b'<rss version="2.0"><channel><title>A</title><item><title>News 1</title><link>https://example.org/a</link><description>Hello &amp; welcome</description><pubDate>Tue, 27 Oct 2026 12:00:00 +0000</pubDate></item></channel></rss>'
        rows=parse_feed(xml,"https://rsshub.app/xiaoheihe/news")
        self.assertEqual(rows[0].title,"News 1")
        self.assertEqual(rows[0].summary,"Hello & welcome")
        self.assertIsNotNone(rows[0].published_at)

    def test_reject_unsafe_feed(self):
        for u in ["http://rsshub.app/test","https://localhost/a","https://rsshub.app:444/a","https://u:p@rsshub.app/x"]:
            self.assertFalse(valid_feed_host(u,["rsshub.app"]))
        self.assertTrue(valid_feed_host("https://rsshub.app/a",["rsshub.app"]))

    def test_choose_exact_key(self):
        s=Story("abcd","test","title","https://a.com/","desc")
        self.assertIsNone(choose_story('{"share":true,"key":"wrong"}',[s]))
        self.assertEqual(choose_story('{"share":true,"key":"abcd"}',[s]),s)
        self.assertIsNone(choose_story('{"share":false,"key":"abcd"}',[s]))

    def test_gate_and_quiet(self):
        self.assertEqual(parse_decision('{"reach_out":true,"topic":"聊聊昨天的bug"}'),(True,"聊聊昨天的bug"))
        self.assertFalse(parse_decision('{"reach_out":false,"topic":""}')[0])
        now=datetime(2026,10,28,1,0,tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(is_quiet(now,"23:00","09:00"))
        self.assertFalse(is_quiet(now,"09:00","23:00"))

    def test_storage_dedup_budget_restart(self):
        with tempfile.TemporaryDirectory() as d:
            filename=Path(d)/"events.db"
            s=Store(filename)
            news=Story("story1","b站","一个新闻","https://www.bilibili.com/video/BV1234567890","摘要",time.time())
            self.assertEqual(s.save_stories([news,news]),1)
            self.assertEqual(len(s.candidates("qq:private:123",time.time(),168)),1)
            now=time.time()
            self.assertEqual(s.may_contact("qq:private:123",now,now-2000,3600,2),(True,"ok"))
            s.record_attempt("qq:private:123",now,"explore","story1")
            self.assertEqual(len(s.candidates("qq:private:123",now,168)),0)
            self.assertEqual(len(s.candidates("qq:private:456",now,168)),1)
            self.assertEqual(s.may_contact("qq:private:123",now+1,now-2000,3600,2),(False,"cooldown"))
            s.record_attempt("qq:private:123",now+3601,"history",None)
            self.assertEqual(s.may_contact("qq:private:123",now+7200,now-2000,3600,2),(False,"daily_limit"))
            s.mark_memory_saved("story1")
            s.close()
            s2=Store(filename)
            self.assertEqual(s2.statistics(),{"stories":1,"wake_attempts":2,"memory_saved":1})
            self.assertEqual(len(s2.candidates("qq:private:123",now,168)),0)
            s2.close()

if __name__ == '__main__':
    unittest.main()
