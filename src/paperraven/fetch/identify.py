"""Which paper is this PDF? From its first page, never its file name.

Used by resume, `--collect` and `export --pdf-dir`. A DOI printed on the page decides it; failing
that, the page's title agrees with exactly one entry's. Several entries matching is
**ambiguous** and is not guessed. A file that is not a PDF (a saved paywall page) or matches
nothing is **unidentified**.

The cache holds what page 1 *says* (its DOI and title candidates), keyed by the file's SHA-256,
never a verdict: every run matches against the entries it is given, so a `.bib` that gained or
lost entries since the file was read is matched correctly.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..normalize import normalize_doi, title_similarity, titles_conflict
from ..seeds import extract_seed_signals

IDENTIFIED, AMBIGUOUS, UNIDENTIFIED = "identified", "ambiguous", "unidentified"

#: A page title must agree with an entry's at least this well: `matching.title_high`'s default.
#: There is no looser fallback: title words found anywhere on page 1 misidentify too many files.
TITLE_AGREEMENT = 0.85
#: When page 1's title agrees with several entries, the one it agrees with best by at least this
#: much is the file's: `matching.title_fallback_margin`'s default, the verifier's clear-best
#: margin. A paper's own copy can also agree closely with an entry whose title contains its words.
TITLE_MARGIN = 0.05


@dataclass(frozen=True)
class Target:
    """What a PDF is matched against: an entry's identifier, DOI, title and year."""

    identifier: str
    doi: str | None = None
    title: str | None = None
    year: int | None = None
    #: The DOIs of the entry's other versions (preprint, version of record), when known.
    other_dois: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Identification:
    status: str
    identifier: str | None = None
    method: str | None = None
    detail: str = ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: An email address. A first page often lists its authors' addresses, and the lines kept from
#: it go into the fetch manifest beside the PDFs, so none is kept.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _without_emails(page: dict[str, Any]) -> dict[str, Any]:
    """The page with every email address removed from its kept lines."""
    return {**page, "titles": [" ".join(_EMAIL.sub("", x).split()) for x in page["titles"]]}


def identify(path: str | Path, targets: list[Target],
             cache: dict[str, Any] | None = None, *, strict: bool = False) -> Identification:
    p = Path(path)
    digest = sha256(p)
    page = cache.get(digest) if cache is not None else None
    if not _is_page(page):              # absent, or not a page record
        page = read_page(p)
    page = _without_emails(page)        # a cached page is cleaned too
    if cache is not None:
        cache[digest] = page
    return match(page, targets, strict=strict)


def read_page(p: Path) -> dict[str, Any]:
    """What page 1 says: its DOI and title candidates, or why it could not be read."""
    with open(p, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            return {"doi": None, "titles": [], "years": [],
                    "error": "not a PDF (an HTML page saved as .pdf?)"}
    cand = extract_seed_signals(p, "front_matter")
    if cand.error:
        return {"doi": None, "titles": [], "error": f"unreadable: {cand.error}", "years": []}
    return {"doi": cand.doi, "titles": [x for x in (cand.title, *cand.title_candidates) if x],
            "error": None,
            "years": sorted({int(y) for y in re.findall(r"\b(1[89]\d\d|20\d\d)\b", cand.text)})}


def _is_page(value: Any) -> bool:
    """A cached page entry this version can use: the keys, with the right types (a hand-edited
    manifest is read again, not a crash)."""
    if not (isinstance(value, dict) and {"doi", "titles", "error", "years"} <= value.keys()):
        return False
    text_or_none = lambda v: v is None or isinstance(v, str)
    return (text_or_none(value["doi"]) and text_or_none(value["error"])
            and isinstance(value["titles"], list) and all(isinstance(x, str) for x in value["titles"])
            and isinstance(value["years"], list)
            and all(isinstance(y, int) and not isinstance(y, bool) for y in value["years"]))


def match(page: dict[str, Any], targets: list[Target], *, strict: bool = False
          ) -> Identification:
    """Which one entry, if any, page 1 belongs to: its DOI, else its title.

    **Strict** is for folders, where unrelated PDFs appear (a downloads folder; the
    destination on resume). A title match there is refused when page 1
    shows a DOI that is no entry's, when the titles conflict (a different part or edition, a
    correction or reply to the paper), or when page 1 shows years and none lies in
    [entry year - 3, entry year + 1] (preprints come earlier; a reprint fails, which is the safe
    direction). A file the tool fetched from the entry's own metadata is matched normally.
    """
    if page["error"]:
        return Identification(UNIDENTIFIED, detail=page["error"])
    if page["doi"]:
        hits = [t for t in targets
                if page["doi"] in {normalize_doi(t.doi), *map(normalize_doi, t.other_dois)} - {None}]
        if len(hits) == 1:
            return Identification(IDENTIFIED, hits[0].identifier, "doi")
        # A DOI cut short by a line break ("10.1603/0013-") that begins an entry's DOI cannot
        # contradict that entry, so it vetoes nothing.
        truncated = any((t.doi or "").startswith(page["doi"]) for t in targets)
        if strict and not hits and not truncated:
            return Identification(UNIDENTIFIED, detail=f"page 1 shows the DOI {page['doi']}, "
                                                       f"which is no entry's")

    titles = page["titles"]
    years = page.get("years") or []

    def agrees(t: Target) -> bool:
        close = [x for x in titles if title_similarity(t.title, x) >= TITLE_AGREEMENT]
        if not strict:
            return bool(close)
        if not any(not titles_conflict(t.title, x) for x in close):
            return False
        return not (years and t.year and not any(t.year - 3 <= y <= t.year + 1
                                                 for y in years))

    hits = [t for t in targets if t.title and agrees(t)]
    if len(hits) > 1:
        score = lambda t: max(title_similarity(t.title, x) for x in titles)
        hits.sort(key=score, reverse=True)
        if score(hits[0]) - score(hits[1]) >= TITLE_MARGIN:
            hits = hits[:1]
    if len(hits) == 1:
        return Identification(IDENTIFIED, hits[0].identifier, "title")
    if len(hits) > 1:
        return Identification(AMBIGUOUS, detail=f"matches {len(hits)} entries by title")
    return Identification(UNIDENTIFIED, detail="no DOI or title on page 1 matches an entry")


def identify_folder(folder: Path, targets: list[Target], cache: dict[str, Any] | None = None
                    ) -> dict[str, list[tuple[Path, Identification]]]:
    """Every PDF in `folder` identified as each entry, in name order (not recursive; hidden
    files skipped). `--collect` looks at them all for a published version."""
    found: dict[str, list[tuple[Path, Identification]]] = {}
    if not folder.is_dir():
        return found
    # `.PDF` too; a directory or an unreadable file is skipped, never fatal.
    for pdf in sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf"):
        if pdf.name.startswith(".") or not pdf.is_file():
            continue
        try:
            r = identify(pdf, targets, cache=cache, strict=True)
        except OSError:
            continue
        if r.status == IDENTIFIED:
            found.setdefault(r.identifier, []).append((pdf, r))
    return found


def scan_folder(folder: Path, targets: list[Target],
                cache: dict[str, Any] | None = None) -> dict[str, Path]:
    """The first PDF in `folder` identified as each entry, by name.

    Used by `fetch` (resume and `--collect`) and by `export --pdf-dir`.
    """
    return {ident: files[0][0] for ident, files in identify_folder(folder, targets, cache).items()}
