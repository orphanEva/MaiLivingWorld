import json
import os
import unittest
from unittest.mock import MagicMock, patch

from MaiLivingWorld import mastodon_api as api


ENV = {"MLW_MASTODON_INSTANCE": "https://social.example", "MLW_MASTODON_TOKEN": "example-test-token"}
ME = {"id": "1", "acct": "samplebot", "note": "", "source": {"privacy": "private"}}
AUTHOR = {"id": "2", "acct": "reader@remote.example", "note": ""}
KEY = "idempotent-for-target-3"


def post(ident="3", author=AUTHOR, **extra):
    return {"id": ident, "account": author, "visibility": "public", "content": "<p>你好<br>朋友</p>",
            "url": "https://untrusted.example/?q=private", "in_reply_to_id": None,
            "mentions": [{"id": "1"}], "created_at": "2026-01-01T00:00:00Z", **extra}


@patch.dict(os.environ, ENV)
class MastodonTests(unittest.TestCase):
    def test_search_only_public_original_posts_on_configured_origin(self):
        data = {"statuses": [post(), post(visibility="private"), post(reblog={"id": "44"}), post(visibility="direct")]}
        with patch.object(api, "_request", return_value=data) as request:
            result = api.mastodon_search("科学", 9)
        self.assertEqual([p["id"] for p in result["items"]], ["3"])
        self.assertIn("resolve=false", request.call_args.args[1])
        self.assertEqual(request.call_args.args[0][0], "social.example")
        self.assertNotIn("untrusted", str(result))
        self.assertEqual(result["items"][0]["text"], "你好 朋友")

    def test_notifications_reference_actual_addressed_status_not_notification_id(self):
        events = [{"id": "300", "type": "mention", "account": AUTHOR, "status": post()},
                  {"id": "301", "type": "mention", "account": AUTHOR, "status": post("4", mentions=[])},
                  {"id": "302", "type": "mention", "account": AUTHOR, "status": post("5", visibility="direct")},
                  {"id": "303", "type": "mention", "account": ME, "status": post("6", author=ME)},
                  {"id": "304", "type": "favourite", "account": AUTHOR, "status": post("7")},
                  {"id": "305", "type": "mention", "account": ME, "status": post("8")},
                  {"id": "306", "type": "mention", "account": AUTHOR,
                   "status": post("9", mentions=[], in_reply_to_account_id="1")}]
        with patch.object(api, "_request", side_effect=[ME, events]):
            items = api.mastodon_mentions(limit=10)["items"]
        self.assertEqual([(i["id"], i["notification_id"]) for i in items], [("3", "300"), ("9", "306")])

    def test_verified_reply_binds_account_target_and_key_without_retries(self):
        reply = post("10", author=ME, in_reply_to_id="3")
        with patch.object(api, "_request", side_effect=[ME, post(), reply]) as request:
            result = api.mastodon_reply("3", "谢谢", KEY)
        self.assertEqual(result["id"], "10")
        self.assertEqual(result["in_reply_to_id"], "3")
        self.assertEqual(request.call_args.kwargs["fields"],
                         {"in_reply_to_id": "3", "status": "@reader@remote.example 谢谢", "visibility": "public"})
        self.assertEqual(request.call_args.kwargs["key"], KEY)
        for bad in (post("10", in_reply_to_id="3"), post("10", author=ME, in_reply_to_id="4"),
                    post("10", author=ME, in_reply_to_id="3", visibility="private"), {}):
            with patch.object(api, "_request", side_effect=[ME, post(), bad]) as request:
                with self.assertRaisesRegex(api.MastodonError, "mastodon_reply_uncertain"):
                    api.mastodon_reply("3", "谢谢", KEY)
                self.assertEqual(request.call_count, 3)

    def test_extra_recipients_nonpublic_and_self_posts_cannot_receive_writes(self):
        with patch.object(api, "_request") as request:
            with self.assertRaisesRegex(api.MastodonError, "extra_recipients"):
                api.mastodon_reply("3", "@stranger 看看", KEY)
            request.assert_not_called()
        for status, text, code in ((post(author=ME), "嗨", "self_reply"),
                                   (post(visibility="unlisted"), "嗨", "not_public")):
            with patch.object(api, "_request", side_effect=[ME, status]) as request:
                with self.assertRaisesRegex(api.MastodonError, code):
                    api.mastodon_reply("3", text, KEY)
                self.assertEqual(request.call_count, 2)

    def test_preflight_rechecks_edited_post_and_public_account_bio_for_bot_opt_out(self):
        for updated in (post(content="x" * 4300 + " <a>#<span>noBot</span></a>"),
                        post(author={**AUTHOR, "note": "<p>＃ＮｏＢｏｔｓ</p>"}),
                        post(spoiler_text="不要机器人回复"), post(author={**AUTHOR, "note": "機器人請勿回覆"}),
                        post(author={"id": "2", "acct": "unknown_profile"})):
            with self.subTest(updated=updated), patch.object(api, "_request", side_effect=[post(), ME, updated]) as request:
                self.assertEqual(api.mastodon_read("3")["id"], "3")
                with self.assertRaisesRegex(api.MastodonError, "^mastodon_rejected$"):
                    api.mastodon_reply("3", "我也对这个问题感兴趣", KEY)
                self.assertEqual(request.call_count, 3)
                self.assertEqual(request.call_args.args[1], "/api/v1/statuses/3")
                self.assertTrue(all("fields" not in call.kwargs for call in request.call_args_list))

    def test_invalid_id_or_origin_never_sends_credentials(self):
        with patch.object(api, "_connect") as connect:
            for value in ("../timelines/home", "0", "٣", "https://third.example/3"):
                with self.assertRaisesRegex(api.MastodonError, "invalid_mastodon_id"):
                    api.mastodon_read(value)
            for origin in ("http://social.example", "https://social.example/path", "https://u@social.example",
                           "https://social.example:80", "https://localhost", "https://social.example\n"):
                with patch.dict(os.environ, {"MLW_MASTODON_INSTANCE": origin}):
                    with self.assertRaisesRegex(api.MastodonError, "mastodon_invalid_config"):
                        api.mastodon_identity()
            connect.assert_not_called()

    def test_transport_rejects_redirects_gzip_truncation_and_post_uncertainty(self):
        for status, encoding, length, writing, code in (
                (301, "identity", None, False, "mastodon_unavailable"),
                (302, "identity", None, True, "mastodon_reply_uncertain"),
                (500, "identity", None, True, "mastodon_reply_uncertain"),
                (401, "identity", None, True, "mastodon_unauthorized"),
                (403, "identity", None, True, "mastodon_forbidden"),
                (429, "identity", None, False, "mastodon_rate_limited"),
                (200, "gzip", None, False, "mastodon_unavailable"),
                (200, "identity", "5000000", False, "mastodon_unavailable"),
                (200, "identity", "200", False, "mastodon_unavailable")):
            resp = MagicMock()
            resp.status = status
            resp.read.return_value = json.dumps(ME).encode()
            resp.getheader.side_effect = lambda name, default=None: encoding if name == "Content-Encoding" else length
            with patch.object(api, "_connect") as connect, patch.object(api.http.client, "HTTPConnection") as factory:
                conn = factory.return_value
                conn.getresponse.return_value.__enter__.return_value = resp
                with self.assertRaisesRegex(api.MastodonError, code):
                    api._request(api._config(), "/api/v1/statuses", fields={"status": "test"} if writing else None,
                                 key=KEY if writing else None)
                self.assertEqual((connect.call_count, conn.request.call_count), (1, 1))
                self.assertEqual(conn.request.call_args.kwargs["headers"]["Authorization"], "Bearer example-test-token")
