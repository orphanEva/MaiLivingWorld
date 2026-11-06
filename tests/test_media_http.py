from contextlib import nullcontext
import socket
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from MaiLivingWorld import media_http as media


HOST = "i0.hdslb.com"
URL = "https://" + HOST + "/path"


class PublicMediaHTTP(unittest.TestCase):
    def test_disallowed_targets_never_reach_dns(self):
        with patch.object(media.socket, "getaddrinfo") as resolve:
            for url in ("http://" + HOST, "https://127.0.0.1/file", "https://evilhdslb.com/file",
                        URL.replace(HOST, HOST + ":8443"), URL.replace(HOST, "user@" + HOST),
                        URL + "\r\nX-Test: yes", "file:///etc/hosts"):
                with self.subTest(url=url), self.assertRaises(media.MediaFetchError):
                    media.fetch_public(url, allowed_hosts=(".hdslb.com",), max_bytes=8)
            resolve.assert_not_called()

    def test_mixed_private_public_dns_fails_before_connect(self):
        for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.1.1.1"):
            results = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in ("1.1.1.1", private)]
            with patch.object(media.socket, "getaddrinfo", return_value=results), patch.object(media.socket, "socket") as connect:
                with self.assertRaises(media.MediaFetchError):
                    media.fetch_public(URL, allowed_hosts=(HOST,), max_bytes=8)
                connect.assert_not_called()

    def test_tls_connects_to_checked_address_and_verifies_original_name(self):
        addr = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        with patch.object(media.socket, "getaddrinfo", return_value=[addr]) as resolve, \
                patch.object(media.socket, "socket") as new_socket, patch.object(media.ssl, "create_default_context") as tls:
            media._connect(HOST, time.monotonic()+5)
        resolve.assert_called_once_with(HOST, 443, type=socket.SOCK_STREAM)
        new_socket.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
        tls.return_value.wrap_socket.assert_called_once_with(new_socket.return_value, server_hostname=HOST)

    def response(self, code=200, headers=None, data=b"abc"):
        reply = Mock(status=code)
        reply.isclosed.return_value = False
        reply.read1.side_effect = [data, b""]
        reply.getheader.side_effect = (headers or {}).get
        return reply

    def get(self, response):
        sock = MagicMock()
        with patch.object(media, "_connect", return_value=sock) as connect, patch.object(media.http.client, "HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = nullcontext(response)
            result = media.fetch_public(URL, allowed_hosts=(HOST,), max_bytes=4)
            connect.assert_called_once()
            return result

    def test_bounds_apply_to_body_even_without_length(self):
        self.assertEqual(self.get(self.response()), b"abc")
        for reply in (self.response(data=b"abcde"), self.response(headers={"Content-Length": "8"}),
                      self.response(headers={"Content-Length": "4"}), self.response(headers={"Content-Encoding": "gzip"})):
            with self.assertRaises(media.MediaFetchError):
                self.get(reply)

    def test_media_redirect_cannot_escape_allowlist(self):
        reply = self.response(302, {"Location": "https://localhost/sensitive"})
        with self.assertRaises(media.MediaFetchError):
            self.get(reply)

    def test_search_key_is_only_sent_to_fixed_api_and_never_follows_redirect(self):
        api = "https://api.search.brave.com/res/v1/web/search?q=space"
        for url in (URL, api.replace("/web/search", "/chat"), api.replace("api.search", "not-api.search")):
            with patch.object(media, "_connect") as connect, self.assertRaises(media.MediaFetchError):
                media.fetch_brave_api(url, token="fixture-key", max_bytes=20)
            connect.assert_not_called()
        for location in (URL, api):
            with patch.object(media, "_connect", return_value=MagicMock()) as connect, \
                    patch.object(media.http.client, "HTTPConnection") as conn:
                conn.return_value.getresponse.return_value = nullcontext(self.response(302, {"Location": location}))
                with self.assertRaisesRegex(media.MediaFetchError, "bad_redirect"):
                    media.fetch_brave_api(api, token="fixture-key", max_bytes=20)
                self.assertEqual(connect.call_count, 1)
                self.assertEqual(conn.return_value.request.call_args.kwargs["headers"]["X-Subscription-Token"], "fixture-key")
