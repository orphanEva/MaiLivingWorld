"""Size-limited HTTPS reads for public media with pinned, verified destinations."""
from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit


class MediaFetchError(ValueError):
    """Only fixed error categories, never signed URLs or downloaded content."""


def _parts(url, hosts):
    if (not isinstance(url, str) or not url.isascii() or len(url) > 8192 or "\\" in url
            or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url)):
        raise MediaFetchError("invalid_url")
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if (p.scheme != "https" or p.username is not None or p.password is not None or p.port not in (None, 443)
                or not any(host.endswith(name) if name.startswith(".") else host == name for name in hosts)):
            raise MediaFetchError("host_denied")
    except ValueError:
        raise MediaFetchError("host_denied") from None
    return p


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaFetchError("timeout")
    return remaining


def _connect(host, deadline):
    resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not resolved:
        raise MediaFetchError("dns_failed")
    for _, _, _, _, address in resolved:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
            raise MediaFetchError("non_public_address")
    context = ssl.create_default_context()
    for family, socktype, protocol, _, address in resolved[:6]:
        raw = socket.socket(family, socktype, protocol)
        try:
            raw.settimeout(min(3, _remaining(deadline)))
            raw.connect(address)
            raw.settimeout(_remaining(deadline))
            return context.wrap_socket(raw, server_hostname=host)
        except ssl.SSLError:
            raw.close()
            raise MediaFetchError("tls_failed") from None
        except OSError:
            raw.close()
        except BaseException:
            raw.close()
            raise
    raise MediaFetchError("connect_failed")


def _interrupt(sock):
    # Per-read timeouts alone allow a peer to trickle headers indefinitely.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _request(url, hosts, max_bytes, deadline, referer, redirect_only, api_key=None, allow_redirects=True):
    for hop in range(4):
        p = _parts(url, hosts)
        host = p.hostname
        target = (p.path or "/") + ("?" + p.query if p.query else "")
        headers = {"Host": host, "User-Agent": "MaiLivingWorldBot/0.31 (+https://github.com/orphanEva/MaiLivingWorld)", "Accept-Encoding": "identity",
                   "Connection": "close"}
        if referer:
            headers["Referer"] = referer
        if api_key:
            headers["X-Subscription-Token"] = api_key
            headers["Accept"] = "application/json"
        with _connect(host, deadline) as sock:
            conn = http.client.HTTPConnection(host, 443, timeout=_remaining(deadline))
            conn.sock = sock  # Keep TLS hostname verification while connecting to the already checked IP.
            watchdog = threading.Timer(_remaining(deadline), _interrupt, args=(sock,))
            watchdog.daemon = True
            watchdog.start()
            try:
                conn.request("GET", target, headers=headers)
                sock.settimeout(_remaining(deadline))
                with conn.getresponse() as response:
                    if response.status in (301, 302, 303, 307, 308):
                        if not allow_redirects or api_key:
                            raise MediaFetchError("bad_redirect")
                        location = response.getheader("Location")
                        if not location or hop == 3 or len(location) > 8192:
                            raise MediaFetchError("bad_redirect")
                        url = urljoin(url, location)
                        if redirect_only:
                            return url  # Caller parses this as data, never opens it without a fresh allowlist.
                        continue
                    if redirect_only or response.status != 200:
                        raise MediaFetchError("unavailable")
                    if response.getheader("Content-Encoding", "identity").lower() != "identity":
                        raise MediaFetchError("invalid_encoding")
                    length = response.getheader("Content-Length")
                    if length is not None and (not length.isdecimal() or len(length) > 10 or int(length) > max_bytes):
                        raise MediaFetchError("too_large")
                    data = bytearray()
                    while len(data) <= max_bytes:
                        chunk = b""
                        if not response.isclosed():
                            sock.settimeout(_remaining(deadline))
                            chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                        if not chunk:
                            if length is not None and len(data) != int(length):
                                raise MediaFetchError("truncated")
                            return bytes(data)
                        data.extend(chunk)
                    raise MediaFetchError("too_large")
            finally:
                watchdog.cancel()
                conn.close()
    raise MediaFetchError("bad_redirect")


def _read(url, hosts, max_bytes, timeout, referer, redirect_only, api_key=None, allow_redirects=True):
    if (type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 45):
        raise MediaFetchError("invalid_limit")
    try:
        return _request(url, hosts, max_bytes, time.monotonic() + timeout, referer, redirect_only, api_key, allow_redirects)
    except MediaFetchError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise MediaFetchError("fetch_failed") from None


def fetch_public(url, *, allowed_hosts, max_bytes, timeout=8.0, referer="", allow_redirects=True) -> bytes:
    return _read(url, allowed_hosts, max_bytes, timeout, referer, False, allow_redirects=allow_redirects)


def public_redirect(url, *, allowed_hosts, timeout=5.0) -> str:
    return _read(url, allowed_hosts, 1, timeout, "", True)


def fetch_brave_api(url, *, token, max_bytes, timeout=8.0):
    hosts = ("api.search.brave.com",)
    if (_parts(url, hosts).path != "/res/v1/web/search" or not isinstance(token, str)
            or not 1 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token)):
        raise MediaFetchError("invalid_search_configuration")
    return _read(url, hosts, max_bytes, timeout, "", False, token)
