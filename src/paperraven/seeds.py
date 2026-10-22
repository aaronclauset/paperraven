"""Seed loading — PDFs on disk to verified records.

Seeds are **PDF files**, not DOIs, to avoid publisher paywalls. Resolved seeds enter the
corpus and the redlist; a headless path seeds from DOIs (`init --seed-doi`).

## Why three signals

Neither PDF signal is reliable alone. `/Title` metadata is often missing or a build
artifact (LaTeX-built preprints rarely carry a usable one), and a DOI is not always on
page 1; the two fail on different files. So the cascade tries both rather than picking
one, and then a third: :func:`title_candidates_from_text` offers the first plausible lines
of page 1 (boilerplate skipped, wrapped pairs joined) to the title-fallback cascade, whose
0.93 threshold and clear-best rule reject the wrong ones. With the version-of-record
tie-break in `verify.py` this resolves most seeds. What remains, typically a preprint
whose title changed before publication, is the agent's, through the `seed` command.

## Division of labour

Python extracts mechanically — page text, a DOI regex, PDF metadata, title *candidates*.
Deciding which line is the title on a PDF where every candidate fails is judgement, and
belongs to the agent. :func:`write_seed_digest` hands over exactly those seeds, and
`paperraven seed` is how the agent's answer gets back in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .config import Config
from .corpus import Corpus
from .models import (
    Bibliographic,
    DiscoveredBy,
    LocalRecord,
    PaperProposal,
    Provenance,
    RegistrySource,
    Relevance,
    Verdict,
    VerificationPath,
)
from .normalize import normalize_doi, normalize_title, title_similarity, titles_conflict
from .registry.base import NotFound, RegistryUnavailable
from .verify import KnownState, Verifier

#: A DOI as it appears in running text. Trailing punctuation is excluded from the class
#: rather than stripped afterwards, since `normalize_doi` handles the residue.
#: `) ; ] < >` are legal inside a DOI (SICI and ESA forms); `normalize_doi` trims what
#: surrounding prose adds, by bracket balance.
DOI_IN_TEXT = re.compile(r'10\.\d{4,9}/[^\s",]+')

#: Below this many characters, a `/Title` metadata value is more likely a filename stub or
#: a template placeholder than a real title.
MIN_METADATA_TITLE_CHARS = 15

#: `/Title` metadata is frequently a build artifact rather than a title, such as a LaTeX
#: source filename like ``manuscript_final_v2.tex``, which sails past a bare length check.
#: A real title has spaces and no file extension.
_FILENAME_LIKE = re.compile(
    r"\.(tex|pdf|docx?|indd|qxd|xml|dvi|ps|eps)$|^[\w\-.]+$", re.IGNORECASE
)

# NO year extraction. A year scraped from page-1 text is often wrong, and wrong
# specifically on the OLDEST papers: it picks up download stamps and citation years, and
# old scans carry more of both. That is a systematically biased signal aimed at exactly
# the cases where corroboration matters most, and a *wrong* corroboration is worse than an
# absent one. The front-matter DOI path does the real work without it.


#: Page-1 lines that are never a title: arXiv stamps, journal headers, DOIs, URLs, dates,
#: bare numbers, copyright and receipt lines.
_BOILERPLATE = re.compile(
    r"^(arXiv:|PHYSICAL REVIEW|Phys\.?\s*Rev\.?|PNAS|Proc(eedings|\.)|Vol(ume|\.)|No\.|pp\.|doi:|"
    r"https?://|www\.|\d[\d\s,.-]*$|Downloaded|Copyright|©|Received|Accepted|Published|"
    r"Available online|Journal of|Nature|Science\b|ISSN|Open Access|Article|Original)",
    re.I,
)
#: A title has more than a couple of words and fewer than a paragraph's worth.
_TITLE_WORDS = (3, 25)
#: How many leading text lines to consider. The title is on page 1 near the top or it is
#: not going to be found mechanically.
_CANDIDATE_LINES = 6
_MAX_CANDIDATES = 6


def title_candidates_from_text(text: str) -> list[str]:
    """Plausible title strings from the top of page 1, for the fallback cascade to test.

    Each surviving line alone, and each line joined with the next (titles wrap: a title
    may extract as ``"Seasonal variation in soil"`` / ``"microbial communities"``). Order is the order on the page, so the real title is usually first.
    This is not title *detection*: the verifier's threshold and clear-best rule do the
    deciding, and this only has to offer the right string among a handful.
    """
    lines = [" ".join(l.split()) for l in text.splitlines() if l.strip()][:_CANDIDATE_LINES]
    lines = [l for l in lines if not _BOILERPLATE.search(l)]
    out: list[str] = []
    lo, hi = _TITLE_WORDS
    for i, line in enumerate(lines):
        if lo <= len(line.split()) <= hi and line not in out:
            out.append(line)
        if i + 1 < len(lines):
            joined = f"{line} {lines[i + 1]}"
            if lo <= len(joined.split()) <= hi + 5 and joined not in out:
                out.append(joined)
    return out[:_MAX_CANDIDATES]


def looks_like_a_title(value: str | None) -> bool:
    """Whether a `/Title` metadata value is plausibly a real title.

    Rejects build artifacts: anything with a document file extension, and anything with no
    whitespace at all (a single token is a filename or an ID, not a title).
    """
    if not value:
        return False
    v = " ".join(value.split())
    if len(v) < MIN_METADATA_TITLE_CHARS:
        return False
    return not _FILENAME_LIKE.search(v)


@dataclass
class SeedCandidate:
    """What mechanical extraction could recover from one seed PDF."""

    path: Path
    doi: str | None = None
    title: str | None = None
    #: Page-1 text (or the whole document when `read_depth` is `full`), kept so the agent
    #: can read it when neither signal was usable.
    text: str = ""
    #: The third signal: title strings mined from the page text for the cascade to try.
    title_candidates: list[str] = field(default_factory=list)
    pages: int = 0
    error: str | None = None
    #: The file named in the config does not exist. Not an extraction failure: the
    #: seed is reported as not found, and kept out of the digest the agent is sent to.
    missing: bool = False
    #: Where a missing `files` entry was looked for, in order.
    looked_in: list[str] = field(default_factory=list)
    #: The config entry as written, for messages about a missing file.
    entry: str | None = None

    @property
    def stem(self) -> str:
        return self.path.stem


def extract_seed_signals(path: str | Path, read_depth: str = "front_matter") -> SeedCandidate:
    """Pull a DOI and a title out of one PDF, without judgement.

    ``read_depth`` follows the user's choice: ``front_matter`` reads page 1 only,
    which is much cheaper and where both signals live in practice; ``full`` reads
    everything.
    """
    p = Path(path)
    cand = SeedCandidate(path=p)
    try:
        import pypdf
    except ImportError:  # pragma: no cover - pypdf is a declared dependency
        cand.error = "pypdf not installed"
        return cand

    # pypdf logs "Ignoring wrong pointing object" per damaged xref on real PDFs, which can
    # bury `init`'s report under dozens of lines. Warnings about a file we go on to read
    # successfully are noise.
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    try:
        reader = pypdf.PdfReader(str(p))
        cand.pages = len(reader.pages)
        page_range = reader.pages if read_depth == "full" else reader.pages[:1]
        cand.text = "\n".join((pg.extract_text() or "") for pg in page_range)

        meta_title = ""
        try:
            meta_title = (reader.metadata.title or "") if reader.metadata else ""
        except Exception:  # noqa: BLE001 - malformed metadata must not sink a seed
            meta_title = ""
        if looks_like_a_title(meta_title):
            cand.title = " ".join(meta_title.split())

        m = DOI_IN_TEXT.search(cand.text)
        if m:
            cand.doi = normalize_doi(m.group(0))
        cand.title_candidates = title_candidates_from_text(cand.text)

    except Exception as exc:  # noqa: BLE001 - one unreadable PDF must not sink the run
        cand.error = f"{type(exc).__name__}: {exc}"
    return cand


def load_seed_candidates(config: Config, base_dir: Path | None = None) -> list[SeedCandidate]:
    """Enumerate and extract every configured seed PDF, each once.

    `files` are **extra** seeds, in addition to every PDF in
    `pdf_dir`. A relative entry is looked for in `pdf_dir` first, then in `base_dir`; an
    absolute one is used as given. A file named twice, or both listed and in `pdf_dir`, is
    read once. A file found nowhere is returned as ``missing``, not as unreadable, and so is a
    `pdf_dir` that does not exist.
    """
    paths, missing = seed_paths(config, base_dir)
    return [extract_seed_signals(q, config.seeds.read_depth) for q in paths] + missing


def seed_paths(config: Config, base_dir: Path | None = None
               ) -> tuple[list[Path], list[SeedCandidate]]:
    """The configured seed files that exist, each once, and the entries found nowhere, as
    `load_seed_candidates` finds them; nothing is read."""
    base = base_dir or Path.cwd()
    pdf_dir = None
    paths: list[Path] = []
    missing: list[SeedCandidate] = []
    if config.seeds.pdf_dir:
        pdf_dir = Path(config.seeds.pdf_dir)
        if not pdf_dir.is_absolute():
            pdf_dir = base / pdf_dir
        if pdf_dir.is_dir():
            paths.extend(sorted(pdf_dir.glob("*.pdf")))
        else:
            missing.append(SeedCandidate(path=pdf_dir, missing=True,
                                         entry=str(config.seeds.pdf_dir),
                                         looked_in=[str(pdf_dir.parent)]))
    for f in config.seeds.files:
        if any(m.entry == str(f) for m in missing):
            continue
        p = Path(f)
        tries = [p] if p.is_absolute() else [*([pdf_dir / p] if pdf_dir else []), base / p]
        found = next((t for t in tries if t.is_file()), None)
        if found is None:
            missing.append(SeedCandidate(path=tries[-1], missing=True, entry=str(f),
                                         looked_in=[str(t.parent) for t in tries]))
        else:
            paths.append(found)
    seen: set[object] = set()
    unique = []
    for q in paths:
        key = _file_identity(q)
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique, missing


def _file_identity(path: Path) -> object:
    """The same file under two names (a case-insensitive disk, a link) has one identity."""
    st = path.stat()
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    return os.path.normcase(str(path.resolve()))      # a filesystem without inode numbers


@dataclass
class SeedResult:
    """What seeding achieved, reported rather than summarised away."""

    resolved: list[str] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    needs_agent: list[str] = field(default_factory=list)
    #: Config `files` entries that exist nowhere, as written. Reported, not fatal.
    missing: list[str] = field(default_factory=list)
    #: Where each missing entry was looked for, for the message that names it.
    missing_looked_in: dict[str, list[str]] = field(default_factory=dict)
    #: Could not be checked because a registry was unavailable: kept locally,
    #: **not** redlisted, and retryable with the `seed` command.
    unchecked: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"seeds: {len(self.resolved)} resolved to DOIs, "
            f"{len(self.local_only)} kept locally without a DOI, "
            f"{len(self.needs_agent)} need the agent to read them, "
            f"{len(self.failed)} unreadable"
            + (f", {len(self.missing)} not found" if self.missing else "")
            + (f", {len(self.unchecked)} not checked (a registry was unavailable; retry them "
               f"with `seed`)" if self.unchecked else "")
        )


#: A resolved seed enters the **greenlist** directly, on the user's authority rather than
#: the judge's.
#:
#: Suppose a user supplies 10 papers that *are* the entire relevant literature on a niche
#: topic. With seeds held in the uncertain pool, the tool would open by reporting a corpus
#: of **0** while holding the complete answer, and the greenlist export (the deliverable)
#: would omit the user's own papers. Worse, the judge is given seeds as worked examples of
#: "relevant here"; asking that same judge whether they are relevant is circular, and lets
#: the judge *exclude* a paper the user asserted was relevant.
#:
#: So the verdict is `include` and `decided_by` records **"user (seed)"**, never the judge.
#: Provenance stays legible: `discovered_by: seed` keeps them out of any count of what the
#: tool found (a seeded paper was handed to it, not found by it), and seeds never enter the
#: per-round discovery counts.
SEED_DECIDED_BY = "user (seed)"


def seed_corpus(
    corpus: Corpus,
    verifier: Verifier,
    candidates: Sequence[SeedCandidate],
    *,
    round_index: int = 0,
) -> SeedResult:
    """Resolve seed candidates into the corpus and redlist.

    Seeds **always enter the redlist**, resolved or not — otherwise the model
    spends proposals re-suggesting papers the user personally supplied, which would look
    broken. Resolved seeds are tagged ``discovered_by: "seed"`` so no count of what the
    tool found includes them.

    A seed whose title resolves to no DOI is **kept** as a :class:`LocalRecord`,
    with a local id and a redlist entry by title. It cannot be DOI-matched, so scoring
    must exclude it.
    """
    result = SeedResult()

    for cand in candidates:
        if cand.missing:
            result.missing.append(cand.entry or str(cand.path))
            result.missing_looked_in[cand.entry or str(cand.path)] = list(cand.looked_in)
            continue
        if cand.error:
            result.failed.append((cand.stem, cand.error))
            continue

        # --- DOI extracted from the paper's own front matter: trust it. --------------
        # A seed is NOT an untrusted model claim. The user handed us the PDF and the DOI
        # came off its own first page, so there is no hallucination to guard against and
        # nothing to corroborate a title against. Routing seeds through the proposal
        # cascade would compare the registry's title against `/Title` metadata that is
        # often a build artifact or absent, and reject a correct match.
        if cand.doi:
            try:
                record = _fetch_seed_record(verifier, cand, round_index)
            except RegistryUnavailable:
                _register_local(corpus, cand, round_index, title=cand.title, redlist=False)
                result.unchecked.append(cand.stem)
                continue
            if record is not None:
                _admit_seed(corpus, record)
                result.resolved.append(record.doi)
                continue
            # The DOI did not resolve — it may have been a cited paper's DOI picked up
            # from the same page. Fall through to the title path.

        # --- Titles: metadata first, then candidates mined from the page text. This is
        # genuinely a search, so every string goes through the fallback cascade, whose
        # threshold and clear-best rule reject the wrong ones.
        tried: list[str] = []
        unchecked = False
        for title in ([cand.title] if cand.title else []) + list(cand.title_candidates):
            if title in tried:
                continue
            tried.append(title)
            outcome = verifier.verify(
                PaperProposal(
                    title=title,
                    doi=None,
                    explanation=f"Seed paper supplied by the user: {cand.path.name}",
                    proposed_in_round=round_index,
                ),
                round_index=round_index, known=corpus.known_state(),
                discovered_by=DiscoveredBy.SEED,
            )
            if outcome.accepted:
                _admit_seed(corpus, outcome.record)
                result.resolved.append(outcome.record.doi)
                break
            if outcome.rejection.reason.is_retryable:
                unchecked = True
                break
        else:
            if cand.title:
                # A real (metadata) title that resolved to nothing: kept locally, redlisted
                # under that title.
                _register_local(corpus, cand, round_index, title=cand.title)
                result.local_only.append(cand.stem)
            else:
                # No title at all: the agent must read it. Registered with **no title**:
                # a filename is not the shape of anything the redlist is compared
                # against, and the record exists for the digest and the `seed` command,
                # not for matching. It enters the redlist when it resolves.
                _register_local(corpus, cand, round_index, title=None)
                result.needs_agent.append(cand.stem)
        if unchecked:
            _register_local(corpus, cand, round_index, title=cand.title, redlist=False)
            result.unchecked.append(cand.stem)

    return result


def _page_agrees(cand: SeedCandidate, registry_title: str | None, floor: float) -> bool:
    """Does page 1 name the paper the DOI resolved to?

    The DOI first on a comment's, reply's or erratum's page is often the paper it responds
    to. So the DOI is trusted only if the registry's title agrees, loosely (`title_moderate`,
    since extraction is imperfect), with one of the page's own title lines, and that line is
    not a response to it ("Comment on …"). A page with no title text cannot contradict the
    DOI, which is then trusted.
    """
    lines = [t for t in ([cand.title] if cand.title else []) + list(cand.title_candidates) if t]
    if not lines or not registry_title:
        return True
    return any(title_similarity(registry_title, t) >= floor
               and not titles_conflict(registry_title, t) for t in lines)


def _fetch_seed_record(verifier: Verifier, cand: SeedCandidate, round_index: int, *,
                       check_page: bool = True):
    """Fetch a seed's registry record by its own DOI, bypassing title agreement.

    Existence is still verified — a DOI that resolves to nothing returns ``None`` so the
    caller can fall back to the title path. What is *not* required is that the PDF's
    extracted title agree with the registry's, because on a user-supplied paper that
    comparison tests our text extraction rather than the paper's identity.
    """
    from .models import PaperRecord

    try:
        work, source = verifier._fetch(cand.doi)  # noqa: SLF001 - same package
    except NotFound:
        return None        # perhaps a cited paper's DOI picked up from the page: try the title
    if not verifier._type_in_scope(work):  # noqa: SLF001
        return None
    # A DOI lifted off the page must agree with the page. Not for the `seed`
    # command, where the agent read the paper and names the DOI itself (`check_page=False`).
    if check_page and not _page_agrees(cand, work.title,
                                       verifier.config.matching.title_moderate):
        return None        # the page is about another paper; try the title path
    doi = normalize_doi(work.doi) or cand.doi
    return PaperRecord(
        doi=doi,
        title=work.title or cand.title or cand.stem.replace("_", " "),
        authors=work.authors,
        year=work.year,
        venue=work.venue,
        type=work.type,
        oa_status=work.oa_status,
        oa_url=work.oa_url,
        oa_pdf_url=work.best_oa_pdf_url,
        oa_version=work.best_oa_version,
        oa_license=work.best_oa_license,
        cited_by_count=work.cited_by_count,
        openalex_id=work.registry_id,
        references=list(work.references),
        abstract=work.abstract,
        abstract_source=source.value if work.abstract else None,
        bibliographic=Bibliographic(**work.bibliographic),
        provenance=Provenance(
            discovered_by=DiscoveredBy.SEED,
            discovered_round=round_index,
            source=source,
            verification_path=VerificationPath.DOI_EXACT,
            match_condition="seed: DOI from the paper's own front matter",
        ),
    )


def _admit_seed(corpus: Corpus, record) -> None:
    """File a resolved seed into the greenlist on the user's authority (`SEED_DECIDED_BY`).

    Kept in one place so every seeding path — front-matter DOI, title fallback, and the
    headless DOI list — admits seeds identically.
    """
    record.relevance.supersede()        # a found paper's verdicts survive as history
    record.relevance = Relevance(
        agent_verdict=Verdict.INCLUDE,
        agent_reason="Supplied by the user as a seed paper.",
        decided_by=SEED_DECIDED_BY,
        history=record.relevance.history,
    )
    corpus.add_record(record, verdict=Verdict.INCLUDE)
    corpus.rejudge_pending.discard(record.doi)          # never the judge's again


def _register_local(corpus: Corpus, cand: SeedCandidate, round_index: int,
                    *, title: str | None, redlist: bool = True) -> None:
    """Keep an unresolved seed. With a title, it is redlisted under it; without one, it is
    kept for the digest and the `seed` command only (never under a filename)."""
    corpus.add_local_record(LocalRecord(
        local_id=f"seed:{cand.stem}",
        title=title,
        source_path=str(cand.path),
        provenance=Provenance(
            discovered_by=DiscoveredBy.SEED,
            discovered_round=round_index,
            source=RegistrySource.OPENALEX,
            verification_path=VerificationPath.TITLE_FALLBACK,
        ),
    ), redlist=redlist)


def seed_from_dois(
    corpus: Corpus,
    verifier: Verifier,
    dois: Iterable[str],
    *,
    round_index: int = 0,
) -> SeedResult:
    """Seed directly from DOIs — the **headless** path.

    A re-run from a known list of DOIs runs without a human, so it cannot use the PDF
    path. Kept separate rather than folded in, because the
    production path is deliberately PDF-based and this one must not become the default.
    """
    result = SeedResult()
    for raw in dois:
        doi = normalize_doi(raw)
        if doi is None:
            result.failed.append((str(raw), "malformed DOI"))
            continue

        # Same trust path as a front-matter DOI, and for the same reason: the DOI *is* the
        # assertion, and there is no title to corroborate it against.
        #
        # This must NOT go through `verifier.verify`. That path always evaluates title
        # agreement, so passing the DOI as a placeholder title would compare a DOI string
        # to a real title, score ~0, and reject every seed as `doi_title_mismatch` —
        # silently, because each failure looks like an ordinary `local_only` outcome.
        # Seeded papers would then be counted as discoveries or misses.
        try:
            record = _fetch_seed_record(
                verifier, SeedCandidate(path=Path(doi), doi=doi), round_index)
        except RegistryUnavailable:
            result.unchecked.append(doi)         # not checked, so not redlisted
            continue
        if record is not None:
            _admit_seed(corpus, record)
            result.resolved.append(record.doi)
        else:
            # Did not resolve, or out of scope. Redlisted so it is not re-proposed, but
            # not admitted: existence was genuinely not established.
            corpus.redlist_dois.add(doi)
            result.local_only.append(doi)
    return result


def write_seed_digest(candidates: Sequence[SeedCandidate], path: str | Path,
                      *, needs_agent: Sequence[str]) -> Path:
    """Write the extracted text of the seeds that need the agent, and say what to do.

    Driven by the **same list `init` counted**, so every seed counted as needing the agent
    is in the file the agent is sent to, and it names the command that finishes the job.
    """
    from .corpus import atomic_write_text
    from .install import python_command

    wanted = set(needs_agent)
    lines = [
        "# Seed papers needing a title",
        "",
        "Mechanical extraction could not resolve these files: no usable DOI, and no title "
        "candidate from page 1 matched a registry record. Read the text below, identify "
        "each paper's title (and DOI, if you can find one), then for each run:",
        "",
        f'    {python_command()} -m paperraven --run-dir <run> seed --file <name> --title "<title>" [--doi <doi>]',
        "",
        "**If the PDF is a preprint, the title on its face may not be the published "
        "one.** arXiv and similar versions are frequently retitled before publication -- "
        "a dropped or added word is enough to make a registry title search return "
        "nothing. If the title as printed does not resolve, look the paper up and try the "
        "published title, or pass `--doi`.",
        "",
    ]
    for cand in candidates:
        if cand.missing or (cand.stem not in wanted and not cand.error):
            continue
        lines += [f"## {cand.path.name}", ""]
        if cand.error:
            lines += [f"*extraction failed: {cand.error}*", ""]
            continue
        snippet = "\n".join(cand.text.splitlines()[:40])
        lines += ["```", snippet, "```", ""]
    p = Path(path)
    atomic_write_text(p, "\n".join(lines) + "\n")
    return p
