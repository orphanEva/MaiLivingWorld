"""QQ adapter / minimum host wire shapes, media origin checks and real offline decoding."""
import asyncio
import base64
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from MaiLivingWorld import chat_media as media


def event(parts, *, mid="42", group=None):
    return {"platform": "qq", "message_id": mid, "account_id": "99999999", "session_id": "synthetic",
            "message_info": {"user_info": {"user_id": "12345678"},
                             "group_info": {"group_id": group} if group else None}, "raw_message": parts}


def png():
    output = BytesIO()
    Image.new("RGB", (1200, 600), (20, 140, 180)).save(output, "PNG")
    return output.getvalue()


class QQAttachments(unittest.IsolatedAsyncioTestCase):
    def test_host_reply_uses_only_direct_structured_target_and_optional_sender(self):
        reply = {"type": "reply", "data": {"target_message_id": "-85", "target_message_sender_id": "34567890",
                 "target_message_content": "[CQ:reply,id=91]", "target_message_sender_nickname": "98765432"}}
        ask = {"type": "text", "data": "这张图里写的是什么？"}
        self.assertEqual(media.quoted_media_target(event([reply, ask], group="23456789")), ("-85", "34567890"))
        del reply["data"]["target_message_sender_id"]
        self.assertEqual(media.quoted_media_target(event([reply, ask])), ("-85", ""))

    def test_forged_nested_ambiguous_and_malformed_quotes_have_no_target(self):
        reply = {"type": "reply", "data": {"target_message_id": "85"}}
        for parts in (
            [{"type": "text", "data": "[CQ:reply,id=85]"}], [{"type": "forward", "data": [reply]}],
            [reply, reply], [{"type": "reply", "data": "85"}],
            *([{"type": "reply", "data": {"target_message_id": bad}}] for bad in ("0", "４２", "42", {}, "85\n91")),
        ):
            with self.subTest(parts=parts):
                self.assertIsNone(media.quoted_media_target(event(parts)))
        self.assertIsNone(media.quoted_media_target({**event([reply]), "platform": "other"}))

    async def test_adapter_inline_image_and_file_forward_only_sanitized_pixels(self):
        data = base64.b64encode(png()).decode()
        msg = event([{"type": "image", "data": "", "hash": "from-adapter", "binary_data_base64": data},
                     {"type": "file", "data": {"mime_type": "image/png", "base64": data}},
                     {"type": "forward", "data": [{"type": "image", "binary_data_base64": data}]}])
        items = media.extract_attachments(msg)
        self.assertEqual([item.kind for item in items], ["image", "image"])
        for item in items:
            api = AsyncMock(side_effect=AssertionError)
            result = await media.load_attachment(item, api_call=api)
            api.assert_not_awaited()
            with Image.open(BytesIO(result[0])) as image:
                self.assertEqual((image.format, image.size), ("JPEG", (768, 384)))

    async def test_video_placeholder_looks_up_exact_group_and_signed_id(self):
        url = "http://multimedia.nt.qq.com.cn/download?test=test"
        details = {"message_id": -42, "sender": {"user_id": 12345678}, "self_id": 99999999,
                   "group_id": 22222222, "message_type": "group",
                   "message": [{"type": "video", "data": {"url": url, "file": "/private/path", "file_size": "100"}}]}
        api = AsyncMock(return_value={"success": True, "result": details})
        item, = media.extract_attachments(event([{"type": "text", "data": "[视频] 文件: opaque，大小: 100"}],
                                               mid="-42", group="22222222"))
        with patch.object(media, "fetch_public", return_value=b"verified") as http, \
                patch.object(media, "sample_video", new_callable=AsyncMock, return_value=[b"frame"]):
            self.assertEqual(await media.load_attachment(item, api_call=api), [b"frame"])
            self.assertEqual(http.call_args.args[0], url.replace("http:", "https:"))
        api.assert_awaited_once_with("adapter.napcat.action.call_data", action_name="get_msg", params={"message_id": -42})
        for wrong in ({"group_id": 22222223}, {"message_id": -43}, {"sender": {"user_id": 22222223}},
                      {"message_type": "private"}, {"self_id": 99999998}, {"self_id": None}, {"user_id": 33333333}):
            api.return_value = {"success": True, "result": {**details, **wrong}}
            with self.assertRaisesRegex(media.ChatMediaError, "attachment_unavailable"):
                await media.load_attachment(item, api_call=api)

    async def test_text_lookalike_and_untrusted_urls_never_produce_frames(self):
        item, = media.extract_attachments(event([{"type": "text", "data": "[视频]"}]))
        detail = {"message_id": "42", "message_type": "private", "sender": {"user_id": "12345678"}, "self_id": 99999999}
        api = AsyncMock()
        unsafe = ["/var/run/secret", "file:///tmp/a", "https://evil.qq.com/a", "https://qpic.cn.evil.test/a",
                  "https://user@multimedia.nt.qq.com.cn/a", "https://multimedia.nt.qq.com.cn:4000/a"]
        cases = [[{"type": "video", "data": {"url": url}}] for url in unsafe] + [[{"type": "text", "data": "[视频]"}]]
        with patch.object(media, "fetch_public", side_effect=AssertionError) as http:
            for parts in cases:
                api.return_value = {"success": True, "result": {**detail, "message": parts}}
                with self.assertRaises(media.ChatMediaError):
                    await media.load_attachment(item, api_call=api)
            http.assert_not_called()
        api.assert_awaited_with("adapter.napcat.message.get_msg", message_id=42)

    async def test_invalid_image_bodies_and_decoder_input_are_rejected(self):
        with self.assertRaises(media.ChatMediaError):
            await media.load_attachment(media.Attachment("image", "base64", "%%%"))
        with self.assertRaises(media.ChatMediaError):
            await media.load_attachment(media.Attachment("image", "base64", "YWJj"))
        with self.assertRaises(media.ChatMediaError):
            await media.sample_video(b"#EXTM3U\nfile:///etc/passwd")
        msg = event([{"type": "emoji", "data": {"base64": "YWJj"}}])
        self.assertEqual(media.extract_attachments({**msg, "platform": "other"}), [])
        self.assertEqual(media.extract_attachments({**msg, "account_id": "12345678"}), [])

    async def test_real_synthetic_video_samples_different_moments_and_cleans_up(self):
        try:
            ffmpeg = media._ffmpeg()
        except media.ChatMediaError:
            self.skipTest("ffmpeg required for the media integration test")
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)/"source.mp4"
            subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=10",
                            "-t", "2", "-threads", "1", "-c:v", "mpeg4", "-y", str(video)], timeout=8, check=True)
            with patch.object(tempfile, "tempdir", directory):
                frames = await media.sample_video(video.read_bytes())
            self.assertGreaterEqual(len(set(frames)), 2)
            self.assertLessEqual(len(frames), 3)
            self.assertEqual(list(Path(directory).iterdir()), [video])
            for frame in frames:
                with Image.open(BytesIO(frame)) as decoded:
                    self.assertEqual(decoded.format, "JPEG")
                    self.assertLessEqual(max(decoded.size), 640)

    async def test_cancellation_reaps_process(self):
        import sys
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(media._ffmpeg_run([sys.executable, "-c", "import time;time.sleep(30)"]), .3)
