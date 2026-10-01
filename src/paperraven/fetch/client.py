"""The fetch client: polite by construction.

- An honest User-Agent (`PaperRaven-fetch/<version>`), no email, no credentials.
- At least `host_gap` seconds between requests to one host (10 s; a DOI resolver 2 s plus
  jitter) and `global_gap` between any two (0.5 s). Up to 4 threads share one client: a request
  is claimed under a lock only once its gaps have passed, so every host keeps its gap whichever
  thread asks; no thread sleeps holding it.
- At most `per_host_cap` PDFs from one host per command (25), exactly: a request waits while
  those in flight could still take the host past it, and the host is "capped" only when its PDFs
  alone reach it; its entries then wait for the next run.
- A host is dropped for the rest of the command at its first 403, 429 or challenge page.
  At least 12 publisher hosts answer any plain client with a JavaScript challenge; that is a
  person's job, not ours.
- Redirects are followed hop by hop, so each hop's host is checked; one HTML meta-refresh is
  followed. Nothing else: no link-following, no forms, no `robots.txt` (it behaves as a
  reference manager, which fetches named documents on the user's behalf and is not a crawler).
- One retry, on a network error or a 5xx. Responses over `max_bytes` are refused.
- Only `http` and `https` URLs to a public host are requested, at every hop (`unsafe_url`): a
  registry record or a publisher's page must not be able to send a request to this machine or
  its local network. A host name is resolved first, and refused if any address it resolves to
  is not public. What this cannot stop is a name whose answer changes between that check and
  the request (DNS rebinding).
"""

from __future__ import annotations

import ipaddress
import random
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

CHALLENGE = re.compile(rb"just a moment|cf-chl|captcha|are you a robot|unusual traffic|"
                       rb"enable javascript and cookies", re.I)
_META_REFRESH = re.compile(rb'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?\s*\d+\s*;'
                           rb'\s*url=([^"\'>\s]+)', re.I)


def unsafe_url(url: str) -> str | None:
    """Why `url` must not be requested, or None when it is an `http`/`https` URL to a public host.

    Refused: any other scheme, credentials in the URL, `localhost`, and an IP address that is
    not global (loopback, private, link-local, multicast, reserved or unspecified), in any of the
    forms a resolver accepts (`127.1`, `0x7f.1`, `2130706433`); IPv4-mapped IPv6 is never global.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme or 'none'!r}"
    if parts.username is not None or parts.password is not None:
        return "credentials in the URL"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "no host"
    if host == "localhost" or host.endswith(".localhost"):
        return "a local host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return None                                   # a name, and not resolved here
    if not ip.is_global or ip.is_multicast:
        return "a local or private address"
    return None


@dataclass
class FetchResult:
    #: pdf | html | blocked | dropped | capped | error | too_large | not_found | stopped
    kind: str
    url: str
    final_url: str
    content: bytes | None = None
    status: int | None = None
    detail: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.final_url).netloc


#: DOI resolvers only redirect, so they get a 2 second gap (plus jitter). Every publisher host
#: they redirect to keeps `host_gap`.
RESOLVER_GAP = {"doi.org": 2.0, "dx.doi.org": 2.0}
#: At most this much is added to a resolver's gap, at random, so requests do not arrive on a beat.
RESOLVER_JITTER = 0.5


#: Longest wait, in seconds, for requests in flight near the cap before looking again.
_CAP_WAIT = 0.5

#: Results of a request that was never sent: nothing to release.
_NOT_SENT = {"stopped", "dropped", "capped"}


class _Stopped(Exception):
    """The run was stopped while a download was being read."""


def resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to, or none if it does not resolve."""
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def private_resolution(url: str, resolve: Callable[[str], list[str]]) -> str | None:
    """Why `url`'s host name must not be requested: it resolves to a local or private address."""
    host = (urlparse(url).hostname or "").rstrip(".")
    for address in resolve(host):
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast:
            return f"{host} resolves to a local or private address"
    return None


class PoliteClient:
    def __init__(self, *, version: str, session_factory: Callable = requests.Session,
                 host_gap: float = 10.0, global_gap: float = 0.5, per_host_cap: int = 25,
                 timeout: float = 30.0, max_bytes: int = 100 * 1024 * 1024,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 rand: Callable[[], float] = random.random,
                 resolver: Callable[[str], list[str]] = resolve_host) -> None:
        self.user_agent = f"PaperRaven-fetch/{version}"
        self.resolver = resolver
        self._session_factory = session_factory
        self.host_gap, self.global_gap, self.per_host_cap = host_gap, global_gap, per_host_cap
        self.timeout, self.max_bytes = timeout, max_bytes
        self._clock, self._rand = clock, rand
        #: Set to stop every thread: waits end at once and no new request starts (Ctrl-C).
        self.stop = threading.Event()
        # The default sleep is the stop event's wait, so a stop ends a 10 s wait at once.
        self._sleep = sleep or self.stop.wait
        self._lock = threading.Lock()
        #: Notified at every release, for a claim waiting on requests in flight near the cap.
        self._released = threading.Condition(self._lock)
        self._last_host: dict[str, float] = {}
        self._last_any: float | None = None
        self._in_flight: dict[str, int] = {}
        self.pdfs: dict[str, int] = {}
        self.dropped: dict[str, str] = {}
        self.capped: set[str] = set()
        self.requests = 0

    # ------------------------------------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        """One document, following its own redirects. A fresh cookie jar per call."""
        session = self._session_factory()
        session.headers = {"User-Agent": self.user_agent,
                           "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                           "Accept-Language": "en-US,en;q=0.9"}
        try:
            return self._follow(session, url)
        except (requests.RequestException, ValueError) as exc:
            # A stream that breaks mid-body, or a malformed redirect or meta-refresh
            # URL (`urljoin` raises ValueError), is one failed download, not a crashed run.
            return FetchResult("error", url, url, detail=type(exc).__name__)
        finally:
            session.close()

    def _follow(self, session, url: str) -> FetchResult:
        start, refreshed = url, False
        for _ in range(10):
            if reason := unsafe_url(url) or private_resolution(url, self.resolver):
                return FetchResult("error", start, url,
                                   detail=f"not a public web address ({reason}); not requested")
            host = urlparse(url).netloc
            resp = self._request(session, url, host)
            if isinstance(resp, FetchResult) and resp.kind in _NOT_SENT:
                resp.url, resp.final_url = start, url
                return resp
            pdf, refused = False, None
            try:                                        # a claimed request: in flight until read
                if isinstance(resp, FetchResult):
                    resp.url = start
                    return resp
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    url = urljoin(url, resp.headers["Location"])
                    resp.close()
                    continue
                body, too_large = self._read(resp)
                status = resp.status_code
                resp.close()
                challenge = (not body.startswith(b"%PDF") and len(body) < 200_000
                             and CHALLENGE.search(body[:20000]))
                if not too_large and (status in (403, 429) or challenge):
                    refused = f"HTTP {status}" + (" challenge" if challenge else "")
                pdf = not too_large and status < 400 and body.startswith(b"%PDF")
            except _Stopped:
                return FetchResult("stopped", start, url, detail="the run was stopped")
            finally:
                # The PDF counted and the refusal recorded as the request stops being in
                # flight, in one locked step, so no claim sees neither.
                self._release(host, pdf=pdf, refused=refused)
            if too_large:
                return FetchResult("too_large", start, url, status=status)
            if refused:
                return FetchResult("blocked", start, url, status=status, detail=refused)
            if status == 404:
                return FetchResult("not_found", start, url, status=status)
            if status >= 400:
                return FetchResult("error", start, url, status=status, detail=f"HTTP {status}")
            if pdf:
                return FetchResult("pdf", start, url, body, status)
            m = _META_REFRESH.search(body[:20000])
            if m and not refreshed:
                refreshed = True
                url = urljoin(url, m.group(1).decode("latin-1"))
                continue
            return FetchResult("html", start, url, body, status)
        return FetchResult("error", start, url, detail="too many redirects")

    def _request(self, session, url: str, host: str):
        """One attempt, and one retry after a network error or a 5xx. A claimed attempt stays
        in flight until the caller releases it; a retry releases the first attempt."""
        resp = self._attempt(session, url, host)
        if isinstance(resp, FetchResult) and resp.kind in _NOT_SENT:
            return resp
        if isinstance(resp, FetchResult) or resp.status_code >= 500:
            if not isinstance(resp, FetchResult):
                resp.close()
            self._release(host)
            resp = self._attempt(session, url, host)
        return resp

    def _attempt(self, session, url: str, host: str):
        refused = self._claim(host)
        if refused is not None:
            refused.url = refused.final_url = url
            return refused
        try:
            return session.get(url, timeout=self.timeout, allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            return FetchResult("error", url, url, detail=type(exc).__name__)
        except BaseException:
            self._release(host)                        # never left in flight
            raise

    def _claim(self, host: str) -> FetchResult | None:
        """Wait until a request to `host` may start, then claim it: under the lock, after the
        wait, so a host another thread got refused or capped meanwhile is never asked (a host
        is dropped at its first refusal). The claim counts the request as in flight. The host
        is capped when its PDFs reach the cap; while requests in flight could still reach it,
        the claim waits for one to finish. No thread sleeps
        holding the lock; a thread that wakes checks again, so the gaps hold even when it wakes
        late. `None` means claimed."""
        gap = self.gap_for(host)                       # one jitter draw per request
        while True:
            with self._lock:
                if self.stop.is_set():
                    return FetchResult("stopped", "", "", detail="the run was stopped")
                if host in self.dropped:
                    return FetchResult("dropped", "", "", detail=self.dropped[host])
                if self.pdfs.get(host, 0) >= self.per_host_cap:
                    self.capped.add(host)
                    return FetchResult("capped", "", "")
                if self.pdfs.get(host, 0) + self._in_flight.get(host, 0) >= self.per_host_cap:
                    # Woken by a release; the timeout only bounds how late a stop is seen.
                    self._released.wait(_CAP_WAIT)
                    continue
                now = self._clock()
                start = self._earliest(host, now, gap)
                if start <= now:
                    self._last_host[host] = self._last_any = now
                    self._in_flight[host] = self._in_flight.get(host, 0) + 1
                    self.requests += 1
                    return None
            self._sleep(start - now)

    def _release(self, host: str, *, pdf: bool = False, refused: str | None = None) -> None:
        with self._lock:
            self._in_flight[host] -= 1
            if pdf:
                self.pdfs[host] = self.pdfs.get(host, 0) + 1
            if refused:
                self.dropped.setdefault(host, refused)
            self._released.notify_all()

    def _earliest(self, host: str, now: float, gap: float) -> float:
        """When a request to `host` may start: the global gap after any request, and `gap` after
        this host's last."""
        start = now
        if self._last_any is not None:
            start = max(start, self._last_any + self.global_gap)
        if host in self._last_host:
            start = max(start, self._last_host[host] + gap)
        return start

    def gap_for(self, host: str) -> float:
        """Seconds between two requests to `host`: 10 for a publisher, 2 plus jitter for a DOI
        resolver."""
        if host in RESOLVER_GAP:
            return RESOLVER_GAP[host] + RESOLVER_JITTER * self._rand()
        return self.host_gap

    def _read(self, resp) -> tuple[bytes, bool]:
        chunks, size = [], 0
        for chunk in resp.iter_content(1 << 16):
            if self.stop.is_set():                     # Ctrl-C mid-download
                raise _Stopped
            size += len(chunk)
            if size > self.max_bytes:
                return b"", True
            chunks.append(chunk)
        return b"".join(chunks), False
