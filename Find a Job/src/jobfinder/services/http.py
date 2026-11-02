"""Polite HTTP client: per-domain rate limit, robots.txt, response cache, OS certificate store."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from jobfinder.models import short_hash

log = logging.getLogger("jobfinder.http")

_TRUST_DONE = False
_NET_DONE = False


def fix_broken_ipv6() -> bool:
    """Some networks advertise IPv6 but drop it, so every connection waits for a timeout before IPv4.
    Probe once; if IPv6 is unusable, make getaddrinfo return IPv4 addresses first (affects all libraries)."""
    global _NET_DONE
    if _NET_DONE:
        return False
    _NET_DONE = True
    import os
    import socket

    if os.environ.get("JOBFINDER_IPV6") == "1":
        return False
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("2606:4700:4700::1111", 443))
        s.close()
        return False
    except OSError:
        pass
    original = socket.getaddrinfo

    def ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore[no-untyped-def]  # noqa: A002
        res = original(host, port, family, type, proto, flags)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res

    socket.getaddrinfo = ipv4_first  # type: ignore[assignment]
    log.info("IPv6 unreachable: preferring IPv4")
    return True


def use_os_trust_store() -> None:
    """Corporate proxies re-sign TLS; the OS store knows their CA, certifi does not."""
    global _TRUST_DONE
    if _TRUST_DONE:
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as e:  # pragma: no cover
        log.warning("truststore unavailable: %s", e)
    _TRUST_DONE = True


class RobotsDenied(Exception):
    pass


class Http:
    def __init__(self, user_agent: str, timeout_s: float = 25, per_domain_rps: float = 1.0,
                 respect_robots: bool = True, cache: Any = None, cache_ttl_s: float = 12 * 3600,
                 transport: httpx.BaseTransport | None = None):
        use_os_trust_store()
        fix_broken_ipv6()
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en,es;q=0.9,fr;q=0.8"},
            timeout=timeout_s, follow_redirects=True, transport=transport,
        )
        self.user_agent = user_agent
        self.min_interval = 1.0 / per_domain_rps if per_domain_rps > 0 else 0
        self.respect_robots = respect_robots
        self.cache = cache
        self.cache_ttl_s = cache_ttl_s
        self._last: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def close(self) -> None:
        self.client.close()

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            self._last[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt", timeout=10)
                if r.status_code >= 400:
                    rp = None
                else:
                    rp.parse(r.text.splitlines())  # type: ignore[union-attr]
            except httpx.HTTPError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: Any = None,
                headers: dict[str, str] | None = None, use_cache: bool = True, check_robots: bool = False,
                ) -> httpx.Response:
        key = None
        if use_cache and self.cache is not None and method == "GET":
            key = "GET " + url + "?" + json.dumps(params or {}, sort_keys=True)
            key = short_hash(key, 32)
            hit = self.cache.cache_get(key, self.cache_ttl_s)
            if hit:
                return httpx.Response(hit[0], content=hit[1], request=httpx.Request(method, url))
        if check_robots and not self.allowed(url):
            raise RobotsDenied(url)
        self._throttle(urlsplit(url).netloc)
        self.calls += 1
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = self.client.request(method, url, params=params, json=json_body, headers=headers)
                if r.status_code in (429, 502, 503, 504) and attempt < 2:
                    delay = float(r.headers.get("retry-after", 2 * (attempt + 1))) if r.headers.get("retry-after", "").isdigit() else 2 * (attempt + 1)
                    time.sleep(min(delay, 20))
                    continue
                if key and r.status_code == 200:
                    self.cache.cache_put(key, r.status_code, r.content)
                return r
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        raise last_exc or RuntimeError("request failed")

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        r = self.request("POST", url, json_body=body, use_cache=False, **kw)
        r.raise_for_status()
        return r.json()
