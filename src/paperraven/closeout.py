"""The close-out check: is the run's deliverable what the tool wrote, and is it sound.

A deterministic check of the export folder (files added or edited after export, the .bib
file's integrity, the tables, the README, personal data, JSON validity) and of the shared
scratchpad (PDFs and extracted text left there). It reads, and reports; it changes and
deletes nothing, except writing its own report to `audit/closeout.json`. Invalid JSON is
reported, not repaired.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .corpus import Corpus

MANIFEST = "audit/manifest.json"
REPORT = "audit/closeout.json"
#: The one file the agent may add to the output folder; reported as the agent's.
AGENT_NOTES = "agent_notes.md"
#: A text file this large in a scratchpad is most likely a paper's extracted text.
LARGE_TEXT_BYTES = 20_000
LARGE_FILE_BYTES = 1_000_000
#: A folder holding this is `paperraven fetch`'s output (`fetch.run.MANIFEST`); its default
#: destination is `pdfs/` inside the export folder.
FETCH_MANIFEST = "fetch_manifest.json"
#: The list of the user's seed PDFs `export` copies there for `fetch` (`fetch.run.SEEDS`).
SEED_LIST = "seeds.json"

_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>|&(?:lt|gt|amp|quot|apos|#\d+);")
_FIELD = re.compile(r"^  (\w+) = \{(.*)\},?$")
_ENTRY = re.compile(r"^@(\w+)\{([^,\s]+),$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _result(name: str, fails: list[str], warns: list[str],
            notes: list[str] | None = None) -> dict[str, Any]:
    """A check's outcome. `notes` are information only and never change the status."""
    status = "fail" if fails else "warn" if warns else "pass"
    return {"name": name, "status": status, "fails": fails, "warns": warns,
            "notes": notes or []}


def write_manifest(dest: Path, written: list[Path]) -> None:
    """Record the SHA-256 of every file `export` wrote, so later edits and additions show."""
    manifest = {_rel(p, dest): _sha(p) for p in written if p and p.is_file()}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")


def _output_folder(dest: Path) -> dict[str, Any]:
    path = dest / MANIFEST
    if not path.is_file():
        return _result("output folder", [], ["no manifest, so edits and added files cannot "
                                             "be told apart"])
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        manifest = exc
    if not isinstance(manifest, dict) or not all(isinstance(v, str) for v in manifest.values()):
        return _result("output folder", [f"{MANIFEST}: unreadable, so edits and added files "
                                         f"cannot be checked; export again to rewrite it"], [])
    fails = [f"{name}: missing" if not (dest / name).is_file()
             else f"{name}: changed after export"
             for name, digest in manifest.items()
             if not (dest / name).is_file() or _sha(dest / name) != digest]
    fetched = sorted({m.parent for name in (FETCH_MANIFEST, SEED_LIST)
                      for m in dest.rglob(name) if m.parent != dest})
    warns, notes = [], []
    for folder in fetched:
        n = sum(1 for _ in _files(folder))
        notes.append(f"{_rel(folder, dest)}/: {n} files written by `paperraven fetch` "
                     f"(PDFs and its click-list); not part of the export"
                     if (folder / FETCH_MANIFEST).is_file() else
                     f"{_rel(folder, dest)}/: {n} files, your seed PDFs copied there for "
                     f"`paperraven fetch`; not part of the export")
    for p in _files(dest):
        name = _rel(p, dest)
        if name in manifest or name in (MANIFEST, REPORT):
            continue
        if any(folder in p.parents for folder in fetched):
            continue
        warns.append(f"{name}: written by the agent, not the tool" if name == AGENT_NOTES
                     else f"{name}: not written by the tool")
    return _result("output folder", fails, warns, notes)


def _parse_bib(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    entries, errors, entry, depth, entry_depth = [], [], None, 0, 0
    for n, line in enumerate(text.splitlines(), start=1):
        change = line.count("{") - line.count("}")
        depth += change
        if entry is None:
            if m := _ENTRY.match(line):
                entry = {"type": m.group(1), "key": m.group(2), "fields": {}, "line": n}
                entry_depth = change
            elif line.strip():
                errors.append(f"line {n}: text outside an entry")
        elif line == "}":
            # Counted per entry, as BibTeX counts them (a backslash does not escape a brace),
            # so an open brace in one entry cannot cancel a close in another.
            if entry_depth + change != 0:
                errors.append(f"line {entry['line']}: unbalanced braces in entry "
                              f"{entry['key']}")
            entries.append(entry)
            entry = None
        elif m := _FIELD.match(line):
            entry["fields"][m.group(1)] = m.group(2)
            entry_depth += change
        else:
            errors.append(f"line {n}: not a single-line field (a newline inside a value?)")
            entry_depth += change
    if entry is not None or depth:
        errors.append("unbalanced braces: an entry is not closed")
    return entries, errors


def _bib(dest: Path, corpus: "Corpus") -> dict[str, Any]:
    path = dest / "papers.bib"
    if not path.is_file():
        return _result("papers.bib", ["missing"] if corpus.greenlist else [], [])
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _result("papers.bib", ["not valid UTF-8"], [])
    entries, fails = _parse_bib(text)
    keys = [e["key"] for e in entries]
    fails += [f"duplicate key {k}" for k in sorted({k for k in keys if keys.count(k) > 1})]
    ids = {e["fields"].get("doi") or "openalex:" + e["fields"].get("url", "").rsplit("/", 1)[-1]
           for e in entries}
    fails += [f"no entry for {i}" for i in sorted(set(corpus.greenlist) - ids)]
    fails += [f"an entry for {i}, which is not in the corpus" for i in sorted(ids - set(corpus.greenlist))]
    warns = []
    for e in entries:
        f = e["fields"]
        if _MARKUP.search(f.get("title", "")) or _MARKUP.search(f.get("author", "")):
            fails.append(f"{e['key']}: markup in the title or authors")
        for need in ("title", "year", "author"):
            if not f.get(need):
                (fails if need == "title" else warns).append(f"{e['key']}: no {need}")
    if entries and not any("volume" in e["fields"] or "pages" in e["fields"] for e in entries):
        warns.append("no entry has volume or pages")
    return _result("papers.bib", fails, warns)


def _rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:          # closed, not left to GC
        return list(csv.reader(fh))[1:]


def _tables(dest: Path, corpus: "Corpus") -> dict[str, Any]:
    fails = []
    for name, pool in (("papers.csv", corpus.greenlist), ("excluded.csv", corpus.excluded),
                       ("uncertain.csv", corpus.uncertain)):
        rows = _rows(dest / name)
        if len(rows) != len(pool):
            fails.append(f"{name}: {len(rows)} rows for {len(pool)} records")
    warns = []
    path = dest / "papers.csv"
    table = []
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            table = list(csv.reader(fh))
    head = table[0] if table else []
    if "Retracted" in head:          # named for the user's decision; never removed
        col = head.index("Retracted")
        flagged = [r[0] for r in table[1:] if len(r) > col and r[col]]
        if flagged:
            warns.append(f"papers.csv: {len(flagged)} paper(s) marked retracted by a registry: "
                         f"{'; '.join(flagged[:5])}")
    dois = [r[4] for r in _rows(dest / "papers.csv") if len(r) > 4]
    fails += [f"papers.csv: {d} listed twice" for d in sorted({d for d in dois if dois.count(d) > 1})]
    fails += [f"papers.csv: {d} missing" for d in sorted(set(corpus.greenlist) - set(dois))]
    return _result("tables", fails, warns)


def _readme(dest: Path, corpus: "Corpus") -> dict[str, Any]:
    path = dest / "README.md"
    if not path.is_file():
        return _result("README", ["missing"], [])
    text, fails = path.read_text(encoding="utf-8"), []
    m = re.search(r"^# Literature corpus — (\d+) paper", text, re.M)
    if not m or int(m.group(1)) != len(corpus.greenlist):
        fails.append(f"the heading says {m.group(1) if m else 'no'} papers; the corpus has "
                     f"{len(corpus.greenlist)}")
    m = re.search(r"Target: (\d+) found", text)
    if m and int(m.group(1)) != corpus.found_size:
        fails.append(f"the target line says {m.group(1)} found; the corpus has {corpus.found_size}")
    return _result("README", fails, [])


def _secrets(dest: Path, email: str | None, api_key: str | None) -> dict[str, Any]:
    from .export import _home_forms

    needles = [(s, label) for s, label in ((email, "the contact email"), (api_key, "an API key"))
               if s]
    fails = []
    for p in _files(dest):
        if _rel(p, dest) == REPORT:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        fails += [f"{_rel(p, dest)}: contains {label}" for s, label in needles
                  if s.lower() in text.lower()]
        if any(h in text for h in _home_forms()):
            fails.append(f"{_rel(p, dest)}: a home directory path")
        values = [m.group(1) if m.group(2) is None else m.group(2)
                  for m in _EMAIL_FIELD.finditer(text)]
        if any(v.strip("'\" ") not in _UNSET_EMAIL for v in values):
            fails.append(f"{_rel(p, dest)}: a contact_email that is not <redacted>")
    return _result("personal data and secrets", fails, [])


#: A `contact_email` field in exported YAML or JSON, whatever address it holds: the export
#: replaces every address the run used, so any value left is one it did not know about.
_EMAIL_FIELD = re.compile(r'(?m)^\s*contact_email:[ \t]*(.*?)\s*$|"contact_email"\s*:\s*("[^"]*"|null)')
_UNSET_EMAIL = {"<redacted>", "null", "~", ""}


def _json(roots: list[Path]) -> dict[str, Any]:
    fails = []
    for root in roots:
        for p in _files(root):
            if p.suffix not in (".json", ".jsonl"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
                for line in (text.splitlines() if p.suffix == ".jsonl" else [text]):
                    if line.strip():
                        json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                fails.append(f"{p}: invalid JSON ({str(exc)[:60]})")
    return _result("JSON", fails, [])


def _scratchpad(scratch: Path | None) -> dict[str, Any]:
    if scratch is None:
        return _result("scratchpad", [], ["not checked: no scratchpad path was given"])
    warns = []
    for p in _files(Path(scratch)):
        with p.open("rb") as fh:
            head = fh.read(8192)
        size = p.stat().st_size
        if head.startswith(b"%PDF"):
            warns.append(f"{p.name}: a PDF ({size:,} bytes)")
        elif p.suffix.lower() == ".pdf":
            warns.append(f"{p.name}: named .pdf but not a PDF (a saved web page?)")
        elif b"\0" in head:
            warns.append(f"{p.name}: a binary file ({size:,} bytes)")
        elif size > LARGE_TEXT_BYTES:
            warns.append(f"{p.name}: a large text file ({size:,} bytes; extracted text?)")
        elif size > LARGE_FILE_BYTES:
            warns.append(f"{p.name}: {size:,} bytes")
    return _result("scratchpad", [], warns)


def usage_check(usage: dict[str, Any]) -> dict[str, Any]:
    """The run's token usage as report lines: information only, never a failure."""
    if usage.get("raw_tokens") is None:
        return _result("token usage", [], [], [f"not recorded: {usage.get('note')}"])
    u, per = usage, usage.get("per_paper")
    notes = [f"{u['raw_tokens']:,} tokens over {u['calls']} model calls in "
             f"{len(u['sessions'])} session(s): {u['cache_read']:,} cache reads, "
             f"{u['cache_write']:,} cache writes, {u['uncached_input']:,} uncached input, "
             f"{u['output_tokens']:,} output",
             f"weighted by the published cache rates: {u['input_equivalent']:,.0f} "
             f"input-equivalent tokens and {u['output_tokens']:,} output"]
    if per:
        notes.append(f"per paper returned ({u['papers']}): {per['input_equivalent']:,.0f} "
                     f"input-equivalent and {per['output_tokens']:,.0f} output tokens "
                     f"({per['raw_tokens']:,.0f} raw)")
    return _result("token usage", [], [], notes)


def closeout(corpus: "Corpus", config: "Config", run_dir: Path, dest: Path, *,
             scratch: Path | None = None, api_key: str | None = None,
             transcripts: Path | None = None) -> dict[str, Any]:
    """Run every check, write `audit/closeout.json`, and return the report. `transcripts` is
    where the host keeps its session transcripts; given, the run's token usage is recorded."""
    from datetime import datetime

    from .session_usage import session_usage

    dest, run_dir = Path(dest), Path(run_dir)
    email = config.contact_email or os.environ.get("PAPERRAVEN_CONTACT_EMAIL")
    checks = [_output_folder(dest), _bib(dest, corpus), _tables(dest, corpus),
              _readme(dest, corpus), _secrets(dest, email, api_key),
              _json([run_dir, dest]), _scratchpad(scratch)]
    usage = None
    if transcripts is not None:
        first = corpus.audit_events[0].timestamp if corpus.audit_events else None
        since = datetime.strptime(first, "%Y-%m-%dT%H:%M:%S%z") if first else None
        usage = session_usage(run_dir, len(corpus.greenlist), projects=transcripts, since=since)
        checks.append(usage_check(usage))
    report = {"ok": not any(c["status"] == "fail" for c in checks), "checks": checks,
              **({"usage": usage} if usage is not None else {})}
    (dest / REPORT).parent.mkdir(parents=True, exist_ok=True)
    (dest / REPORT).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def report_lines(report: dict[str, Any]) -> list[str]:
    """One line per check, then the detail of every failure and warning."""
    lines = [f"closeout: {'no failures' if report['ok'] else 'FAILURES'} -- "
             + ", ".join(f"{c['name']} {c['status']}" for c in report["checks"])]
    for c in report["checks"]:
        lines += [f"  [{level}] {c['name']}: {d}"
                  for level, key in (("fail", "fails"), ("warn", "warns"), ("note", "notes"))
                  for d in c.get(key, [])]
    return lines
