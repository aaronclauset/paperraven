"""OpenAlex client — the **primary** registry.

Field shapes below were **verified against the live API**; two findings shape this module:

1. ``best_oa_location.pdf_url`` can be ``None`` while ``open_access.is_oa`` is ``True``
   (the green-OA case resolves to a landing page only).
2. ``version`` is exposed per location, and the best *OA* location is sometimes a
   ``submittedVersion`` preprint while a ``publishedVersion`` exists in the same record.
   ``best_oa_location`` optimises for openness, not for version — so version-of-record
   selection must scan ``locations`` rather than trust it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

from ..normalize import doi_for_url
from .base import Location, PoliteSession, RegistryWork, query_title, reconstruct_abstract

OPENALEX_API = "https://api.openalex.org"

#: Where the user's own key lives: this name, in `.env` or the
#: environment. With no key the keyless pool is used.
OPENALEX_KEY_NAME = "OPENALEX_API_KEY"


def find_api_key(dotenv_path: str | Path = ".env",
                 environ: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """The user's OpenAlex key and where it came from, or ``(None, None)``.

    The environment wins over `.env`. Only this one name is read from the file, and
    nothing is loaded into ``os.environ``. The key never enters `Config`, which is published in
    every generation and copied into the export.
    """
    environ = os.environ if environ is None else environ
    if key := (environ.get(OPENALEX_KEY_NAME) or "").strip():
        return key, "the environment"
    path = Path(dotenv_path)
    key = (dotenv_values(path).get(OPENALEX_KEY_NAME) or "").strip() if path.is_file() else ""
    return (key, ".env") if key else (None, None)


def _authors(work: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for a in work.get("authorships") or []:
        name = ((a or {}).get("author") or {}).get("display_name")
        if name:
            out.append(name)
    return out


def _venue(work: dict[str, Any]) -> str | None:
    src = ((work.get("primary_location") or {}).get("source") or {})
    return src.get("display_name")


def _locations(work: dict[str, Any]) -> list[Location]:
    out: list[Location] = []
    for loc in work.get("locations") or []:
        if not loc:
            continue
        src = loc.get("source") or {}
        out.append(
            Location(
                version=loc.get("version"),
                is_oa=bool(loc.get("is_oa")),
                pdf_url=loc.get("pdf_url"),
                landing_page_url=loc.get("landing_page_url"),
                license=loc.get("license"),
                source_name=src.get("display_name"),
                source_type=src.get("type"),
            )
        )
    return out


def work_id(url: Any) -> str | None:
    """``"https://openalex.org/W102443495"`` -> ``"W102443495"``; anything else -> None."""
    if not isinstance(url, str):
        return None
    wid = url.rsplit("/", 1)[-1]
    return wid if wid.startswith("W") else None


def work_from_json(work: dict[str, Any]) -> RegistryWork:
    """Reduce an OpenAlex ``Work`` object to a :class:`RegistryWork`."""
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    inverted = work.get("abstract_inverted_index")

    doi = work.get("doi")
    if isinstance(doi, str):
        # OpenAlex returns the DOI as a full URL; normalization happens downstream, but
        # trimming the scheme here keeps the object readable.
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

    year = work.get("publication_year")

    return RegistryWork(
        doi=doi,
        title=work.get("title") or work.get("display_name"),
        source="openalex",
        registry_id=work_id(work.get("id")),
        references=[w for w in map(work_id, work.get("referenced_works") or []) if w],
        authors=_authors(work),
        year=int(year) if isinstance(year, int) else None,
        venue=_venue(work),
        type=work.get("type"),
        is_oa=bool(oa.get("is_oa")),
        oa_status=oa.get("oa_status"),
        oa_url=oa.get("oa_url"),
        best_oa_pdf_url=best.get("pdf_url"),
        best_oa_version=best.get("version"),
        best_oa_license=best.get("license"),
        cited_by_count=work.get("cited_by_count"),
        primary_version=(work.get("primary_location") or {}).get("version"),
        locations=_locations(work),
        abstract_inverted_index=inverted,
        abstract=reconstruct_abstract(inverted),
        bibliographic=_bibliographic(work),
        raw=work,
    )


def _pages(first: Any, last: Any) -> str | None:
    first, last = (str(x).strip() if x not in (None, "") else None for x in (first, last))
    if first and last and last != first:
        return f"{first}-{last}"
    return first or last


def _bibliographic(work: dict[str, Any]) -> dict[str, Any]:
    """OpenAlex's `biblio`, language, date, retraction flag and the host's name."""
    biblio = work.get("biblio") or {}
    src = ((work.get("primary_location") or {}).get("source") or {})
    retracted = work.get("is_retracted")
    out = {"volume": biblio.get("volume") or None, "issue": biblio.get("issue") or None,
           "pages": _pages(biblio.get("first_page"), biblio.get("last_page")),
           "language": work.get("language") or None,
           "publication_date": work.get("publication_date") or None,
           # A repository's host is an institution, not a publisher.
           "publisher": (src.get("host_organization_name") or None)
                        if src.get("type") != "repository" else None,
           "issn": [src["issn_l"]] if src.get("issn_l") else [],
           "is_retracted": retracted if isinstance(retracted, bool) else None}
    if retracted is True:
        out["retraction_source"] = "OpenAlex"
    return {k: v for k, v in out.items() if v not in (None, [])}



class OpenAlexRegistry:
    """OpenAlex metadata client: the polite pool (a ``mailto``), keyed when the user has an
    ``OPENALEX_API_KEY`` and keyless otherwise. The key rides on the session, so it is on every
    request this client makes.
    """

    name = "openalex"

    #: Fields requested explicitly. Asking for a subset keeps responses small and, more
    #: importantly, makes the dependency on each field visible in code review.
    SELECT_FIELDS = (
        "id,doi,title,display_name,publication_year,type,authorships,primary_location,"
        "locations,locations_count,open_access,best_oa_location,cited_by_count,"
        "abstract_inverted_index,biblio,is_retracted,language,publication_date,"
        "referenced_works"
    )

    def __init__(self, session: PoliteSession, base_url: str = OPENALEX_API) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    # ----------------------------------------------------------------------------------

    def fetch_by_doi(self, doi: str) -> RegistryWork:
        """Exact key lookup — the primary path.

        A DOI lookup either resolves or it does not: no fuzzy scoring, no `rows: N`, no
        score thresholds, so a real paper cannot be lost to a wrong top hit.
        """
        url = f"{self.base_url}/works/doi:{doi_for_url(doi)}"
        data = self.session.get_json(url, params={"select": self.SELECT_FIELDS})
        return work_from_json(data)

    def fetch_by_id(self, work_id: str) -> RegistryWork:
        """A work by its OpenAlex id (`W…`), for a record without a DOI."""
        url = f"{self.base_url}/works/{work_id}"
        data = self.session.get_json(url, params={"select": self.SELECT_FIELDS})
        return work_from_json(data)

    #: The filters `fetch_batch` accepts, and the most values one call may carry.
    BATCH_FIELDS = ("ids.openalex", "doi")
    BATCH_SIZE = 50

    def fetch_batch(self, field: str, values: Sequence[str]) -> list[RegistryWork]:
        """Up to `BATCH_SIZE` works in one call, by OpenAlex work ID or by DOI.

        Values are joined with `|`, OpenAlex's OR within a filter, so a DOI containing `|`
        or `,` cannot be sent this way; the caller looks such a DOI up alone. Works the
        registry does not hold are simply absent from the result.
        """
        if field not in self.BATCH_FIELDS:
            raise ValueError(f"fetch_batch: unsupported filter {field!r}")
        values = list(values)
        if len(values) > self.BATCH_SIZE:
            raise ValueError(f"fetch_batch: at most {self.BATCH_SIZE} values per call, "
                             f"got {len(values)}")
        params = {"filter": f"{field}:" + "|".join(values), "per-page": self.BATCH_SIZE,
                  "select": self.SELECT_FIELDS}
        data = self.session.get_json(f"{self.base_url}/works", params=params)
        return [work_from_json(w) for w in (data.get("results") or [])]

    #: Fields a `cites:` listing needs: the work's identity, the line it is listed on, and its
    #: reference list. Lighter than `SELECT_FIELDS`, since a page holds `CITING_PAGE_SIZE` works.
    CITING_FIELDS = ("id,doi,title,display_name,publication_year,type,authorships,"
                     "primary_location,referenced_works")
    CITING_PAGE_SIZE = 200

    def fetch_citing(self, cited: Sequence[str],
                     cursor: str = "*") -> tuple[list[RegistryWork], str | None]:
        """One page of the works citing any of `cited` (up to `BATCH_SIZE` OpenAlex work IDs),
        and the cursor of the next page, `None` once a page comes back empty."""
        cited = list(cited)
        if len(cited) > self.BATCH_SIZE:
            raise ValueError(f"fetch_citing: at most {self.BATCH_SIZE} works per call, "
                             f"got {len(cited)}")
        if not all(isinstance(w, str) and w[:1] == "W" and w[1:].isdigit() for w in cited):
            raise ValueError("fetch_citing: OpenAlex work IDs only")
        params = {"filter": "cites:" + "|".join(cited), "per-page": self.CITING_PAGE_SIZE,
                  "cursor": cursor, "select": self.CITING_FIELDS}
        data = self.session.get_json(f"{self.base_url}/works", params=params)
        results = data.get("results") or []
        nxt = (data.get("meta") or {}).get("next_cursor") if results else None
        return [work_from_json(w) for w in results], nxt

    def search_by_title(self, title: str, limit: int = 5) -> list[RegistryWork]:
        """Title search returning **several** candidates.

        Retrieving more than one is deliberate: with only the top hit, a real paper whose
        top hit happened to be wrong would fail verification silently.
        """
        url = f"{self.base_url}/works"
        params = {
            "filter": f"title.search:{query_title(title)}",
            "per-page": max(1, min(int(limit), 50)),
            "select": self.SELECT_FIELDS,
        }
        data = self.session.get_json(url, params=params)
        return [work_from_json(w) for w in (data.get("results") or [])]

    def search_general(self, title: str, limit: int = 5) -> list[RegistryWork]:
        """OpenAlex's general `search=`, the title cascade's second step.

        `filter=title.search:` matches whole terms in the stored title. OpenAlex stores some
        titles with two words fused where an italicised name was, and a term search cannot
        match the fused token, so real papers are missed. `search=` is broader
        (title, abstract, full text), so its candidates are noisier; the matcher decides.
        """
        url = f"{self.base_url}/works"
        params = {"search": query_title(title), "per-page": max(1, min(int(limit), 50)),
                  "select": self.SELECT_FIELDS}
        data = self.session.get_json(url, params=params)
        return [work_from_json(w) for w in (data.get("results") or [])]

    def search_published_version(self, title: str, limit: int = 5) -> list[RegistryWork]:
        """Find a non-preprint work matching ``title``, to resolve a preprint to its version of record.

        The preprint and the published paper are **separate Works with no linkage
        field** (verified against the live API), and ``related_works`` is topical similarity, not
        version linkage, so it must not be used for this.
        """
        url = f"{self.base_url}/works"
        params = {
            "filter": f"title.search:{query_title(title)},type:article",
            "per-page": max(1, min(int(limit), 50)),
            "select": self.SELECT_FIELDS,
        }
        data = self.session.get_json(url, params=params)
        return [work_from_json(w) for w in (data.get("results") or [])]
