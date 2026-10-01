"""Crossref client — the **failover** registry.

Crossref is used when OpenAlex is *unresponsive*, not as a
cross-check: OpenAlex contains ~99% of Crossref and Crossref ~90% of OpenAlex, so
agreement between two heavily overlapping sources is weak evidence of existence, not
strong. Crossref's value is that its REST API is fully free and open, which makes it a
dependable failover.

When Crossref verifies a paper, the abstract it carries is kept. Missing abstracts are
looked up in Europe PMC, not here (`registry/europepmc.py`).
"""

from __future__ import annotations

import re
from typing import Any

from ..normalize import clean_text, doi_for_url
from .base import PoliteSession, RegistryWork, query_title

CROSSREF_API = "https://api.crossref.org"

_JATS = re.compile(r"</?jats:[^>]*>")


def _author_parts(a: Any) -> tuple[Any, Any, Any]:
    """A Crossref author's given name, family name and whole name, a part that is blank once
    cleaned as `None` (a blank family would write an empty author, for which biber drops the
    whole entry, and would hide an organisation's `name`)."""
    a = a if isinstance(a, dict) else {}
    return tuple(v if clean_text(v) else None
                 for v in (a.get("given"), a.get("family"), a.get("name")))


def _authors(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for given, family, name in map(_author_parts, item.get("author") or []):
        if given and family:
            out.append(f"{given} {family}")
        elif family:
            out.append(str(family))
        elif name:
            out.append(str(name))
    # A given name alone is kept in `author_names` for the bib, not here: these names are
    # matched as surnames and make keys and file names.
    return out


def _year(item: dict[str, Any]) -> int | None:
    """Prefer `issued` over `created`.

    `created` is when the DOI was registered, which for back-catalogue digitisation can
    be decades after publication — a real hazard for a corpus spanning the 1950s onward.
    """
    for key in ("issued", "published-print", "published-online", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and isinstance(parts[0], int):
            return parts[0]
    return None


def _date_year(obj: Any) -> int | None:
    parts = ((obj or {}).get("date-parts") or [[None]])[0]
    return parts[0] if parts and isinstance(parts[0], int) else None


def _print_year(item: dict[str, Any]) -> int | None:
    """The print date, or failing that the journal issue's print date."""
    return (_date_year(item.get("published-print"))
            or _date_year((item.get("journal-issue") or {}).get("published-print")))


def _publication_years(item: dict[str, Any]) -> tuple[int, ...]:
    """Every publication year stated. `issued` is "earliest of published-print and
    published-online" (Crossref REST API format), so it adds nothing, but it is kept for a
    record that states no other date. `created` is DOI registration and is excluded: for
    back-catalogue digitisation it can be decades after publication."""
    years = {_date_year(item.get(k)) for k in ("published-print", "published-online",
                                               "published", "issued")}
    years.add(_print_year(item))
    return tuple(sorted(y for y in years if y is not None))


def _first(seq: Any) -> str | None:
    if isinstance(seq, list) and seq:
        return str(seq[0])
    return None


def _abstract(item: dict[str, Any]) -> str | None:
    """Crossref abstracts arrive as JATS-tagged XML; strip the tags."""
    raw = item.get("abstract")
    if not raw:
        return None
    text = _JATS.sub(" ", str(raw))
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split()).strip() or None


def work_from_item(item: dict[str, Any]) -> RegistryWork:
    """Reduce a Crossref ``message`` item to a :class:`RegistryWork`."""
    return RegistryWork(
        doi=(item.get("DOI") or "").lower() or None,
        title=_first(item.get("title")),
        source="crossref",
        authors=_authors(item),
        year=_year(item),
        publication_years=_publication_years(item),
        year_print=_print_year(item),
        venue=_first(item.get("container-title")),
        type=item.get("type"),
        cited_by_count=item.get("is-referenced-by-count"),
        abstract=_abstract(item),
        bibliographic=_bibliographic(item),
        raw=item,
    )


_CORRECTION_TYPES = {"correction", "erratum", "corrigendum", "addendum"}


def _date(item: dict[str, Any]) -> str | None:
    """The `issued` date as ISO text, as precise as Crossref states it (2009, 2009-11, …)."""
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    parts = [p for p in (parts or []) if isinstance(p, int)]
    if not parts:
        return None
    return "-".join([f"{parts[0]:04d}", *(f"{p:02d}" for p in parts[1:3])])


def _bibliographic(item: dict[str, Any]) -> dict[str, Any]:
    """What Crossref deposits for a citation. A page range wins over an article number."""
    page = str(item.get("page") or "").strip() or None          # may arrive as a number
    notices = [u for u in (item.get("updated-by") or []) if isinstance(u, dict)]
    retractions = [u.get("DOI") for u in notices
                   if str(u.get("type", "")).lower() == "retraction" and u.get("DOI")]
    relations: dict[str, list[str]] = {}
    for kind, targets in (item.get("relation") or {}).items():
        ids = [t.get("id") for t in (targets or []) if isinstance(t, dict) and t.get("id")]
        if ids:
            relations[kind] = ids
    out = {"volume": item.get("volume") or None, "issue": item.get("issue") or None,
           "pages": page or (item.get("article-number") or None),
           "publisher": item.get("publisher") or None,
           "issn": list(item.get("ISSN") or []),
           "journal_abbrev": _first(item.get("short-container-title")),
           "language": item.get("language") or None,
           "publication_date": _date(item),
           "author_names": [{"given": given, "family": family,
                             "name": name if not family else None}
                            for given, family, name in map(_author_parts, item.get("author") or [])
                            if family or name or given],
           "retraction_notices": retractions,
           "corrections": [u.get("DOI") for u in notices if u.get("DOI")
                           and str(u.get("type", "")).lower() in _CORRECTION_TYPES],
           "relations": relations}
    if retractions:
        out["is_retracted"], out["retraction_source"] = True, "Crossref"
    return {k: v for k, v in out.items() if v not in (None, [], {})}


class CrossrefRegistry:
    """Crossref metadata client (failover)."""

    name = "crossref"

    def __init__(self, session: PoliteSession, base_url: str = CROSSREF_API) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    def fetch_by_doi(self, doi: str) -> RegistryWork:
        url = f"{self.base_url}/works/{doi_for_url(doi)}"
        data = self.session.get_json(url)
        return work_from_item(data.get("message") or {})

    def search_by_title(self, title: str, limit: int = 5) -> list[RegistryWork]:
        """Bibliographic title search returning several candidates.

        ``rows`` is deliberately > 1: with ``rows: 1``, any paper whose top hit was wrong
        would be silently lost.
        """
        url = f"{self.base_url}/works"
        params = {
            "query.bibliographic": query_title(title),
            "rows": max(1, min(int(limit), 50)),
            "select": "DOI,title,author,issued,created,container-title,type,"
                      "is-referenced-by-count,abstract,volume,issue,page,article-number,"
                      "publisher,ISSN,short-container-title,relation,updated-by",
            # Not `language`: Crossref offers no such `select` here and refuses the title
            # search with HTTP 400; the DOI lookup, which selects nothing, still has it.
        }
        data = self.session.get_json(url, params=params)
        items = ((data.get("message") or {}).get("items")) or []
        return [work_from_item(i) for i in items]
