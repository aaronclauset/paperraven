"""Registry protocol, retry policy and polite-pool discipline."""

from __future__ import annotations

import random
import re
import time
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

# --------------------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------------------

#: Worth retrying with exponential backoff. 429 additionally honours `Retry-After`.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Never retried. Retrying these wastes the polite pool's goodwill and never succeeds.
#: NB 404 is fatal *and meaningful*: it is the signal for `doi_unresolved`, i.e. the
#: paper does not exist. That is a real answer, not a failure.
FATAL_STATUS: frozenset[int] = frozenset({400, 401, 403, 404})

#: The longest `Retry-After` honoured. A longer one trips the session instead, rather than
#: sleeping for hours inside one command.
RETRY_AFTER_CAP_SECONDS = 60.0


class RegistryUnavailable(Exception):
    """Raised when a registry could not be reached after exhausting attempts.

    This maps to `registry_error` — **our** infrastructure failing, not a rejection of
    the paper. Callers must route these to a retry queue rather than
    dropping the proposal, or a registry outage silently shrinks the corpus.
    """

    def __init__(self, service: str, detail: str) -> None:
        super().__init__(f"{service} unavailable: {detail}")
        self.service = service
        self.detail = detail


class QueryRejected(RegistryUnavailable):
    """The registry refused *our query* (HTTP 400): our fault, not an outage.

    A subclass, so every caller that treats an unanswered request as a registry error keeps
    doing so; only the title cascade tells the two apart, because retrying a query the
    registry has already called malformed only repeats it.
    """


class NotFound(Exception):
    """The registry answered definitively that the identifier does not exist (HTTP 404)."""


class AuthRefused(Exception):
    """The registry refused the user's API key: HTTP 401 or 403 on a keyed request.

    Not a `RegistryUnavailable`, so no fallback absorbs it: silently going keyless would hide
    the misconfiguration and bring back the rate limits the key exists to avoid. The command
    stops and says how to fix it.
    """

    def __init__(self, service: str, status: int, key_name: str = "OPENALEX_API_KEY") -> None:
        super().__init__(f"{service} refused {key_name} (HTTP {status}). Fix or remove it in "
                         f".env to use the keyless pool.")
        self.service = service
        self.status = status


# --------------------------------------------------------------------------------------
# Normalized work
# --------------------------------------------------------------------------------------


@dataclass
class Location:
    """One place a work is available. Used for version-of-record selection."""

    version: str | None = None
    is_oa: bool = False
    pdf_url: str | None = None
    landing_page_url: str | None = None
    license: str | None = None
    source_name: str | None = None
    source_type: str | None = None


@dataclass
class RegistryWork:
    """A registry response reduced to the fields PaperRaven uses.

    Deliberately narrow: everything here is a *registry fact*. Model-supplied metadata
    never enters this object: the registry value wins.
    """

    doi: str | None
    title: str | None
    source: str
    #: The registry's own identifier for the work (OpenAlex ``W...``), so a work indexed
    #: without a DOI can still be admitted and deduplicated.
    registry_id: str | None = None
    #: The OpenAlex work IDs this work cites, in the registry's order. Empty when the
    #: registry has no reference list, and always from Crossref.
    references: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    #: Every **publication** year the registry states — print, online, issue — and never
    #: DOI registration (`created`). Filled by Crossref, which is the only registry that
    #: carries a print date; read by the year limits, which admit a paper if any
    #: of them is inside the limits. `year` keeps its own semantics unchanged.
    publication_years: tuple[int, ...] = ()
    #: The print or journal-issue year, when stated. What a citation normally gives.
    year_print: int | None = None
    venue: str | None = None
    type: str | None = None

    is_oa: bool = False
    oa_status: str | None = None
    oa_url: str | None = None
    best_oa_pdf_url: str | None = None
    best_oa_version: str | None = None
    best_oa_license: str | None = None

    cited_by_count: int | None = None
    primary_version: str | None = None
    locations: list[Location] = field(default_factory=list)

    #: OpenAlex stores abstracts as a word -> positions map that must be reconstructed
    #: before a model can read it. Kept raw here; `abstracts.py` owns
    #: reconstruction, the Europe PMC lookup and coverage reporting.
    abstract_inverted_index: dict[str, list[int]] | None = None
    abstract: str | None = None

    #: The `Bibliographic` fields this registry states, as a plain dict (the model lives
    #: in `models`, which the registries do not import).
    bibliographic: dict[str, Any] = field(default_factory=dict)

    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Every registry builds this object, so its text is cleaned once, here.
        from ..normalize import clean_text, usable_abstract
        self.title = clean_text(self.title)
        self.venue = clean_text(self.venue)
        self.authors = [a for a in (clean_text(x) for x in self.authors) if a]
        self.abstract = usable_abstract(clean_text(self.abstract), self.title)
        self.bibliographic = _cleaned(self.bibliographic)

    @property
    def is_preprint(self) -> bool:
        """Whether this work is a preprint (drives version-of-record resolution for preprints)."""
        t = (self.type or "").lower()
        return t in {"preprint", "posted-content"}


#: The human-readable text in a registry's bibliographic detail: cleaned as titles are. All
#: other strings (identifiers, DOIs, URLs, pages, dates) are only trimmed. The name parts are
#: text only inside `author_names`, never under another key.
_TEXT_FIELDS = frozenset({"publisher", "journal_abbrev", "language"})
_NAME_FIELDS = frozenset({"given", "family", "name"})


def _cleaned(value: Any, text: bool = False, fields: frozenset = _TEXT_FIELDS) -> Any:
    """Bibliographic detail made safe to store: human text cleaned as titles are, identifiers
    only trimmed, numbers as text, and a blank list item dropped rather than stored as `None`.
    `fields` are the keys whose values are text at this level."""
    from ..normalize import clean_text
    if isinstance(value, dict):
        return {k: _cleaned(v, k in fields, _NAME_FIELDS if k == "author_names" else frozenset())
                for k, v in value.items()}
    if isinstance(value, list):
        return [c for c in (_cleaned(v, text, fields) for v in value) if c is not None]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return clean_text(value) if text else (value.strip() or None)
    return value


def query_title(title: str) -> str:
    """A title as search text: its words only.

    OpenAlex's search syntax gives meaning to `?` and `*` (wildcards, a 400 in stemmed search),
    quotes, `~`, AND/OR/NOT, and, in a filter, `,` `|` `!` `+`; it has no escaping, and it
    matches whole words, so punctuation adds nothing. Letters
    and digits of any script, and hyphens inside words, are kept; everything else is a space;
    AND/OR/NOT are written in lower case. The matcher still compares the full titles."""
    text = re.sub(r"(?<=\w)-(?=\w)", "\0", title)
    text = re.sub(r"[^\w\0]+|_", " ", text).replace("\0", "-")
    return " ".join(w.lower() if w in ("AND", "OR", "NOT") else w for w in text.split())


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """Rebuild readable text from OpenAlex's `abstract_inverted_index`.

    The index maps each word to the positions at which it occurs, so inverting it means
    placing every word at each of its positions and reading left to right.
    """
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort(key=lambda t: t[0])
    return " ".join(word for _, word in positions)


# --------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------


class Registry(Protocol):
    """What `verify.py` needs from a metadata source."""

    name: str

    def fetch_by_doi(self, doi: str) -> RegistryWork:
        """Return the work for ``doi``.

        Raises ``NotFound`` when the registry says it does not exist, and
        ``RegistryUnavailable`` when we could not get an answer.
        """
        ...

    def search_by_title(self, title: str, limit: int = 5) -> list[RegistryWork]:
        """Return up to ``limit`` candidate works matching ``title``.

        More than one deliberately: with only the top hit, any paper whose top hit is wrong
        would be silently lost.
        """
        ...


# --------------------------------------------------------------------------------------
# HTTP with retry + politeness
# --------------------------------------------------------------------------------------


class PoliteSession:
    """A ``requests`` session applying retry and politeness rules.

    Politeness is not optional courtesy: both registries grant faster and
    more reliable service to identified clients, so `mailto` goes in the query string
    *and* a real contact appears in the User-Agent.

    A session with no `contact_email` sends no `mailto`: Europe PMC asks for no
    identification, and the user's address goes only where they authorised it.
    """

    def __init__(
        self,
        service: str,
        contact_email: str | None,
        user_agent: str,
        *,
        max_attempts: int = 4,
        timeout_seconds: float = 30.0,
        request_delay_seconds: float = 0.1,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        auth_params: dict[str, str] | None = None,
        auth_source: str | None = None,
    ) -> None:
        self.service = service
        self.contact_email = contact_email
        self.user_agent = user_agent
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.request_delay_seconds = request_delay_seconds
        self._session = session or requests.Session()
        self._sleep = sleep
        #: Per-run counters. A small metrics object, not a throttle database: at a handful of
        #: requests per round, persistent throttle state is complexity with no payoff.
        self.metrics: dict[str, int] = {
            "requests": 0,
            "retries": 0,
            "rate_limit_hits": 0,
            "not_found": 0,
            "unavailable": 0,
        }
        #: Sent on every request: one code path, so no call can go keyless by omission.
        self.auth_params = dict(auth_params or {})
        self.auth_source = auth_source
        #: Why this session stopped calling out, once one request exhausted its retries. It
        #: lasts the rest of the process, i.e. one CLI command.
        self.tripped: str | None = None
        self.retry_after_max: float | None = None
        self.remaining_usd: float | None = None

    # ----------------------------------------------------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``url`` and return parsed JSON, applying retry policy.

        Raises ``NotFound`` on 404 and ``RegistryUnavailable`` on exhausted attempts.
        """
        if self.tripped:
            raise RegistryUnavailable(self.service, f"skipped: {self.service} refused service "
                                                    f"earlier in this command ({self.tripped})")
        params = {**(params or {}), **self.auth_params}
        if self.contact_email:
            params.setdefault("mailto", self.contact_email)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}

        last_detail = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            self.metrics["requests"] += 1
            try:
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=self.timeout_seconds
                )
            except requests.RequestException as exc:
                # Connection errors and read timeouts are retryable. Their message carries
                # the URL with its query string, so secrets are masked.
                last_detail = self._redact(f"{type(exc).__name__}: {exc}")
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt)
                continue

            status = resp.status_code
            self._note_budget(resp.headers)

            if status == 404:
                self.metrics["not_found"] += 1
                raise NotFound(f"{self.service} 404 for {url}")

            if status in FATAL_STATUS:
                if self.auth_params and status in (401, 403):
                    raise AuthRefused(self.service, status)
                if status == 400:
                    raise QueryRejected(self.service, "HTTP 400 (fatal)")
                # Fatal and not meaningful: no retry, but it is still our failure to
                # get an answer, so it surfaces as registry_error rather than a rejection.
                raise RegistryUnavailable(self.service, f"HTTP {status} (fatal)")

            if status in RETRYABLE_STATUS:
                last_detail = f"HTTP {status}"
                if status == 429:
                    self.metrics["rate_limit_hits"] += 1
                # The cap holds for every retryable status, not only 429: a 503 can also
                # ask for hours.
                wait = retry_after_seconds(resp.headers.get("Retry-After"))
                if wait is not None:
                    self.retry_after_max = max(wait, self.retry_after_max or 0.0)
                    if wait > RETRY_AFTER_CAP_SECONDS:
                        last_detail = (f"HTTP {status}, Retry-After {wait:g}s is over the "
                                       f"{RETRY_AFTER_CAP_SECONDS:g}s cap")
                        break
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                continue

            if not resp.ok:
                last_detail = f"HTTP {status}"
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt)
                continue

            try:
                data = resp.json()
                if not isinstance(data, dict):
                    # A proxy or error page can answer JSON that is not a record.
                    raise ValueError(f"not a JSON object ({type(data).__name__})")
            except ValueError as exc:
                last_detail = f"invalid JSON: {exc}"
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt)
                continue

            if self.request_delay_seconds:
                self._sleep(self.request_delay_seconds)
            return data

        self.metrics["unavailable"] += 1
        self.tripped = last_detail
        raise RegistryUnavailable(self.service, last_detail)

    def report(self) -> dict[str, Any]:
        """What this session did, for the command's event and its summary line."""
        return {"auth": "keyed" if self.auth_params else "keyless", **self.metrics,
                "tripped": self.tripped, "retry_after_max": self.retry_after_max,
                "remaining_usd": self.remaining_usd}

    def _redact(self, text: str) -> str:
        """Mask the contact address and any credential, raw or URL-encoded.

        A session may have no contact address (Europe PMC's): that `None` is skipped, so a
        network error is retried rather than crashing in `str.replace`.
        """
        for secret in (self.contact_email, *self.auth_params.values()):
            if not secret:
                continue
            for form in {secret, quote(secret, safe="")} - {""}:
                text = text.replace(form, "<redacted>")
        return text

    def _note_budget(self, headers: Any) -> None:
        """Keep OpenAlex's latest free-allowance figure, when it sends one."""
        usd = _seconds(headers.get("X-RateLimit-Remaining-USD"))
        if usd is not None:
            self.remaining_usd = usd

    # ----------------------------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        """Exponential backoff with jitter, honouring ``Retry-After`` when present.

        Honouring the header rather than guessing is the point: the server has told us
        how long to wait, and guessing shorter is what gets a client throttled harder.
        """
        self.metrics["retries"] += 1
        wait = retry_after_seconds(retry_after)
        if wait:            # seconds, or an HTTP date still ahead
            self._sleep(wait)
            return
        delay = min(30.0, (2 ** (attempt - 1)) * 0.5)
        self._sleep(delay + random.uniform(0, 0.25))


def retry_after_seconds(value: Any) -> float | None:
    """A `Retry-After` header in seconds: a number, or an HTTP date, or `None`.

    A date already past is 0 seconds, so the caller falls back to its own backoff.
    """
    wait = _seconds(value)
    if wait is not None or not value:
        return None if wait is None else max(0.0, wait)
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    try:
        when = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _seconds(value: Any) -> float | None:
    """A header's numeric value, or `None` (an HTTP-date `Retry-After` included)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
