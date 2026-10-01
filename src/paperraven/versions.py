"""Version-of-record resolution and acquisition-location selection.

There is no single "pointer to VoR" field in OpenAlex. There are two structurally
different cases, and they need different handling:

**Case A — one Work, several locations.** The published version is already inside the
record; ``best_oa_location`` may point at a ``submittedVersion`` instead because it
optimises for *openness*, not for *version*. So scan ``locations`` and select by version
preference rather than trusting that field.

**Case B — preprint and published paper are separate Works.** No version-linkage field
connects them, and ``related_works`` is topical similarity, so it must not be used here.
A title search for a non-preprint counterpart is the available route.

Policy: attempt VoR resolution first; on failure keep the non-VoR record and flag it.
Preprints are acceptable — but **provenance for extracted data must be tracked**, because
numbers are pulled out of whichever copy's figures a reader is handed, and figures get
revised between preprint and publication.
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import identifier_url, normalize_doi, title_similarity
from .registry.base import Location, NotFound, Registry, RegistryUnavailable, RegistryWork

#: Most-preferred first. `publishedVersion` is the version of record; `acceptedVersion`
#: is peer-reviewed but not typeset; `submittedVersion` has not been reviewed at all.
VERSION_PREFERENCE: tuple[str, ...] = ("publishedVersion", "acceptedVersion", "submittedVersion")

#: Acquisition difficulty tiers for the human worklist.
#: Three tiers, not two, because `pdf_url` can be ``None`` while ``is_oa`` is ``True`` —
#: verified on the green-OA case, which resolves to a PMC landing page only.
TIER_DIRECT_PDF = 0
TIER_OA_LANDING = 1
TIER_CLOSED = 2

TIER_LABELS = {
    TIER_DIRECT_PDF: "direct_pdf",
    TIER_OA_LANDING: "oa_landing_page",
    TIER_CLOSED: "closed",
}


def _version_rank(version: str | None) -> int:
    """Lower is better. Unknown versions sort after all known ones."""
    if not version:
        return len(VERSION_PREFERENCE)
    try:
        return VERSION_PREFERENCE.index(version)
    except ValueError:
        return len(VERSION_PREFERENCE)


@dataclass
class AcquisitionTarget:
    """Where a human should go to obtain this paper, and how hard it will be."""

    tier: int
    url: str | None
    version: str | None
    license: str | None
    source_name: str | None
    is_oa: bool

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "unknown")

    @property
    def is_version_of_record_copy(self) -> bool:
        """Whether the *copy* being offered is the version of record.

        Distinct from whether the *work* is the VoR: a published article can have only a
        submitted-version copy openly available, in which case extracting data from it
        and attributing it to the published paper is a data-integrity problem.
        """
        return self.version == "publishedVersion"


@dataclass
class AcquisitionOptions:
    """What the worklist offers a human for one paper — **both** copies, not a choice.

    Published versions are preferred, but an accessible non-published version is valuable
    in a way that an inaccessible published version is not.

    So this is not either/or. The version of record and the best accessible copy are both
    surfaced and both labelled, and the human picks. Hiding the open preprint because it
    is not the version of record would withhold the only copy they can actually read;
    hiding the paywalled version of record would conceal what they ought to cite.

    When one copy is both, ``version_of_record`` and ``best_accessible`` are the same
    object and ``single_copy_suffices`` is True.
    """

    version_of_record: "AcquisitionTarget | None"
    best_accessible: "AcquisitionTarget | None"

    @property
    def single_copy_suffices(self) -> bool:
        """True when the version of record is itself accessible — the happy case."""
        vor, acc = self.version_of_record, self.best_accessible
        return vor is not None and acc is not None and vor.url == acc.url

    @property
    def recommended(self) -> "AcquisitionTarget | None":
        """The copy to try first: the version of record when it is accessible, else the
        accessible copy, else the version of record even though it is paywalled."""
        if self.single_copy_suffices:
            return self.version_of_record
        if self.best_accessible is not None and self.best_accessible.is_oa:
            return self.best_accessible
        return self.version_of_record or self.best_accessible

    @property
    def needs_version_warning(self) -> bool:
        """True when the accessible copy is **not** the version of record.

        The worklist must say so: extracting data from a preprint figure and attributing
        it to the published paper is a data-integrity problem, and figures get
        revised between preprint and publication.
        """
        acc = self.best_accessible
        return acc is not None and not acc.is_version_of_record_copy


def select_acquisition_options(work: RegistryWork) -> AcquisitionOptions:
    """Surface both the version of record and the best accessible copy."""
    vor = select_acquisition_target(work, prefer="version")
    accessible = select_acquisition_target(work, prefer="access")
    return AcquisitionOptions(version_of_record=vor, best_accessible=accessible)


def select_acquisition_target(
    work: RegistryWork, prefer: str = "version"
) -> AcquisitionTarget:
    """Choose one copy to fetch — **Case A** above.

    ``prefer="version"`` ranks ``publishedVersion`` above openness, which is what
    ``best_oa_location`` gets wrong: it picks the open copy even when that copy is a
    submitted preprint and a published version exists in the same record.

    ``prefer="access"`` ranks openness first, for the accessible-copy half of
    :class:`AcquisitionOptions`.
    """
    locations = [loc for loc in work.locations if loc is not None]

    if not locations:
        # Fall back to the top-level OA fields, then to nothing at all.
        if work.best_oa_pdf_url:
            return AcquisitionTarget(TIER_DIRECT_PDF, work.best_oa_pdf_url,
                                     work.best_oa_version, work.best_oa_license, None, True)
        if work.oa_url:
            return AcquisitionTarget(TIER_OA_LANDING, work.oa_url, work.best_oa_version,
                                     work.best_oa_license, None, bool(work.is_oa))
        doi_url = identifier_url(work.doi) if work.doi else None
        return AcquisitionTarget(TIER_CLOSED, doi_url, None, None, None, False)

    def sort_key(loc: Location) -> tuple[int, int, int]:
        version_rank = _version_rank(loc.version)
        openness_rank = 0 if loc.is_oa else 1
        directness_rank = 0 if loc.pdf_url else 1
        if prefer == "access":
            # Openness first: this is the copy the user can actually read today.
            return (openness_rank, version_rank, directness_rank)
        # Version first: this is the copy they ought to cite.
        return (version_rank, openness_rank, directness_rank)

    best = sorted(locations, key=sort_key)[0]

    if best.is_oa and best.pdf_url:
        tier = TIER_DIRECT_PDF
        url = best.pdf_url
    elif best.is_oa and best.landing_page_url:
        tier = TIER_OA_LANDING
        url = best.landing_page_url
    else:
        tier = TIER_CLOSED
        url = best.landing_page_url or (identifier_url(work.doi) if work.doi else None)

    return AcquisitionTarget(
        tier=tier,
        url=url,
        version=best.version,
        license=best.license,
        source_name=best.source_name,
        is_oa=bool(best.is_oa),
    )


@dataclass
class VorResolution:
    """Outcome of attempting to reach the version of record."""

    is_version_of_record: bool
    vor_doi: str | None = None
    preprint_doi: str | None = None
    vor_work: RegistryWork | None = None
    #: Why we ended up here — recorded so a flagged preprint is explicable later.
    note: str = ""

    @property
    def resolved(self) -> bool:
        """True when we started from a preprint and found its published counterpart."""
        return self.vor_doi is not None and self.preprint_doi is not None


def resolve_version_of_record(
    work: RegistryWork,
    registry: Registry | None = None,
    *,
    title_threshold: float = 0.93,
    limit: int = 5,
) -> VorResolution:
    """Resolve to the version of record where possible — **Case B** above.

    A non-preprint work *is* the version of record, so it returns immediately. For a
    preprint, search for a non-preprint work with an agreeing title.

    The title check is not optional: without it a title search would happily link two
    unrelated papers, and a wrong VoR link is worse than an unresolved preprint because
    it silently reattributes data.
    """
    if not work.is_preprint:
        return VorResolution(
            is_version_of_record=True,
            vor_doi=normalize_doi(work.doi),
            note="work is not a preprint",
        )

    preprint_doi = normalize_doi(work.doi)

    if registry is None or not work.title:
        return VorResolution(
            is_version_of_record=False,
            preprint_doi=preprint_doi,
            note="preprint; no registry available to search for a published version",
        )

    # Prefer a type-filtered search when the registry offers one; otherwise search
    # broadly and filter locally so this works with any Registry implementation.
    try:
        searcher = getattr(registry, "search_published_version", None)
        candidates = (
            searcher(work.title, limit=limit)
            if callable(searcher)
            else [c for c in registry.search_by_title(work.title, limit=limit)]
        )
    except (RegistryUnavailable, NotFound) as exc:
        return VorResolution(
            is_version_of_record=False,
            preprint_doi=preprint_doi,
            note=f"preprint; VoR search failed: {exc}",
        )

    best: tuple[RegistryWork, float] | None = None
    for cand in candidates:
        if cand.is_preprint:
            continue
        cand_doi = normalize_doi(cand.doi)
        if cand_doi is None or cand_doi == preprint_doi:
            continue
        score = title_similarity(work.title, cand.title)
        if score >= title_threshold and (best is None or score > best[1]):
            best = (cand, score)

    if best is None:
        return VorResolution(
            is_version_of_record=False,
            preprint_doi=preprint_doi,
            note="preprint; no published counterpart found",
        )

    vor, score = best
    return VorResolution(
        is_version_of_record=False,
        vor_doi=normalize_doi(vor.doi),
        preprint_doi=preprint_doi,
        vor_work=vor,
        note=f"preprint resolved to published version (title similarity {score:.3f})",
    )
