import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from MaiLivingWorld import public_reference as wiki
from MaiLivingWorld.media_http import MediaFetchError


STAMP = "2026-05-31T14:00:00Z"


def entry(**updates):
    return {"pageid": 42, "ns": 0, "title": "月球", "lastrevid": 17,
            "revisions": [{"revid": 17, "timestamp": STAMP}], "extract": "月球的表面。" * 300, **updates}


def response(data):
    return json.dumps({"query": data}, ensure_ascii=False).encode()


class PublicReferenceTests(unittest.TestCase):
    def test_search_is_scoped_to_articles_and_escapes_source_and_terms(self):
        source = {"ns": 0, "pageid": 42, "title": "Moon #1", "timestamp": STAMP,
                  "snippet": 'A <span class="searchmatch">moon</span> &amp; sky'}
        with patch.object(wiki, "fetch_public", return_value=response({"search": [source]})) as http:
            item, = wiki.reference_search("Moon + sky", language="en", limit=1)["items"]
        url = urlsplit(http.call_args.args[0])
        self.assertEqual((url.hostname, url.path, http.call_args.kwargs["allowed_hosts"]),
                         ("en.wikipedia.org", "/w/api.php", ("en.wikipedia.org",)))
        query = parse_qs(url.query)
        self.assertEqual(query["srsearch"], ["Moon + sky"])
        self.assertEqual(query["srnamespace"], ["0"])
        self.assertEqual((item["basis"], item["summary"], item["page_id"]),
                         ("search_snippet", "A moon & sky", 42))
        self.assertEqual(item["url"], "https://en.wikipedia.org/wiki/Moon_%231")

    def test_only_public_topic_arguments_can_leave_the_process(self):
        with patch.object(wiki, "fetch_public") as http:
            for value in ("我的QQ号是123456", "https://example.com", "name@example.org", "my phone number",
                          "如果你愿意，帮我查这个", "Moon\r\nX-Token", "a"*81, "", 123):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    wiki.reference_search(value)
            for args in ((42, "fr"), ("42", "en"), (True, "zh"), (-1, "zh"), (42, "zh", 12001)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    wiki.reference_read(*args)
            with self.assertRaises(ValueError):
                wiki.reference_search("月球", "evil.org")
            with self.assertRaises(ValueError):
                wiki.reference_search("月球", limit=True)
            http.assert_not_called()

    def test_read_in_two_bounded_excerpts_with_public_revision(self):
        article = entry()
        with patch.object(wiki, "fetch_public", return_value=response({"pages": [article]})) as http:
            first = wiki.reference_read(42)
            second = wiki.reference_read(42, offset=first["next_offset"])
        request = parse_qs(urlsplit(http.call_args.args[0]).query)
        self.assertEqual(request["pageids"], ["42"])
        self.assertNotIn("redirects", request)
        self.assertEqual(len(first["text"]), 1200)
        self.assertEqual(first["text"] + second["text"], article["extract"])
        self.assertTrue(first["has_more_text"])
        self.assertFalse(second["has_more_text"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(first["revision_url"], "https://zh.wikipedia.org/wiki/Special:PermanentLink/17")
        self.assertEqual(first["updated_at"], STAMP)

    def test_page_over_the_local_window_is_labeled_incomplete(self):
        with patch.object(wiki, "fetch_public", return_value=response({"pages": [entry(extract="测" * 20_000)]})):
            last = wiki.reference_read(42, offset=12000)
        self.assertEqual(len(last["text"]), 1200)
        self.assertTrue(last["has_more_text"])
        self.assertIsNone(last["next_offset"])

    def test_missing_redirect_wrong_id_and_revision_race_cannot_be_attributed(self):
        cases = [
            ({"pages": [{"missing": True}]}, "reference_page_missing"),
            ({"pages": [entry(pageid=43)]}, "reference_page_changed"),
            ({"pages": [entry(redirect=True)]}, "reference_page_changed"),
            ({"redirects": [{"from": "A", "to": "B"}], "pages": [entry()]}, "reference_page_changed"),
            ({"pages": [entry(lastrevid=18)]}, "reference_page_changed"),
            ({"pages": [entry(extract="")]}, "reference_no_excerpt"),
            ({"pages": [entry(ns=4)]}, "reference_invalid_response"),
        ]
        for data, code in cases:
            with self.subTest(code=code), patch.object(wiki, "fetch_public", return_value=response(data)):
                with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                    wiki.reference_read(42)

    def test_network_denial_empty_search_and_decode_errors_stay_distinct(self):
        with patch.object(wiki, "fetch_public", side_effect=MediaFetchError("non_public_address")):
            with self.assertRaisesRegex(ValueError, "^reference_unavailable$"):
                wiki.reference_search("月球")
        with patch.object(wiki, "fetch_public", return_value=response({"search": []})):
            self.assertEqual(wiki.reference_search("月球"), {"items": []})
        for raw in (b"!DOCTYPE html", b'{"error":{"info":"upstream secret"}}'):
            with patch.object(wiki, "fetch_public", return_value=raw):
                with self.assertRaisesRegex(ValueError, "^reference_(invalid_response|unavailable)$"):
                    wiki.reference_search("月球")
