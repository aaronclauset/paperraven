"""The acquisition worklist — `corpus/worklist.csv`.

The worklist tells a person, paper by paper, which copies are easy to get, which are hard,
and which accessible copy is the wrong version. `paperraven fetch` downloads what a reference
manager politely can (open copies and publisher PDF links; most major publishers refuse any
program) and lists the rest on a click-list. The worklist remains the per-paper guide.

The tier logic lives in `versions.select_acquisition_options`, which takes a `RegistryWork`.
That type exists only during verification, while the worklist is produced at export time from
stored `PaperRecord`s. :func:`_work_from_record` adapts one to the other rather than
reimplementing tier selection against `PaperRecord`: two selection routines would drift.

**What the adapter loses.** `RegistryWork.locations` is not persisted, so the preference among
a record's several locations by version cannot be recomputed at export time.
`select_acquisition_target` falls back to the top-level OA fields, which `PaperRecord` does
carry, and still produces the right three tiers. The version-of-record *warning* does not
depend on the adapter at all: `_resolve_vor` runs during verification and
`Provenance.is_version_of_record` is already on disk. Full fidelity would need `locations`
persisted, or the acquisition options computed at verify time and stored; both are schema
changes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .corpus import Corpus, atomic_write_text
from .fetch.identify import Target, scan_folder
from .normalize import REGISTRY_ID_PREFIX, identifier_url
from .models import PaperRecord
from .registry.base import RegistryWork
from .versions import (
    TIER_CLOSED,
    TIER_DIRECT_PDF,
    TIER_OA_LANDING,
    AcquisitionOptions,
    select_acquisition_options,
)

#: The first four columns, **including the trailing spaces** in two of the headers: the shape
#: a hand-kept worklist has, kept so the tool's file reads the same. The extra columns below
#: are appended, never inserted, so anything reading the first four columns keeps working.
BASE_COLUMNS = ["Title ", "PDF/LINK", "PDF File name ", "DOI"]

EXTRA_COLUMNS = [
    "Tier",
    "Difficulty",
    "Version",
    "License",
    "OA status",
    "Version warning",
    "Already present",
]

TIER_ORDER = (TIER_DIRECT_PDF, TIER_OA_LANDING, TIER_CLOSED)

_NON_WORD = re.compile(r"[^A-Za-z0-9]+")


def _work_from_record(record: PaperRecord) -> RegistryWork:
    """Adapt a stored record back into the shape `versions.py` consumes.

    `locations` is deliberately empty — it was never persisted — which sends
    `select_acquisition_target` down its documented fallback path over the top-level OA
    fields. Those are exactly the fields `PaperRecord` keeps.
    """
    # A record's identifier is an `openalex:W…` id when there is no DOI; handed on as the
    # DOI, it would make `versions` build `https://doi.org/openalex:W…`.
    doiless = record.doi.startswith(REGISTRY_ID_PREFIX)
    return RegistryWork(
        doi=None if doiless else record.doi,
        registry_id=record.doi[len(REGISTRY_ID_PREFIX):] if doiless else None,
        title=record.title,
        source=record.provenance.source.value,
        authors=list(record.authors),
        year=record.year,
        venue=record.venue,
        type=record.type,
        is_oa=bool(record.oa_status and record.oa_status != "closed"),
        oa_status=record.oa_status,
        oa_url=record.oa_url,
        best_oa_pdf_url=record.oa_pdf_url,
        best_oa_version=record.oa_version,
        best_oa_license=record.oa_license,
        abstract=record.abstract,
    )


def _first_author_surname(record: PaperRecord) -> str:
    if not record.authors:
        return "Unknown"
    name = record.authors[0].strip()
    # "Ada Alpha" -> "Alpha"; "Alpha, Ada" -> "Alpha"; a single token stays as it is.
    if "," in name:
        surname = name.split(",", 1)[0]
    else:
        parts = name.split()
        surname = parts[-1] if parts else name
    return _NON_WORD.sub("", surname) or "Unknown"


def target_filename(record: PaperRecord) -> str:
    """`FirstAuthor_venue_Year`, the naming convention of the hand-built worklist.

    `paperraven fetch` saves what it fetches or collects under this name. A PDF the user
    downloads keeps whatever name the server gave it, since users do not rename files; it is
    recognised by its content (`fetch.identify`), so the name is a convenience, not a key.

    **Not unique on its own** — see :func:`assign_filenames`. Two papers by the same first
    author in the same journal in the same year produce the same string, which would mean
    one fetched PDF silently overwriting another. Callers building a worklist must use
    `assign_filenames`; this function is the base name it starts from.
    """
    author = _first_author_surname(record)
    venue = _NON_WORD.sub("", (record.venue or "")).lower() or "unknownvenue"
    year = record.year or 0
    return f"{author}_{venue}_{year}"


def assign_filenames(records: list[PaperRecord]) -> dict[str, str]:
    """Map DOI -> target filename, disambiguating collisions.

    `Surname_venue_Year` collides whenever one author publishes twice in a journal in a
    year, which is ordinary rather than exotic. Two entries sharing a filename would mean
    `paperraven fetch` saving the second paper over the first. (Whether a PDF is present is
    decided by its content, never its name, so a collision cannot mark the wrong paper
    present.)

    On collision, **every** member of the colliding group gets a short DOI-derived suffix,
    rather than letting one member keep the bare name. Giving one row the clean name would
    make which row wins depend on iteration order.

    Caveat, stated because idempotent resumption depends on it: a name is stable while a
    group's membership is stable, but the round that turns a unique name into a colliding
    pair *does* rename the existing file's target. The worklist then shows both rows as
    not-yet-present, which is visible and recoverable; silently overwriting a PDF is not.
    """
    base: dict[str, str] = {r.doi: target_filename(r) for r in records}
    # Compared case-folded: `McDonald_…` and `MCDONALD_…` are one file on the default macOS
    # and Windows filesystems, so the second download would replace the first.
    counts: dict[str, int] = {}
    for name in base.values():
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1

    out: dict[str, str] = {}
    for doi, name in base.items():
        if counts[name.casefold()] > 1:
            token = hashlib.sha1(doi.encode("utf-8")).hexdigest()[:6]
            out[doi] = f"{name}_{token}"
        else:
            out[doi] = name
    return out


@dataclass
class WorklistRow:
    """One paper in the worklist: how to get it, and whether a PDF of it is already present."""

    record: PaperRecord
    options: AcquisitionOptions
    already_present: bool = False
    #: Assigned by `assign_filenames`, which is corpus-aware; `target_filename` alone
    #: cannot see collisions.
    filename: str = ""

    @property
    def tier(self) -> int:
        rec = self.options.recommended
        return rec.tier if rec is not None else TIER_CLOSED

    @property
    def url(self) -> str | None:
        rec = self.options.recommended
        return (rec.url if rec is not None else None) or identifier_url(self.record.doi)

    @property
    def difficulty(self) -> str:
        rec = self.options.recommended
        return rec.tier_label if rec is not None else "closed"

    @property
    def version_warning(self) -> str:
        """Why the accessible copy may be the wrong copy.

        Two independent problems, and both matter to data integrity:
        the *work* may be a preprint rather than the version of record, and the *copy*
        offered may be a submitted version of a published work.
        """
        notes = []
        # `is_version_of_record` is deliberately **tri-state** (`bool | None`): `None`
        # means resolution never ran, which is the case for every ordinary article, since
        # `_resolve_vor` only fires on preprints. Testing `if not ...` would collapse
        # "not checked" into "confirmed not the version of record" and warn on *every*
        # row — a warning that fires on 100% of rows trains the user to ignore it, which
        # costs exactly the cases it exists for.
        if self.record.provenance.is_version_of_record is False:
            vor = self.record.provenance.version_of_record_doi
            notes.append(
                f"not the version of record; VoR is {vor}" if vor
                else "not the version of record, and none was found"
            )
        # `AcquisitionOptions.needs_version_warning` answers "is the accessible copy the
        # published version", and returns True when the version is merely *unknown* — the
        # same unknown-vs-bad conflation one layer down. Two guards before reporting it:
        #
        #   * there must actually be an open copy. For a closed paper the user is going to
        #     the paywall regardless, so "the accessible copy is the wrong version" names
        #     a copy that does not exist.
        #   * the version must be *known*. An unlabelled version is not evidence of a
        #     preprint; most closed records carry no version string at all.
        #
        # Without these the warning would fire on nearly every row, and a warning that
        # always fires is one the user stops reading.
        acc = self.options.best_accessible
        if (
            self.options.needs_version_warning
            and acc is not None
            and acc.is_oa
            and acc.version
        ):
            notes.append(
                f"accessible copy is {acc.version}, not the published version"
            )
        return "; ".join(notes)

    def as_base_and_extra(self) -> list[str]:
        opts = self.options
        rec = opts.recommended
        return [
            self.record.title,
            self.url or "",
            self.filename or target_filename(self.record),
            self.record.doi,
            str(self.tier),
            self.difficulty,
            (rec.version if rec else None) or "",
            (rec.license if rec else None) or "",
            self.record.oa_status or "",
            self.version_warning,
            "yes" if self.already_present else "",
        ]


def _present_ids(pdf_dir: Path | None, records: list[PaperRecord]) -> set[str]:
    """The records a PDF in `pdf_dir` is identified as, by its first page.

    Re-running marks what is already there, so a user can stop and resume across days.
    Identified by content, not file name, because users do not rename downloaded files. The
    same routine serves `paperraven fetch`'s resume and `--collect`.
    """
    if pdf_dir is None:
        return set()
    targets = [Target(r.doi, doi=r.doi if r.doi.startswith("10.") else None, title=r.title,
                      year=r.year) for r in records]
    return set(scan_folder(pdf_dir, targets))


def build_worklist(
    corpus: Corpus, *, pdf_dir: str | Path | None = None
) -> list[WorklistRow]:
    """Build the worklist from the greenlist, sorted easiest-first.

    **Greenlist only.** The worklist is the acquisition queue for the deliverable; asking
    a human to chase down papers the judge excluded, or has not judged yet, would waste
    exactly the effort this list exists to direct. Uncertain records are exported
    separately and can be promoted by a later round.
    """
    records = list(corpus.greenlist.values())
    present = _present_ids(Path(pdf_dir) if pdf_dir else None, records)
    names = assign_filenames(records)

    rows = [
        WorklistRow(
            record=record,
            options=select_acquisition_options(_work_from_record(record)),
            filename=names[record.doi],
        )
        for record in records
    ]
    for row in rows:
        row.already_present = row.record.doi in present

    # Easy first, so the user clears the bulk quickly and then decides whether the hard
    # tail is worth their time. Papers already on disk sink to the bottom: they need
    # no action. Ties break on title for a stable, reviewable file.
    rows.sort(key=lambda r: (r.already_present, TIER_ORDER.index(r.tier)
                             if r.tier in TIER_ORDER else len(TIER_ORDER),
                             r.record.title.lower()))
    return rows


def render_worklist_csv(rows: list[WorklistRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(BASE_COLUMNS + EXTRA_COLUMNS)
    for row in rows:
        writer.writerow(row.as_base_and_extra())
    return buf.getvalue()


def write_worklist(
    corpus: Corpus, dest: str | Path, *, pdf_dir: str | Path | None = None
) -> tuple[Path, list[WorklistRow]]:
    """Write `worklist.csv` and return its path with the rows."""
    rows = build_worklist(corpus, pdf_dir=pdf_dir)
    path = Path(dest)
    atomic_write_text(path, render_worklist_csv(rows), newline="")   # csv writes its own \r\n
    return path, rows


def worklist_summary(rows: list[WorklistRow]) -> str:
    """A one-line account of the acquisition burden, by tier."""
    if not rows:
        return "worklist: nothing to acquire (greenlist is empty)"
    counts = {tier: 0 for tier in TIER_ORDER}
    for row in rows:
        counts[row.tier] = counts.get(row.tier, 0) + 1
    present = sum(1 for r in rows if r.already_present)
    warned = sum(1 for r in rows if r.version_warning)
    return (
        f"worklist: {len(rows)} papers — "
        f"{counts.get(TIER_DIRECT_PDF, 0)} direct PDF, "
        f"{counts.get(TIER_OA_LANDING, 0)} OA landing page, "
        f"{counts.get(TIER_CLOSED, 0)} closed"
        + (f"; {present} already on disk" if present else "")
        + (f"; {warned} carry a version warning" if warned else "")
    )
