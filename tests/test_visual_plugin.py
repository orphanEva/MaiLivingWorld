"""Real receive/reply/storage paths, synthetic pixels, offline vision and expression models."""
import asyncio
import base64
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from MaiLivingWorld.chat_media import _ffmpeg
from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_audio_model import silk_bytes, wav_bytes


OBSERVATION = "窗边的蓝布上趴着一只橘猫,尾巴卷成了一个圈。"


def media_event(mid="image1", *, uid=OWNER, group=None, text=""):
    event = message(mid=mid, uid=uid, group=group, sid=group or uid, text=text)
    with io.BytesIO() as buf:
        Image.new("RGB", (24, 16), "#e78028").save(buf, format="PNG")
        event["raw_message"] = [{"type": "image", "binary_data_base64": base64.b64encode(buf.getvalue()).decode()}]
    event["is_picture"] = True
    return event


class VisualPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_model_ticket_is_scoped_single_use_and_cannot_be_reissued(self):
        event = self.audio_event("1101")
        context = {"sid": OWNER, "mid": "1101", "scope": "private:"+OWNER, "speaker_id": OWNER}
        calls = 0
        async def transcribe(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertFalse(self.bot._visual.consume_model_ticket({**context, "mid": "wrong"}))
                self.assertTrue(self.bot._visual.consume_model_ticket(context))
            else:
                self.assertFalse(self.bot._visual.consume_model_ticket(context))
            return {"success": True, "text": "想听一会儿雨声。"}
        self.bot.ctx.llm.transcribe_audio = AsyncMock(side_effect=transcribe)
        clips = [{"start": n, "end": n+1, "audio": b"wav"} for n in range(3)]
        with patch("MaiLivingWorld.audio_model.audio_excerpts", AsyncMock(return_value=clips)):
            await self.receive(event)
            await asyncio.gather(*self.bot._memory_jobs)
        self.assertEqual(calls, 3)
        self.assertFalse(self.bot._visual.consume_model_ticket(context))

    async def test_media_model_ticket_rejects_config_change_expiry_and_reset(self):
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": False})
        for index, reason in enumerate(("config", "expiry", "reset")):
            mid = str(1201+index)
            await self.receive(self.audio_event(mid))
            await asyncio.gather(*self.bot._memory_jobs)
            context = {"sid": OWNER, "mid": mid, "scope": "private:"+OWNER, "speaker_id": OWNER}
            if reason == "config":
                self.bot.config.unified_mind.min_interval_seconds += 1
            elif reason == "expiry":
                self.bot._visual.model_tickets[(OWNER, mid, "private:"+OWNER, OWNER)]["created"] -= 91
            else:
                self.bot._visual.reset()
            self.assertFalse(self.bot._visual.consume_model_ticket(context))

    async def test_real_qq_silk_voice_enters_the_same_source_observation(self):
        try:
            encoded = silk_bytes()
        except ImportError:
            self.skipTest("silk-python required for QQ SILK integration")
        event = self.audio_event("901")
        event["raw_message"][0]["binary_data_base64"] = base64.b64encode(encoded).decode()
        heard = "这首曲子让我想起了夏天。"
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": heard})
        self.assertIn(heard, await self.reply(await self.receive(event)))
        self.assertEqual(self.bot.ctx.llm.transcribe_audio.await_count, 1)
        row = next(r for r in self.bot._collect_roots("夏天") if r.get("source_name") == "chat_media")
        self.assertEqual(row["evidence_status"], "audio_transcription")

    async def test_long_visual_subtitle_and_audio_each_keep_complete_evidence(self):
        self.bot.ctx.llm.transcribe_audio = AsyncMock()
        caption = "窗边的橘猫安静地趴在软垫上。" * 14
        subtitles = "远处传来鸟鸣。" * 40
        with io.BytesIO() as buffer:
            Image.new("RGB", (24, 16)).save(buffer, format="JPEG")
            frame = buffer.getvalue()
        info = {"title": "长标题" * 30, "basis": "storyboard", "frames": [frame]*3,
                "frame_times": [0, 30, 70], "audio_bytes": b"synthetic video",
                "subtitle_excerpt": subtitles}
        segments = [{"start": n, "end": n+20, "text": "有人在说今天下雨了。" * 12} for n in (0, 25, 65)]
        with patch("MaiLivingWorld.visual_runtime.read_bilibili_link", AsyncMock(return_value=info)), \
                patch("MaiLivingWorld.visual_runtime.describe_visual", AsyncMock(return_value=caption)), \
                patch("MaiLivingWorld.visual_runtime.transcribe_audio", AsyncMock(return_value=segments)):
            await self.receive(message(mid="1001", uid=OWNER, sid=OWNER,
                text="https://www.bilibili.com/video/BV17x411w7KC"))
            await asyncio.gather(*self.bot._memory_jobs)
        row = next(r for r in self.bot._collect_roots("橘猫") if r.get("source_name") == "chat_media")
        self.assertLessEqual(len(row["text"]), 590)
        for detail in ("窗边的橘猫安静地趴在软垫上。", "远处传来鸟鸣。", "有人在说今天下雨了。",
                       "0.0–20.0秒", "25.0–45.0秒", "65.0–85.0秒"):
            self.assertIn(detail, row["text"])
        self.assertTrue(row["text"].endswith("」。"))

    def audio_event(self, mid="501", *, uid=OWNER, group=None, text=""):
        event = message(mid=mid, uid=uid, group=group, sid=group or uid, text=text)
        event["raw_message"] = [{"type": "voice", "binary_data_base64": base64.b64encode(wav_bytes()).decode()}]
        if text:
            event["raw_message"].append({"type": "text", "data": text})
        return event

    async def test_audio_to_reply_growth_restart_replay_and_recall(self):
        heard = "窗外的雨声很好听。"
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": heard})
        event = self.audio_event()
        result = await self.reply(await self.receive(event))
        self.assertIn(heard, result)
        self.assertIn("0.0–1.0秒", result)
        self.assertEqual(self.bot.ctx.llm.transcribe_audio.await_count, 1)
        row = next(r for r in self.bot._collect_roots("雨声") if r.get("source_name") == "chat_media")
        self.assertEqual(row["evidence_status"], "audio_transcription")
        self.assertNotIn(event["raw_message"][0]["binary_data_base64"], str(row))
        await self.bot.on_unload()
        self.bot = self.new_bot()
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": heard})
        later = message(mid="502", uid=OWNER, sid="restarted", text="刚才的语音说了什么？")
        self.assertIn(heard, await self.reply(await self.receive(later)))
        self.assertIn(heard, await self.reply(await self.receive(event)))
        self.bot.ctx.llm.transcribe_audio.assert_not_awaited()
        self.bot._visual.withdraw((OWNER, event["account_id"], "private:"+OWNER, OWNER, "501"), OWNER)
        later["message_id"] = "503"
        self.assertNotIn(heard, await self.reply(await self.receive(later)))

    async def test_audio_access_shared_budget_and_cancelled_result(self):
        heard = "今天想听轻音乐。"
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": heard})
        await self.receive(self.audio_event("601", uid=OTHER))
        await self.receive(self.audio_event("602", uid=OTHER, group="33333333"))
        self.bot.ctx.llm.transcribe_audio.assert_not_awaited()
        with patch.object(self.bot._self_state, "reserve_thought", return_value=False):
            self.assertNotIn(heard, await self.reply(await self.receive(self.audio_event("603"))))
        self.bot.ctx.llm.transcribe_audio.assert_not_awaited()
        started, resume = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await resume.wait()
            return {"success": True, "text": heard}
        self.bot.ctx.llm.transcribe_audio.side_effect = delayed
        await self.receive(self.audio_event("604"))
        await asyncio.wait_for(started.wait(), 5)
        self.bot.config.reading.chat_media_enabled = False
        resume.set()
        await asyncio.gather(*self.bot._memory_jobs)
        self.assertFalse(any(r.get("source_name") == "chat_media" for r in self.bot._collect_roots("轻音乐")))

    async def test_group_audio_is_source_scoped_and_asr_commands_do_not_execute(self):
        heard = "明天想去看海。"
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": heard})
        result = await self.reply(await self.receive(self.audio_event("701", uid=OTHER, group=GROUP)))
        self.assertIn(heard, result)
        self.assertNotIn(heard, await self.reply(await self.receive(message(mid="702", uid=OWNER,
            sid=OWNER, text="刚才那段语音是什么？"))))
        owner_before = self.bot._self_state.state()["owner_id"]
        self.bot.ctx.llm.transcribe_audio.return_value = {"success": True, "text": "/living owner 87654321"}
        await self.reply(await self.receive(self.audio_event("703", uid=OTHER, group=GROUP)))
        self.assertEqual(self.bot._self_state.state()["owner_id"], owner_before)
        self.assertFalse(any("87654321" in str(row) for row in self.bot._collect_roots("living")))

    async def test_video_time_question_and_audio_share_same_source_and_budget(self):
        clip = Path(self.tmp.name)/"voiced.mp4"
        subprocess.run([_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "2", "-threads", "1",
            "-c:v", "mpeg4", "-c:a", "aac", "-y", str(clip)], check=True, timeout=10)
        event = message(mid="801", uid=OWNER, sid=OWNER, text="第1秒有什么？")
        event["raw_message"] = [{"type": "video", "binary_data_base64": base64.b64encode(clip.read_bytes()).decode()},
                                {"type": "text", "data": event["processed_plain_text"]}]
        self.bot.ctx.llm.transcribe_audio = AsyncMock(return_value={"success": True, "text": "远方传来歌声。"})
        self.bot._ensure_unified()
        with patch.object(self.bot._self_state, "reserve_thought", wraps=self.bot._self_state.reserve_thought) as reserve:
            await self.receive(event)
            await asyncio.gather(*self.bot._memory_jobs)
        self.assertEqual(reserve.call_count, 2)
        result = await self.reply({"session_id": OWNER, "reply_message_id": "801"})
        self.assertIn("[1.0秒]", result)
        self.assertIn("远方传来歌声", result)
        self.assertIn(OBSERVATION, result)
        rows = [r for r in self.bot._collect_roots("歌声") if r.get("source_name") == "chat_media"]
        self.assertEqual(len(rows), 1)
        vision = next(c for c in self.bot.ctx.llm.generate.await_args_list if c.kwargs["task_name"] == "vlm")
        self.assertEqual(len(vision.args[0][-1]["content"]), 2)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = self.new_bot()

    def new_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.tmp.name))
        b.set_plugin_config({"owner": {"setup_mode": "日常陪伴", "qq": OWNER},
            "brain_memory": {"group_ids": [GROUP], "private_users": [OTHER]},
            "unified_mind": {"min_interval_seconds": 0}, "reading": {"chat_media_enabled": True}})
        async def model(prompt, **kw):
            if kw["task_name"] == "vlm":
                parts = prompt[-1]["content"]
                self.assertTrue(base64.b64decode(parts[1]["image_base64"]).startswith(b"\xff\xd8\xff"))
                return {"success": True, "response": json.dumps({"visible": OBSERVATION})}
            data = json.loads(prompt.rsplit("\n", 1)[-1])
            return {"success": True, "response": json.dumps({"behavior": "neutral", "decisions": [
                {"mode": "say", "id": r["id"]} for r in data["memories"]]})}
        b.ctx.llm.generate = AsyncMock(side_effect=model)
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.tmp.cleanup()

    async def receive(self, event):
        await self.bot.capture_unified(message=event)
        return {"session_id": event["session_id"], "reply_message_id": event["message_id"]}

    async def reply(self, request):
        result = await self.bot.brain_reply_hint(**request, messages=[{"role": "system", "content": "聊天"}])
        return str(result)

    def vlm_calls(self):
        return sum(c.kwargs["task_name"] == "vlm" for c in self.bot.ctx.llm.generate.await_args_list)

    async def test_image_to_reply_growth_candidates_restart_followup_and_replay(self):
        event = media_event(text="右下角的小字写着牛奶吗？")
        event["raw_message"].append({"type": "text", "data": event["processed_plain_text"]})
        request = await self.receive(event)
        response = await self.reply(request)
        self.assertEqual(response.count(OBSERVATION), 1)
        vision = next(call for call in self.bot.ctx.llm.generate.await_args_list if call.kwargs["task_name"] == "vlm")
        question = vision.args[0][-1]["content"][0]["text"]
        self.assertIn("右侧、下方、文字", question)
        self.assertNotIn("牛奶", question)
        self.assertIn("MaiLivingWorld:visual", response)
        self.assertNotIn("image_base64", response)
        self.assertEqual(self.vlm_calls(), 1)
        self.assertIn(OBSERVATION, str(self.bot._collect_roots("橘猫")))
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot._ensure_growth()
        rows = [r for r in self.bot._growth_candidates() if r.get("source_name") == "chat_media"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_status"], "visual_observation")
        self.assertEqual(rows[0]["scope"], "private:"+OWNER)
        self.assertNotIn(event["raw_message"][0]["binary_data_base64"], str(rows))
        await self.bot.on_unload()
        self.bot = self.new_bot()
        later = await self.receive(message(mid="return", uid=OWNER, sid="next-private", text="刚才那张图里有什么？"))
        self.assertIn(OBSERVATION, await self.reply(later))
        self.assertIn(OBSERVATION, await self.reply(await self.receive(event)))
        self.assertEqual(self.vlm_calls(), 0)

    async def test_group_members_can_discuss_the_same_image_without_exporting_it_to_other_rooms(self):
        self.assertIn(OBSERVATION, await self.reply(await self.receive(media_event("group-image", uid=OTHER, group=GROUP))))
        later = message(mid="member", uid=OWNER, sid=GROUP, group=GROUP, text="刚才的照片里是什么？")
        self.assertIn(OBSERVATION, await self.reply(await self.receive(later)))
        detail = message(mid="detail", uid=OWNER, sid=GROUP, group=GROUP, text="右下角有什么？")
        self.assertIn(OBSERVATION, await self.reply(await self.receive(detail)))
        private = message(mid="private", uid=OWNER, sid=OWNER, text="刚才的照片里是什么？")
        self.assertNotIn(OBSERVATION, await self.reply(await self.receive(private)))
        row = next(r for r in self.bot._collect_roots("橘猫") if r["source_name"] == "chat_media")
        self.assertEqual((row["scope"], row["subject_id"]), ("group:"+GROUP, ""))
        await self.receive(media_event("stranger", uid=OTHER))
        await self.receive(media_event("othergroup", uid=OTHER, group="33333333"))
        self.assertEqual(self.vlm_calls(), 1)

    async def test_metadata_subtitles_and_sampled_frames_give_different_evidence(self):
        with io.BytesIO() as buf:
            Image.new("RGB", (24, 16)).save(buf, format="JPEG")
            frame = buf.getvalue()
        for n, (frames, sub, expected) in enumerate([
            ([], "", "source_metadata"), ([], "欢迎来到剧场", "subtitle_excerpt"), ([frame], "", "visual_observation")
        ]):
            result = dict(title="窗边", frames=frames, basis="storyboard", subtitle_excerpt=sub)
            with patch("MaiLivingWorld.visual_runtime.read_bilibili_link", AsyncMock(return_value=result)):
                request = await self.receive(message(mid=f"b{n}", uid=OWNER, sid=OWNER, text="https://www.bilibili.com/video/BV17x411w7KC"))
                prompt = await self.reply(request)
            self.assertIn("窗边", prompt)
            self.assertIn(expected, [r["evidence_status"] for r in self.bot._collect_roots("窗边")])
            self.assertEqual(self.vlm_calls(), int(bool(frames)))
        answer = await self.reply(await self.receive(message(mid="ambiguous", uid=OWNER, sid=OWNER, text="刚才那个视频呢")))
        self.assertIn("指代不清", answer)
        self.assertNotIn(OBSERVATION, answer)

    async def test_quoted_picture_in_group_stays_exact_after_restart(self):
        async def quote(mid, target, author, text="这一张图你觉得怎么样？"):
            event = message(mid=mid, uid=OWNER, sid=GROUP, group=GROUP, text=text)
            event["raw_message"] = [
                {"type": "reply", "data": {"target_message_id": target, "target_message_sender_id": author,
                 "target_message_content": "引用预览里的是雪山"}},
                {"type": "text", "data": text}]
            return await self.reply(await self.receive(event))
        await self.reply(await self.receive(media_event("401", uid=OTHER, group=GROUP)))
        second = "远处的山脊上铺着一层雪。"
        with patch("MaiLivingWorld.visual_runtime.describe_visual", AsyncMock(return_value=second)):
            await self.reply(await self.receive(media_event("402", uid=OTHER, group=GROUP)))
        self.assertEqual((await quote("403", "401", OTHER)).count(OBSERVATION), 1)
        await self.bot.on_unload()
        self.bot = self.new_bot()
        selected = await quote("404", "401", OTHER)
        self.assertEqual(selected.count(OBSERVATION), 1)
        self.assertNotIn(second, selected)
        self.assertIn(second, await quote("406", "402", OTHER))
        self.assertNotIn("雪山", selected)
        for target, author in (("401", OWNER), ("499", OTHER)):
            self.assertNotIn(OBSERVATION, await quote("405", target, author))
        self.assertEqual(self.vlm_calls(), 0)

    async def test_group_video_placeholder_to_real_frames_and_normal_reply(self):
        clip = Path(self.tmp.name) / "clip.mp4"
        subprocess.run([_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10",
                        "-t", "2", "-threads", "1", "-c:v", "mpeg4", "-y", str(clip)], check=True, timeout=10)
        event = message(mid="1234", uid=OTHER, sid=GROUP, group=GROUP, text="[视频]")
        event["raw_message"] = [{"type": "text", "data": "[视频]"}]
        encoded = base64.b64encode(clip.read_bytes()).decode()
        self.bot.ctx.api = SimpleNamespace(call=AsyncMock(return_value={"success": True, "result": {
            "message_id": 1234, "sender": {"user_id": OTHER}, "self_id": event["account_id"],
            "message_type": "group", "group_id": GROUP, "message": [{"type": "video", "data": {"base64": encoded}}]}}))
        prompt = await self.reply(await self.receive(event))
        self.assertIn("聊天视频", prompt)
        self.assertIn(OBSERVATION, prompt)
        self.bot.ctx.api.call.assert_awaited_once_with("adapter.napcat.message.get_msg", message_id=1234)
        vision = next(c for c in self.bot.ctx.llm.generate.await_args_list if c.kwargs["task_name"] == "vlm")
        self.assertEqual(len(vision.args[0][-1]["content"]) - 1, 3)

    async def test_disabled_private_request_and_revoked_inflight_result(self):
        self.bot.config.reading.chat_media_enabled = False
        await self.receive(media_event())
        self.assertIsNone(self.bot._visual)
        self.bot.config.reading.chat_media_enabled = True
        started, resume = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await resume.wait()
            return OBSERVATION
        with patch("MaiLivingWorld.visual_runtime.describe_visual", side_effect=slow):
            request = await self.receive(media_event())
            await asyncio.wait_for(started.wait(), 3)
            self.bot.config.reading.chat_media_enabled = False
            resume.set()
            await asyncio.gather(*self.bot._memory_jobs)
            self.assertNotIn(OBSERVATION, await self.reply(request))
        self.assertFalse(any(r.get("source_name") == "chat_media" for r in self.bot._collect_roots("猫")))

    async def test_secret_and_model_failure_do_not_supply_visible_facts(self):
        self.assertNotIn(OBSERVATION, await self.reply(await self.receive(media_event(text="这张图片请保密"))))
        self.bot.ctx.llm.generate = AsyncMock(return_value={"success": False})
        result = await self.reply(await self.receive(media_event("failed")))
        self.assertIn("没有可引用的画面细节", result)
        self.assertNotIn(OBSERVATION, result)

    async def test_media_that_finishes_after_host_default_timeout_reaches_current_reply(self):
        async def slow_model(*args, **kwargs):
            await asyncio.sleep(6.1)
            return OBSERVATION

        with patch("MaiLivingWorld.visual_runtime.describe_visual", side_effect=slow_model):
            prompt = await self.reply(await self.receive(media_event()))
        self.assertIn(OBSERVATION, prompt)

    async def test_restart_after_busy_or_daily_refusal_does_not_use_the_old_picture(self):
        for limit, setting, group in ((0, "_MAX_ACTIVE", GROUP), (0, "_DAILY_READS", None)):
            scope_id = group or OWNER
            old = media_event("old-" + setting, uid=OWNER, group=group)
            self.assertIn(OBSERVATION, await self.reply(await self.receive(old)))
            with patch("MaiLivingWorld.visual_runtime." + setting, limit):
                skipped = media_event("skipped-" + setting, uid=OWNER, group=group)
                self.assertNotIn(OBSERVATION, await self.reply(await self.receive(skipped)))
            await self.bot.on_unload()
            self.bot = self.new_bot()
            later = message(mid="ask-" + setting, uid=OWNER, group=group, sid=scope_id, text="刚才那张图呢？")
            answer = await self.reply(await self.receive(later))
            self.assertIn("指代不清", answer)
            self.assertNotIn(OBSERVATION, answer)
