"""Europe PMC client — abstracts for verified records that have none.

One question only: what abstract does Europe PMC hold for this DOI? Existence, titles and
metadata stay with OpenAlex and Crossref; `abstracts.fill_missing_abstracts` decides whether
an answer is usable.

**What goes out**: no email and no `mailto`, and a User-Agent naming only the
tool. Europe PMC asks for no key and no identification, and applies its limits per IP address.

**Pacing**: a second between requests. Europe PMC staff have stated a throttle
of 10 requests a second and 500 a minute, past which answers may come back **empty rather than
as an error**, which would read as "no record". The pass records every lookup's outcome so such
a pattern is visible.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import PoliteSession

EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: Seconds between Europe PMC requests.
REQUEST_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class AbstractHit:
    """Europe PMC's record for a DOI, raw: the title it holds and its abstract, if any."""

    title: str | None
    abstract: str | None


class EuropePMC:
    name = "europepmc"

    def __init__(self, session: PoliteSession) -> None:
        self.session = session

    def abstract_for(self, doi: str) -> AbstractHit | None:
        """The first record Europe PMC holds under ``doi``, or `None` when it holds none.

        One request per DOI. Raises `RegistryUnavailable` when Europe PMC
        cannot be reached.
        """
        data = self.session.get_json(EUROPE_PMC_SEARCH, {
            "query": f'DOI:"{doi.replace(chr(34), "")}"',
            "resultType": "core",
            "format": "json",
        })
        results = (data.get("resultList") or {}).get("result") or []
        if not results:
            return None
        first = results[0]
        return AbstractHit(title=first.get("title"), abstract=first.get("abstractText"))

    def pdf_links_for(self, doi: str) -> list[str]:
        """Europe PMC's open-access or free PDF links for ``doi``, for `paperraven fetch`.

        From `fullTextUrlList`: entries whose `documentStyle` is ``pdf`` and whose
        `availabilityCode` is ``OA`` (open access) or ``F`` (free). Subscription links (``S``)
        are left out.
        """
        data = self.session.get_json(EUROPE_PMC_SEARCH, {
            "query": f'DOI:"{doi.replace(chr(34), "")}"', "resultType": "core", "format": "json"})
        results = (data.get("resultList") or {}).get("result") or []
        if not results:
            return []
        links = (results[0].get("fullTextUrlList") or {}).get("fullTextUrl") or []
        return [u["url"] for u in links
                if u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F")
                and u.get("url")]
