"""Owner plans and resumable reading; transport and identity come from the Host."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from .companion_store import CompanionStore
from .growth_store import source_fingerprint
from .mcp_runtime import day_start, digest
from .owner import extract_sender, normalize_qq
from .reading_model import understand_text
from .tool_store import _digest as observation_digest


def safe_reading(row):
    return {key: row.get(key) for key in ("id", "provider", "resource_type", "resource_id", "version",
            "title", "status", "cursor", "total_units", "revision", "pause_reason")}


def safe_plan(row):
    return {key: row.get(key) for key in ("id", "title", "next_step", "due_at", "status", "revision")}


def plan_command(text):
    """Exact full-message commands only; quotes and forwarded prose cannot authorize."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"/共同计划 (接受|暂停|继续|完成|取消) ([a-z0-9:_-]{16,100}) ([1-9][0-9]{0,8})", text.strip())
    if not match:
        return None
    return {"action": {"接受": "accept", "暂停": "pause", "继续": "resume", "完成": "complete",
                       "取消": "cancel"}[match[1]], "id": match[2], "expected_revision": int(match[3])}


class CompanionRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = CompanionStore(Path(bot.ctx.paths.data_dir) / "companion.sqlite3",
                                    **bot._ledger_storage_options())

    def close(self):
        self.store.close()

    def owner_context(self, kwargs, *, mcp=False):
        b = self.bot
        runtime = b._ensure_unified()
        owner = normalize_qq(b.config.owner.qq) if b.config.owner.enabled else ""
        if not runtime or not owner or (mcp and not b.config.mcp.enabled):
            return None
        request = dict(kwargs)
        sender = extract_sender(kwargs.get("message"))
        if sender:
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = runtime.context(request)
        if (not context or context["speaker_id"] != owner or context["scope"] != "private:"+owner
                or sender and (sender.kind != "private" or sender.user_id != owner)):
            return None
        return context, request

    def signature(self):
        b = self.bot
        return digest([b._mcp_signature("cap"), b._mcp_signature("pixiv"),
                       b.config.reading.model_dump(), b.config.shared_plans.model_dump()])

    def validity(self, trusted=None):
        b = self.bot
        owner, signature, generation = b._self_state.state()["owner_id"], self.signature(), b._mcp_generation
        return lambda: (signature == self.signature() and generation == b._mcp_generation
                        and b._self_state.state()["owner_id"] == owner
                        and (trusted is None or b._unified.context(trusted[1]) == trusted[0]))

    async def capture_plan_command(self, message):
        b = self.bot
        if not b.config.shared_plans.enabled or not b._ensure_unified():
            return
        sender = extract_sender(message)
        owner = b._self_state.state()["owner_id"]
        if (not sender or sender.kind != "private" or sender.user_id != owner
                or not sender.message_id or not sender.stream_id or not isinstance(message, dict)):
            return
        text = message.get("processed_plain_text")
        command = plan_command(text)
        if command:
            try:
                plan = self.store.command_plan(owner_id=owner, message_id=sender.message_id,
                    summary_hash=digest(text.strip()), **command)
                tools = b._ensure_tool_store()
                if plan["status"] == "cancelled":
                    tools.retract_origin("companion", "plan:"+plan["id"])
                else:
                    tools.put_observation("companion", "plan:"+plan["id"],
                        ("主人通过可信私聊确认的共同计划，状态="+plan["status"]+"；标题="+plan["title"]
                         +"；下一步="+plan["next_step"])[:600], "private:"+owner,
                        subject_id=owner, sensitivity="personal", origin_key="plan:"+plan["id"])
            except ValueError:
                # State stays unchanged; the list tool shows the current revision.
                pass

    async def plans(self, action, title, next_step, due_at, kwargs):
        b = self.bot
        trusted = self.owner_context(kwargs)
        if not b.config.shared_plans.enabled or not trusted:
            return {"success": False, "code": "owner_private_plan_required"}
        context = trusted[0]
        owner = context["speaker_id"]
        if action == "list":
            return {"success": True, "data": [safe_plan(p) for p in self.store.plans(owner_id=owner)]}
        if action != "propose":
            return {"success": False, "code": "use_owner_message_to_confirm_plan"}
        try:
            plan = self.store.propose_plan(owner_id=owner, scope=context["scope"], title=title,
                next_step=next_step, due_at=due_at, message_id=context["mid"],
                summary_hash=digest(context.get("query", "")))
            return {"success": True, "data": {**safe_plan(plan), "confirmation":
                f"/共同计划 接受 {plan['id']} {plan['revision']}",
                "instructions": "这是草案。仅主人亲自私聊发送确认命令后生效；暂停/继续/完成/取消同格式带当前版本。"}}
        except (ValueError, TypeError):
            return {"success": False, "code": "invalid_or_conflicting_plan"}

    def _ref(self, record):
        return {"id": record["id"], "digest": source_fingerprint(record),
                "scope": record["scope"], "sensitivity": record["sensitivity"]}

    async def _fetch(self, provider, kind, resource, index, context, valid):
        b = self.bot
        if not b.config.reading.enabled or (provider, kind) not in {("pixiv", "novel"), ("pixiv", "illust"), ("cap", "pdf")}:
            raise ValueError("reading_not_permitted")
        if not isinstance(resource, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", resource):
            raise ValueError("invalid_resource")
        b._ensure_tool_store()
        async def invoke(tool, args):
            result = await b._mcp_bridge.execute(provider, tool, args, config=b.config.mcp,
                context=context, owner=context["speaker_id"], client=b._mcp_client(provider), valid=valid,
                timezone=b.config.general.timezone, record_observations=b.config.mcp.remember_results)
            if not result.get("success"):
                raise ValueError(result.get("code", "read_failed"))
            if not valid():
                raise ValueError("context_changed")
            return result["data"]
        if provider == "cap":
            if not b.config.reading.cap_pdf_enabled:
                raise ValueError("cap_pdf_disabled")
            from .document_reader import normalize_reading_page
            payload = await invoke("living_read_pdf_page", {"jm_id": resource, "page_index": index})
            if payload.get("available") is False:
                if payload.get("provider") == "cap" and payload.get("kind") == "pdf_page" and payload.get("jm_id") == resource:
                    raise ValueError("source_unavailable")
                raise ValueError("tool_error")
            return normalize_reading_page(provider, payload, resource_id=resource, page_index=index)
        metadata = await invoke("get_library_work", {"kind": kind, "pixiv_id": int(resource),
                                                   "file_offset": 0, "file_limit": 100 if kind == "illust" else 1})
        version = metadata.get("current_version")
        if (metadata.get("pixiv_id") != int(resource) or metadata.get("accessible") is False
                or metadata.get("status") in {"private", "deleted", "auth_required"}
                or type(version) is not int or version < 1):
            raise ValueError("source_unavailable")
        if kind == "novel":
            payload = await invoke("read_novel_excerpt", {"pixiv_id": int(resource), "version_no": version,
                "offset": index, "length": b.config.reading.excerpt_chars})
            excerpt, total = payload.get("text"), payload.get("total_characters")
            if (payload.get("pixiv_id") != int(resource) or payload.get("version") != version
                    or payload.get("kind") != "novel" or payload.get("content_available") is not True
                    or payload.get("offset") != index or type(total) is not int or total <= index
                    or not isinstance(excerpt, str) or not excerpt or len(excerpt) > b.config.reading.excerpt_chars
                    or payload.get("end_offset") != index+len(excerpt) or index+len(excerpt) > total):
                raise ValueError("source_unavailable")
            return {"version": str(version), "index": index, "total_units": total, "excerpt": excerpt,
                    "title": str(metadata.get("title", ""))[:180], "unit_id": f"novel:{resource}:{version}:{index}",
                    "image_sha256": "", "image_base64": "", "mime_type": "", "text_truncated": False}
        if not b.config.mcp.media_enabled:
            raise ValueError("media_disabled")
        # File offsets are not page indexes; one page can have several assets.
        offset = 0
        for _ in range(32):
            files = metadata.get("current_files", [])
            if not isinstance(files, list):
                raise ValueError("invalid_image_index")
            if any(isinstance(f, dict) and type(f.get("page_index")) is int and f["page_index"] == index
                   and f.get("file_type") in {"image", "cover"} for f in files):
                break
            following = metadata.get("file_pagination", {}).get("next_offset")
            if type(following) is not int or not offset < following <= 10000:
                raise ValueError("image_page_missing")
            offset = following
            metadata = await invoke("get_library_work", {"kind": kind, "pixiv_id": int(resource),
                                                        "file_offset": offset, "file_limit": 100})
            if metadata.get("current_version") != version or metadata.get("pixiv_id") != int(resource):
                raise ValueError("source_unavailable")
        else:
            raise ValueError("image_index_scan_limit")
        from .mcp_media import fetch_pixiv_image, MediaError
        from .document_reader import normalize_reading_page
        call = b._tool_store.reserve_call(provider, "image_bytes", digest([resource, version, index]),
            digest([context["mid"], "reading_image", resource, index]), context["scope"], context["speaker_id"],
            day_start(b.config.general.timezone), b.config.mcp.max_daily_calls)
        if not call["allowed"]:
            raise ValueError(call["status"])
        try:
            image = await fetch_pixiv_image(backend_url=os.environ.get("PIXIV_MCP_BACKEND_URL", ""),
                api_key=os.environ.get("PIXIV_MCP_API_KEY", ""), details=metadata, pixiv_id=int(resource),
                page_index=index, max_bytes=b.config.mcp.max_media_bytes,
                timeout_seconds=min(30, b.config.mcp.timeout_seconds))
            b._tool_store.complete_call(call["id"], "succeeded")
        except MediaError:
            b._tool_store.complete_call(call["id"], "failed")
            raise ValueError("fetch_failed") from None
        except BaseException:
            b._tool_store.complete_call(call["id"], "failed")
            raise
        if not valid():
            raise ValueError("context_changed")
        return normalize_reading_page(provider, image, resource_id=int(resource), page_index=index,
            version=version, total_units=metadata.get("page_count"), max_image_bytes=b.config.mcp.max_media_bytes)

    def _anchor(self, provider, kind, resource, page, owner):
        # One stable version anchor invalidates all derived notes if source changes.
        key = f"reading:{kind}:{resource}:source"
        text = json.dumps({"kind": kind, "resource": resource,
                           "version": page["version"], "total_units": page["total_units"]}, ensure_ascii=False)
        previous = self.bot._tool_store.resolve(["tool:observation:"+observation_digest(provider, key)])
        if previous and previous[0]["text"] != text:
            # Opening a new version also withdraws earlier original excerpts;
            # growth may refer to those excerpts without the reading anchor.
            self.bot._tool_store.retract_origin(provider, self._origin(kind, resource))
        return self._ref(self.bot._tool_store.put_observation(provider,
            key, text,
            "private:"+owner, subject_id=owner, sensitivity="personal", origin_key=self._origin(kind, resource)))

    @staticmethod
    def _origin(kind, resource):
        return "album:"+resource if kind == "pdf" else "work:"+kind+":"+resource

    def _current_refs(self, reading):
        self.bot._ensure_tool_store()
        resolved = self.bot._tool_store.resolve([ref["id"] for ref in reading["source_refs"]])
        return [self._ref(record) for record in resolved]

    async def read(self, action, provider, kind, resource, reading_id, kwargs, *, internal=False, external_valid=None):
        b = self.bot
        trusted = self.owner_context(kwargs, mcp=True) if not internal else None
        if not b.config.reading.enabled or not b.config.mcp.enabled or (not internal and not trusted):
            return {"success": False, "code": "owner_private_reading_required"}
        owner = b._self_state.state()["owner_id"]
        if action == "list":
            return {"success": True, "data": [safe_reading(r) for r in self.store.readings(owner_id=owner)]}
        base_valid = self.validity(trusted)
        valid = lambda: base_valid() and (external_valid is None or external_valid())
        if not valid():
            return {"success": False, "code": "context_changed"}
        try:
            row = None if action in {"open", "list"} else self.store.reading(reading_id, owner_id=owner)
        except ValueError:
            return {"success": False, "code": "reading_not_found"}
        if action != "open" and not row:
            return {"success": False, "code": "reading_not_found"}
        if row:
            provider, kind, resource = row["provider"], row["resource_type"], row["resource_id"]
            if action == "notes":
                return {"success": True, "data": self.store.notes(row["id"], owner_id=owner,
                    current_refs=self._current_refs(row), limit=30)}
            if action == "review":
                return await self.review_pending(row, valid)
        if action not in {"open", "next", "resume"}:
            return {"success": False, "code": "invalid_reading_action"}
        index = 0 if row is None or action == "resume" else row["cursor"]
        if row and row["cursor"] >= row["total_units"] and action == "next":
            return {"success": True, "data": safe_reading(row)}
        # Derived request identity deduplicates one unit, without granting a new speaker.
        context = dict(trusted[0]) if trusted else {"scope": "private:"+owner, "speaker_id": owner,
            "sid": "internal-reading", "mid": str(time.time_ns())}
        context["mid"] = digest([context["mid"], action, reading_id, index])
        task = asyncio.current_task()
        b._mcp_calls.add(task)
        claim = None
        try:
            if row and action == "next":
                checked = self.store.check_reading(row["id"], owner_id=owner, version=row["version"],
                                                   current_refs=self._current_refs(row))
                claim = self.store.claim_reading(row["id"], owner_id=owner, expected_revision=checked["revision"],
                    lease_seconds=360)
                if not claim:
                    return {"success": False, "code": "reading_busy_or_paused"}
            page = await self._fetch(provider, kind, resource, index, context, valid)
            if not valid():
                raise ValueError("context_changed")
            if row and (page["version"] != row["version"] or page["total_units"] != row["total_units"]):
                b._tool_store.retract_origin(provider, self._origin(kind, resource))
                self.store.check_reading(row["id"], owner_id=owner, version=page["version"], current_refs=[])
                raise ValueError("source_version_changed_open_new_reading")
            ref = self._anchor(provider, kind, resource, page, owner)
            if action == "open":
                row = self.store.open_reading(provider, kind, resource, page["version"], owner_id=owner,
                    scope="private:"+owner, total_units=page["total_units"], source_refs=[ref], title=page.get("title", ""))
                return {"success": True, "data": safe_reading(row)}
            if action == "resume":
                row = self.store.resume_reading(row["id"], owner_id=owner, expected_revision=row["revision"],
                                               version=page["version"], current_refs=[ref])
                return {"success": True, "data": safe_reading(row)}
            # Fetching images only proves page delivery; text review never pretends to see pixels.
            def reserve():
                return b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls)
            note = await understand_text(page["excerpt"], generate=b.ctx.llm.generate, reserve=reserve,
                valid=valid, task_name=b.config.brain.model_task, timeout=b.config.unified_mind.model_timeout_seconds)
            if not valid():
                raise ValueError("context_changed")
            if self._current_refs(row) != [ref]:
                raise ValueError("source_unavailable")
            # Long text is indexed as original excerpts, never model-authored conclusions.
            if b.config.mcp.remember_results:
                for offset in range(0, len(page["excerpt"]), 500):
                    b._tool_store.put_observation(provider, f"reading:{kind}:{resource}:{page['version']}:{index}:{offset}",
                        "作品原文（空白归一化，虚构或未核实）："+" ".join(page["excerpt"][offset:offset+500].split()),
                        context["scope"], subject_id=owner, sensitivity="personal", origin_key=self._origin(kind, resource))
            row = self.store.complete_unit(row["id"], claim["token"], owner_id=owner,
                expected_revision=claim["revision"], version=page["version"], index=index, unit_id=page["unit_id"],
                source_refs=[ref], excerpt=page["excerpt"], image_sha256=page["image_sha256"],
                note=note or "", note_status="reviewed" if note else "pending")
            result = {"success": True, "data": {"reading": safe_reading(row), "note": note,
                "note_status": "reviewed" if note else "pending", "excerpt": page["excerpt"],
                "text_truncated": page.get("text_truncated", False),
                "reading_scope": "仅本页/本段；图片未经过独立视觉理解，笔记只依据返回文字"}}
            if page.get("image_base64") and not internal:
                result["content_items"] = [{"type": "image", "data": page["image_base64"], "mime_type": page["mime_type"]}]
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if row and isinstance(exc, ValueError) and str(exc) == "source_unavailable":
                b._tool_store.retract_origin(provider, self._origin(kind, resource))
                self.store.check_reading(row["id"], owner_id=owner, version=row["version"],
                                        current_refs=[], available=False)
            elif row and isinstance(exc, ValueError) and str(exc) in {"tool_error", "tool_unavailable", "fetch_failed"}:
                # An unavailable check is not proof of permanent deletion, but
                # reviewed notes must wait for an explicit successful recheck.
                self.store.check_reading(row["id"], owner_id=owner, version=row["version"],
                                        current_refs=self._current_refs(row), available=False)
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "source_unavailable", "source_version_changed_open_new_reading", "source_reference_changed",
                "reading_binding_conflict", "context_changed", "reading_not_permitted", "cap_pdf_disabled",
                "media_disabled", "tool_error", "tool_unavailable", "fetch_failed", "image_page_missing",
                "image_index_scan_limit", "revision_conflict", "reading_lease_lost"} else "reading_not_completed_check_source_budget_or_revision"
            return {"success": False, "code": code}
        finally:
            if claim:
                try:
                    self.store.release_reading(claim["id"], claim["token"], owner_id=owner, reason="retry_on_next_request")
                except ValueError:
                    pass  # Completed, paused or superseded leases are already released.
            b._mcp_calls.discard(task)

    async def review_pending(self, row, valid):
        b = self.bot
        owner = row["owner_id"]
        checked = self.store.check_reading(row["id"], owner_id=owner, version=row["version"],
                                           current_refs=self._current_refs(row))
        if checked["status"] == "paused":
            return {"success": False, "code": "source_changed"}
        pending = None
        # Bounded batches; do not repeatedly inspect only the first pages of a long book.
        for offset in range(0, 10000, 200):
            units = self.store.units(row["id"], owner_id=owner, offset=offset, limit=200)
            pending = next((u for u in units if u["note_status"] == "pending" and len(u["excerpt"]) >= 20), None)
            if pending or len(units) < 200:
                break
        if pending is None:
            return {"success": True, "data": {"note_status": "no_pending_text_notes"}}
        task = asyncio.current_task()
        b._mcp_calls.add(task)
        try:
            def current():
                return (valid() and self.store.reading(row["id"], owner_id=owner)["revision"] == checked["revision"]
                        and self._current_refs(row) == row["source_refs"])
            note = await understand_text(pending["excerpt"], generate=b.ctx.llm.generate,
                reserve=lambda: b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls),
                valid=current, task_name=b.config.brain.model_task, timeout=b.config.unified_mind.model_timeout_seconds)
            if not note or not current():
                return {"success": False, "code": "note_pending_review_or_budget"}
            self.store.review_unit(row["id"], pending["unit_id"], owner_id=owner, expected_revision=checked["revision"],
                note=note, note_status="reviewed", current_refs=self._current_refs(row))
            return {"success": True, "data": {"note_status": "reviewed", "note": note}}
        except asyncio.CancelledError:
            raise
        except ValueError:
            return {"success": False, "code": "reading_revision_changed"}
        finally:
            b._mcp_calls.discard(task)

    async def auto_read(self, *, reading_id=None, external_valid=None):
        b = self.bot
        if not (b.config.reading.enabled and b.config.reading.auto_continue and b.config.mcp.enabled
                and b._ensure_unified()):
            return
        owner = b._self_state.state()["owner_id"]
        rows = self.store.readings(owner_id=owner, status="active", resource_type="novel", order="oldest", limit=100 if reading_id else 1)
        if reading_id is not None:
            rows = [row for row in rows if row["id"] == reading_id]
        if not rows or external_valid is not None and not external_valid():
            return
        if not self.store.reserve_reading_cycle(owner_id=owner,
            interval_seconds=b.config.reading.interval_seconds, daily_limit=b.config.reading.max_daily_units,
            day_start=day_start(b.config.general.timezone)):
            return
        row = min(rows, key=lambda r: (r["updated"], r["id"]))
        return await self.read("next", "", "", "", row["id"], {}, internal=True, external_valid=external_valid)

    def due_plan(self, owner):
        if not self.bot.config.shared_plans.enabled or not self.bot.config.shared_plans.followup_enabled:
            return None
        now = time.time()
        rows = self.store.plans(owner_id=owner, status="active", due_before=now, oldest=True, limit=200)
        return next((p for p in rows if p["due_at"] and p["due_at"] <= now
                     and now-p.get("last_followup_at", 0) >= self.bot.config.shared_plans.followup_interval_hours*3600), None)
