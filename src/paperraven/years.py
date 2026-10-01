"""Publication-year limits, enforced by Python.

A year cutoff is mechanical, so it belongs to deterministic code, not to the judge. The
registry's `year` is the **earliest online** date in both registries: Crossref's `issued` is
the "earliest of published-print and published-online", and OpenAlex "usually select[s] the
earliest available date of electronic publication". A paper printed inside the limits can
therefore carry an earlier year, and would be wrongly excluded on that year alone.

**The rule: admit a paper if any of its publication dates is inside the limits.**

1. The record's own `year` is inside → nothing to do, and **no registry call**.
2. Otherwise, a record with a DOI costs **one Crossref lookup**, reading every publication
   date Crossref states — print, online, issue — and never `created`, which is DOI
   registration. Any of them inside → admitted, with `year_basis` saying which.
3. Otherwise → **excluded by rule**, `decided_by: "rule (year)"`, never sent to the judge.
4. A record with no DOI, or when Crossref is unavailable, is decided on the one year it has
   (`year_basis: "earliest only"`). A record with **no year at all** is not
   excluded on missing data — it goes to the judge with `year_basis: "unknown"`, since a
   wrongly admitted paper costs the user seconds and a wrongly excluded one is lost.

Seeds are exempt: the user supplied them, and they are never judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .models import DiscoveredBy, Verdict
from .registry.base import NotFound, RegistryUnavailable

if TYPE_CHECKING:
    from .config import Topic
    from .corpus import Corpus
    from .models import PaperRecord

RULE_DECIDER = "rule (year)"


@dataclass
class YearOutcome:
    lookups: int = 0
    excluded: list[str] = field(default_factory=list)


def limits_text(topic: "Topic") -> str | None:
    """The limits as a reader would state them, or `None` when there are none."""
    lo, hi = topic.year_min, topic.year_max
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"published {lo}-{hi}"
    return f"published in {lo} or later" if lo is not None else f"published in {hi} or earlier"


def _inside(year: int | None, topic: "Topic") -> bool:
    if year is None:
        return False
    if topic.year_min is not None and year < topic.year_min:
        return False
    if topic.year_max is not None and year > topic.year_max:
        return False
    return True


def apply_year_limits(
    corpus: "Corpus", records: Iterable["PaperRecord"], topic: "Topic", crossref
) -> YearOutcome:
    """Apply the limits to freshly verified records; move failures to `excluded`."""
    out = YearOutcome()
    if limits_text(topic) is None:
        return out
    for rec in records:
        if rec.provenance.discovered_by is DiscoveredBy.SEED or _inside(rec.year, topic):
            continue
        stated = [rec.year] if rec.year is not None else []
        work = None
        if crossref is not None and not rec.doi.startswith("openalex:"):
            out.lookups += 1
            try:
                work = crossref.fetch_by_doi(rec.doi)
            except (NotFound, RegistryUnavailable):
                work = None
        if work is not None:
            rec.year_print = work.year_print
            stated += list(work.publication_years)
            if _inside(work.year_print, topic):
                rec.year_basis = "print"
                continue
            if any(_inside(y, topic) for y in work.publication_years):
                rec.year_basis = "online"
                continue
            rec.year_basis = "all dates"
        else:
            rec.year_basis = "earliest only"
        if not stated:
            rec.year_basis = "unknown"
            continue
        _rule_out(corpus, rec, topic, stated, consulted=work is not None)
        out.excluded.append(rec.doi)
    return out


def _rule_out(corpus, rec, topic, stated, *, consulted: bool) -> None:
    rec.relevance.supersede()          # a judged record's verdict is kept
    rec.relevance.agent_verdict = Verdict.EXCLUDE
    rec.relevance.decided_by = RULE_DECIDER
    rec.relevance.criterion = "year limits"
    rec.relevance.config_generation = corpus.config_generation
    rec.relevance.agent_reason = (
        f"Outside the year limits ({limits_text(topic)}): the publication years stated "
        f"for it are {', '.join(str(y) for y in sorted(set(stated)))}"
        + ("" if consulted else
           " (only the registry's earliest date was available — no DOI, or no Crossref "
           "response — so no print date could be checked)")
        + ". Excluded by rule, not by the judge.")
    corpus.add_record(rec, verdict=Verdict.EXCLUDE)
    corpus.rejudge_pending.discard(rec.doi)


@dataclass
class RecheckOutcome:
    ruled_out: list[str] = field(default_factory=list)
    readmitted: list[str] = field(default_factory=list)
    lookups: int = 0


def recheck_year_limits(corpus: "Corpus", topic: "Topic", crossref) -> RecheckOutcome:
    """Re-apply the limits after a topic steer.

    By rule, never by the judge. A `rule (year)` record now inside the limits on a date
    already known is **readmitted**: its verdict goes to `history` and it waits for the
    judge, since it has never had a relevance verdict. A judged record now outside is
    checked exactly as a fresh one is — Crossref consulted if its own year fails — and
    excluded by rule if every date fails, without a judge call.
    """
    out = RecheckOutcome()
    for pool in (corpus.greenlist, corpus.uncertain, corpus.excluded):
        for rec in list(pool.values()):
            if rec.provenance.discovered_by is DiscoveredBy.SEED:
                continue
            if rec.relevance.decided_by == RULE_DECIDER:
                known = [y for y in (rec.year, rec.year_print) if y is not None]
                if limits_text(topic) is None or any(_inside(y, topic) for y in known):
                    rec.relevance.supersede()
                    rec.relevance.agent_verdict = None
                    rec.relevance.agent_reason = None
                    rec.relevance.decided_by = None
                    rec.relevance.criterion = None
                    rec.year_basis = None
                    corpus.add_record(rec)          # no verdict -> uncertain, awaiting
                    out.readmitted.append(rec.doi)
                continue
            if rec.relevance.agent_verdict is None:
                continue                            # not yet judged: the fresh path owns it
            fresh = apply_year_limits(corpus, [rec], topic, crossref)
            out.lookups += fresh.lookups
            out.ruled_out += fresh.excluded
    return out
