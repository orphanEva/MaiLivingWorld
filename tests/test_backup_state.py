import copy
from decimal import Decimal
from datetime import datetime, date, time, timedelta
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mlw_backup_tested", ROOT/"scripts"/"backup_state.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def attention_message():
    return {"platform": "qq", "account_id": "99999", "session_id": "owner-backup-stream", "message_id": "attention-backup-input",
            "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
            "processed_plain_text": "这段原始聊天正文不会进入注意账本"}


def seed_attention(store):
    store.capture(attention_message(), owner_id="12345678", now=100, engagement_seconds=1)
    claimed = store.claim("12345678", candidates=[{"kind": "reading", "key": "reading:"+"a"*64, "version": "b"*64}],
                          day_start=0, daily_limit=6, interval_seconds=0, now=101, activity_awareness=True)
    if (not claimed or not store.start_activity("12345678", claimed["token"], proof="a"*64, signature="b"*64, now=101)
            or not store.finish("12345678", claimed["token"], outcome="progress", now=102,
                                activity_awareness=True, activity_proof="c"*64)):
        raise AssertionError("expected a confirmed attention batch")
    return store.state("12345678", now=103)


def assert_attention_restore(case, store, expected):
    owner = "12345678"
    case.assertEqual(store.state(owner, now=103), expected)
    before = dict(store.con.execute("SELECT * FROM attention_control WHERE owner_id=?", (owner,)).fetchone())
    saved = json.loads(before["payload"])["activity"]
    case.assertEqual(saved, {"load": .25, "at": 102.})
    case.assertAlmostEqual(expected["activity"]["load"], .25-.25/3600, places=7)
    case.assertFalse(expected["activity"]["recovering"])
    case.assertEqual(store.state(owner, now=100)["activity"]["load"], .25)
    case.assertEqual(store.state(owner, now=100)["activity"]["at"], 102.)
    case.assertEqual(store.state(owner, now=3702)["activity"]["load"], 0.)
    case.assertEqual(dict(store.con.execute("SELECT * FROM attention_control WHERE owner_id=?", (owner,)).fetchone()), before)
    events = store.events(owner)
    started = next(row for row in events if row["action"] == "activity_start")
    finished = next(row for row in events if row["action"] == "finish")
    case.assertEqual(started["proof"], "a"*64)
    case.assertEqual(finished["activity"]["proof"], "c"*64)
    case.assertEqual(started["token_hash"], finished["activity"]["token_hash"])
    case.assertNotIn(attention_message()["processed_plain_text"], json.dumps(events, ensure_ascii=False))
    case.assertFalse(store.capture(attention_message(), owner_id=owner, now=104, engagement_seconds=1))
    case.assertEqual(store.state(owner, now=103), expected)


def seed_belief_revision(store):
    """Exercise old JSON, a pending counterview, adoption and rollback on real tables."""
    def source(letter, text):
        return {"id": "brain:event:backup-belief-"+letter, "source": "brain_event", "kind": "event",
                "origin_key": "backup-belief-origin-"+letter, "scope": "private:12345678",
                "subject_id": "12345678", "text": text, "timestamp": 100,
                "sensitivity": "personal", "evidence_status": "recorded_event"}
    original = source("a", "编程工具甲在我的旧项目中运行稳定。")
    first = source("b", "编程工具甲在新的移动端项目中存在兼容问题。")
    second = source("c", "编程工具乙在独立的服务器项目中更易维护。")
    legacy = {"beliefs": [
        {"topic": "编程工具", "statement": "编程工具甲适合目前的项目", "confidence": .8,
         "evidence_ids": [original["id"]]},
        {"topic": "旧阅读偏好", "statement": "阅读时喜欢保留原文依据", "confidence": .6,
         "evidence_ids": [original["id"]]}],
        "interests": [{"topic": "编程工具", "delta": .1, "evidence_ids": [original["id"]]}]}
    def submit(proposal, records, now, reviewed):
        job_id = store.enqueue(records, mode="active", now=now)
        job = store.claim(now=now)
        if not job or job["id"] != job_id:
            raise AssertionError("expected real growth claim")
        result = store.complete(job_id, job["token"], proposal, records, now=now+1,
                                expected_revision=store.state(now=now)["revision"], review_beliefs=reviewed)
        if not result["changed"]:
            raise AssertionError("expected persisted belief change")
        return job_id
    legacy_job = submit(legacy, [original], 100, False)
    reviewed_jobs = []
    candidate = "编程工具应根据项目兼容性与维护成本选择"
    for evidence, records, now in ((first, [original, first], 110), (second, [original, first, second], 120)):
        old = next(row for row in store.state(now=now)["beliefs"] if row["topic"] == "编程工具")
        proposal = {"beliefs": [{"topic": old["topic"], "statement": candidate, "confidence": .9,
            "evidence_ids": [evidence["id"]], "relation": "challenge", "previous_key": old["key"],
            "previous_revision": old["revision"], "quotes": [{"source_id": evidence["id"], "quote": evidence["text"]}]}]}
        reviewed_jobs.append(submit(proposal, records, now, True))
    adopted = next(row for row in store.state(now=121)["beliefs"] if row["topic"] == "编程工具")
    if adopted["text"] != candidate or adopted["assessment"]["status"] != "considered":
        raise AssertionError("expected independently supported adoption before rollback")
    store.rollback(store.state(now=121)["revision"], now=130)
    order = {"growth_state": "slot", "growth_jobs": "id", "growth_history": "revision", "growth_consumed": "source_id"}
    return {"rows": {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+order[table])]
                     for table in backup.LEDGERS["growth.sqlite3"]},
            "state": store.state(now=131), "records": [original, first, second],
            "legacy_job": legacy_job, "reviewed_jobs": reviewed_jobs, "candidate": candidate}


def assert_belief_revision_restore(case, store, expected):
    order = {"growth_state": "slot", "growth_jobs": "id", "growth_history": "revision", "growth_consumed": "source_id"}
    for table, rows in expected["rows"].items():
        case.assertTrue(rows, table)
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+order[table])], rows)
    case.assertEqual(store.state(now=131), expected["state"])
    original, first, second = expected["records"]
    beliefs = store.state(now=131)["beliefs"]
    held = next(row for row in beliefs if row["topic"] == "编程工具")
    legacy = next(row for row in beliefs if row["topic"] == "旧阅读偏好")
    case.assertNotIn("assessment", legacy)
    case.assertEqual(held["text"], "编程工具甲适合目前的项目")
    case.assertEqual(held["assessment"]["status"], "contested")
    case.assertEqual(held["assessment"]["candidate_text"], expected["candidate"])
    case.assertEqual({ref["id"] for ref in held["sources"]}, {original["id"], first["id"]})
    case.assertEqual([ref["id"] for ref in held["assessment"]["candidate_sources"]], [first["id"]])
    history = {row["revision"]: row for row in store.history()}
    adopted = next(row for row in history[3]["after"]["beliefs"] if row["topic"] == "编程工具")
    case.assertTrue(history[3]["rolled_back"])
    case.assertEqual(history[4]["kind"], "rollback")
    case.assertEqual(adopted["text"], expected["candidate"])
    case.assertEqual({ref["id"] for ref in adopted["sources"]}, {original["id"], first["id"], second["id"]})
    case.assertEqual(adopted["assessment"]["last_previous_text"], held["text"])
    from MaiLivingWorld.growth_store import source_fingerprint
    from MaiLivingWorld.unified_memory import derived_memories
    resolved = {row["id"]: row for row in expected["records"]}
    recalled = derived_memories(store.state(now=131), resolved, fingerprint=source_fingerprint)
    identity = "growth:belief:"+held["key"]
    view = next(row for row in recalled if row["id"] == identity)
    case.assertIn("尚未定论", view["text"])
    case.assertEqual((view["scope"], view["sensitivity"]), ("private:12345678", "personal"))
    case.assertEqual(view["sources"], held["sources"])
    resolved.pop(first["id"])
    case.assertNotIn(identity, {row["id"] for row in derived_memories(store.state(now=131), resolved,
                                                                   fingerprint=source_fingerprint)})
    case.assertEqual(store.enqueue([original], mode="active", now=140), expected["legacy_job"])
    case.assertEqual(store.enqueue([original, first, second], mode="active", now=140), expected["reviewed_jobs"][1])
    case.assertIsNone(store.claim(now=140))
    # Another ID for the same observation cannot strengthen the pending view.
    alias = dict(first, id=first["id"]+"-alias")
    records = [original, first, alias]
    job_id = store.enqueue(records, mode="active", now=141)
    job = store.claim(now=141)
    case.assertIsNotNone(job)
    case.assertEqual(job["id"], job_id)
    proposal = {"beliefs": [{"topic": held["topic"], "statement": expected["candidate"], "confidence": .99,
        "evidence_ids": [alias["id"]], "relation": "challenge", "previous_key": held["key"],
        "previous_revision": held["revision"], "quotes": [{"source_id": alias["id"], "quote": alias["text"]}]}]}
    completed = store.complete(job_id, job["token"], proposal, records, now=142,
                               expected_revision=expected["state"]["revision"], review_beliefs=True)
    case.assertFalse(completed["changed"])
    case.assertEqual(store.state(now=142), expected["state"])
    case.assertEqual(len(store.history()), 4)
    case.assertEqual(store.status()["consumed"], 1)


def seed_recollection(store):
    from MaiLivingWorld.growth_store import source_fingerprint
    owner = "12345678"
    raw = {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact", "scope": "private:"+owner,
           "subject_id": owner, "text": "我住在上海", "timestamp": 1,
           "sensitivity": "personal", "evidence_status": "active"}
    digest = source_fingerprint(raw)
    message = dict(attention_message(), message_id="recollection-backup-input", processed_plain_text="我现在住在北京")
    store.enqueue(message, owner_id=owner, now=100)
    batch = store.claim(owner, now=101)
    if not batch:
        raise AssertionError("expected one real recollection claim")
    store.complete(batch["id"], batch["token"], {"updates": [{"target_id": raw["id"],
        "target_digest": digest, "old_quote": "我住在上海", "quote": "我现在住在北京", "relation": "replace"}]},
        owner_id=owner, expected_revision=batch["base_revision"], targets={raw["id"]: raw}, now=102)
    store.record_delivery(owner_id=owner, scope="private:"+owner, receipt_id="recollection-backup-receipt",
        topic_refs=[{"id": raw["id"], "digest": digest}], text_digest="a"*64, now=103)
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
            for table in backup.LEDGERS["recollection.sqlite3"]}


def assert_recollection_restore(case, store, expected):
    for table, rows in expected.items():
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table)], rows)
        case.assertTrue(rows)
    delivery = expected["recollection_deliveries"][0]
    payload = json.loads(delivery["payload"])
    case.assertFalse(store.record_delivery(owner_id="12345678", scope="private:12345678",
        receipt_id="recollection-backup-receipt", topic_refs=payload["topic_refs"], text_digest="a"*64, now=104))
    case.assertEqual(store.con.execute("SELECT COUNT(*) FROM recollection_deliveries").fetchone()[0], 1)
    case.assertIsNone(store.claim("12345678", now=105))


def seed_habits(store):
    owner = "12345678"
    commands = (("set-short", "set", "general", "length", "short"),
                ("set-advice", "set", "technical", "support", "advice"),
                ("clear-advice", "clear", "technical", "support", ""))
    for offset, (mid, action, situation, dimension, value) in enumerate(commands):
        body = "/习惯 "+("设置" if action == "set" else "清除")+" "+situation+" "+dimension+(" "+value if value else "")
        message = dict(attention_message(), message_id="habits-backup-"+mid, processed_plain_text=body)
        if not store.command(message, owner_id=owner, action=action, situation=situation,
                             dimension=dimension, value=value, now=100+offset):
            raise AssertionError("expected a real habits command")
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
            ("owner_id" if table == "habits_control" else "id"))] for table in backup.LEDGERS["habits.sqlite3"]}


def seed_followthrough(store):
    owner, sid = "12345678", "owner-backup-stream"
    store.bind_source("cap", "a"*32, owner_id=owner, scope="private:"+owner,
                      operation="jm_start_download", source_signature="b"*64, now=100)
    row = store.draft(owner, sid, "cap", "a"*32, "jm_start_download", "c"*64, "b"*64, now=100)
    if not store.record_delivery(owner, sid, row["promise_text"], "d"*64,
                                item_id=row["id"], expected_revision=row["revision"], now=101):
        raise AssertionError("expected an actual promise receipt")
    claim = store.claim_poll(owner, day_start=0, now=102)
    if not claim or not store.finish_poll(owner, claim["id"], claim["lease_token"], claim["revision"], "succeeded", now=103):
        raise AssertionError("expected a confirmed task result")
    row = store.due(owner, now=104)
    row = store.prepare_report(owner, sid, row["id"], row["revision"], now=104)
    if not store.record_delivery(owner, sid, row["delivery_text"], "e"*64,
                                item_id=row["id"], expected_revision=row["revision"], now=105):
        raise AssertionError("expected an actual result receipt")
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
            ("owner_id" if table == "followthrough_control" else "id"))] for table in backup.LEDGERS["followthrough.sqlite3"]}


def seed_projects(store):
    owner = "12345678"
    def seed(letter):
        return {"question_key": "q:"+letter*24, "interest_key": "i:"+letter*24,
                "topic": "天文作品", "question": "这篇作品有哪些天文线索？",
                "seed_refs": [{"id": "brain:fact:"+letter, "digest": "a"*64,
                               "scope": "private:"+owner, "sensitivity": "personal"}],
                "source_signature": "b"*64}
    completed = store.create(owner, seed("a"), now=100)
    claim = store.claim(owner, [completed["id"]], day_start=0, daily_limit=6, interval_seconds=300, now=101)
    evidence = {"id": "tool:observation:"+"c"*64, "digest": "d"*64,
                "scope": "private:"+owner, "sensitivity": "personal"}
    if not claim or not store.finish(owner, claim["id"], claim["lease_token"], claim["revision"],
                                      action="gathered", evidence_refs=[evidence], now=102):
        raise AssertionError("expected a real project gather stage")
    claim = store.claim(owner, [completed["id"]], day_start=0, daily_limit=6, interval_seconds=300, now=402)
    result = {"outcome": "supported", "conclusion": "本次资料包含天文主题。",
              "quotes": [{"source_id": evidence["id"], "quote": "作品包含天文主题"}]}
    if not claim or not store.finish(owner, claim["id"], claim["lease_token"], claim["revision"],
                                      action="reviewed", result=result, now=403):
        raise AssertionError("expected a reviewed project result")
    paused = store.create(owner, seed("b"), now=404)
    if not store.command(owner, paused["id"], paused["revision"], "pause", "e"*64, "f"*64, now=405):
        raise AssertionError("expected an owner-paused project")
    changed = store.create(owner, seed("c"), now=406)
    if not store.pause_invalid(owner, changed["id"], changed["revision"], now=407):
        raise AssertionError("expected a source-invalidated project")
    stopped = store.create(owner, seed("d"), now=408)
    if not store.command(owner, stopped["id"], stopped["revision"], "stop", "0"*64, "1"*64, now=409):
        raise AssertionError("expected an owner-stopped project")
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
            ("owner_id" if table.endswith("_control") else "id"))] for table in backup.LEDGERS["projects.sqlite3"]}


def shared_reading_fixture(letter="a"):
    owner = "12345678"
    return {"id": "reading:"+letter*64, "owner_id": owner, "scope": "private:"+owner,
            "provider": "pixiv", "resource_type": "novel", "resource_id": "123" if letter == "a" else "456",
            "version": "1", "service_signature": "d"*64,
            "source_refs": [{"id": "tool:observation:"+letter*64, "digest": "c"*64,
                             "scope": "private:"+owner, "sensitivity": "personal"}]}


def shared_reading_message(action, letter="a", units=0):
    from MaiLivingWorld.shared_reading_store import command_text
    return dict(attention_message(), message_id="shared-backup-"+letter+"-"+action+"-"+str(units),
                processed_plain_text=command_text(action, "reading:"+letter*64, units=units))


def seed_shared_reading(store):
    owner, sid = "12345678", "owner-backup-stream"
    for offset, letter in enumerate(("a", "b", "c")):
        now, reading = 100+offset*10, shared_reading_fixture(letter)
        if not store.command(shared_reading_message("start", letter), owner_id=owner, action="start", reading=reading, unit_count=3, now=now):
            raise AssertionError("expected actual shared reading command")
        store.command(shared_reading_message("position", letter, 1), owner_id=owner, action="position", reading=reading,
                      unit_count=3, units=1, now=now+1)
        row = store.session(owner, reading["id"])
        row = store.prepare(owner, sid, reading["id"], row["revision"], 1, "e"*64, "我们提到了第一段的星空。", now=now+2)
        if not row:
            raise AssertionError("expected a bounded discussion draft")
        if letter in {"a", "c"}:
            if not store.record_delivery(owner, sid, "我们提到了第一段的星空。", ("f" if letter == "a" else "e")*64,
                                         reading_id=reading["id"], expected_revision=row["revision"], now=now+3):
                raise AssertionError("expected an actual discussion delivery")
            action = "position" if letter == "a" else "clear"
            store.command(shared_reading_message(action, letter, 0), owner_id=owner, action=action, reading=reading,
                          unit_count=3, units=0, now=now+4)
        else:
            store.command(shared_reading_message("stop", letter), owner_id=owner, action="stop", reading=reading,
                          unit_count=3, now=now+3)
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
            ("owner_id" if table.endswith("_control") else "id"))] for table in backup.LEDGERS["shared_reading.sqlite3"]}


def assert_v20_restore(case, store, expected):
    for table, rows in expected.items():
        order = "owner_id" if table.endswith("_control") else "id"
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+order)], rows)
        case.assertTrue(rows, table)
    if "projects_items" in expected:
        items = {item["seed"]["question_key"]: item for item in store.items("12345678")}
        case.assertEqual(items["q:"+"a"*24]["status"], "completed")
        case.assertEqual(items["q:"+"b"*24]["status"], "paused")
        case.assertEqual(items["q:"+"c"*24]["reason"], "source_changed")
        case.assertEqual(items["q:"+"d"*24]["status"], "stopped")
        case.assertIsNone(store.claim("12345678", [row["id"] for row in items.values()],
                                     day_start=0, daily_limit=6, interval_seconds=300, now=800))
        case.assertEqual(store.state("12345678")["daily_actions"], 2)
    else:
        active = store.session("12345678", "reading:"+"a"*64)
        case.assertEqual((active["status"], active["owner_units"], active["discussed_units"]), ("active", 0, 1))
        case.assertEqual(active["binding"]["service_signature"], "d"*64)
        case.assertFalse(store.command(shared_reading_message("position", "a", 1), owner_id="12345678", action="position",
                                      reading=shared_reading_fixture(), unit_count=3, units=1, now=120))
        case.assertEqual(store.session("12345678", active["reading_id"])["owner_units"], 0)
        case.assertIsNone(store.prepare("12345678", "owner-backup-stream", active["reading_id"], active["revision"],
                                       1, "e"*64, "我们提到了第一段的星空。", now=121))
        stopped = store.session("12345678", "reading:"+"b"*64)
        case.assertEqual(stopped["status"], "stopped")
        case.assertIsNone(stopped["draft"])
        case.assertFalse(store.record_delivery("12345678", "owner-backup-stream", "我们提到了第一段的星空。", "0"*64,
                        reading_id=stopped["reading_id"], expected_revision=stopped["revision"], now=121))
        cleared = store.session("12345678", "reading:"+"c"*64)
        case.assertEqual((cleared["status"], cleared["owner_units"], cleared["discussed_units"]), ("stopped", 0, 0))
        case.assertIsNone(cleared["draft"])
        case.assertFalse(store.command(shared_reading_message("start", "c"), owner_id="12345678", action="start",
                                      reading=shared_reading_fixture("c"), unit_count=3, now=130))
        changed = dict(shared_reading_fixture(), service_signature="e"*64)
        message = dict(shared_reading_message("position", "a", 1), message_id="shared-after-service-change")
        with case.assertRaisesRegex(ValueError, "source_binding_changed"):
            store.command(message, owner_id="12345678", action="position", reading=changed, unit_count=3, units=1, now=131)
        case.assertEqual(store.session("12345678", active["reading_id"])["owner_units"], 0)


def seed_advice(store):
    owner, sid = "12345678", "owner-backup-stream"
    refs = [{"id": "brain:fact:F1", "digest": "a"*64, "scope": "private:"+owner, "sensitivity": "personal"}]
    def sent(tag, topic, text, now):
        row = store.prepare(owner, sid, "advice-backup-"+tag, topic, text, refs, now=now)
        if not store.record_delivery(owner, row["id"], row["revision"], sid, row["delivery_text"], tag+"-receipt", now=now+1):
            raise AssertionError("expected actual advice delivery")
        return store.get(owner, row["id"])
    row = sent("learning", "整理书桌", "先收好桌上的书，再看看是否方便。", 100)
    tried = dict(attention_message(), message_id="advice-backup-tried",
                 processed_plain_text=f'/建议 尝试过 {row["id"]} {row["revision"]}')
    if not store.command(tried, owner, now=102):
        raise AssertionError("expected explicit tried outcome")
    row = store.get(owner, row["id"])
    feedback = dict(attention_message(), message_id="advice-backup-feedback", processed_plain_text="整理书桌后还是没什么用")
    if not store.enqueue(feedback, owner, row["id"], row["revision"], now=103):
        raise AssertionError("expected one actual feedback input")
    claim = store.claim(owner, day_start=0, interval_seconds=0, daily_limit=4, now=104)
    if not claim or not store.finish(owner, claim, "unhelpful", "整理书桌后还是没什么用", now=105):
        raise AssertionError("expected literal owner outcome")
    removed = sent("forgotten", "备份遗忘题目", "备份中应当清除的旧建议正文。", 110)
    store.enqueue(dict(attention_message(), message_id="advice-backup-forgotten-input", processed_plain_text="旧建议没有帮助"),
                  owner, removed["id"], removed["revision"], now=112)
    if not store.command(dict(attention_message(), message_id="advice-backup-forget",
        processed_plain_text=f'/建议 忘记 {removed["id"]} {removed["revision"]}'), owner, now=113):
        raise AssertionError("expected an explicit forgotten record")
    stopped = sent("stopped", "安排休息", "可以在两段任务之间停一下。", 120)
    if not store.command(dict(attention_message(), message_id="advice-backup-stop",
        processed_plain_text=f'/建议 停止 {stopped["id"]} {stopped["revision"]}'), owner, now=122):
        raise AssertionError("expected an explicit stopped record")
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
        ("owner_id" if table.endswith("_control") else "id"))] for table in backup.LEDGERS["advice.sqlite3"]}


def seed_reference(store):
    owner, other = "12345678", "87654321"
    refs = [{"id": "brain:fact:F"+str(i), "digest": "a"*64, "scope": "private:"+owner, "sensitivity": "personal"}
            for i in range(3)]
    store.capture(owner, "a"*64, "b"*64, now=100)
    if not store.delivered(owner, "c"*64, "d"*64, refs, expected_revision=1,
                           input_receipt="a"*64, ttl_seconds=3600, now=101):
        raise AssertionError("expected real referenced discussion")
    if not store.capture(owner, "e"*64, "f"*64, keep_focus=False, now=102):
        raise AssertionError("expected clear of the old topic")
    # Another owner's live references survive intact, including a >191-character payload.
    refs = [{**ref, "scope": "private:"+other} for ref in refs]
    store.capture(other, "1"*64, "2"*64, now=103)
    if not store.delivered(other, "3"*64, "4"*64, refs, expected_revision=1,
                           input_receipt="1"*64, ttl_seconds=3600, now=104):
        raise AssertionError("expected another owner discussion binding")
    store.capture(other, "5"*64, "6"*64, keep_focus=True, now=105)
    paused = "11223344"
    refs = [{**ref, "scope": "private:"+paused} for ref in refs]
    store.capture(paused, "7"*64, "8"*64, now=106)
    if not store.delivered(paused, "9"*64, "0"*64, refs, expected_revision=1,
                           input_receipt="7"*64, ttl_seconds=3600, now=107):
        raise AssertionError("expected third owner discussion binding")
    store.capture(paused, "ab"*32, "cd"*32, keep_focus=True, invalidate_focus=True, now=108)
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
        ("owner_id" if table.endswith("_control") else "receipt_id"))] for table in backup.LEDGERS["reference.sqlite3"]}


def assert_v21_restore(case, store, expected):
    owner = "12345678"
    for table, rows in expected.items():
        key = "owner_id" if table.endswith("_control") else "receipt_id" if table == "reference_receipts" else "id"
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+key)], rows)
        case.assertTrue(rows)
    if "advice_control" in expected:
        rows = store.items(owner)
        active = next(row for row in rows if row["status"] == "active")
        forgotten = next(row for row in rows if row["status"] == "forgotten")
        case.assertEqual(active["outcome"], "unhelpful")
        case.assertEqual(active["feedback"], "整理书桌后还是没什么用")
        case.assertEqual(store.state(owner)["cycle_count"], 1)
        case.assertEqual(forgotten["source_refs"], [])
        case.assertEqual((forgotten["topic"], forgotten["advice"], forgotten["feedback"]), ("", "", ""))
        case.assertNotIn("旧建议没有帮助", json.dumps(expected, ensure_ascii=False))
        case.assertNotIn("备份中应当清除的旧建议正文", json.dumps(expected, ensure_ascii=False))
        case.assertIsNone(store.claim(owner, day_start=0, interval_seconds=0, daily_limit=4, now=130))
        case.assertFalse(store.record_delivery(owner, forgotten["id"], 1, "owner-backup-stream", "late", "forgotten-receipt", now=131))
        refs = [{"id": "brain:fact:F1", "digest": "a"*64, "scope": "private:"+owner, "sensitivity": "personal"}]
        with case.assertRaisesRegex(ValueError, "advice_already_known"):
            store.prepare(owner, "another-session", "another-message", "备份遗忘题目", "备份中应当清除的旧建议正文。", refs, now=132)
        case.assertFalse(store.command(dict(attention_message(), message_id="advice-backup-tried",
            processed_plain_text=f'/建议 尝试过 {active["id"]} 2'), owner, now=133))
        case.assertEqual(store.get(owner, active["id"])["outcome"], "unhelpful")
    else:
        state = store.state(owner, now=130)
        case.assertEqual(state["focus_refs"], [])
        case.assertFalse(state["active"])
        case.assertFalse(store.capture(owner, "a"*64, "b"*64, keep_focus=True, now=131))
        refs = [{"id": "brain:fact:F0", "digest": "a"*64, "scope": "private:"+owner, "sensitivity": "personal"}]
        case.assertFalse(store.delivered(owner, "c"*64, "d"*64, refs, expected_revision=1,
            input_receipt="a"*64, ttl_seconds=3600, now=132))
        case.assertEqual(store.state(owner, now=133)["focus_refs"], [])
        case.assertEqual(len(store.state("87654321", now=133)["focus_refs"]), 3)
        case.assertTrue(store.state("87654321", now=133)["active"])
        case.assertEqual(store.state("87654321", now=133)["mentioned_at"], 104)
        case.assertEqual(store.state("87654321", now=133)["updated"], 105)
        case.assertEqual(store.state("87654321", now=133)["expires"], 3704)
        case.assertFalse(store.state("87654321", now=3705)["active"])
        paused = store.state("11223344", now=133)
        case.assertEqual(len(paused["focus_refs"]), 3)
        case.assertEqual(paused["mentioned_at"], 107)
        case.assertEqual(paused["expires"], 0)
        case.assertFalse(paused["active"])
        case.assertFalse(store.delivered("11223344", "9"*64, "0"*64, paused["focus_refs"],
            expected_revision=1, input_receipt="7"*64, ttl_seconds=3600, now=134))


def assert_v19_restore(case, store, expected):
    for table, rows in expected.items():
        order = "owner_id" if table.endswith("_control") else "id"
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+order)], rows)
        case.assertTrue(rows, table)
    if "habits_preferences" in expected:
        states = {(row["situation"], row["dimension"]): row["status"] for row in expected["habits_preferences"]}
        case.assertEqual(states, {("general", "length"): "active", ("technical", "support"): "cleared"})
    else:
        case.assertIsNone(store.due("12345678", now=106))
        case.assertIsNone(store.claim_poll("12345678", day_start=0, now=500))
        case.assertEqual(store.items("12345678")[0]["status"], "reported")
        case.assertEqual(store.source("cap", "a"*32)["source_signature"], "b"*64)


def seed_repair(store):
    """Real deliveries, accepted/retracted/forgotten repairs and an in-flight claim."""
    owner, sid = "12345678", "owner-backup-stream"
    receipts, messages, sources, commands = [], [], [], []
    for offset, topic in enumerate(("面试", "考试", "体检")):
        now = 100+offset*10
        old = "你周五有"+topic
        text = old+"，记得准备材料。"
        receipt = {"owner": owner, "sid": sid, "mid": "repair-backup-sent-"+str(offset), "text": text,
                   "receipt_id": hashlib.sha256(("receipt-"+str(offset)).encode()).hexdigest(),
                   "text_digest": hashlib.sha256(text.encode()).hexdigest()}
        if not store.record_delivery(owner, receipt, now=now):
            raise AssertionError("expected real verified delivery")
        source = store.recent(owner, now=now+1)[0]
        correction = "你刚才说“"+old+"”，其实我的"+topic+"在周六。"
        message = dict(attention_message(), message_id="repair-backup-input-"+str(offset), processed_plain_text=correction)
        if not store.enqueue(message, owner, source["id"], source["digest"], now=now+2):
            raise AssertionError("expected exact owner correction")
        claim = store.claim(owner, day_start=0, daily_limit=4, interval_seconds=0, now=now+3)
        if not claim or not store.finish(owner, claim, {"old_quote": old, "correction_quote": correction}, now=now+4):
            raise AssertionError("expected accepted literal repair")
        item = store.items(owner)[0]
        if offset:
            action = "撤回" if offset == 1 else "忘记"
            command = dict(attention_message(), message_id="repair-backup-command-"+str(offset),
                           processed_plain_text="/修复 "+action+" "+item["id"]+" "+str(item["revision"]))
            if not store.command(command, owner, now=now+5):
                raise AssertionError("expected actual owner repair command")
            commands.append(command)
        receipts.append(receipt)
        messages.append(message)
        sources.append(source)
    pending_message = dict(attention_message(), message_id="repair-backup-pending",
                           processed_plain_text="你刚才说“你周五有面试”，其实我的面试改在周日。")
    store.enqueue(pending_message, owner, sources[0]["id"], sources[0]["digest"], now=140)
    claim = store.claim(owner, day_start=0, daily_limit=4, interval_seconds=0, now=141)
    if not claim:
        raise AssertionError("expected a persisted in-flight claim")
    rows = {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
            ("owner_id" if table.endswith("_control") else "id"))] for table in backup.LEDGERS["repair.sqlite3"]}
    return {"rows": rows, "receipts": receipts, "messages": messages, "sources": sources,
            "commands": commands, "claim": claim}


def assert_repair_restore(case, store, expected):
    owner = "12345678"
    for table, rows in expected["rows"].items():
        case.assertTrue(rows, table)
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table+" ORDER BY "+
                         ("owner_id" if table.endswith("_control") else "id"))], rows)
    items = store.items(owner)
    case.assertEqual({row["status"] for row in items}, {"active", "retracted", "forgotten"})
    case.assertTrue(all(row["evidence_status"] == "owner_reported" for row in items))
    forgotten = next(row for row in items if row["status"] == "forgotten")
    case.assertEqual((forgotten["old_quote"], forgotten["correction_quote"]), ("", ""))
    serialized = json.dumps(expected["rows"], ensure_ascii=False)
    case.assertNotIn(expected["receipts"][2]["text"], serialized)
    case.assertNotIn(expected["messages"][2]["processed_plain_text"], serialized)
    case.assertEqual((store.state(owner)["epoch"], store.state(owner)["cycle_count"]), (2, 4))
    case.assertTrue(store.valid(owner, expected["claim"], now=142))
    case.assertIsNone(store.claim(owner, day_start=0, daily_limit=4, interval_seconds=0, now=142))
    for receipt, message, source in zip(expected["receipts"], expected["messages"], expected["sources"]):
        case.assertFalse(store.record_delivery(owner, receipt, now=143))
        case.assertIsNone(store.enqueue(message, owner, source["id"], source["digest"], now=143))
    for command in expected["commands"]:
        case.assertFalse(store.command(command, owner, now=144))
    case.assertEqual(store.items(owner), items)
    case.assertEqual(store.recent(owner, now=90000), [])
    case.assertFalse(store.valid(owner, expected["claim"], now=90000))
    case.assertFalse(store.finish(owner, expected["claim"], {"old_quote": "你周五有面试",
        "correction_quote": "你刚才说“你周五有面试”，其实我的面试改在周日。"}, now=90000))
    case.assertEqual(store.items(owner), items)


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"source"
        self.source.mkdir()
        self.archive = self.root/"backup.zip"
        self.target = self.root/"restore"

    def seed(self, all_ledgers=False):
        for name, tables in backup.LEDGERS.items():
            if not all_ledgers and name != "self_state.sqlite3":
                continue
            con = sqlite3.connect(self.source/name)
            try:
                for table in tables:
                    con.execute('CREATE TABLE "'+table+'" (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
                    con.execute('INSERT INTO "'+table+'"(payload) VALUES (?)', ("持久身份/主人/记忆/计划："+table,))
                con.commit()
            finally:
                con.close()

    def archive_now(self):
        return backup.backup_sqlite(self.source, self.archive, apply=True, writers_stopped=True)

    def restore_now(self):
        return backup.restore_sqlite(self.archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)

    def tamper(self, mutation):
        altered = self.root/"altered.zip"
        with zipfile.ZipFile(self.archive) as source:
            members = {info.filename: source.read(info) for info in source.infolist()}
        mutation(members)
        with zipfile.ZipFile(altered, "w") as target:
            for key, value in members.items():
                target.writestr(key, value)
        return altered

    def test_inventory_twenty_one_databases_82_tables_and_frozen_legacy_catalogs(self):
        self.assertEqual(len(backup.LEDGERS), 21)
        self.assertEqual(len(backup.TABLES), 82)
        self.assertEqual(len(backup.CATALOGS["v0.14"]), 10)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.14"].values())), 44)
        self.assertEqual(len(backup.CATALOGS["v0.15"]), 11)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.15"].values())), 47)
        self.assertEqual(len(backup.CATALOGS["v0.16"]), 12)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.16"].values())), 51)
        self.assertEqual(len(backup.CATALOGS["v0.17"]), 13)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.17"].values())), 54)
        self.assertEqual(len(backup.CATALOGS["v0.18"]), 14)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.18"].values())), 58)
        self.assertEqual(len(backup.CATALOGS["v0.19"]), 16)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.19"].values())), 66)
        self.assertEqual(len(backup.CATALOGS["v0.20"]), 18)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.20"].values())), 72)
        self.assertEqual(len(backup.CATALOGS["v0.21"]), 20)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.21"].values())), 78)
        self.assertEqual(backup.CATALOGS["v0.22"], backup.CATALOGS["v0.21"])
        self.assertIsNot(backup.CATALOGS["v0.22"], backup.CATALOGS["v0.21"])
        self.assertEqual(backup.CATALOGS["v0.23"], backup.CATALOGS["v0.22"])
        self.assertIsNot(backup.CATALOGS["v0.23"], backup.CATALOGS["v0.22"])
        self.assertEqual(backup.CURRENT_CATALOG, "v0.24")
        self.assertNotIn("repair.sqlite3", backup.V23_LEDGERS)
        self.assertEqual(set(backup.V24_LEDGERS)-set(backup.V23_LEDGERS), {"repair.sqlite3"})
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.14"]["bond.sqlite3"] = ("bond_profiles",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.15"]["continuity.sqlite3"] = ("continuity_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.16"]["attention.sqlite3"] = ("attention_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.17"]["recollection.sqlite3"] = ("recollection_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.18"]["habits.sqlite3"] = ("habits_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.18"]["followthrough.sqlite3"] = ("followthrough_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.19"]["projects.sqlite3"] = ("projects_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.19"]["shared_reading.sqlite3"] = ("shared_reading_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.20"]["advice.sqlite3"] = ("advice_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.20"]["reference.sqlite3"] = ("reference_control",)
        for version in ("v0.21", "v0.22", "v0.23", "v0.24"):
            with self.subTest(version=version), self.assertRaises(TypeError):
                backup.CATALOGS[version]["growth.sqlite3"] = ("replacement",)

    def test_full_offline_restore_drill_preserves_every_table(self):
        self.seed(all_ledgers=True)
        self.archive_now()
        self.assertEqual(backup.verify_archive(self.archive)["files"], 21)
        result = self.restore_now()
        self.assertEqual(result["files"], 21)
        for name, tables in backup.LEDGERS.items():
            original, restored = sqlite3.connect(self.source/name), sqlite3.connect(self.target/name)
            try:
                for table in tables:
                    self.assertEqual(original.execute('SELECT * FROM "'+table+'"').fetchall(), restored.execute('SELECT * FROM "'+table+'"').fetchall())
                self.assertEqual(original.execute("SELECT * FROM sqlite_sequence").fetchall(), restored.execute("SELECT * FROM sqlite_sequence").fetchall())
            finally:
                original.close()
                restored.close()

    def test_dry_run_never_creates_archive_or_restore_directory(self):
        self.seed()
        self.assertTrue(backup.backup_sqlite(self.source, self.archive)["dry_run"])
        self.assertFalse(self.archive.exists())
        self.archive_now()
        self.assertTrue(backup.restore_sqlite(self.archive, self.target)["dry_run"])
        self.assertFalse(self.target.exists())

    def test_requires_explicit_stop_and_restore_acknowledgement(self):
        self.seed()
        with self.assertRaisesRegex(backup.BackupError, "stop_writers"):
            backup.backup_sqlite(self.source, self.archive, apply=True)
        self.archive_now()
        with self.assertRaisesRegex(backup.BackupError, "restore_confirmation"):
            backup.restore_sqlite(self.archive, self.target, apply=True, writers_stopped=True)
        self.assertFalse(self.target.exists())

    def test_wal_committed_state_is_in_snapshot(self):
        self.seed()
        con = sqlite3.connect(self.source/"self_state.sqlite3")
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("UPDATE self_identity SET payload='committed WAL owner'")
            con.commit()
            self.archive_now()
            self.restore_now()
            restored = sqlite3.connect(self.target/"self_state.sqlite3")
            try:
                self.assertEqual(restored.execute("SELECT payload FROM self_identity").fetchone()[0], "committed WAL owner")
            finally:
                restored.close()
        finally:
            con.close()

    def test_existing_target_and_archive_never_overwritten(self):
        self.seed()
        self.archive_now()
        existing = self.archive.read_bytes()
        with self.assertRaisesRegex(backup.BackupError, "exists"):
            self.archive_now()
        self.assertEqual(self.archive.read_bytes(), existing)
        self.target.mkdir()
        marker = self.target/"real-user-data.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupError, "not_empty"):
            self.restore_now()
        self.assertEqual(marker.read_text(), "preserve")

    def test_restore_existing_empty_directory(self):
        self.seed()
        self.archive_now()
        self.target.mkdir()
        self.restore_now()
        self.assertTrue((self.target/"self_state.sqlite3").is_file())

    def test_corrupt_hash_refused_before_target_created(self):
        self.seed()
        self.archive_now()
        def mutation(members):
            key = "sqlite/self_state.sqlite3"
            members[key] = b"x"+members[key][1:]
        archive = self.tamper(mutation)
        with self.assertRaisesRegex(backup.BackupError, "hash_mismatch"):
            backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertFalse(self.target.exists())

    def test_archive_traversal_extra_members_and_duplicate_names_rejected(self):
        self.seed()
        self.archive_now()
        for name in ("../escape", "sqlite/../../escape", "C:/escape", "sqlite\\self_state.sqlite3", "/absolute"):
            with self.subTest(name=name):
                archive = self.tamper(lambda members: members.update({name: b"unsafe"}))
                with self.assertRaises(backup.BackupError):
                    backup.verify_archive(archive)
        self.assertFalse((self.root/"escape").exists())
        with zipfile.ZipFile(self.archive, "a") as package:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                package.writestr("manifest.json", "{}")
        with self.assertRaisesRegex(backup.BackupError, "inventory"):
            backup.verify_archive(self.archive)

    def test_symlink_archive_entry_rejected(self):
        self.seed()
        self.archive_now()
        archive = self.root/"link.zip"
        with zipfile.ZipFile(self.archive) as origin, zipfile.ZipFile(archive, "w") as destination:
            for item in origin.infolist():
                if item.filename.startswith("sqlite/"):
                    item.create_system = 3
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                destination.writestr(item, origin.read(item))
        with self.assertRaisesRegex(backup.BackupError, "unsupported_archive_entry"):
            backup.verify_archive(archive)

    def test_manifest_limits_and_missing_inventory_checked(self):
        self.seed()
        self.archive_now()
        for change in (lambda m: m["files"][0].update(size=backup.MAX_FILE+1), lambda m: m.update(missing=[]), lambda m: m["files"][0].update(path=["bad"])):
            def mutate(members):
                manifest = json.loads(members["manifest.json"])
                change(manifest)
                members["manifest.json"] = json.dumps(manifest).encode()
            archive = self.tamper(mutate)
            with self.assertRaises(backup.BackupError):
                backup.verify_archive(archive)

    def test_readonly_source_rejects_unexpected_schema(self):
        self.seed()
        con = sqlite3.connect(self.source/"self_state.sqlite3")
        con.execute("CREATE VIEW secret_view AS SELECT * FROM self_identity")
        con.close()
        with self.assertRaisesRegex(backup.BackupError, "unexpected_sqlite_schema"):
            self.archive_now()

    def test_source_unchanged_and_missing_optional_libs_recorded(self):
        self.seed()
        path = self.source/"self_state.sqlite3"
        before = path.read_bytes()
        result = self.archive_now()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("companion.sqlite3", result["missing"])
        self.assertEqual(backup.verify_archive(self.archive)["missing"], result["missing"])

    def legacy_archive(self, *, explicit=False, catalog="v0.14"):
        self.seed(all_ledgers=True)
        self.archive_now()
        def mutation(members):
            manifest = json.loads(members["manifest.json"])
            excluded = set(backup.LEDGERS) - set(backup.CATALOGS[catalog])
            manifest["files"] = [item for item in manifest["files"] if item["path"].removeprefix("sqlite/") not in excluded]
            for filename in excluded:
                members.pop("sqlite/"+filename)
            if explicit:
                manifest["catalog"] = catalog
            else:
                manifest["format"] = backup.LEGACY_FORMAT
                manifest.pop("catalog")
            members["manifest.json"] = json.dumps(manifest).encode()
        return self.tamper(mutation)

    def test_legacy_v14_ten_ledger_archive_restores_and_reports_unprovided_bond(self):
        archive = self.legacy_archive()
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.14")
        self.assertEqual(result["files"], 10)
        self.assertEqual(result["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertFalse(self.target.exists())
        result = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(result["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertFalse((self.target/"bond.sqlite3").exists())
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V14_LEDGERS))
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(json.loads(package.read("manifest.json"))["missing"], [])

    def test_explicit_known_v14_catalog_is_supported(self):
        archive = self.legacy_archive(explicit=True)
        self.assertEqual(backup.verify_archive(archive)["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])

    def test_v15_eleven_ledger_archive_restores_without_inventing_continuity(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.15")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.15")
        self.assertEqual(result["files"], 11)
        self.assertEqual(result["missing"], ["continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        result = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(result["missing"], ["continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V15_LEDGERS))

    def test_v15_catalog_cannot_smuggle_continuity_or_hide_missing_file(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.15")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["continuity.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/continuity.sqlite3"),
                       lambda m: m.update(catalog="v0.16")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v16_twelve_ledger_archive_restores_without_inventing_attention(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.16")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.16")
        self.assertEqual(result["files"], 12)
        self.assertEqual(result["missing"], ["attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], ["attention.sqlite3", "recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V16_LEDGERS))
        self.assertFalse((self.target/"attention.sqlite3").exists())

    def test_v16_catalog_cannot_hide_omissions_or_smuggle_attention(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.16")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["attention.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/attention.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(attention_events=0),
                       lambda m: m.update(catalog="v0.17")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v17_thirteen_ledger_archive_restores_without_inventing_recollection(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.17")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.17")
        self.assertEqual(result["files"], 13)
        self.assertEqual(result["missing"], ["recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], ["recollection.sqlite3", "habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V17_LEDGERS))
        self.assertFalse((self.target/"recollection.sqlite3").exists())

    def test_v17_catalog_cannot_hide_omissions_or_smuggle_recollection(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.17")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["recollection.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/recollection.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(recollection_inputs=0),
                       lambda m: m.update(catalog="v0.18")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v18_fourteen_ledger_archive_restores_without_inventing_habits_or_followthrough(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.18")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.18")
        self.assertEqual(result["files"], 14)
        self.assertEqual(result["missing"], ["habits.sqlite3", "followthrough.sqlite3", "projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], result["missing"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], result["missing"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V18_LEDGERS))
        for name in ("habits.sqlite3", "followthrough.sqlite3"):
            self.assertFalse((self.target/name).exists())

    def test_v18_catalog_cannot_hide_omissions_or_smuggle_new_ledgers(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.18")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["habits.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/followthrough.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(habits_preferences=0),
                       lambda m: m.update(catalog="v0.19")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v19_sixteen_ledger_archive_reports_new_progress_as_missing(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.19")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.19")
        self.assertEqual(result["files"], 16)
        self.assertEqual(result["missing"], ["projects.sqlite3", "shared_reading.sqlite3", "advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], result["missing"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], result["missing"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V19_LEDGERS))
        for name in result["missing"]:
            self.assertFalse((self.target/name).exists())

    def test_v19_catalog_cannot_hide_or_smuggle_project_and_shared_reading_state(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.19")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["projects.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/shared_reading.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(projects_items=0),
                       lambda m: m.update(catalog="v0.20")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v20_eighteen_ledger_archive_reports_advice_and_reference_as_missing(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.20")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.20")
        self.assertEqual(result["files"], 18)
        self.assertEqual(result["missing"], ["advice.sqlite3", "reference.sqlite3", "repair.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], result["missing"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], result["missing"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V20_LEDGERS))
        for name in result["missing"]:
            self.assertFalse((self.target/name).exists())

    def test_v20_catalog_cannot_hide_or_smuggle_advice_and_reference_state(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.20")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["advice.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/reference.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(advice_items=0),
                       lambda m: m.update(catalog="v0.21")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_catalog_version_cannot_hide_missing_files_or_smuggle_new_ledger(self):
        archive = self.legacy_archive()
        with zipfile.ZipFile(archive) as package:
            legacy = json.loads(package.read("manifest.json"))
        for change in (
            lambda m: m["files"].pop(),
            lambda m: m.update(missing=["bond.sqlite3"]),
            lambda m: m["files"][0].update(path="sqlite/bond.sqlite3"),
            lambda m: m.update(catalog="v0.15"),
            lambda m: m["files"][0]["tables"].update(bond_profiles=0),
        ):
            candidate = copy.deepcopy(legacy)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v21_archive_restores_without_new_database_requirements(self):
        self.check_same_schema_archive("v0.21")

    def test_v22_archive_restores_without_new_database_requirements(self):
        self.check_same_schema_archive("v0.22")

    def test_v23_archive_restores_without_inventing_repair_records(self):
        self.check_same_schema_archive("v0.23")
        self.assertFalse((self.target/"repair.sqlite3").exists())

    def test_v22_real_attention_payload_restores_without_invented_workload(self):
        from MaiLivingWorld.attention_store import AttentionStore
        owner = "12345678"
        candidate = {"kind": "reading", "key": "reading:"+"a"*64, "version": "b"*64}
        store = AttentionStore(self.source/"attention.sqlite3")
        try:
            store.capture(attention_message(), owner_id=owner, now=100, engagement_seconds=1)
            claim = store.claim(owner, candidates=[candidate], day_start=0, daily_limit=6, interval_seconds=0, now=101)
            self.assertTrue(store.finish(owner, claim["token"], outcome="progress", now=102))
            payload = json.loads(store.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (owner,)).fetchone()[0])
            payload.pop("activity")
            payload.pop("lease_activity")
            with store.con:
                store.con.execute("UPDATE attention_control SET payload=? WHERE owner_id=?", (json.dumps(payload), owner))
        finally:
            store.close()
        self.archive_now()
        def old_catalog(members):
            manifest = json.loads(members["manifest.json"])
            manifest["catalog"] = "v0.22"
            manifest["missing"].remove("repair.sqlite3")
            members["manifest.json"] = json.dumps(manifest).encode()
        archive = self.tamper(old_catalog)
        backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        restored = AttentionStore(self.target/"attention.sqlite3")
        try:
            state = restored.state(owner, now=103)
            self.assertEqual(state["focus"], candidate)
            self.assertEqual(state["cycle_count"], 1)
            self.assertEqual(state["last_outcome"], "progress")
            self.assertEqual(state["activity"]["load"], 0.)
            self.assertFalse(state["activity"]["recovering"])
            self.assertFalse(any(event["action"] == "activity_start" for event in restored.events(owner)))
            claim = restored.claim(owner, candidates=[candidate], day_start=0, daily_limit=6,
                                   interval_seconds=0, now=104, activity_awareness=True)
            self.assertTrue(restored.start_activity(owner, claim["token"], proof="a"*64, signature="b"*64, now=104))
            self.assertTrue(restored.finish(owner, claim["token"], outcome="progress", now=105,
                                           activity_awareness=True, activity_proof="c"*64))
            self.assertEqual(restored.state(owner, now=105)["activity"]["load"], .25)
        finally:
            restored.close()

    def check_same_schema_archive(self, catalog):
        archive = self.legacy_archive(explicit=True, catalog=catalog)
        checked = backup.verify_archive(archive)
        self.assertEqual((checked["catalog"], checked["files"], checked["missing"]), (catalog, 20, ["repair.sqlite3"]))
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True,
                                         confirm=backup.CONFIRM)
        self.assertEqual((restored["files"], restored["missing"]), (20, ["repair.sqlite3"]))
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.CATALOGS[catalog]))

    def test_new_archive_requires_explicit_known_catalog_and_complete_inventory(self):
        self.seed()
        self.archive_now()
        with zipfile.ZipFile(self.archive) as package:
            current = json.loads(package.read("manifest.json"))
        self.assertEqual(current["format"], backup.FORMAT)
        self.assertEqual(current["catalog"], "v0.24")
        for change in (lambda m: m.pop("catalog"), lambda m: m.update(catalog="future"),
                       lambda m: m.update(catalog=[]), lambda m: m["missing"].remove("bond.sqlite3")):
            candidate = copy.deepcopy(current)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_real_store_schema_identity_budget_growth_and_plan_survive_restore(self):
        classes = (("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
            ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
            ("experience_loop", "ExperienceLoop", "experience_loop"), ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
            ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
            ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"),
            ("bond_store", "BondStore", "bond"), ("continuity_store", "ContinuityStore", "continuity"),
            ("attention_store", "AttentionStore", "attention"),
            ("recollection_store", "RecollectionStore", "recollection"),
            ("habits_store", "HabitsStore", "habits"), ("followthrough_store", "FollowthroughStore", "followthrough"),
            ("projects_store", "ProjectsStore", "projects"), ("shared_reading_store", "SharedReadingStore", "shared_reading"),
            ("advice_store", "AdviceStore", "advice"), ("reference_store", "ReferenceStore", "reference"), ("repair_store", "RepairStore", "repair"))
        for module, name, filename in classes:
            store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(self.source/(filename+".sqlite3"))
            try:
                if module == "self_state":
                    identity = store.configure_owner("12345678", now=100)
                    self.assertTrue(store.reserve_thought(now=101, day_start=0, min_interval_seconds=0, daily_limit=2))
                elif module == "growth_store":
                    growth = seed_belief_revision(store)
                elif module == "companion_store":
                    plan = store.propose_plan(owner_id="12345678", scope="private:12345678", title="一起读书",
                        next_step="第一章", message_id="trusted-real-schema-test", summary_hash="a"*64, now=100)
                    plan = store.command_plan(plan["id"], owner_id="12345678", action="accept", expected_revision=plan["revision"],
                        message_id="trusted-accept", summary_hash="b"*64, now=101)
                elif module == "bond_store":
                    message = {"platform": "qq", "session_id": "owner-test-stream", "message_id": "bond-backup-test",
                        "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
                        "processed_plain_text": "记下这次一起读书的约定"}
                    moment = store.confirm_moment(message, owner_id="12345678", summary="一起读书的约定", now=101)
                    store.command(dict(message, message_id="bond-ban", processed_plain_text="不要叫我老板"),
                        owner_id="12345678", action="ban_address", value="老板", now=102)
                    old = store.confirm_moment(dict(message, message_id="bond-old", processed_plain_text="记下旧回忆"),
                        owner_id="12345678", summary="旧回忆", now=103)
                    retired = store.retire_moment(dict(message, message_id="bond-retire", processed_plain_text="撤回旧回忆"),
                        owner_id="12345678", moment_id=old["id"], expected_revision=old["revision"], now=104)
                elif module == "continuity_store":
                    message = {"platform": "qq", "session_id": "owner-test-stream", "message_id": "continuity-backup-input",
                        "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
                        "processed_plain_text": "我有点担心考试"}
                    source = store.enqueue(message, owner_id="12345678", now=100)
                    batch = store.claim("12345678", now=101)
                    store.complete(batch["id"], batch["token"], {"updates": [{"action": "open", "thread_id": "",
                        "kind": "concern", "title": "考试", "summary": "担心考试", "evidence": [{"id": source, "quote": "担心考试"}]}]},
                        owner_id="12345678", expected_revision=batch["base_revision"], now=102)
                    old_thread = store.threads("12345678", now=102)[0]
                    source = store.enqueue(dict(message, message_id="continuity-backup-resolved", processed_plain_text="考试已经结束了"),
                                           owner_id="12345678", now=103)
                    batch = store.claim("12345678", now=104)
                    store.complete(batch["id"], batch["token"], {"updates": [{"action": "resolve", "thread_id": old_thread["id"],
                        "kind": "concern", "title": "考试", "summary": "考试已结束", "evidence": [{"id": source, "quote": "考试已经结束了"}]}]},
                        owner_id="12345678", expected_revision=batch["base_revision"], now=105)
                    store.command(dict(message, message_id="continuity-backup-busy", processed_plain_text="接下来我要忙一会儿"),
                        owner_id="12345678", action="contact_pause", value="busy", until=500, now=106)
                    self.assertTrue(store.reserve_cycle("12345678", day_start=0, interval_seconds=0, daily_limit=2, now=107))
                    continuity_state = store.state("12345678", now=108)
                    continuity_threads = store.threads("12345678", status="all", now=108)
                elif module == "attention_store":
                    attention = seed_attention(store)
                    attention_rows = {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
                                      for table in backup.LEDGERS["attention.sqlite3"]}
                elif module == "recollection_store":
                    recollection_rows = seed_recollection(store)
                elif module == "habits_store":
                    habits_rows = seed_habits(store)
                elif module == "followthrough_store":
                    followthrough_rows = seed_followthrough(store)
                elif module == "projects_store":
                    projects_rows = seed_projects(store)
                elif module == "shared_reading_store":
                    shared_reading_rows = seed_shared_reading(store)
                elif module == "advice_store":
                    advice_rows = seed_advice(store)
                elif module == "reference_store":
                    reference_rows = seed_reference(store)
                elif module == "repair_store":
                    repair_rows = seed_repair(store)
            finally:
                store.close()
        self.archive_now()
        self.restore_now()
        from MaiLivingWorld.self_state import SelfState
        from MaiLivingWorld.growth_store import GrowthStore
        from MaiLivingWorld.companion_store import CompanionStore
        from MaiLivingWorld.bond_store import BondStore
        from MaiLivingWorld.continuity_store import ContinuityStore
        from MaiLivingWorld.attention_store import AttentionStore
        from MaiLivingWorld.recollection_store import RecollectionStore
        from MaiLivingWorld.habits_store import HabitsStore
        from MaiLivingWorld.followthrough_store import FollowthroughStore
        from MaiLivingWorld.projects_store import ProjectsStore
        from MaiLivingWorld.shared_reading_store import SharedReadingStore
        from MaiLivingWorld.advice_store import AdviceStore
        from MaiLivingWorld.reference_store import ReferenceStore
        from MaiLivingWorld.repair_store import RepairStore
        stores = [SelfState(self.target/"self_state.sqlite3"), GrowthStore(self.target/"growth.sqlite3"),
                  CompanionStore(self.target/"companion.sqlite3"), BondStore(self.target/"bond.sqlite3"),
                  ContinuityStore(self.target/"continuity.sqlite3"), AttentionStore(self.target/"attention.sqlite3"),
                  RecollectionStore(self.target/"recollection.sqlite3"), HabitsStore(self.target/"habits.sqlite3"),
                  FollowthroughStore(self.target/"followthrough.sqlite3"), ProjectsStore(self.target/"projects.sqlite3"),
                  SharedReadingStore(self.target/"shared_reading.sqlite3"), AdviceStore(self.target/"advice.sqlite3"),
                  ReferenceStore(self.target/"reference.sqlite3"), RepairStore(self.target/"repair.sqlite3")]
        try:
            current = stores[0].state()
            self.assertEqual(current["agent_id"], identity["agent_id"])
            self.assertEqual(current["owner_id"], identity["owner_id"])
            self.assertEqual(current["thought_count"], 1)
            assert_belief_revision_restore(self, stores[1], growth)
            self.assertEqual(stores[2].plan(plan["id"], owner_id="12345678"), plan)
            self.assertEqual(stores[3].moments("12345678"), [moment])
            self.assertEqual(stores[3].moments("12345678", status="retired"), [retired])
            self.assertEqual(stores[3].profile("12345678")["blocked_addresses"], ["老板"])
            self.assertEqual(len(stores[3].audit("12345678")), 4)
            self.assertEqual(stores[4].state("12345678", now=108), continuity_state)
            self.assertEqual(stores[4].threads("12345678", status="all", now=108), continuity_threads)
            self.assertEqual(stores[4].threads("12345678", now=108), [])
            self.assertEqual(stores[4].state("12345678", now=501)["contact"]["effective_mode"], "normal")
            assert_attention_restore(self, stores[5], attention)
            for table in backup.LEDGERS["attention.sqlite3"]:
                self.assertEqual([dict(row) for row in stores[5].con.execute("SELECT * FROM "+table)], attention_rows[table])
            stores[5].capture(attention_message(), owner_id="12345678", now=104, engagement_seconds=1)
            self.assertEqual(stores[5].state("12345678", now=104)["revision"], attention["revision"])
            self.assertEqual(stores[5].state("12345678", now=104)["cycle_count"], 1)
            self.assertEqual(stores[5].con.execute("SELECT COUNT(*) FROM attention_receipts").fetchone()[0], 1)
            assert_recollection_restore(self, stores[6], recollection_rows)
            assert_v19_restore(self, stores[7], habits_rows)
            assert_v19_restore(self, stores[8], followthrough_rows)
            assert_v20_restore(self, stores[9], projects_rows)
            assert_v20_restore(self, stores[10], shared_reading_rows)
            assert_v21_restore(self, stores[11], advice_rows)
            assert_v21_restore(self, stores[12], reference_rows)
            assert_repair_restore(self, stores[13], repair_rows)
        finally:
            for store in stores:
                store.close()


def schema():
    return [{"Field": "id", "Type": "bigint", "Null": "NO", "Key": "PRI", "Default": None, "Extra": "auto_increment", "Collation": None},
            {"Field": "payload", "Type": "longtext", "Null": "NO", "Key": "", "Default": None, "Extra": "", "Collation": "utf8mb4_0900_ai_ci"}]


class FakeMySQL:
    def __init__(self, rows=None, fail=False, prefix="mlw_"):
        self.prefix = prefix
        self.rows = copy.deepcopy(rows if rows is not None else {"self_identity": [], "companion_plans": []})
        self.schemas = {table: schema() for table in self.rows}
        self.increments = {table: 900 for table in self.rows}
        self.cached_increments = dict(self.increments)
        self.stats_expiry = 86400
        self.unopened = set()
        self.operations, self.result = [], []
        self.closed = self.committed = False
        self.before = copy.deepcopy(self.rows)
        self.fail = fail
        self.definition_suffix = ""

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.operations.append((sql, params))
        self.result = []
        if sql.startswith("START TRANSACTION"):
            self.before = copy.deepcopy(self.rows)
        elif sql == "SET SESSION information_schema_stats_expiry = 0":
            self.stats_expiry = 0
        elif sql.startswith("SHOW TABLE STATUS"):
            counters = self.increments if self.stats_expiry == 0 else self.cached_increments
            self.result = [{"Name": self.prefix+table, "Engine": "InnoDB", "Auto_increment": None if table in self.unopened else counters[table]}
                           for table in self.rows if params is None or self.prefix+table == params[0]]
        elif sql.startswith("SHOW FULL COLUMNS"):
            table = re.findall(r"`([^`]+)`", sql)[0][len(self.prefix):]
            self.result = copy.deepcopy(self.schemas[table])
        elif sql.startswith("SHOW CREATE TABLE"):
            table = re.findall(r"`([^`]+)`", sql)[0]
            self.unopened.discard(table[len(self.prefix):])
            self.result = [{"Create Table": "CREATE TABLE `"+table+"` (id BIGINT PRIMARY KEY AUTO_INCREMENT, payload LONGTEXT) ENGINE=InnoDB AUTO_INCREMENT=900 "+self.definition_suffix}]
        elif sql.startswith("SELECT"):
            physical = re.search(r" FROM `([^`]+)`", sql).group(1)
            table = physical[len(self.prefix):]
            self.result = sorted(copy.deepcopy(self.rows[table]), key=lambda row: row["id"])
            if " LIMIT 1" in sql:
                self.result = self.result[:1]
        elif sql.startswith("ALTER TABLE"):
            physical = re.findall(r"`([^`]+)`", sql)[0]
            self.increments[physical[len(self.prefix):]] = int(sql.rsplit(" ", 1)[-1])
        elif sql.startswith("INSERT INTO"):
            if self.fail:
                raise RuntimeError("super-private-password and database URL")
            physical, *columns = re.findall(r"`([^`]+)`", sql)
            self.rows[physical[len(self.prefix):]].append(dict(zip(columns, params)))

    def fetchone(self):
        return self.result.pop(0) if self.result else None

    def fetchall(self):
        rows, self.result = self.result, []
        return rows

    def fetchmany(self, size):
        rows, self.result = self.result[:size], self.result[size:]
        return rows

    def rollback(self):
        self.rows = copy.deepcopy(self.before)

    def commit(self):
        self.committed = True
        self.before = copy.deepcopy(self.rows)

    def close(self):
        self.closed = True


class MySQLBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name)/"mysql.zip"
        self.config = {"prefix": "mlw_", "password": "secret-not-archived"}
        self.data = {"self_identity": [{"id": 2, "payload": "persistent-owner"}], "companion_plans": [{"id": 8, "payload": "共同计划"}]}
        self.source = FakeMySQL(self.data)
        backup.backup_mysql(self.config, self.archive, apply=True, writers_stopped=True, connect=lambda _: self.source)

    def restore(self, target, **kwargs):
        options = dict(apply=True, writers_stopped=True, confirm=backup.CONFIRM, connect=lambda _: target)
        options.update(kwargs)
        return backup.restore_mysql(self.archive, self.config, **options)

    def test_consistent_snapshot_and_roundtrip_single_commit(self):
        self.assertIn(("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY", None), self.source.operations)
        target = FakeMySQL()
        target.increments = {key: 1 for key in target.rows}
        result = self.restore(target)
        self.assertEqual(result["rows"], 2)
        self.assertTrue(target.committed)
        self.assertTrue(target.closed)
        self.assertEqual(target.rows, self.data)
        self.assertEqual(set(target.increments.values()), {900})
        self.assertTrue(any("FOR UPDATE" in sql for sql, _ in target.operations))

    def test_dry_run_reads_but_never_inserts_or_alters(self):
        target = FakeMySQL()
        self.assertTrue(self.restore(target, apply=False)["dry_run"])
        self.assertFalse(target.committed)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_nonempty_target_refused_before_any_mutation(self):
        target = FakeMySQL({"self_identity": [{"id": 99, "payload": "real-owner"}], "companion_plans": []})
        before = copy.deepcopy(target.rows)
        with self.assertRaisesRegex(backup.BackupError, "not_empty"):
            self.restore(target)
        self.assertEqual(target.rows, before)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_stale_null_or_low_counter_cache_never_changes_archive_or_restore(self):
        for stale in (None, 1):
            with self.subTest(stale=stale):
                archive = Path(self.temp.name)/("stale-"+str(stale)+".zip")
                source = FakeMySQL(self.data)
                source.cached_increments = {key: stale for key in source.rows}
                backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
                with zipfile.ZipFile(archive) as package:
                    manifest = json.loads(package.read("manifest.json"))
                self.assertEqual({item["auto_increment"] for item in manifest["files"]}, {900})
                statements = [sql for sql, _ in source.operations]
                self.assertLess(statements.index("SET SESSION information_schema_stats_expiry = 0"), statements.index("SHOW TABLE STATUS"))
                # The same nonempty source must be refused for its rows, without
                # reporting a fake schema change caused by cached NULL -> 1.
                with self.assertRaisesRegex(backup.BackupError, "^target_not_empty$"):
                    backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                        confirm=backup.CONFIRM, connect=lambda _: source)
                target = FakeMySQL()
                target.increments = {key: 1 for key in target.rows}
                target.cached_increments = {key: None for key in target.rows}
                backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                    confirm=backup.CONFIRM, connect=lambda _: target)
                self.assertEqual(target.rows, self.data)
                self.assertEqual(set(target.increments.values()), {900})

    def test_missing_live_counter_is_rejected_not_coerced_to_one(self):
        source = FakeMySQL(self.data)
        source.increments["self_identity"] = None
        archive = Path(self.temp.name)/"unreliable.zip"
        with self.assertRaisesRegex(backup.BackupError, "^mysql_auto_increment_mismatch$"):
            backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        self.assertFalse(archive.exists())

    def test_unopened_tables_are_opened_before_reading_native_counter(self):
        archive = Path(self.temp.name)/"unopened.zip"
        source = FakeMySQL(self.data)
        source.unopened = set(source.rows)
        source.increments = {key: 1900 for key in source.rows}
        backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        self.assertEqual({item["auto_increment"] for item in manifest["files"]}, {1900})
        target = FakeMySQL()
        target.unopened = set(target.rows)
        target.increments = {key: 1 for key in target.rows}
        backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertEqual(target.rows, self.data)
        self.assertEqual(set(target.increments.values()), {1900})
        for con in (source, target):
            for table in con.rows:
                physical = con.prefix+table
                opened = next(i for i, (sql, _) in enumerate(con.operations) if sql == "SHOW CREATE TABLE `"+physical+"`")
                read = next(i for i, (sql, args) in enumerate(con.operations) if sql == "SHOW TABLE STATUS WHERE Name = %s" and args == (physical,))
                self.assertLess(opened, read)

    def test_non_auto_increment_tables_keep_null_counter(self):
        source = FakeMySQL(self.data)
        for name in source.rows:
            source.increments[name] = None
            source.schemas[name][0]["Extra"] = ""
        archive = Path(self.temp.name)/"no-increments.zip"
        backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        self.assertTrue(backup.verify_archive(archive)["verified"])
        target = FakeMySQL()
        for name in target.rows:
            target.increments[name] = None
            target.schemas[name][0]["Extra"] = ""
        backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertEqual(target.rows, self.data)
        self.assertFalse(any(sql.startswith("ALTER TABLE") for sql, _ in target.operations))

    def test_schema_and_indexes_must_match(self):
        for changed in ("column", "index"):
            target = FakeMySQL()
            if changed == "column":
                target.schemas["self_identity"][1]["Type"] = "text"
            else:
                target.definition_suffix = "EXTRA UNIQUE INDEX"
            with self.subTest(changed=changed), self.assertRaisesRegex(backup.BackupError, "mismatch"):
                self.restore(target)
            self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_restore_failure_rolls_back_every_insert_and_redacts_errors(self):
        target = FakeMySQL(fail=True)
        with self.assertRaisesRegex(backup.BackupError, "^mysql_restore_failed$"):
            self.restore(target)
        self.assertFalse(target.committed)
        self.assertEqual(target.rows, {"self_identity": [], "companion_plans": []})

    def test_credential_and_raw_schema_not_archived(self):
        with zipfile.ZipFile(self.archive) as package:
            manifest = package.read("manifest.json")
        self.assertNotIn(b"secret-not-archived", manifest)
        self.assertNotIn(b"CREATE TABLE", manifest)
        self.assertNotIn(b"persistent-owner", manifest)

    def test_bare_or_foreign_schema_rejected(self):
        for rows in ({}, {"business_data": []}):
            with self.subTest(rows=rows), self.assertRaises(backup.BackupError):
                self.restore(FakeMySQL(rows))

    def test_legacy_mysql_44_tables_require_matching_schema_before_upgrade(self):
        old_tables = {table: [] for group in backup.V14_LEDGERS.values() for table in group}
        old_tables.update(copy.deepcopy(self.data))
        source = FakeMySQL(old_tables)
        current = Path(self.temp.name)/"old-schema.zip"
        legacy = Path(self.temp.name)/"legacy-v14.zip"
        backup.backup_mysql(self.config, current, apply=True, writers_stopped=True, connect=lambda _: source)
        with zipfile.ZipFile(current) as origin, zipfile.ZipFile(legacy, "w") as destination:
            for info in origin.infolist():
                data = origin.read(info)
                if info.filename == "manifest.json":
                    manifest = json.loads(data)
                    manifest["format"] = backup.LEGACY_FORMAT
                    manifest.pop("catalog")
                    data = json.dumps(manifest).encode()
                destination.writestr(info, data)
        self.assertEqual(backup.verify_archive(legacy)["files"], 44)
        new_target = FakeMySQL({table: [] for table in backup.TABLES})
        with self.assertRaisesRegex(backup.BackupError, "^mysql_catalog_requires_matching_schema_then_upgrade$"):
            backup.restore_mysql(legacy, self.config, apply=True, writers_stopped=True,
                confirm=backup.CONFIRM, connect=lambda _: new_target)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in new_target.operations))
        old_target = FakeMySQL({table: [] for table in old_tables})
        backup.restore_mysql(legacy, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: old_target)
        self.assertEqual(old_target.rows, old_tables)

    def test_mysql_value_codec_is_exact_and_rejects_nonfinite(self):
        values = [None, True, 7, "中文", 1.25, b"\x00\xff", Decimal("1.2300"), datetime(2026, 1, 2, 3, 4), date(2026, 1, 2), time(3, 4), timedelta(days=-1, microseconds=17)]
        for value in values:
            self.assertEqual(backup._decode(backup._loads(backup._json(backup._encode(value)))), value)
        for value in (float("inf"), float("nan"), Decimal("NaN")):
            with self.assertRaises(backup.BackupError):
                backup._encode(value)

    def test_v15_v16_v17_v18_v19_v20_mysql_catalogs_require_matching_schema_before_upgrade(self):
        for catalog, expected_count in (("v0.15", 47), ("v0.16", 51), ("v0.17", 54), ("v0.18", 58), ("v0.19", 66), ("v0.20", 72)):
            with self.subTest(catalog=catalog):
                old_tables = {table: [] for group in backup.CATALOGS[catalog].values() for table in group}
                old_tables.update(copy.deepcopy(self.data))
                old_tables["bond_moments"] = [{"id": 19, "payload": "保留旧主人纪念与撤回状态"}]
                if catalog in {"v0.16", "v0.17", "v0.18", "v0.19"}:
                    old_tables["continuity_threads"] = [{"id": 20, "payload": "保留已解决话题及原话来源"}]
                current = Path(self.temp.name)/(catalog+"-source.zip")
                archive = Path(self.temp.name)/(catalog+"-schema.zip")
                backup.backup_mysql(self.config, current, apply=True, writers_stopped=True,
                                    connect=lambda _: FakeMySQL(old_tables))
                with zipfile.ZipFile(current) as origin, zipfile.ZipFile(archive, "w") as destination:
                    for info in origin.infolist():
                        data = origin.read(info)
                        if info.filename == "manifest.json":
                            manifest = json.loads(data)
                            manifest["catalog"] = catalog
                            data = json.dumps(manifest).encode()
                        destination.writestr(info, data)
                self.assertEqual(backup.verify_archive(archive)["catalog"], catalog)
                self.assertEqual(backup.verify_archive(archive)["files"], expected_count)
                new_target = FakeMySQL({table: [] for table in backup.TABLES})
                with self.assertRaisesRegex(backup.BackupError, "^mysql_catalog_requires_matching_schema_then_upgrade$"):
                    backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                        confirm=backup.CONFIRM, connect=lambda _: new_target)
                self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in new_target.operations))
                old_target = FakeMySQL({table: [] for table in old_tables})
                backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                    confirm=backup.CONFIRM, connect=lambda _: old_target)
                self.assertEqual(old_target.rows, old_tables)

    def test_config_only_uses_supplied_environment_and_never_reveals_credentials(self):
        env = {"MLW_MYSQL_USER": "operator", "MLW_MYSQL_PASSWORD": "private", "MLW_MYSQL_DATABASE": "restore_only", "MLW_MYSQL_PREFIX": "mlw_"}
        config = backup.mysql_config(env)
        self.assertEqual(config["password"], "private")
        env["MLW_MYSQL_PORT"] = "private"
        with self.assertRaisesRegex(backup.BackupError, "^invalid_mysql_configuration$"):
            backup.mysql_config(env)

    def test_v21_mysql_archive_requires_matching_old_schema(self):
        self.check_same_schema_archive("v0.21")

    def test_v22_mysql_archive_requires_matching_old_schema(self):
        self.check_same_schema_archive("v0.22")

    def test_v23_mysql_archive_requires_matching_old_schema(self):
        self.check_same_schema_archive("v0.23")

    def check_same_schema_archive(self, catalog):
        rows = {table: [] for tables in backup.CATALOGS[catalog].values() for table in tables}
        rows.update(copy.deepcopy(self.data))
        source = Path(self.temp.name)/(catalog+"-source.zip")
        archive = Path(self.temp.name)/(catalog+"-schema.zip")
        backup.backup_mysql(self.config, source, apply=True, writers_stopped=True,
                            connect=lambda _: FakeMySQL(rows))
        with zipfile.ZipFile(source) as origin, zipfile.ZipFile(archive, "w") as destination:
            for info in origin.infolist():
                data = origin.read(info)
                if info.filename == "manifest.json":
                    manifest = json.loads(data)
                    manifest["catalog"] = catalog
                    data = json.dumps(manifest).encode()
                destination.writestr(info, data)
        target = FakeMySQL({table: [] for table in backup.TABLES})
        checked = backup.verify_archive(archive)
        self.assertEqual((checked["catalog"], checked["files"]), (catalog, 78))
        with self.assertRaisesRegex(backup.BackupError, "^mysql_catalog_requires_matching_schema_then_upgrade$"):
            backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                                 confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))
        target = FakeMySQL({table: [] for table in rows})
        restored = backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                                        confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertEqual(restored["tables"], 78)
        self.assertEqual(target.rows, rows)


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1" and os.getenv("MLW_BACKUP_MYSQL_DATABASE", "").startswith("mlw_backup_test"),
                     "Only with an explicitly configured empty dedicated MySQL CI database")
class BackupMySQLLiveTests(unittest.TestCase):
    def test_real_82_table_archive_empty_restore_and_counter_high_water(self):
        import pymysql
        from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ensure_schema
        from MaiLivingWorld.self_state import SelfState
        from MaiLivingWorld.growth_store import GrowthStore
        from MaiLivingWorld.attention_store import AttentionStore
        from MaiLivingWorld.recollection_store import RecollectionStore
        from MaiLivingWorld.habits_store import HabitsStore
        from MaiLivingWorld.followthrough_store import FollowthroughStore
        from MaiLivingWorld.projects_store import ProjectsStore
        from MaiLivingWorld.shared_reading_store import SharedReadingStore
        from MaiLivingWorld.advice_store import AdviceStore
        from MaiLivingWorld.reference_store import ReferenceStore
        from MaiLivingWorld.repair_store import RepairStore
        env = dict(os.environ, MLW_MYSQL_DATABASE=os.environ["MLW_BACKUP_MYSQL_DATABASE"], MLW_MYSQL_PREFIX="backup_")
        config = backup.mysql_config(env)
        settings = {key: value for key, value in config.items() if key != "prefix"}
        settings["table_prefix"] = config["prefix"]
        connection = pymysql.connect(**{key: value for key, value in config.items() if key != "prefix"}, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                self.assertEqual(cursor.fetchall(), (), "backup CI database must be empty before this test")
            with tempfile.TemporaryDirectory() as temp:
                directory, archive = Path(temp), Path(temp)/"mysql.zip"
                created = False
                try:
                    created = True
                    ensure_schema(settings, directory)
                    state = SelfState(directory/"unused.sqlite3", mysql_settings=settings)
                    try:
                        identity = state.configure_owner("12345678", now=100)
                        state.reserve_thought(now=101, day_start=0, min_interval_seconds=0, daily_limit=2)
                    finally:
                        state.close()
                    growth = GrowthStore(directory/"unused-growth.sqlite3", mysql_settings=settings)
                    try:
                        growth_before = seed_belief_revision(growth)
                    finally:
                        growth.close()
                    attention = AttentionStore(directory/"unused-attention.sqlite3", mysql_settings=settings)
                    try:
                        attention_before = seed_attention(attention)
                    finally:
                        attention.close()
                    recollection = RecollectionStore(directory/"unused-recollection.sqlite3", mysql_settings=settings)
                    try:
                        recollection_before = seed_recollection(recollection)
                    finally:
                        recollection.close()
                    v19_before = {}
                    for name, cls, seed in (("habits", HabitsStore, seed_habits),
                                            ("followthrough", FollowthroughStore, seed_followthrough)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            v19_before[name] = seed(store)
                        finally:
                            store.close()
                    v20_before = {}
                    for name, cls, seed in (("projects", ProjectsStore, seed_projects),
                                            ("shared_reading", SharedReadingStore, seed_shared_reading)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            v20_before[name] = seed(store)
                        finally:
                            store.close()
                    v21_before = {}
                    for name, cls, seed in (("advice", AdviceStore, seed_advice),
                                            ("reference", ReferenceStore, seed_reference)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            v21_before[name] = seed(store)
                        finally:
                            store.close()
                    repair = RepairStore(directory/"unused-repair.sqlite3", mysql_settings=settings)
                    try:
                        repair_before = seed_repair(repair)
                    finally:
                        repair.close()
                    with connection.cursor() as cursor:
                        cursor.execute("ALTER TABLE backup_attempts AUTO_INCREMENT = 900")
                        cursor.execute("INSERT INTO backup_attempts(target,created_at,kind) VALUES ('private:test',100,'test')")
                        deleted_id = cursor.lastrowid
                        cursor.execute("DELETE FROM backup_attempts WHERE id=%s", (deleted_id,))
                    result = backup.backup_mysql(config, archive, apply=True, writers_stopped=True)
                    self.assertEqual(result["tables"], 82)
                    self.assertTrue(backup.verify_archive(archive)["verified"])
                    with self.assertRaisesRegex(backup.BackupError, "target_not_empty"):
                        backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    # Every table here was created by this test in its dedicated empty DB.
                    with connection.cursor() as cursor:
                        for table in reversed(backup.TABLES):
                            cursor.execute("DROP TABLE `backup_"+table+"`")
                    ensure_schema(settings, directory)
                    restored = backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    self.assertEqual(restored["tables"], 82)
                    state = SelfState(directory/"unused.sqlite3", mysql_settings=settings)
                    try:
                        self.assertEqual(state.state()["agent_id"], identity["agent_id"])
                        self.assertEqual(state.state()["thought_count"], 1)
                    finally:
                        state.close()
                    growth = GrowthStore(directory/"unused-growth.sqlite3", mysql_settings=settings)
                    try:
                        assert_belief_revision_restore(self, growth, growth_before)
                    finally:
                        growth.close()
                    attention = AttentionStore(directory/"unused-attention.sqlite3", mysql_settings=settings)
                    try:
                        assert_attention_restore(self, attention, attention_before)
                        attention.capture(attention_message(), owner_id="12345678", now=104, engagement_seconds=1)
                        self.assertEqual(attention.state("12345678", now=104)["revision"], attention_before["revision"])
                        self.assertEqual(attention.state("12345678", now=104)["cycle_count"], 1)
                        self.assertEqual(attention.con.execute("SELECT COUNT(*) FROM attention_receipts").fetchone()[0], 1)
                    finally:
                        attention.close()
                    recollection = RecollectionStore(directory/"unused-recollection.sqlite3", mysql_settings=settings)
                    try:
                        assert_recollection_restore(self, recollection, recollection_before)
                    finally:
                        recollection.close()
                    for name, cls in (("habits", HabitsStore), ("followthrough", FollowthroughStore)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            assert_v19_restore(self, store, v19_before[name])
                        finally:
                            store.close()
                    for name, cls in (("projects", ProjectsStore), ("shared_reading", SharedReadingStore)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            assert_v20_restore(self, store, v20_before[name])
                        finally:
                            store.close()
                    for name, cls in (("advice", AdviceStore), ("reference", ReferenceStore)):
                        store = cls(directory/("unused-"+name+".sqlite3"), mysql_settings=settings)
                        try:
                            assert_v21_restore(self, store, v21_before[name])
                        finally:
                            store.close()
                    repair = RepairStore(directory/"unused-repair.sqlite3", mysql_settings=settings)
                    try:
                        assert_repair_restore(self, repair, repair_before)
                    finally:
                        repair.close()
                    with connection.cursor() as cursor:
                        cursor.execute("INSERT INTO backup_attempts(target,created_at,kind) VALUES ('private:test',101,'test')")
                        self.assertGreater(cursor.lastrowid, deleted_id)
                finally:
                    if created:
                        with connection.cursor() as cursor:
                            for table in reversed(backup.TABLES):
                                cursor.execute("DROP TABLE IF EXISTS `backup_"+table+"`")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
