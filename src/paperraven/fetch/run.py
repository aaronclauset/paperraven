"""`paperraven fetch`: one pass over a bibliography.

Per entry, stopping at the first PDF that is identified as that entry:
1. its identifier: the `.bib`'s DOI or OpenAlex id, else the title cascade;
2. already in `dest`? (identified by content, whatever its file name: resumable);
3. known open copies: OpenAlex's open `pdf_url`s (published, then of unknown version, then
   accepted, then submitted), then Europe PMC's open-access PDF links; with `--published-only`,
   OpenAlex's published versions alone (the user chooses);
4. the publisher's page: the DOI's redirects, then its `citation_pdf_url`, one hop;
5. otherwise the click-list, with the reason.

Entries are fetched up to 4 publishers (DOI prefixes) at a time, with one shared politeness
state. A downloaded PDF that is not identified as its entry goes to `dest/_unverified/`, never
under the entry's name. `--collect` copies PDFs the user downloaded, identified by content, into
`dest`. Nothing is written outside `dest`, and a run's state is never touched.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from .. import __version__
from ..normalize import identifier_url, normalize_doi
from ..registry.base import NotFound, RegistryUnavailable
from ..worklist import assign_filenames
from .bibfile import BibEntry
from .client import RESOLVER_GAP, RESOLVER_JITTER
from .clicklist import write_click_list
from .identify import IDENTIFIED, Target, identify, identify_folder, scan_folder, sha256

MANIFEST = "fetch_manifest.json"
#: The user's seed PDFs `export` copied into the folder: per entry, the file and its SHA-256.
SEEDS = "seeds.json"
UNVERIFIED = "_unverified"
#: Where a preprint goes when the user collects its published version.
PREPRINTS = "_preprints"
#: Publishers (DOI prefixes) fetched at once.
WORKERS = 4
#: Open copies in the order tried: a copy of unknown version is usually the
#: publisher's or PMC's, so it comes before accepted and submitted versions.
_VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 2, "submittedVersion": 3}
_UNKNOWN_RANK = 1
_PREPRINT_TYPES = {"preprint", "posted-content"}
_ONLY_PREPRINT = ("only a preprint or a copy of unknown version is openly available (you asked "
                  "for publishers' versions only)")
#: Where downloads are written before they are identified: the tool's own hidden folder, so a
#: clean-up can never touch a file of the user's.
PARTIAL_DIR = ".paperraven-partial"
#: Choosing a free file name and renaming into it happen together, whichever thread.
_NAME_LOCK = threading.Lock()
#: Permissions for a written PDF, from the umask (set by `fetch_bibliography` before threads).
_FILE_MODE = 0o644
_TAG = re.compile(rb'<meta[^>]+name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)|'
                  rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url', re.I)


class FetchRefused(ValueError):
    """The destination is not somewhere PDFs may be written (inside a git repository)."""


@dataclass
class Item:
    bib: BibEntry
    identifier: str | None
    name: str
    outcome: str = ""
    why: str = ""
    open_link: str | None = None
    version_warning: str = ""
    record: dict[str, Any] = field(default_factory=dict)
    #: The entry itself is a preprint (OpenAlex's type): no published version to point to.
    entry_is_preprint: bool = False

    @property
    def doi(self) -> str | None:
        return self.identifier if self.identifier and self.identifier.startswith("10.") else None


def downloads_folder() -> Path:
    """Where browsers save downloads by default on macOS, Windows and most Linux desktops."""
    return Path.home() / "Downloads"


def refuse_if_tracked(dest: Path) -> None:
    """Refuse a destination git would track: PDFs are copyrighted and never belong in a repo.

    Inside a repository, a destination its ignore rules cover is allowed, which is how a
    clone keeps its corpus folders out of git (`*-corpus/`). Without git to ask, any
    destination inside a repository is refused.
    """
    target = dest.resolve()
    repo = next((p for p in (target, *target.parents) if (p / ".git").exists()), None)
    if repo is None:
        return
    advice = (f"{dest} is inside the git repository at {repo}, where git would track it; PDFs "
              f"are copyrighted and must not be committed. Choose a folder outside it, or one "
              f"its .gitignore covers.")
    if shutil.which("git") is None:
        raise FetchRefused(advice + " (git is not installed, so ignore rules cannot be checked.)")
    probe = (target / "probe.pdf").relative_to(repo)
    result = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", str(probe)],
                            capture_output=True)
    if result.returncode != 0:
        raise FetchRefused(advice)


def fetch_bibliography(entries: list[BibEntry], dest: Path, *, bib_path: Path, client,
                       openalex=None, europe_pmc=None,
                       resolve: Callable[[BibEntry], str | None] | None = None,
                       collect: Path | None = None,
                       progress: Callable[[str], None] | None = None,
                       published_only: bool = False,
                       workers: int = WORKERS) -> dict[str, Any]:
    say = progress or (lambda message: None)
    dest = Path(dest)
    refuse_if_tracked(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # Left by an interrupted run: the tool's own temporary folder. A file where the folder
    # goes is removed too.
    _remove(dest / PARTIAL_DIR)
    (dest / PARTIAL_DIR).mkdir()
    global _FILE_MODE                                         # read once, before any thread
    umask = os.umask(0)
    os.umask(umask)
    _FILE_MODE = 0o666 & ~umask
    manifest = _load(dest / MANIFEST)
    previous = dict(manifest.get("entries") or {})
    manifest.setdefault("runs", []).append(
        {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "published_only": published_only})
    # What each file's page 1 says, by SHA-256; matched afresh every run.
    cache: dict[str, Any] = manifest.setdefault("identified_files", {})

    items = [_item(e, resolve) for e in entries]
    names = assign_filenames([_NameInfo(i) for i in items])
    for i in items:
        i.name = names[i.identifier or f"bib:{i.bib.key}"] + ".pdf"
    # One target per identifier: an entry listed twice must not match "two" entries.
    by_id: dict[str, list[Item]] = {}
    for i in items:
        if i.identifier:
            by_id.setdefault(i.identifier, []).append(i)
    targets = [Target(ident, doi=same[0].doi, title=same[0].bib.title, year=same[0].bib.year)
               for ident, same in by_id.items()]
    published_collected = not_swapped = 0

    # Whatever stops the run (a crash, Ctrl-C, a full disk), the manifest and the
    # click-list are written for what was done; entries not reached go on the click-list.
    try:
        # 2. What `dest` already holds, by content (resume). A file the manifest recorded for
        # this entry keeps its record, so a preprint stays known as one.
        for ident, path in scan_folder(dest, targets, cache).items():
            prev = previous.get(ident)
            record = _kept_record(prev if isinstance(prev, dict) else None, path)
            for item in by_id[ident]:
                item.outcome, item.record = "already_present", dict(record)
                item.version_warning = _preprint_warning(record)
        # The user's seed PDFs, listed by `export`: each is its entry's while its content is
        # unchanged, even one whose page 1 identifies nothing.
        for ident, path in listed_seeds(dest).items():
            for item in by_id.get(ident, []):
                if not item.outcome:
                    item.outcome = "already_present"
                    item.record = {"file": path.name, "identified_by": "your seed (export)"}

        # --collect: copy in what the user downloaded. Read with a throwaway cache: nothing
        # from the user's other files may reach the manifest.
        if collect is not None:
            for ident, found in identify_folder(Path(collect), targets, {}).items():
                # A copy identified by DOI first, else the first by name.
                first = by_id[ident][0]
                path = next((p for p, r in found if r.method == "doi"), found[0][0])
                if first.outcome == "already_present" and first.version_warning:
                    # The published version only if page 1 shows the entry's own DOI; another
                    # copy of the preprint is not swapped in. Every copy in the folder is looked
                    # at, not only the first by name.
                    held = first.record.get("sha256")
                    others = [p for p, _ in found if sha256(p) != held]
                    by_doi = next((p for p, r in found if r.method == "doi" and p in others), None)
                    swapped = _collect_published(by_id[ident], by_doi, dest) if by_doi else 0
                    published_collected += swapped
                    not_swapped += not by_doi and bool(others)
                    continue
                if first.outcome:
                    continue
                with _NAME_LOCK:
                    name = _free_name(dest, first.name)    # never over another file
                    shutil.copy2(path, dest / name)
                for item in by_id[ident]:
                    item.outcome, item.record = "collected", {"file": name,
                                                              "from_collect": path.name}

        # 3-5. Fetch the rest; an entry listed twice is fetched once, and its twin copied
        # after. First every registry lookup, so the time the politeness gaps imply can be
        # said before any download, then the downloads, up to `workers` publishers at a
        # time, with a progress line every 10 completions.
        todo = []
        for ident, same in by_id.items():
            if not same[0].outcome:
                todo.append(same[0])
        for item in items:
            if not item.identifier:
                item.outcome, item.why = "click_list", "not found in the registries"
        if todo:
            say(f"fetch: looking up {len(todo)} entries in the registries first")
        plans = [_plan(item, openalex, europe_pmc, targets, published_only=published_only)
                 for item in todo]
        seconds = estimate_seconds(plans, client, workers) if client else 0.0
        if todo:
            say(f"fetch: estimate about {_minutes(seconds)} for {len(todo)} papers (up to "
                f"{workers} publishers at a time, each at most one request every 10 s; less if "
                f"publishers refuse)")
        _download_all(todo, plans, dest, client, cache, workers, seconds, say)
    finally:
        # An entry listed twice takes its first's outcome, even after Ctrl-C.
        for same in by_id.values():
            for twin in same[1:]:
                if not twin.outcome and same[0].outcome:
                    twin.outcome, twin.why, twin.record = (same[0].outcome, same[0].why,
                                                           dict(same[0].record))
                    twin.open_link, twin.version_warning = same[0].open_link, same[0].version_warning
                    twin.entry_is_preprint = same[0].entry_is_preprint
        shutil.rmtree(dest / PARTIAL_DIR, ignore_errors=True)
        for i in items:
            if not i.outcome:
                i.outcome, i.why = "click_list", "not reached: the run stopped early"
        click = [i for i in items if i.outcome == "click_list"]
        preprints = [i for i in items if i.version_warning
                     and i.outcome in ("fetched", "already_present")]
        html_path, csv_path = _write_outputs(items, click, manifest, cache, dest, bib_path,
                                             preprints, published_only)

    count = lambda o: sum(1 for i in items if i.outcome == o)
    return {"entries": len(items), "already_present": count("already_present"),
            "collected": count("collected"), "fetched": count("fetched"),
            "click_list": len(click), "preprints": len(preprints),
            "published_collected": published_collected, "not_swapped": not_swapped,
            "optional_rows": sum(1 for i in preprints if i.doi),
            "published_only": published_only,
            "only_preprint": sum(1 for i in click if i.why == _ONLY_PREPRINT),
            "unverified": sum(1 for i in items if i.record.get("unverified")),
            "hosts_refused": sorted(client.dropped) if client else [],
            "hosts_capped": sorted(client.capped) if client else [],
            "run_again": bool(client and client.capped),
            "click_list_html": str(html_path), "click_list_csv": str(csv_path),
            "dest": str(dest), "requests": client.requests if client else 0}


def _download_all(todo: list[Item], plans: list["Plan"], dest: Path, client, cache,
                  workers: int, seconds: float, say) -> None:
    """The downloads, a publisher (DOI prefix) per worker, largest group first.
    Politeness is the client's, per host, whichever worker asks. Ctrl-C or a crash in
    any worker stops them all before the caller writes its outputs."""
    if not todo:
        return
    groups: dict[str, list[tuple[Item, Plan]]] = {}
    for item, plan in zip(todo, plans):
        groups.setdefault(plan.group or item.identifier or "", []).append((item, plan))
    queue = sorted(groups.values(), key=len, reverse=True)
    done, lock = [0, 0], threading.Lock()                     # completed, fetched

    def work(group):
        for item, plan in group:
            if client is not None and client.stop.is_set():
                return
            _fetch_one(item, dest, client, None, None, None, cache, plan=plan)
            with lock:
                done[0] += 1
                done[1] += item.outcome == "fetched"
                n, got = done
            if n % 10 == 0 and n < len(todo):
                say(f"fetch: {n} of {len(todo)} done, {got} fetched, about "
                    f"{_minutes(seconds * (1 - n / len(todo)))} left")

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(queue)))) as pool:
        futures = [pool.submit(work, g) for g in queue]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            if client is not None:
                client.stop.set()
            for future in futures:
                future.cancel()
            raise


def listed_seeds(dest: Path) -> dict[str, Path]:
    """The files `SEEDS` lists that `dest` holds unchanged, by entry; a name that is not a plain
    file name in `dest`, or a list that is not ours to read, is ignored."""
    try:
        seeds = json.loads((dest / SEEDS).read_text(encoding="utf-8")).get("seeds")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return {}
    out = {}
    for ident, entry in (seeds.items() if isinstance(seeds, dict) else ()):
        name = entry.get("file") if isinstance(entry, dict) else None
        if not (isinstance(name, str) and name and Path(name).name == name
                and not name.startswith(".")):
            continue
        path = dest / name
        if path.is_file() and _digest(path) == entry.get("sha256"):
            out[ident] = path
    return out


def _kept_record(prev: dict | None, path: Path) -> dict[str, Any]:
    """What the manifest recorded for this entry, if it is this very file; else the name."""
    if prev and prev.get("sha256") and prev.get("sha256") == sha256(path):
        kept = {k: v for k, v in prev.items() if k not in ("title", "outcome", "why")}
        kept["file"] = path.name
        return kept
    return {"file": path.name}


def _preprint_warning(record: dict[str, Any]) -> str:
    version = record.get("version")
    if not version or version == "publishedVersion" or record.get("entry_is_preprint"):
        return ""
    return (f"The copy fetched is the {version} (a preprint): get the published version if "
            f"you need it")


def _collect_published(same: list[Item], path: Path, dest: Path) -> int:
    """A different PDF identified as a paper whose file is a preprint: the published version
    the user downloaded. It takes the paper's file name; the preprint moves to `_preprints/`.
    Nothing is deleted, and the user's file is copied, not moved."""
    present = dest / same[0].record["file"]
    if not present.is_file() or sha256(present) == sha256(path):
        return 0
    digest = sha256(path)
    aside = dest / PREPRINTS
    aside.mkdir(exist_ok=True)
    with _NAME_LOCK:
        moved = aside / _free_name(aside, present.name)
        present.replace(moved)
        shutil.copy2(path, present)
    for item in same:
        item.outcome, item.version_warning = "collected", ""
        item.record = {"file": present.name, "from_collect": path.name, "sha256": digest,
                       "version": "publishedVersion", "identified_by": "doi",
                       "preprint_moved_to": f"{PREPRINTS}/{moved.name}"}
    return 1


#: Seconds a request takes, for the estimate: the gaps dominate, this only keeps it honest.
_REQUEST_SECONDS = 1.0


def _requests(plan: Plan, typical: bool) -> list[str]:
    """The hosts one entry's attempts reach, in order. A publisher page is three requests: the
    DOI resolver, the publisher's page and its PDF link; the publisher's host is not known
    before the redirect, so the DOI prefix (one publisher's) stands in for it."""
    out = []
    for url, step, _ in (plan.candidates[:1] if typical else plan.candidates):
        host = urlparse(url).netloc
        if step == "publisher page":
            prefix = url.split("doi.org/", 1)[-1].split("/", 1)[0]
            out += [host, f"publisher:{prefix}", f"publisher:{prefix}"]
        else:
            out.append(host)
    return out


def estimate_seconds(plans: list[Plan], client, workers: int = WORKERS) -> float:
    """Seconds for the downloads: each entry's first attempt, `workers` DOI-prefix
    groups at a time, largest first, under the client's gaps shared across workers. Refusals
    and the cap make a real run shorter; slow servers make it longer. One figure, no bound."""
    groups: dict[str, list[list[str]]] = {}
    for n, plan in enumerate(plans):
        hosts = _requests(plan, typical=True)
        prefix = next((h.split(":", 1)[1] for h in hosts if h.startswith("publisher:")), None)
        groups.setdefault(plan.group or prefix or f"entry-{n}", []).append(hosts)
    queue = [hosts for g in sorted(groups.values(), key=len, reverse=True)
             if (hosts := [host for entry in g for host in entry])]
    mean_gap = lambda host: (client.host_gap if host.startswith("publisher:")
                             else RESOLVER_GAP[host] + RESOLVER_JITTER / 2 if host in RESOLVER_GAP
                             else client.host_gap)
    last_any, last, end = None, {}, 0.0
    heap = [(0.0, w, []) for w in range(max(1, workers))]
    heapq.heapify(heap)
    while heap:
        ready, w, pending = heapq.heappop(heap)
        if not pending:
            if not queue:
                end = max(end, ready)
                continue
            pending = queue.pop(0)
        host = pending.pop(0)
        start = ready
        if last_any is not None:
            start = max(start, last_any + client.global_gap)
        if host in last:
            start = max(start, last[host] + mean_gap(host))
        last_any = last[host] = start
        heapq.heappush(heap, (start + _REQUEST_SECONDS, w, pending))
    return end


#: For `--estimate`: the registry lookups before any download (OpenAlex, then Europe PMC at 1 s).
_LOOKUP_SECONDS = 1.5


def estimate_from_bib(entries: list[BibEntry], client, workers: int = WORKERS) -> float:
    """Seconds for a whole `fetch`, from the `.bib` alone: no network, no files (so the
    agent can tell the user how long before asking). Each entry is assumed to go to its
    publisher's page, grouped by DOI prefix. What is already in the folder is not subtracted."""
    plans = [Plan(candidates=[(identifier_url(e.doi), "publisher page", None)],
                  group=e.doi.split("/", 1)[0]) for e in entries if e.doi]
    return len(entries) * _LOOKUP_SECONDS + estimate_seconds(plans, client, workers)


def _minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute" + ("" if m == 1 else "s")


def _write_outputs(items, click, manifest, cache, dest: Path, bib_path,
                   preprints=(), published_only=False):
    row = lambda i, section, link: {
        "title": i.bib.title or i.bib.key, "authors": "; ".join(i.bib.authors),
        "year": i.bib.year, "venue": i.bib.venue or "", "identifier": i.identifier or "",
        "open": link, "why": i.why, "version_warning": i.version_warning, "section": section}
    rows = [row(i, "click", i.open_link or (identifier_url(i.identifier) if i.identifier
                                            else "")) for i in click]
    # A paper fetched as a preprint, listed with its published version's link.
    rows += [row(i, "optional", identifier_url(i.identifier)) for i in preprints if i.doi]
    html_path, csv_path = write_click_list(rows, dest, Path(bib_path),
                                           published_only=published_only)
    # Keep signals only for files `dest` still holds.
    held = {_digest(p) for folder in (dest, dest / UNVERIFIED) if folder.is_dir()
            for p in folder.glob("*.pdf")} - {None}
    # A copy: a second Ctrl-C can bring this here while a worker still writes.
    manifest["identified_files"] = {k: v for k, v in cache.copy().items() if k in held}
    manifest["tool_version"] = __version__
    manifest["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest.setdefault("entries", {})
    written = set()
    for i in items:                     # one entry per identifier: the first
        key = i.identifier or f"bib:{i.bib.key}"
        if key in written:
            continue
        written.add(key)
        manifest["entries"][key] = {"title": i.bib.title, "outcome": i.outcome, "why": i.why,
                                    **i.record}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return html_path, csv_path


def _digest(path: Path) -> str | None:
    """A file's SHA-256, or `None` for a folder or a file that cannot be read, so neither
    stops the run."""
    try:
        return sha256(path) if path.is_file() else None
    except OSError:
        return None


def _remove(path: Path) -> None:
    """The tool's own temporary folder, gone, or a file in its place."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _free_name(dest: Path, name: str) -> str:
    """`name`, or `stem_2.pdf`, `stem_3.pdf`, … if a file (another paper, or the user's own
    copy that could not be identified) already has it. Nothing is ever replaced."""
    stem, n = Path(name).stem, 1
    while (dest / name).exists():
        n += 1
        name = f"{stem}_{n}.pdf"
    return name


# ----------------------------------------------------------------------------------------


@dataclass
class _NameInfo:
    """The fields `worklist.assign_filenames` reads, from a bib entry."""

    item: Item

    @property
    def doi(self) -> str:
        return self.item.identifier or f"bib:{self.item.bib.key}"

    @property
    def authors(self) -> list[str]:
        return self.item.bib.authors

    @property
    def venue(self) -> str | None:
        return self.item.bib.venue

    @property
    def year(self) -> int | None:
        return self.item.bib.year


def _item(e: BibEntry, resolve) -> Item:
    ident = e.doi or (f"openalex:{e.openalex_id}" if e.openalex_id else None)
    if ident is None and resolve is not None and e.title:
        ident = resolve(e)
    return Item(bib=e, identifier=ident, name="")


@dataclass
class Plan:
    """What `fetch` will try for one entry, in order, found before any download."""

    candidates: list[tuple[str, str, str | None]] = field(default_factory=list)
    own_targets: list | None = None
    reasons: list[str] = field(default_factory=list)
    #: The entry's DOI prefix: the publisher, for running publishers side by side.
    group: str = ""


def _fetch_one(item: Item, dest: Path, client, openalex, europe_pmc, targets, cache,
               plan: Plan | None = None) -> None:
    _download(item, plan or _plan(item, openalex, europe_pmc, targets), dest, client, cache)


def _plan(item: Item, openalex, europe_pmc, targets, *, published_only: bool = False) -> Plan:
    """The registry lookups for one entry: its open copies and its publisher page. With
    `published_only`, only copies OpenAlex labels the published version (accepted, submitted
    and unknown versions, Europe PMC's included, are skipped and said)."""
    reasons: list[str] = []
    candidates: list[tuple[str, str, str | None]] = []          # (url, step, version)
    work, answered = None, False
    if openalex is not None:
        try:
            work = (openalex.fetch_by_doi(item.doi) if item.doi
                    else openalex.fetch_by_id(item.identifier.split(":", 1)[1]))
            answered = True
        except NotFound as exc:
            answered = True                    # "no record" is an answer
            reasons.append(f"registry lookup failed ({type(exc).__name__})")
        except RegistryUnavailable as exc:
            reasons.append(f"registry lookup failed ({type(exc).__name__})")
    # A copy fetched from this entry's own metadata may be another version
    # that prints its own DOI (a bioRxiv preprint); those DOIs count as this entry's.
    own_targets = targets
    if work is not None:
        versions = tuple(sorted({d for l in work.locations
                                 for u in (l.landing_page_url, l.pdf_url) if u
                                 if (d := _doi_in_url(u))} - {item.doi}))
        if versions:
            own_targets = [replace(t, other_dois=versions) if t.identifier == item.identifier
                           else t for t in targets]
    skipped = False
    if work is not None:
        item.entry_is_preprint = (work.type or "").lower() in _PREPRINT_TYPES
        open_locs = sorted((l for l in work.locations if l.is_oa),
                           key=lambda l: _VERSION_RANK.get(l.version or "", _UNKNOWN_RANK))
        if published_only:
            skipped = any(l.pdf_url and l.version != "publishedVersion" for l in open_locs)
            open_locs = [l for l in open_locs if l.version == "publishedVersion"]
        candidates += [(l.pdf_url, "open copy (OpenAlex)", l.version) for l in open_locs
                       if l.pdf_url]
        landing = next((l.landing_page_url for l in open_locs if l.landing_page_url), None)
        item.open_link = landing
    if europe_pmc is not None and item.doi:
        # Looked up in either mode, so the lookups are the same; under `published_only` its
        # copies (version unknown) are skipped and said.
        try:
            links = europe_pmc.pdf_links_for(item.doi)
        except (NotFound, RegistryUnavailable):
            links = []
        if published_only:
            # Said only when OpenAlex answered: in an outage nothing says there was no
            # published copy (a NotFound is an answer).
            skipped = skipped or (answered and bool(links))
        else:
            candidates += [(u, "open copy (Europe PMC)", None) for u in links]
    # The reason only when copies were skipped and no published open copy was there to try:
    # a published copy that failed keeps its own reason.
    if skipped and not any(step.startswith("open copy") for _, step, _ in candidates):
        reasons.append(_ONLY_PREPRINT)
    if item.doi:
        candidates.append((identifier_url(item.doi), "publisher page", None))
    return Plan(candidates=candidates, own_targets=own_targets, reasons=reasons,
                group=item.doi.split("/", 1)[0] if item.doi else "")


def _download(item: Item, plan: Plan, dest: Path, client, cache) -> None:
    reasons, own_targets = plan.reasons, plan.own_targets
    tried: set[str] = set()
    for url, step, version in plan.candidates:
        if url in tried:
            continue
        tried.add(url)
        res = client.get(url)
        if res.kind == "stopped":            # Ctrl-C or a crash elsewhere: not reached
            return
        if res.kind == "html" and step == "publisher page":
            m = _TAG.search(res.content or b"")
            if not m:
                reasons.append("the publisher's page offers no PDF link to a program")
                continue
            tag = (m.group(1) or m.group(2)).decode("latin-1")
            try:
                link = urljoin(res.final_url, tag)
            except ValueError:                        # a malformed link, not a crash
                reasons.append("the publisher's PDF link is malformed")
                continue
            res = client.get(link)
            step = "publisher PDF link"
            if res.kind == "stopped":
                return
        if res.kind != "pdf":
            reasons.append({"blocked": "the publisher refused an automated request",
                            "dropped": "the publisher refused an automated request",
                            "capped": "per-publisher limit reached: run again later",
                            "html": "no PDF at the open-access link",
                            "too_large": "the file was over the size limit",
                            }.get(res.kind, f"the download failed ({res.detail or res.kind})"))
            continue
        # Each download its own temporary file (workers run side by side).
        handle, tmp_name = tempfile.mkstemp(dir=dest / PARTIAL_DIR, suffix=".pdf")
        tmp = Path(tmp_name)
        try:
            with open(handle, "wb") as fh:
                fh.write(res.content)
            os.chmod(tmp, _FILE_MODE)                    # as a plain write would
            r = identify(tmp, own_targets, cache=cache)  # cached by content: survives the rename
            if r.status == IDENTIFIED and r.identifier == item.identifier:
                with _NAME_LOCK:                         # never over another file
                    final = dest / _free_name(dest, item.name)
                    tmp.replace(final)
                item.outcome = "fetched"
                item.record = {"file": final.name, "source_url": res.url, "step": step,
                               "version": version, "sha256": sha256(final),
                               "bytes": final.stat().st_size, "identified_by": r.method}
                if item.entry_is_preprint:
                    item.record["entry_is_preprint"] = True
                if version and version != "publishedVersion":
                    item.record["version_warning"] = f"this copy is the {version}"
                item.version_warning = _preprint_warning(item.record)
                return
            (dest / UNVERIFIED).mkdir(exist_ok=True)
            digest = sha256(tmp)
            with _NAME_LOCK:                             # never over a kept file
                same = next((p for p in (dest / UNVERIFIED).glob("*.pdf")
                             if _digest(p) == digest), None)  # once is enough
                kept = same or dest / UNVERIFIED / _free_name(
                    dest / UNVERIFIED, f"{Path(item.name).stem}__{_host_slug(res.host)}.pdf")
                if same is None:
                    tmp.replace(kept)
        finally:
            tmp.unlink(missing_ok=True)
        item.record = {"unverified": kept.name, "source_url": res.url, "step": step}
        reasons.append(f"a PDF was found but did not match this paper: check its version "
                       f"(kept in {UNVERIFIED}/)")
    item.outcome = "click_list"
    order = ["did not match", "only a preprint", "refused", "limit reached", "no PDF link", "no PDF at",
             "lookup failed", "failed", "size limit"]
    item.why = next((r for key in order for r in reasons if key in r),
                    "no open copy found") if reasons else "no open copy found"


def _host_slug(host: str) -> str:
    """A host as file-name text: letters, digits and `_` only. A `:port` would write an NTFS
    alternate data stream on Windows; credentials must never reach a file name."""
    host = host.rsplit("@", 1)[-1]
    return re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_") or "host"


def _doi_in_url(url: str) -> str | None:
    m = re.search(r"doi\.org/(10\.[^\s?#]+)", url, re.I)
    return normalize_doi(m.group(1)) if m else None


def _load(path: Path) -> dict[str, Any]:
    """The manifest, or a fresh one. One that is not ours to read (not a JSON object,
    wrong-typed sections, not UTF-8) is set aside under a dated name, never fatal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError):
        data = None
    if (isinstance(data, dict) and isinstance(data.get("identified_files", {}), dict)
            and isinstance(data.get("entries", {}), dict)
            and isinstance(data.get("runs", []), list)):
        return data
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path.replace(path.with_name(f"fetch_manifest.invalid-{stamp}.json"))
    return {}
