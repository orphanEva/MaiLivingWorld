"""Synthetic documents only: no downloads, accounts, or private cache access."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from MaiLivingWorld import document_reader as reader

HAS_PDF = importlib.util.find_spec("pymupdf") is not None


def page(**changes):
    data = {"provider": "cap", "kind": "pdf_page", "jm_id": "123",
            "source_id": "cap:album:123", "origin_key": "album:123", "source_version": "a"*64,
            "page_index": 0, "total_pages": 2, "text": "  original text\n", "text_truncated": False,
            "image_base64": base64.b64encode(b"\xff\xd8\xfftest").decode(), "mime_type": "image/jpeg",
            "width": 100, "height": 100}
    data.update(changes)
    return data


class ReadingInputTests(unittest.TestCase):
    def test_pdf_arguments_are_exact_and_do_not_accept_paths_or_boolean_indices(self):
        self.assertEqual(reader.pdf_arguments({"jm_id": "000123"}), ("123", 0))
        for value in ({"jm_id": "1", "path": "/etc/passwd"}, {"jm_id": "../123"}, {"jm_id": 123},
                      {"jm_id": "0"}, {"jm_id": "1", "page_index": True}, {"jm_id": "1", "page_index": -1},
                      {"jm_id": "1", "page_index": 1500}):
            with self.subTest(value=value), self.assertRaises(reader.DocumentError):
                reader.pdf_arguments(value)

    def test_configuration_limits_cannot_be_disabled_or_nonfinite(self):
        for value in ({"max_file_bytes": 0}, {"max_pixels": 3_000_000}, {"timeout_seconds": float("nan")},
                      {"max_pages": True}, {"max_image_bytes": 131072}, {"timeout_seconds": 31}):
            with self.subTest(value=value), self.assertRaises(reader.DocumentError):
                reader.PDFLimits(**value)

    def test_pdf_normalization_preserves_exact_text_and_only_selected_metadata(self):
        payload = page(file_path="/secret/path", password="do-not-copy")
        normalized = reader.normalize_reading_page("cap", payload, resource_id="123")
        self.assertEqual(normalized["excerpt"], "  original text\n")
        self.assertEqual(normalized["unit_id"], "cap:album:123:"+"a"*64+":page:0")
        self.assertEqual(normalized["image_sha256"], hashlib.sha256(b"\xff\xd8\xfftest").hexdigest())
        self.assertNotIn("do-not-copy", json.dumps(normalized))
        self.assertNotIn("/secret", json.dumps(normalized))
        self.assertEqual(normalized["resource_type"], "pdf")

    def test_mismatched_identity_version_bounds_and_media_fail_closed(self):
        for changes in ({"jm_id": "999"}, {"source_id": "cap:album:999"}, {"source_version": "not-a-version"},
                        {"page_index": True}, {"total_pages": 0}, {"width": -1}, {"height": 99999},
                        {"text_truncated": "false"}, {"image_base64": "!!!"}, {"mime_type": "image/png"}):
            with self.subTest(changes=changes), self.assertRaises(reader.DocumentError):
                reader.normalize_reading_page("cap", page(**changes), resource_id="123")

    def test_pixiv_uses_same_page_contract_and_never_copies_provider_urls(self):
        payload = {"content": "untrusted source note", "download_url": "http://private/path",
            "content_items": [{"type": "image", "data": page()["image_base64"], "mime_type": "image/jpeg",
                               "name": "secret-local-file"}]}
        normalized = reader.normalize_reading_page("pixiv", payload, resource_id=42, version=3, total_units=8, page_index=2)
        self.assertEqual(normalized["unit_id"], "pixiv:illust:42:3:page:2")
        self.assertEqual(normalized["resource_type"], "illust")
        self.assertEqual(normalized["excerpt"], "")
        self.assertEqual(normalized["image_sha256"], hashlib.sha256(b"\xff\xd8\xfftest").hexdigest())
        self.assertNotIn("private/path", json.dumps(normalized))
        self.assertNotIn("secret-local-file", json.dumps(normalized))
        # Match the administrator's existing mcp.max_media_bytes upper bound.
        self.assertEqual(reader.normalize_reading_page("pixiv", payload, resource_id=42, version=3,
            total_units=8, max_image_bytes=32*1024*1024)["image_sha256"], normalized["image_sha256"])
        with self.assertRaises(reader.DocumentError):
            reader.normalize_reading_page("pixiv", payload, resource_id=42, version=3, total_units=2, page_index=2)


class CachePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.document = self.cache / "123.pdf"
        self.document.write_bytes(b"%PDF-1.7\nsynthetic-invalid-body")

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_is_immutable_bytes_and_rejects_traversal_or_other_root(self):
        result = reader._snapshot(str(self.cache), str(self.document), reader.PDFLimits())
        self.assertEqual(result, self.document.read_bytes())
        outside = self.root / "other.pdf"
        outside.write_bytes(result)
        for candidate in (str(outside), str(self.cache / ".." / "other.pdf"), "relative.pdf"):
            with self.assertRaisesRegex(reader.DocumentError, "^pdf_path_denied$"):
                reader._snapshot(str(self.cache), candidate, reader.PDFLimits())

    def test_empty_large_nonpdf_and_nonregular_files_are_rejected(self):
        with self.assertRaisesRegex(reader.DocumentError, "^pdf_too_large$"):
            reader._snapshot(str(self.cache), str(self.document), reader.PDFLimits(max_file_bytes=8))
        self.document.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(reader.DocumentError, "^invalid_pdf$"):
            reader._snapshot(str(self.cache), str(self.document), reader.PDFLimits())
        directory = self.cache / "directory.pdf"
        directory.mkdir()
        with self.assertRaises(reader.DocumentError):
            reader._snapshot(str(self.cache), str(directory), reader.PDFLimits())

    def test_hardlinks_are_rejected_even_inside_cache(self):
        linked = self.cache / "linked.pdf"
        try:
            os.link(self.document, linked)
        except OSError:
            self.skipTest("hard links are unavailable on this filesystem")
        with self.assertRaisesRegex(reader.DocumentError, "^pdf_path_denied$"):
            reader._snapshot(str(self.cache), str(linked), reader.PDFLimits())

    def test_symlink_file_and_directory_are_rejected(self):
        file_link, directory_link = self.cache / "link.pdf", self.root / "linked-root"
        try:
            file_link.symlink_to(self.document)
            directory_link.symlink_to(self.cache, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks require an OS privilege unavailable to this runner")
        for root, candidate in ((self.cache, file_link), (directory_link, directory_link / "123.pdf")):
            with self.assertRaises(reader.DocumentError):
                reader._snapshot(str(root), str(candidate), reader.PDFLimits())

    def test_changed_file_during_snapshot_is_rejected(self):
        actual_read = reader.os.read
        changed = False

        def read(fd, size):
            nonlocal changed
            result = actual_read(fd, size)
            if not changed:
                changed = True
                with self.document.open("ab") as stream:
                    stream.write(b"changed")
            return result

        with patch.object(reader.os, "read", side_effect=read), self.assertRaises(reader.DocumentError):
            reader._snapshot(str(self.cache), str(self.document), reader.PDFLimits())


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args, self.options = args, kwargs
        self.returncode = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self, timeout):
        self.waited = True
        return self.returncode


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def request(self, seconds=.03):
        return {"root": "/cache", "path": "/cache/1.pdf", "jm_id": "1", "page_index": 0,
                "limits": {**reader.asdict(reader.PDFLimits()), "timeout_seconds": seconds}}

    async def test_timeout_kills_and_reaps_one_fixed_child_without_leaking_environment(self):
        processes = []

        def factory(*args, **kwargs):
            child = FakeProcess(*args, **kwargs)
            processes.append(child)
            return child

        fake_spec = type("Spec", (), {"origin": str(Path(__file__).parent / "pymupdf/__init__.py")})()
        with patch.object(reader.importlib.util, "find_spec", return_value=fake_spec), patch.dict(os.environ, {"PRIVATE_TOKEN": "secret"}):
            with self.assertRaisesRegex(reader.DocumentError, "^reader_timeout$"):
                await reader._run_worker(self.request(), process_factory=factory)
        self.assertEqual(len(processes), 1)
        child = processes[0]
        self.assertTrue(child.killed and child.waited)
        self.assertFalse(child.options["shell"])
        self.assertNotIn("PRIVATE_TOKEN", child.options["env"])
        self.assertEqual(child.args[-1], "--pdf-worker")
        self.assertEqual(child.options["stderr"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertEqual(child.options["creationflags"], subprocess.CREATE_NO_WINDOW)

    async def test_cancel_kills_and_reaps_worker_instead_of_background_parse(self):
        children, started = [], asyncio.Event()

        def factory(*args, **kwargs):
            children.append(FakeProcess(*args, **kwargs))
            started.set()
            return children[-1]

        fake_spec = type("Spec", (), {"origin": str(Path(__file__).parent / "pymupdf/__init__.py")})()
        with patch.object(reader.importlib.util, "find_spec", return_value=fake_spec):
            task = asyncio.create_task(reader._run_worker(self.request(20), process_factory=factory))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(children[0].killed and children[0].waited)

    async def test_missing_dependency_and_uncached_or_mismatched_source_do_not_start(self):
        with patch.object(reader.importlib.util, "find_spec", return_value=None), patch.object(reader.subprocess, "Popen") as factory:
            with self.assertRaisesRegex(reader.DocumentError, "^reader_unavailable$"):
                await reader._run_worker(self.request())
            for cached, code in (({"jm_id": "1", "cached": False}, "pdf_not_cached"),
                                 ({"jm_id": "2", "cached": True, "pdf": {}}, "invalid_reading_result")):
                with self.assertRaisesRegex(reader.DocumentError, "^"+code+"$"):
                    await reader.read_cached_pdf_page(cached, jm_id="1")
        factory.assert_not_called()


@unittest.skipUnless(HAS_PDF, "requires optional gateway PyMuPDF")
class SyntheticPDFTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import pymupdf
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "123.pdf"
        with pymupdf.open() as document:
            text = document.new_page(width=300, height=400)
            text.insert_text((20, 35), "Synthetic reading page one", fontsize=14)
            image = document.new_page(width=300, height=400)
            image.draw_rect(pymupdf.Rect(20, 20, 280, 190), color=(0, 0, 0), fill=(.9, .8, .1))
            image.draw_circle(pymupdf.Point(150, 270), 50, color=(0, 0, 0), fill=(.2, .7, .9))
            document.save(self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def read(self, index=0, **options):
        return await reader.read_cached_pdf_page({"jm_id": "123", "cached": True, "pdf": {"file_path": str(self.path)}},
                jm_id="123", page_index=index, root=str(self.root), **options)

    async def test_real_subprocess_extracts_text_and_renders_bounded_jpeg_with_version(self):
        import pymupdf
        result = await self.read()
        self.assertIn("Synthetic reading page one", result["text"])
        self.assertEqual(result["source_version"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result["total_pages"], 2)
        self.assertFalse(result["text_truncated"])
        image = base64.b64decode(result["image_base64"])
        self.assertLessEqual(len(image), 96*1024)
        self.assertLess(len(json.dumps(result).encode()), reader.MAX_RESULT_BYTES)
        self.assertNotIn(str(self.path), json.dumps(result))
        pixmap = pymupdf.Pixmap(image)
        self.assertEqual((pixmap.width, pixmap.height), (result["width"], result["height"]))
        self.assertLessEqual(pixmap.width*pixmap.height, 2_000_000)

    async def test_image_only_page_is_readable_without_fabricated_ocr_or_comprehension(self):
        result = await self.read(1)
        self.assertEqual(result["text"], "")
        self.assertTrue(result["image_base64"])
        self.assertEqual(result["page_index"], 1)
        self.assertNotIn("understood", result)

    async def test_text_truncation_and_page_limit_are_explicit(self):
        result = await self.read(limits=reader.PDFLimits(max_text_chars=9))
        self.assertEqual(result["text"], "Synthetic")
        self.assertTrue(result["text_truncated"])
        with self.assertRaisesRegex(reader.DocumentError, "^pdf_page_limit$"):
            await self.read(limits=reader.PDFLimits(max_pages=1))
        with self.assertRaisesRegex(reader.DocumentError, "^page_out_of_range$"):
            await self.read(2)

    async def test_updated_file_has_a_new_source_version(self):
        import pymupdf
        previous = await self.read()
        with pymupdf.open(self.path) as document:
            document[0].insert_text((20, 80), "A changed observation")
            document.saveIncr()
        changed = await self.read()
        self.assertNotEqual(changed["source_version"], previous["source_version"])

    async def test_encrypted_and_malformed_documents_fail_with_fixed_errors(self):
        import pymupdf
        encrypted = self.root / "encrypted.pdf"
        with pymupdf.open(self.path) as document:
            document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                          owner_pw="synthetic-owner", user_pw="synthetic-reader")
        self.path.write_bytes(encrypted.read_bytes())
        with self.assertRaisesRegex(reader.DocumentError, "^encrypted_pdf$"):
            await self.read()
        self.path.write_bytes(b"%PDF-1.7\ninvalid synthetic body")
        with self.assertRaisesRegex(reader.DocumentError, "^invalid_pdf$"):
            await self.read()


if __name__ == "__main__":
    unittest.main()
