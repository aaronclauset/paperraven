"""Abstract availability: report it, and look up the missing ones in Europe PMC.

**Reconstruction lives upstream, not here.** OpenAlex's `abstract_inverted_index` is
reconstructed in `registry/openalex.py` at the moment the response is parsed, so every
`RegistryWork` and every `PaperRecord` already carries plain-text `abstract`. This module
therefore owns only the two jobs nothing else does: reporting coverage to the user, and
looking up the abstracts the verifying registry did not have.

## Coverage varies by publisher, not by age

Measured across a corpus of 100 papers, the predictor of a missing abstract is the
**publisher**, not the decade:

    Springer 15%, Springer/Kluwer 0%, Elsevier 0%
    Oxford / Wiley / Royal Society / MDPI / AAAS / Nature 100%, JSTOR 90%

The mechanism is almost certainly abstract-redistribution licensing. So coverage is reported
**by publisher**, because that is the axis the variance lies on and it tells the user which
part of their corpus was judged without an abstract.

## The lookup

Europe PMC, one request per missing abstract, over each round's new records. An answer is
used only when Europe PMC's title matches the record's and the text passes the junk rule. Every
lookup's outcome is recorded (`OUTCOMES`): Europe PMC may answer a throttled request with an
empty result, which reads as "no record", so only the pattern of outcomes can reveal it.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field

from .corpus import Corpus
from .models import PaperRecord
from .normalize import clean_text, title_similarity, usable_abstract
from .registry.base import NotFound, RegistryUnavailable

#: DOI registrant prefixes measured to withhold abstracts from both OpenAlex and Crossref.
#: Used only to explain a miss to the user, never to skip a lookup — a publisher's policy
#: can change, and hard-coding a skip would make that change invisible.
KNOWN_WITHHOLDING_PREFIXES: dict[str, str] = {
    "10.1007": "Springer",
    "10.1023": "Springer/Kluwer",
    "10.1016": "Elsevier",
}


def registrant(doi: str) -> str:
    """The DOI registrant prefix, e.g. ``10.1007``. This is the publisher axis."""
    return doi.split("/", 1)[0] if doi else ""


def has_abstract(record: PaperRecord) -> bool:
    return bool(record.abstract and record.abstract.strip())


@dataclass
class AbstractCoverage:
    """Abstract availability across a corpus, reported rather than summarised away.

    Coverage is reported to the user because a run where many candidates were judged on
    title alone is a materially weaker run, and the user should know.
    """

    total: int = 0
    with_abstract: int = 0
    #: registrant -> (n, with_abstract). The axis the variance is actually on.
    by_publisher: dict[str, tuple[int, int]] = field(default_factory=dict)
    missing_dois: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float | None:
        """``None`` for an empty corpus rather than a misleading 0.0 or 1.0."""
        return (self.with_abstract / self.total) if self.total else None

    @property
    def judged_on_title_alone(self) -> int:
        return self.total - self.with_abstract

    def summary(self) -> str:
        if not self.total:
            return "no records yet, so no abstract coverage to report"
        pct = f"{self.fraction:.0%}"
        line = (
            f"abstracts: {self.with_abstract}/{self.total} records ({pct}) have a registry "
            f"abstract; the judge is told to look the other {self.judged_on_title_alone} up"
        )
        worst = self.worst_publishers()
        if worst:
            # Name what is *missing*, so the worst-covered publisher reads as the worst.
            detail = ", ".join(
                f"{KNOWN_WITHHOLDING_PREFIXES.get(p, p)} {n - g} of {n}"
                + (" missing" if i == 0 else "")
                for i, (p, n, g) in enumerate(worst)
            )
            line += f" — worst coverage: {detail}"
        return line

    def worst_publishers(self, limit: int = 3) -> list[tuple[str, int, int]]:
        """Publishers with the least coverage, as ``(registrant, n, with_abstract)``.

        Only publishers with at least two records, so one unlucky paper does not get
        reported as a publisher-wide problem.
        """
        rows = [
            (prefix, n, got)
            for prefix, (n, got) in self.by_publisher.items()
            if n >= 2 and got < n
        ]
        rows.sort(key=lambda r: (r[2] / r[1], -r[1]))
        return rows[:limit]


def measure_coverage(records: list[PaperRecord]) -> AbstractCoverage:
    """Tabulate abstract availability, overall and by publisher."""
    cov = AbstractCoverage(total=len(records))
    counts: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for rec in records:
        ok = has_abstract(rec)
        cov.with_abstract += int(ok)
        bucket = counts[registrant(rec.doi)]
        bucket[0] += 1
        bucket[1] += int(ok)
        if not ok:
            cov.missing_dois.append(rec.doi)
    cov.by_publisher = {p: (n, g) for p, (n, g) in counts.items()}
    return cov


def corpus_coverage(corpus: Corpus) -> AbstractCoverage:
    """Coverage across every judged-or-pending record.

    All three pools, because the question "what did the judge get to read" applies to
    excluded records too — an exclusion made on a title alone is weaker evidence than one
    made on an abstract, and the audit trail should show that.
    """
    records = [
        rec
        for pool in (corpus.greenlist, corpus.uncertain, corpus.excluded)
        for rec in pool.values()
    ]
    return measure_coverage(records)


#: Europe PMC's title must match the record's this closely for its abstract to be used.
TITLE_AGREEMENT = 0.9

#: Every outcome of one lookup, in reporting order.
OUTCOMES = ("recovered", "no_record", "title_mismatch", "no_abstract", "junk", "unavailable")

_MISS_WORDS = {
    "no_record": "no record",
    "title_mismatch": "title mismatch",
    "no_abstract": "without an abstract",
    "junk": "unusable text",
    "unavailable": "not reached",
}


@dataclass
class FallbackResult:
    """What the Europe PMC pass achieved, one outcome per lookup, so 0-for-N is visible."""

    #: DOI -> outcome, in lookup order.
    lookups: dict[str, str] = field(default_factory=dict)

    @property
    def outcomes(self) -> collections.Counter[str]:
        return collections.Counter(self.lookups.values())

    @property
    def attempted(self) -> int:
        return len(self.lookups)

    @property
    def recovered(self) -> int:
        return self.outcomes["recovered"]

    @property
    def unavailable(self) -> int:
        return self.outcomes["unavailable"]

    def summary(self) -> str:
        if not self.attempted:
            return "Europe PMC abstracts: not attempted"
        misses = ", ".join(f"{self.outcomes[k]} {_MISS_WORDS[k]}" for k in OUTCOMES[1:]
                           if self.outcomes[k])
        return (f"Europe PMC abstracts: recovered {self.recovered} of {self.attempted}"
                + (f" ({misses})" if misses else ""))


def fill_missing_abstracts(
    corpus: Corpus,
    europe_pmc,
    *,
    limit: int | None = None,
    dois: "set[str] | None" = None,
) -> FallbackResult:
    """Ask Europe PMC for the abstracts of records that have none.

    Mutates records in place and leaves saving to the caller, so a partial pass cannot
    half-write state. `dois` restricts the pass to one round's new records, so a run does not
    re-query every abstract-less record it has ever seen; `limit` caps the request count. DOI
    records only: Europe PMC is asked by DOI.

    A miss of any kind leaves the record without an abstract. It is not a rejection and is not
    queued: the judge looks the paper up.
    """
    result = FallbackResult()
    pools = (corpus.greenlist, corpus.uncertain, corpus.excluded)
    targets = [
        rec for pool in pools for rec in pool.values()
        if not has_abstract(rec) and rec.doi.startswith("10.")
        and (dois is None or rec.doi in dois)
    ]
    if limit is not None:
        targets = targets[:limit]

    for rec in targets:
        result.lookups[rec.doi] = _lookup(europe_pmc, rec)
    return result


def _lookup(europe_pmc, rec: PaperRecord) -> str:
    """One record's lookup: set its abstract when usable, and return the outcome."""
    try:
        hit = europe_pmc.abstract_for(rec.doi)
    except NotFound:
        hit = None
    except RegistryUnavailable:
        # Our failure, not the paper's; the next run can try again.
        return "unavailable"
    if hit is None:
        return "no_record"
    if title_similarity(clean_text(hit.title), clean_text(rec.title)) < TITLE_AGREEMENT:
        return "title_mismatch"
    if not clean_text(hit.abstract):
        return "no_abstract"
    text = usable_abstract(clean_text(hit.abstract), rec.title)
    if text is None:
        return "junk"
    rec.abstract, rec.abstract_source = text, "europe_pmc"
    return "recovered"
