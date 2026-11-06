import unittest
from unittest.mock import patch

from MaiLivingWorld import public_news as news


UN = "https://news.un.org/zh/story/2026/11/1143147"
IT = "https://www.ithome.com/1/023/222.htm"


def feed(items):
    return ("<rss><channel><title>来源</title>" + "".join(
        f"<item><title>{title}</title><link>{url}</link><description>科学教育</description>"
        f"<pubDate>{date}</pubDate></item>" for title, url, date in items) + "</channel></rss>").encode()


class PublicNewsTests(unittest.TestCase):
    def test_search_preserves_two_sources_and_only_their_verified_urls(self):
        date = "Fri, 06 Nov 2026 05:00:00 GMT"
        feeds = {news._SOURCES["un"][2]: feed([("报告", UN.replace("/zh", "/feed/view/zh"), date),
                ("冒名", IT, date), ("跟踪", UN + "?foo=bar", date)]),
                 news._SOURCES["ithome"][2]: feed([("芯片", IT, date)])}
        with patch.object(news, "fetch_public", side_effect=lambda url, **kw: feeds[url]):
            result = news.news_search()
            self.assertEqual(set(i["url"] for i in result["items"]), {UN, IT})
            self.assertEqual(len(result["items"]), 2)
            self.assertEqual(news.news_search("科学 报告")["items"][0]["id"], "un:2026:11:1143147")
            self.assertEqual(news.news_search("missing")["unavailable_sources"], [])

    def test_read_without_search_extracts_verified_article_and_publication(self):
        article = f"""<link rel="canonical" href="{IT}"><h1>芯片测试</h1>
            <span id="pubtime_baidu">2026/11/6 13:11:24</span><p>头部</p>
            <div class="post_content" id="paragraph"><p>确有<strong>细节</strong>。</p>
            <aside><p>广告</p></aside><p>第二段</p></div><p>评论</p>"""
        with patch.object(news, "fetch_public", return_value=article.encode()) as fetch:
            result = news.news_read("ithome:1:023:222")
        self.assertEqual((result["basis"], result["text"]), ("article", "确有细节。\n第二段"))
        self.assertEqual(result["published_at"], "2026-11-06T05:11:24+00:00")
        self.assertEqual(fetch.call_args.args[0], IT)
        self.assertEqual(fetch.call_args.kwargs["allowed_hosts"], ("www.ithome.com",))

    def test_mismatched_canonical_and_unreadable_page_only_use_matching_feed(self):
        rss = feed([("原报道", UN, "Fri, 06 Nov 2026 05:00:00 GMT")])
        html = f'<link rel="canonical" href="{UN[:-1]}0"><h1>替代页</h1><div class="field--name-field-text-column"><p>冒名正文</p></div>'
        with patch.object(news, "fetch_public", side_effect=[html.encode(), rss]):
            self.assertEqual(news.news_read("un:2026:11:1143147")["basis"], "feed_summary")
        with patch.object(news, "fetch_public", side_effect=[news.MediaFetchError("offline"), rss]):
            self.assertEqual(news.news_read("un:2025:11:1143147")["basis"], "unavailable")

    def test_failed_source_reported_and_invalid_inputs_never_fetch(self):
        with patch.object(news, "fetch_public", side_effect=news.MediaFetchError("offline")) as fetch:
            for value in ("https://localhost", "un:2026:99:123456", "ithome:1:001:111?bad", "un:٢٠٢٦:11:1143147"):
                with self.assertRaisesRegex(ValueError, "invalid_news_id"):
                    news.news_read(value)
            for args in ((123, 1), ("", True), ("bad\nq", 1), ("x" * 81, 1)):
                with self.assertRaises(ValueError):
                    news.news_search(*args)
            fetch.assert_not_called()
            self.assertEqual(len(news.news_search()["unavailable_sources"]), 2)
