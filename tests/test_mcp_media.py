"""Offline media boundary tests; no request reaches a real backend."""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from MaiLivingWorld import mcp_media as media


KEY = "offline-test-key-" + "x" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"offline-test-image"
BASE = "http://127.0.0.1:8000/cache"
PIXIV_ID = 12345678
ROUTE = f"/media/illust/{PIXIV_ID}/versions/3/page_0.png"


def details():
    return {"pixiv_id": PIXIV_ID, "type": "illust", "kind": "illust", "status": "active",
            "accessible": True, "current_version": 3, "x_restrict": 0,
            "current_files": [{"page_index": 0, "file_type": "image", "size_bytes": len(PNG),
                               "filename": "page_0.png", "download_url": ROUTE}]}


class Response:
    def __init__(self, body=PNG, *, headers=None, status=200, endpoint=BASE+ROUTE):
        self.headers = headers if headers is not None else {"Content-Type": "image/png", "Content-Length": str(len(body))}
        self.status = status
        self.endpoint = endpoint
        self.body = io.BytesIO(body)
        self.reads = []
        self.closed = False

    def geturl(self):
        return self.endpoint

    def read1(self, size):
        self.reads.append(size)
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class MediaValidationTests(unittest.TestCase):
    def assertCode(self, code, action):
        with self.assertRaises(media.MediaError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_backend_prefix_local_addresses_and_ipv6_are_supported(self):
        for value in (BASE, BASE+"/", "https://pixiv_backend:8443/v1", "http://[::1]:8000"):
            with self.subTest(value=value):
                self.assertEqual(media._backend(value), value.rstrip("/"))

    def test_backend_rejects_credentials_queries_fragments_and_traversal(self):
        values = ["file:///tmp/image", "ftp://host", "//host", "http://u:p@host", "http://host?token=x",
                  "http://host#frag", "http://host/path?", "http://host/path#", "http://host/a/../b",
                  "http://host/%2e%2e", "http://host//api", "http://host\\evil", "http://host:0",
                  "http://host:999999", "http://host\n", " http://host", "http://host/a b"]
        for value in values:
            with self.subTest(value=value):
                self.assertCode("invalid_config", lambda: media._backend(value))

    def test_only_current_registered_image_is_selected(self):
        source = details()
        original = copy.deepcopy(source)
        self.assertEqual(media._select(source, PIXIV_ID, 0, 1024), (ROUTE, ".png"))
        self.assertEqual(source, original)
        self.assertCode("file_not_found", lambda: media._select(source, PIXIV_ID, 1, 1024))
        source["current_files"][0]["file_type"] = "ugoira_zip"
        self.assertCode("file_not_found", lambda: media._select(source, PIXIV_ID, 0, 1024))

    def test_rejects_mismatched_identity_version_page_and_inaccessible_work(self):
        for key, value in (("pixiv_id", PIXIV_ID+1), ("pixiv_id", True), ("current_version", "3"),
                           ("current_version", 0), ("kind", "novel"), ("type", "unknown"),
                           ("status", []), ("accessible", 0), ("current_files", {})):
            source = details()
            source[key] = value
            with self.subTest(key=key, value=value):
                self.assertCode("invalid_details", lambda: media._select(source, PIXIV_ID, 0, 1024))
        for key, value in (("accessible", False), ("status", "private"), ("status", "deleted"),
                           ("status", "auth_required")):
            source = details()
            source[key] = value
            self.assertCode("unavailable", lambda: media._select(source, PIXIV_ID, 0, 1024))
        self.assertCode("invalid_details", lambda: media._select(details(), True, 0, 1024))
        self.assertCode("invalid_details", lambda: media._select(details(), PIXIV_ID, True, 1024))
        self.assertCode("invalid_details", lambda: media._select(details(), PIXIV_ID, -1, 1024))

    def test_conflicting_file_entries_are_not_guessed(self):
        source = details()
        source["current_files"].append(dict(source["current_files"][0], file_type="cover"))
        self.assertCode("invalid_details", lambda: media._select(source, PIXIV_ID, 0, 1024))
        source["current_files"] *= 51
        self.assertCode("invalid_details", lambda: media._select(source, PIXIV_ID, 0, 1024))

    def test_media_routes_reject_url_traversal_encodings_and_wrong_work(self):
        paths = ["https://evil.test/image.png", "//evil.test/image.png", "http://user@host/image.png",
                 ROUTE+"?key=x", ROUTE+"#frag", ROUTE+"?", ROUTE.replace("page_0.png", "../page_0.png"),
                 ROUTE.replace("page_0.png", "%2e%2e%2fpage_0.png"),
                 ROUTE.replace("page_0.png", "%252e%252e%252fpage_0.png"),
                 ROUTE.replace("page_0.png", "x%2Fpage_0.png"),
                 ROUTE.replace("page_0.png", "x%5cpage_0.png"),
                 ROUTE.replace("page_0.png", "x%00page_0.png"),
                 ROUTE.replace("page_0.png", "%ff.png"), ROUTE.replace("page_0.png", "bad%.png"),
                 ROUTE.replace(str(PIXIV_ID), str(PIXIV_ID+1)), ROUTE.replace("/versions/3/", "/versions/2/"),
                 ROUTE.replace("/illust/", "/novel/"), ROUTE.replace("/3/", "/03/"),
                 ROUTE.replace("page_0.png", "page_0\\.png"), ROUTE+"\n"]
        for path in paths:
            source = details()
            source["current_files"][0]["download_url"] = path
            with self.subTest(path=path):
                self.assertCode("invalid_media_path", lambda: media._select(source, PIXIV_ID, 0, 1024))

    def test_single_encoded_utf8_filename_and_real_illustration_namespaces(self):
        for kind in ("illust", "manga", "ugoira"):
            source = details()
            source["type"] = kind
            source["current_files"][0].update(filename="封面 0.PNG", file_type="cover",
                download_url=ROUTE.replace("page_0.png", "%E5%B0%81%E9%9D%A2%200.PNG"))
            self.assertEqual(media._select(source, PIXIV_ID, 0, 1024)[1], ".png")
        source = details()
        source.update(type="novel", kind="novel")
        source["current_files"][0].update(file_type="cover", download_url=ROUTE.replace("/illust/", "/novel/"))
        self.assertEqual(media._select(source, PIXIV_ID, 0, 1024)[1], ".png")

    def test_non_image_extensions_are_rejected_even_if_file_claims_image(self):
        for extension in ("svg", "html", "mp4", "zip", "exe"):
            source = details()
            source["current_files"][0].update(filename="page_0."+extension,
                download_url=ROUTE.replace(".png", "."+extension))
            self.assertCode("invalid_image", lambda: media._select(source, PIXIV_ID, 0, 1024))

    def test_declared_size_has_bounded_and_strict_semantics(self):
        self.assertCode("too_large", lambda: media._select(details(), PIXIV_ID, 0, 8))
        for size in (-1, True, "10"):
            source = details()
            source["current_files"][0]["size_bytes"] = size
            self.assertCode("invalid_details", lambda: media._select(source, PIXIV_ID, 0, 1024))


class MediaTransportTests(unittest.TestCase):
    def fetch(self, response, limit=1024):
        opener = Mock()
        opener.open.return_value = response
        with patch.object(media, "build_opener", return_value=opener) as build:
            result = media._fetch(BASE+ROUTE, {"X-API-Key": KEY}, 3, limit)
        return result, opener, build

    def test_proxy_redirect_cookie_and_credential_boundaries(self):
        response = Response()
        result, opener, build = self.fetch(response)
        self.assertEqual(result, ("image/png", PNG))
        self.assertTrue(response.closed)
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], media._NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, BASE+ROUTE)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)

    def test_redirects_are_closed_and_never_followed(self):
        fp = Mock()
        with self.assertRaisesRegex(media.MediaError, "^redirect$"):
            media._NoRedirect().redirect_request(None, fp, 302, "", {}, "https://evil.test/"+KEY)
        fp.close.assert_called_once()
        with self.assertRaisesRegex(media.MediaError, "^redirect$"):
            self.fetch(Response(endpoint="https://evil.test/"))

    def test_known_and_unknown_length_responses_cannot_exceed_limit(self):
        declared = Response()
        with self.assertRaisesRegex(media.MediaError, "^too_large$"):
            self.fetch(declared, 8)
        self.assertEqual(declared.reads, [])
        streamed = Response(body=b"x"*100, headers={"Content-Type": "image/png"})
        with self.assertRaisesRegex(media.MediaError, "^too_large$"):
            self.fetch(streamed, 8)
        self.assertEqual(streamed.reads, [9])
        self.assertTrue(streamed.closed)

    def test_html_compression_status_and_incomplete_payload_are_rejected(self):
        for response, code in (
            (Response(headers={"Content-Type": "text/html"}), "invalid_image"),
            (Response(headers={"Content-Type": "image/png", "Content-Encoding": "gzip"}), "invalid_image"),
            (Response(headers={"Content-Type": "image/png", "Content-Length": "-1"}), "http_error"),
            (Response(headers={"Content-Type": "image/png", "Content-Length": "100"}), "invalid_image"),
            (Response(status=206), "http_error"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(media.MediaError, "^"+code+"$"):
                    self.fetch(response)
                self.assertTrue(response.closed)

    def test_elapsed_deadline_stops_stream_before_read(self):
        response = Response()
        with patch.object(media.time, "monotonic", side_effect=[0, 4]):
            with self.assertRaisesRegex(media.MediaError, "^timeout$"):
                self.fetch(response)
        self.assertEqual(response.reads, [])

    def test_socket_timeout_is_reduced_to_remaining_total_time(self):
        response = Response()
        socket = Mock()
        response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))
        with patch.object(media.time, "monotonic", side_effect=[0, 1, 2]):
            self.fetch(response)
        self.assertEqual([call.args[0] for call in socket.settimeout.call_args_list], [2, 1])

    def test_transport_errors_have_fixed_codes_and_no_provider_text(self):
        for error, code in (
            (HTTPError(BASE+ROUTE, 403, KEY, {}, io.BytesIO(KEY.encode())), "http_error"),
            (HTTPError(BASE+ROUTE, 302, KEY, {}, io.BytesIO(KEY.encode())), "redirect"),
            (URLError(TimeoutError(KEY)), "timeout"), (URLError(BASE+KEY), "fetch_failed"),
            (RuntimeError(BASE+KEY), "fetch_failed"),
        ):
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(media, "build_opener", return_value=opener):
                with self.assertRaisesRegex(media.MediaError, "^"+code+"$") as caught:
                    media._fetch(BASE+ROUTE, {"X-API-Key": KEY}, 3, 1024)
            self.assertNotIn(KEY, repr(caught.exception))


class MediaAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, **changes):
        values = dict(backend_url=BASE, api_key=KEY, details=details(), pixiv_id=PIXIV_ID,
                      fetch=Mock(return_value=("image/png", PNG)))
        values.update(changes)
        return await media.fetch_pixiv_image(**values)

    async def test_success_returns_one_inline_image_and_no_backend_link_or_key(self):
        fake = Mock(return_value=("image/png", PNG))
        result = await self.invoke(fetch=fake)
        item, = result["content_items"]
        self.assertEqual(item, {"type": "image", "data": base64.b64encode(PNG).decode(),
                               "mime_type": "image/png", "name": f"pixiv_{PIXIV_ID}_p0.png"})
        self.assertNotIn(BASE, str(result))
        self.assertNotIn(KEY, str(result))
        endpoint, headers, timeout, maximum = fake.call_args.args
        self.assertEqual(endpoint, BASE+ROUTE)
        self.assertEqual(headers["X-API-Key"], KEY)
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(timeout, 15)
        self.assertEqual(maximum, 8*1024*1024)

    async def test_supported_mime_and_magic_must_agree_with_extension(self):
        cases = [(".jpg", "image/jpeg", b"\xff\xd8\xff\xe0jpg"),
                 (".jpeg", "image/jpeg", b"\xff\xd8\xff\xe1jpg"),
                 (".gif", "image/gif", b"GIF89aimage"),
                 (".webp", "image/webp", b"RIFF\x08\x00\x00\x00WEBPimage"),
                 (".png", "image/png", PNG)]
        for extension, mime, body in cases:
            source = details()
            source["current_files"][0].update(filename="page_0"+extension, size_bytes=len(body),
                                              download_url=ROUTE.replace(".png", extension))
            result = await self.invoke(details=source, fetch=Mock(return_value=(mime+"; charset=binary", body)))
            self.assertEqual(result["content_items"][0]["mime_type"], mime)
        for mime, body in (("image/jpeg", PNG), ("image/png", b"<html>"),
                           ("image/png", b"<svg/>"), ("text/html", PNG), ("image/svg+xml", PNG),
                           ("image/png", b""), ("image/png", bytearray(PNG))):
            with self.assertRaisesRegex(media.MediaError, "^invalid_image$"):
                await self.invoke(fetch=Mock(return_value=(mime, body)))

    async def test_invalid_credentials_limits_and_timeout_never_reach_fetch(self):
        fake = Mock()
        cases = [{"api_key": value} for value in ("short", KEY+"\n", KEY+" ", KEY+"中", KEY+"\x7f")]
        cases += [{"max_bytes": value} for value in (0, -1, True, "100", 33*1024*1024)]
        cases += [{"timeout_seconds": value} for value in (0, -1, True, float("nan"), float("inf"), 61, 10**500)]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(media.MediaError, "^invalid_config$"):
                    await self.invoke(fetch=fake, **options)
        fake.assert_not_called()

    async def test_invalid_detail_or_unlisted_page_never_reaches_fetch(self):
        fake = Mock()
        with self.assertRaisesRegex(media.MediaError, "^file_not_found$"):
            await self.invoke(page_index=1, fetch=fake)
        with self.assertRaisesRegex(media.MediaError, "^invalid_details$"):
            await self.invoke(pixiv_id=PIXIV_ID+1, fetch=fake)
        source = details()
        source["accessible"] = False
        with self.assertRaisesRegex(media.MediaError, "^unavailable$"):
            await self.invoke(details=source, fetch=fake)
        fake.assert_not_called()

    async def test_fake_cannot_bypass_actual_body_limit_or_result_type(self):
        with self.assertRaisesRegex(media.MediaError, "^too_large$"):
            await self.invoke(max_bytes=40, fetch=Mock(return_value=("image/png", PNG+b"x"*40)))
        for result in ({"body": PNG}, ("image/png", PNG, "extra"), (None, PNG)):
            with self.assertRaisesRegex(media.MediaError, "^invalid_image$"):
                await self.invoke(fetch=Mock(return_value=result))

    async def test_provider_failure_is_sanitized_and_no_retry_occurs(self):
        fake = Mock(side_effect=ValueError(BASE+KEY+"<html>response</html>"))
        with self.assertRaisesRegex(media.MediaError, "^fetch_failed$") as caught:
            await self.invoke(fetch=fake)
        self.assertNotIn(KEY, repr(caught.exception))
        fake.assert_called_once()

    async def test_outer_wait_is_bounded_without_network(self):
        async def never(*args, **kwargs):
            await asyncio.Future()
        with patch.object(media.asyncio, "to_thread", side_effect=never):
            with self.assertRaisesRegex(media.MediaError, "^timeout$"):
                await self.invoke(timeout_seconds=0.01)

    async def test_cancellation_propagates_instead_of_becoming_media_error(self):
        entered = asyncio.Event()
        async def never(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        with patch.object(media.asyncio, "to_thread", side_effect=never):
            task = asyncio.create_task(self.invoke())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
