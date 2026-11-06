from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from MaiLivingWorld import bilibili_media as bili
from MaiLivingWorld.media_http import MediaFetchError


VID = "BV17x411w7KC"
URL = "https://www.bilibili.com/video/" + VID


class ShareLinks(unittest.TestCase):
    def test_flattened_text_from_a_quoted_or_forwarded_message_is_not_a_new_share(self):
        for kind in ("reply", "forward"):
            event = {"processed_plain_text": "上次分享：" + URL, "raw_message": [
                {"type": kind, "data": {"text": URL}}, {"type": "text", "data": "还记得吗？"}]}
            self.assertEqual(bili.extract_bilibili_link(event), "")

    def test_part_links_direct_and_adapter_cards(self):
        link = URL + "/?p=2&share_source=test"
        card = {"meta": {"detail_1": {"qqdocurl": link}}}
        messages = [
            {"processed_plain_text": "这段呢？ " + link + "。"},
            {"raw_message": [{"type": "json", "data": {"data": json.dumps(card)}}]},
            {"message_segment": {"type": "seglist", "data": [{"type": "json", "data": json.dumps(card)}]}},
            {"message_info": {"additional_config": {"platform_card_payloads": [{"payload": card}]}}},
        ]
        for message in messages:
            self.assertEqual(bili.extract_bilibili_link(message), URL + "?p=2")

    def test_unrecognized_hosts_bad_parts_and_nested_forward_are_ignored(self):
        for text in ("https://evilmallb23.tv/abcde", "https://www.bilibili.com.evil/video/" + VID,
                     "https://evil.com/b23.tv/abcde", URL + "?p=0", URL + "?p=3&p=4"):
            self.assertEqual(bili.extract_bilibili_link({"processed_plain_text": text}), "")
        self.assertEqual(bili.extract_bilibili_link({"raw_message": [{"type": "forward", "data": URL}]}), "")
        self.assertEqual(bili.extract_bilibili_link({"processed_plain_text": "b23.tv/aBcDeF"}), "https://b23.tv/aBcDeF")


class Player(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_storyboard_keeps_actual_timestamp_and_subtitles_near_question(self):
        with patch.object(bili, "fetch_public", self.fetch):
            result = await bili.read_bilibili_link(URL, positions=[7], timeline=True)
        self.assertEqual(result["frame_times"], [4])
        self.assertEqual(len(result["frames"]), 1)
        with Image.open(BytesIO(result["frames"][0])) as frame:
            self.assertEqual(max(range(3), key=frame.getpixel((8, 8)).__getitem__), 1)
        body = {"body": [{"from": 0, "to": 2, "content": "开头"},
                         {"from": 27, "to": 30, "content": "目标片段"},
                         {"from": 55, "to": 58, "content": "后续片段"}]}
        text = bili._subtitles([{"subtitle_url": "//aisubtitle.hdslb.com/text"}],
                              lambda *args: json.dumps(body).encode(), positions=[29], timeline=True)
        self.assertEqual(text, "27.0–30.0秒 目标片段")
        self.assertEqual(result["subtitle_excerpt"], "")  # Untimed text cannot answer a precise time question.

    async def test_invalid_storyboard_timestamps_never_label_pixels(self):
        for stamps in ([0, float("nan"), 8, 12], [0, 8, 4, 12], ["0", 4, 8, 12]):
            self.data["/x/player/videoshot"]["index"] = stamps
            with patch.object(bili, "fetch_public", self.fetch):
                result = await bili.read_bilibili_link(URL, timeline=True)
            self.assertEqual(result["frames"], [])

    async def test_audio_download_is_optional_and_public_subtitles_avoid_asr_download(self):
        self.data["/x/player/playurl"] = {"durl": [{"url": "https://upos.bilivideo.com/file", "size": 11}]}
        with patch.object(bili, "fetch_public", self.fetch):
            subtitled = await bili.read_bilibili_link(URL, timeline=True, include_audio=True)
            self.assertNotIn("audio_bytes", subtitled)
            self.data["/x/player/v2"] = {}
            quiet = await bili.read_bilibili_link(URL, timeline=True)
            self.assertNotIn("audio_bytes", quiet)
            voiced = await bili.read_bilibili_link(URL, timeline=True, include_audio=True)
            self.assertEqual(voiced["audio_bytes"], b"video bytes")
            self.assertEqual(len(voiced["frames"]), 3)

    def setUp(self):
        self.hits = []
        self.data = {
            "/x/web-interface/view": {"bvid": VID, "title": "公开演示", "desc": "简介",
                "pages": [{"page": 1, "cid": 11, "part": "甲"}, {"page": 2, "cid": 22, "part": "乙"}]},
            "/x/player/videoshot": {"image": ["//i0.hdslb.com/bfs/videoshot/x.jpg"], "index": [0, 4, 8, 12],
                "img_x_len": 3, "img_y_len": 1, "img_x_size": 16, "img_y_size": 16},
            "/x/player/v2": {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/text"}]}},
            "/x/player/playurl": {"durl": []},
        }
        self.image = BytesIO()
        with Image.new("RGB", (48, 16), "blue") as sheet:
            sheet.paste((240, 0, 0), (0, 0, 16, 16))
            sheet.paste((0, 240, 0), (16, 0, 32, 16))
            sheet.save(self.image, "PNG")

    def fetch(self, url, *, allowed_hosts, max_bytes, timeout, referer):
        parts = urlsplit(url)
        self.hits.append((parts.hostname, parts.path, parse_qs(parts.query)))
        if parts.hostname == "api.bilibili.com":
            self.assertEqual(allowed_hosts, (parts.hostname,))
            record = self.data[parts.path]
            if isinstance(record, Exception):
                raise record
            return json.dumps({"code": 0, "data": record}).encode()
        if parts.hostname == "aisubtitle.hdslb.com":
            return json.dumps({"body": [{"content": "开头"}, {"content": "中间"}, {"content": "结尾"}]}).encode()
        if parts.hostname == "upos.bilivideo.com":
            self.assertIn(".bilivideo.com", allowed_hosts)
            return b"video bytes"
        self.assertIn(parts.hostname, allowed_hosts)
        return self.image.getvalue()

    async def read(self, url=URL):
        with patch.object(bili, "fetch_public", self.fetch):
            return await bili.read_bilibili_link(url)

    async def test_real_sprite_crops_and_subtitles_belong_to_requested_part(self):
        result = await self.read(URL + "?p=2")
        self.assertEqual(result["canonical_url"], URL + "?p=2")
        self.assertEqual(result["basis"], "storyboard")
        self.assertIn("乙", result["title"])
        self.assertEqual(result["subtitle_excerpt"], "开头 … 中间 … 结尾")
        pixels = []
        for jpeg in result["frames"]:
            with Image.open(BytesIO(jpeg)) as frame:
                self.assertEqual(frame.size, (16, 16))
                pixels.append(frame.getpixel((8, 8)))
        self.assertEqual([max(range(3), key=rgb.__getitem__) for rgb in pixels], [0, 1, 2])
        self.assertTrue(all(args["cid"] == ["22"] for _, path, args in self.hits if path.startswith("/x/player/")))

    async def test_access_restricted_or_invalid_part_cannot_request_images(self):
        for n, url in enumerate((URL + "?p=3", URL)):
            self.hits.clear()
            if n:
                self.data["/x/web-interface/view"]["rights"] = {"pay": 1}
            result = await self.read(url)
            self.assertFalse(result["frames"])
            self.assertEqual(result["basis"], "metadata" if n else "unavailable")
            self.assertEqual(len(self.hits), 1)

    async def test_missing_or_unusable_frames_and_no_subtitles_are_metadata_only(self):
        self.data["/x/player/videoshot"]["img_x_size"] = 640  # A mislabelled sheet cannot yield valid frames.
        self.data["/x/player/playurl"] = {"is_preview": 1, "durl": [{"url": "https://upos.bilivideo.com/file", "size": 4}]}
        self.data["/x/player/v2"] = MediaFetchError("unavailable")
        result = await self.read()
        self.assertEqual((result["basis"], result["frames"], result["subtitle_excerpt"]), ("metadata", [], ""))
        self.assertFalse(any(host == "upos.bilivideo.com" for host, _, _ in self.hits))
        self.data["/x/player/v2"] = {"subtitle": {"subtitles": [{"subtitle_url": "//aisubtitle.hdslb.com/text"}]}}
        result = await self.read()
        self.assertEqual(result["basis"], "subtitles")
        self.assertFalse(result["frames"])

    async def test_short_link_rejects_foreign_landing_and_keeps_part(self):
        with patch.object(bili, "public_redirect", return_value="https://example.com/video/" + VID):
            self.assertEqual((await self.read("https://b23.tv/short"))["basis"], "unavailable")
            self.assertEqual(self.hits, [])
        with patch.object(bili, "public_redirect", return_value=URL + "/?p=2&click=untrusted"):
            self.assertEqual((await self.read("https://b23.tv/short"))["canonical_url"], URL + "?p=2")

    async def test_no_thumbnail_falls_back_to_verified_mp4_or_original_metadata(self):
        self.data["/x/player/videoshot"] = {}
        self.data["/x/player/playurl"] = {"durl": [{"url": "https://upos.bilivideo.com/file", "size": 11}]}
        self.data["/x/player/v2"] = {}
        with patch("MaiLivingWorld.chat_media.sample_video", return_value=[b"first", b"last"]) as sample:
            result = await self.read()
        sample.assert_awaited_once_with(b"video bytes")
        self.assertEqual((result["basis"], result["frames"]), ("sampled_video", [b"first", b"last"]))
        with patch("MaiLivingWorld.chat_media.sample_video", side_effect=ValueError("decoder rejected")):
            self.assertEqual((await self.read())["basis"], "metadata")
