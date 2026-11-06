import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from MaiLivingWorld import public_web_search as web
from MaiLivingWorld.media_http import MediaFetchError


URL = "https://www.example.org/news/orbits"
TITLE = "Orbital observation"


def results(*rows):
    return json.dumps({"type": "search", "web": {"results": list(rows)}}).encode()


def row(url=URL):
    return {"url": url, "title": TITLE, "description": "An <strong>orbit</strong> &amp; more"}


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(web.os.environ, {"MLW_BRAVE_SEARCH_KEY": "test-only-token"})
        env.start()
        self.addCleanup(env.stop)
        with web._LOCK:
            web._SEEN.clear()

    def issue(self):
        with patch.object(web, "fetch_brave_api", return_value=results(row())):
            item, = web.web_search("月球")["items"]
        return item["id"]

    def test_registered_search_uses_fixed_authenticated_api_and_bounded_results(self):
        rows = [
            row("https://127.0.0.1/private"), row("https://user:pass@example.org/"), row(URL+"?token=secret"),
            row(URL+"?utm_source=post#anchor"), row(URL),
        ]
        with patch.object(web, "fetch_brave_api", return_value=results(*rows)) as api:
            item, = web.web_search("轨道力学")["items"]
        url = urlsplit(api.call_args.args[0])
        self.assertEqual((url.scheme, url.hostname, url.path), ("https", "api.search.brave.com", "/res/v1/web/search"))
        self.assertEqual(parse_qs(url.query)["q"], ["轨道力学"])
        self.assertEqual(api.call_args.kwargs["token"], "test-only-token")
        self.assertNotIn("test-only-token", str(item) + url.query)
        self.assertEqual((item["url"], item["summary"], item["basis"]), (URL, "An orbit & more", "search_snippet"))
        self.assertRegex(item["id"], r"^web_[0-9a-f]{32}$")

    def test_only_recent_handles_open_public_html_without_search_key(self):
        ident = self.issue()
        paragraph = "A measured orbit with visible evidence. " * 4
        body = f"<title>New article title</title><body><p>floating ad</p><article><p>{paragraph}<script>inject()</script></p></article></body>"
        with patch.object(web, "fetch_public", return_value=body.encode()) as page:
            first, again = web.web_read(ident), web.web_read(ident)
        self.assertEqual(first, again)
        self.assertEqual(first["title"], "New article title")
        self.assertEqual(first["text"], paragraph.strip())
        self.assertEqual(first["basis"], "web_excerpt")
        page.assert_called_with(URL, allowed_hosts=("www.example.org",), max_bytes=768 * 1024,
                                timeout=8, allow_redirects=False)
        self.assertNotIn("test-only-token", str(page.call_args))

    def test_missing_key_private_chat_urls_forgery_and_expiry(self):
        with patch.dict(web.os.environ, {"MLW_BRAVE_SEARCH_KEY": ""}), patch.object(web, "fetch_brave_api") as api:
            with self.assertRaisesRegex(ValueError, "^web_search_key_missing$"):
                web.web_search("Moon")
            api.assert_not_called()
        with patch.object(web, "fetch_brave_api") as api:
            for query in ("https://localhost", "我的手机号15000000000", "a@b.com", "\nC++"):
                with self.subTest(query=query), self.assertRaisesRegex(ValueError, "^web_invalid_query$"):
                    web.web_search(query)
            api.assert_not_called()
        with patch.object(web, "fetch_public") as page:
            for key in (URL, "web_" + "a" * 32, ""):
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "^web_(invalid_id|result_expired)$"):
                    web.web_read(key)
            with patch.object(web.time, "monotonic", return_value=25):
                key = self.issue()
            with patch.object(web.time, "monotonic", return_value=25+web._TTL):
                with self.assertRaisesRegex(ValueError, "^web_result_expired$"):
                    web.web_read(key)
            page.assert_not_called()

    def test_private_dns_redirect_changed_site_binary_and_interstitial_are_unreadable(self):
        key = self.issue()
        for code in ("non_public_address", "bad_redirect"):
            with patch.object(web, "fetch_public", side_effect=MediaFetchError(code)):
                with self.assertRaisesRegex(ValueError, "^web_read_unavailable$"):
                    web.web_read(key)
        for raw in (b"\xff\xd8\xff", b"<body><nav><p>nav</p></nav></body>",
                    ("<title>Just a moment</title><p>" + "challenge " * 30 + "</p>").encode()):
            with self.subTest(raw=raw[:12]), patch.object(web, "fetch_public", return_value=raw):
                with self.assertRaisesRegex(ValueError, "^web_unreadable$"):
                    web.web_read(key)

    def test_empty_brave_results_distinguished_from_bad_transport(self):
        with patch.object(web, "fetch_brave_api", return_value=b'{"type":"search"}'):
            self.assertEqual(web.web_search("Moon"), {"items": []})
        with patch.object(web, "fetch_brave_api", return_value=b'{"error":"secret"}'):
            with self.assertRaisesRegex(ValueError, "^web_invalid_response$"):
                web.web_search("Moon")
        with patch.object(web, "fetch_brave_api", side_effect=MediaFetchError("secret")):
            with self.assertRaisesRegex(ValueError, "^web_search_unavailable$"):
                web.web_search("Moon")
