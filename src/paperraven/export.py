"""The deliverable a scientist takes away.

```
<dest>/
  README.md      papers.bib      papers.csv      worklist.csv      excluded.csv
  uncertain.csv  (only if any paper is undecided)
  audit/         README.md  records.json  flow.json  criteria/  judge_prompts/
                 rejections.json  rounds.jsonl
```

A few files a reader opens, and an `audit/` folder holding everything else: the audit
information is discoverable but kept out of the reader's way until needed. **This module
renders; it decides nothing.** Every number comes from `summary.run_summary`, so the files
cannot disagree with each other or with `status`. Nothing is written empty.

The default destination is **beside `runs/`**, not inside a run: runs are disposable, and
deleting a finished one must not delete what it produced.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path

import yaml
from typing import TYPE_CHECKING, Any

from dataclasses import dataclass, field

from .models import RULING_DECIDED_BY
from .models import AuthorName, DiscoveredBy
from .normalize import identifier_url

if TYPE_CHECKING:
    from .config import Config
    from .corpus import Corpus
    from .models import PaperRecord

AUDIT_README = """# audit/

Everything behind the corpus, for checking it. Nothing here is needed to use the corpus.

- `records.json` — every paper the run verified, in every pool, with its full provenance,
  its verdict, the config generation that verdict was made under, and any earlier verdicts.
- `flow.json` — the run summary: the numbers in the README's flow table, as data. `included`, `excluded_by_judge` and `undecided` are the judge's verdicts; papers the author ruled on are `ruled_in` and `ruled_out`.
- `criteria/` — every configuration generation as published (`genNNN.yaml`), tags intact,
  with the contact email replaced by `<redacted>`; `checksums.json` gives each original
  file's SHA-256, so a copy can still be matched to the run's fingerprint.
- `judge_prompts/` — each distinct judge prompt the tool issued, named by the first 12 hex
  digits of its SHA-256, and `index.json` giving the rounds that used each.
- `rejections.json` — every proposal that did not become a record, with its reason and bin.
- `rounds.jsonl` — the per-round log.
- `events.jsonl` — the complete record: every command the run committed, with the files the
  agent handed in (proposals, judge responses) verbatim.
- `retry_queue.json` — proposals still waiting for a retry after a registry error.
- `reference_candidates/` — each `references` listing the run proposed from, by round
  (`NNN.md`, and the same as `.json`); present only when the run used it.
- `manifest.json` — the SHA-256 of every file `export` wrote, so a later edit or an added file
  shows. `closeout.json`, when present, is the close-out check's report on this folder.
"""


def default_export_dir(run_dir: Path) -> Path:
    """Beside `runs/` when the run lives in one, otherwise beside the run; never inside it."""
    run_dir = Path(run_dir)
    base = run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir.parent
    return base / f"{run_dir.name}-corpus"


#: Every file one `write_export` call wrote, for its manifest. Reset per call.
_WRITTEN: list[Path] = []


def _text(path: Path, text: str, *, newline: str | None = None) -> Path | None:
    """Write non-empty text; skip (and report nothing) when there is nothing to write."""
    if not text.strip():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=newline)
    _WRITTEN.append(path)
    return path


def _csv(path: Path, header: list[str], table: list[list[Any]]) -> Path | None:
    if not table:
        return None
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(table)
    return _text(path, buf.getvalue(), newline="")        # csv writes its own \r\n


def _authors(rec: "PaperRecord") -> str:
    return "; ".join(rec.authors)


def _link(rec: "PaperRecord") -> str:
    if rec.oa_pdf_url or rec.oa_url:
        return rec.oa_pdf_url or rec.oa_url
    return identifier_url(rec.doi)              # doi.org, or OpenAlex for an `openalex:` id


def _found_by(rec: "PaperRecord") -> str:
    """How the proposer says it came to name the paper; self-reported."""
    if rec.provenance.discovered_by is DiscoveredBy.SEED:
        return "supplied by you"
    return rec.provenance.proposer_source or "not stated"


def _source(rec: "PaperRecord") -> str:
    if rec.provenance.discovered_by is DiscoveredBy.SEED:
        return "supplied by you (seed)"
    return f"found in round {rec.provenance.discovered_round}"


# ---- Reference library comparison ----------------------------------------------------


@dataclass
class Library:
    """The DOIs in a user's reference library, and how many entries could not be compared."""

    dois: set[str] = field(default_factory=set)
    entries: int = 0
    without_doi: int = 0


def parse_library(path: str | Path) -> Library:
    """A `.bib` (DOIs from `doi =` fields or a doi.org URL) or a text file of DOIs."""
    import re

    from .normalize import normalize_doi

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lib = Library()
    if Path(path).suffix.lower() == ".bib":
        chunks = [c for c in re.split(r"(?=@\w+\s*\{)", text) if c.lstrip().startswith("@")]
        for chunk in chunks:
            m = (re.search(r"\bdoi\s*=\s*[{\"]\s*([^}\"]+)", chunk, re.I)
                 or re.search(r"doi\.org/([^}\s\",]+)", chunk, re.I))
            doi = normalize_doi(m.group(1)) if m else None
            lib.entries += 1
            if doi:
                lib.dois.add(doi)
            else:
                lib.without_doi += 1
    else:
        for line in (ln.strip() for ln in text.splitlines()):
            if not line:
                continue
            lib.entries += 1
            doi = normalize_doi(line)
            if doi:
                lib.dois.add(doi)
            else:
                lib.without_doi += 1
    return lib


def _in_library(rec: "PaperRecord", lib: Library) -> bool:
    """Any of the record's DOIs — its own, its preprint's, its version of record's."""
    from .normalize import normalize_doi

    mine = {normalize_doi(d) for d in (rec.doi, rec.provenance.preprint_doi,
                                        rec.provenance.version_of_record_doi) if d}
    return bool(mine & lib.dois)


# ---- BibTeX -----------------------------------------------------------------------

_LATEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
          "_": r"\_", "{": r"\textbraceleft{}", "}": r"\textbraceright{}",
          "~": r"\textasciitilde{}",
          "^": r"\textasciicircum{}",
          # Unicode hyphens, which pdflatex rejects, become the ASCII one.
          "\u2010": "-", "\u2011": "-"}
_STOP = {"a", "an", "the", "on", "of", "in", "for", "to", "and", "with", "via", "from"}
_BIB_TYPE = {"article": ("article", "journal"), "journal-article": ("article", "journal"),
             "review": ("article", "journal"),
             "conference-paper": ("inproceedings", "booktitle"),
             "proceedings-article": ("inproceedings", "booktitle"),
             "book-chapter": ("incollection", "booktitle"), "book": ("book", "publisher")}


def bibtex_escape(text: str) -> str:
    """LaTeX special characters escaped; UTF-8 kept (biber reads it; see the README).

    Braces become `\\textbraceleft{}`/`\\textbraceright{}`: BibTeX and biber count braces
    whatever precedes them, so `\\{` in a title would unbalance the entry and swallow the next
    one.
    """
    return "".join(_LATEX.get(ch, ch) for ch in str(text))


def _ascii_word(text: str) -> str:
    import unicodedata

    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in folded if ch.isalpha())


_SUFFIX = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
#: Surname particles, compared case-insensitively.
_PARTICLES = {"van", "von", "der", "den", "vander", "zu", "auf", "ter", "vanden", "des", "del",
              "du", "da", "dalla", "dalle", "degli", "de", "di", "della", "la", "le", "dos",
              "das", "do", "ten"}


def _name_parts(name: str) -> tuple[str, str, str | None]:
    """A display name as (given, family, suffix): the last word is the family name, and any
    particle words before it join it, keeping at least one given word. A trailing suffix
    (`Jr.`) is set apart whenever another word remains."""
    words = name.split()
    suffix = words.pop() if len(words) > 1 and words[-1].lower() in _SUFFIX else None
    if len(words) < 2:
        return "", " ".join(words), suffix
    cut = len(words) - 1
    while cut > 1 and words[cut - 1].lower() in _PARTICLES:
        cut -= 1
    return " ".join(words[:cut]), " ".join(words[cut:]), suffix


def _suffix_after_comma(text: str) -> tuple[str, str] | None:
    """`John Smith, Jr.` → (`John Smith`, `Jr.`): one comma, and a suffix after it."""
    head, _, tail = text.rpartition(",")
    if head and "," not in head and tail.strip().lower() in _SUFFIX:
        return head.strip(), tail.strip()
    return None


def bib_author(name: "str | AuthorName") -> str:
    """One author in BibTeX's standard `von Last, Jr., First` form.

    Registry given/family parts are used as they are, except that particle words ending the
    given name move to the family name (`Laurens de` + `Haan`), keeping one given word. An
    organisation (a whole `name`) is braced so BibTeX keeps it intact; a given name with no
    family is written alone. A display name is split by `_name_parts`; one that already has a
    comma is written as it is, since BibTeX reads `Family, Given` itself, unless the comma
    only sets off a suffix (`John Smith, Jr.`).
    """
    from .models import AuthorName
    if isinstance(name, AuthorName):
        family = (name.family or "").strip()                 # a blank family counts as none
        if not family:
            if name.name:
                return "{" + bibtex_escape(name.name) + "}"
            return bibtex_escape(name.given or "")            # never dropped
        given = (name.given or "").split()
        while len(given) > 1 and given[-1].lower() in _PARTICLES:
            family = f"{given.pop()} {family}"
        given_text = " ".join(given)
        return (f"{bibtex_escape(family)}, {bibtex_escape(given_text)}" if given_text
                else bibtex_escape(family))
    text = " ".join(str(name).split())
    suffixed = _suffix_after_comma(text) if "," in text else None
    if "," in text and not suffixed:
        return bibtex_escape(text)
    given, family, suffix = _name_parts(suffixed[0] if suffixed else text)
    suffix = suffixed[1] if suffixed else suffix
    if not given and suffix:                   # `Smith Jr.`: kept whole, one family name
        return "{" + bibtex_escape(f"{family} {suffix}") + "}"
    return ", ".join(bibtex_escape(x) for x in (family, suffix, given) if x)


def _family(rec: "PaperRecord", detail) -> str:
    """The surname in a citation key: the family name's last word, so a particle never
    changes a key (`Haan2000…` for `de Haan`). A name already written `Family, Given` keys on
    its family; a blank registry family falls back to the display name."""
    names = detail.author_names if detail else []
    if names and names[0].family and names[0].family.split():
        return names[0].family.split()[-1]
    first = " ".join((rec.authors or ["Anon"])[0].split())
    if "," in first and not _suffix_after_comma(first):
        first = first.split(",", 1)[0]
    elif suffixed := _suffix_after_comma(first):
        first = suffixed[0]
    _, family, _ = _name_parts(first)
    return family.split()[-1] if family else "Anon"


_RANGE = re.compile(r"\s*([A-Za-z]*)(\d+)\s*[\u2010-\u2015\u2212-]+\s*([A-Za-z]*)(\d+)\s*")


def _pages(pages: str | None) -> str | None:
    """A true page range with BibTeX's double hyphen (`661--703`, `e12--e19`): the same letter
    prefix on both sides and an end not before the start. Anything else (`043001-1`, an
    article number) exactly as the registry wrote it."""
    if not pages:
        return None
    m = _RANGE.fullmatch(pages)
    if m and m.group(1) == m.group(3) and int(m.group(4)) >= int(m.group(2)):
        return f"{m.group(1)}{m.group(2)}--{m.group(3)}{m.group(4)}"
    return pages.strip()


def _bib_date(date: str | None, year: int | None) -> str:
    """The full date, only when it agrees with the year cited: biblatex takes the year from
    `date`, so a date in another year would contradict the entry's `year`."""
    return date if date and year and date[:4] == str(year) else ""


def _flags(detail) -> str:
    """The retraction and correction note, with the source (never the tool's own finding)."""
    notes = []
    if detail and detail.is_retracted:
        notes.append(f"Retracted (according to {detail.retraction_source or 'the registry'})")
    if detail and detail.corrections:
        notes.append("Correction: " + ", ".join(detail.corrections))
    return "; ".join(notes)


def _key_base(rec: "PaperRecord") -> str:
    """Surname, year and the title's first word: the key before any suffix."""
    surname = _ascii_word(_family(rec, rec.bibliographic)) or "Anon"
    word = next((_ascii_word(w).capitalize() for w in rec.title.split()
                 if w.lower() not in _STOP and _ascii_word(w)), "Paper")
    return f"{surname}{rec.year or ''}{word}"


def _suffix(n: int) -> str:
    """The n-th paper of a base: "", a … z, aa, ab … (bijective base 26)."""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


def _citation_keys(corpus: "Corpus") -> dict[str, str]:
    """Every record's citation key. Papers sharing a base are numbered
    in the order they entered the run, `(discovered_round, identifier)`, over every pool: records
    are never deleted and a new one is never from an earlier round, so an exported key keeps its
    paper when the run grows or verdicts change. A key that is another base's is skipped."""
    records = [*corpus.greenlist.values(), *corpus.uncertain.values(), *corpus.excluded.values()]
    taken: set[str] = set()
    keys: dict[str, str] = {}
    for rec in sorted(records, key=lambda r: (r.provenance.discovered_round, r.doi)):
        base, n = _key_base(rec), 0
        while (key := base + _suffix(n)) in taken:
            n += 1
        keys[rec.doi] = key
        taken.add(key)
    return keys


def _bib(corpus: "Corpus") -> str:
    entries = []
    keys = _citation_keys(corpus)
    for rec in sorted(corpus.greenlist.values(), key=lambda r: (r.year or 0, r.title)):
        # The year and detail verification stored; nothing is looked up here.
        year, d = rec.year, rec.bibliographic
        key = keys[rec.doi]
        kind, venue_field = _BIB_TYPE.get((rec.type or "").lower(), ("misc", "howpublished"))
        # An empty name is left out (`A and  and B` makes biber drop the entry).
        names = ([x for x in map(bib_author, d.author_names) if x]
                 or [x for x in map(bib_author, rec.authors) if x])
        esc = lambda v: bibtex_escape(v) if v else ""
        fields = [("author", " and ".join(names)),
                  ("title", "{" + bibtex_escape(rec.title) + "}"),
                  (venue_field, bibtex_escape(rec.venue) if rec.venue and venue_field != "publisher"
                   else ""),
                  ("year", str(year) if year else ""),
                  ("date", _bib_date(d.publication_date, year)),
                  # The detail a citation needs, for the types that carry it.
                  ("volume", esc(d.volume) if kind in ("article", "inproceedings") else ""),
                  ("number", esc(d.issue) if kind == "article" else ""),
                  ("pages", esc(_pages(d.pages)) if kind != "book" else ""),
                  # No fallback to the venue: for a book found only in a repository the
                  # venue is the repository, a wrong publisher; and a record only OpenAlex
                  # holds names its host as the publisher, which is no publisher either.
                  ("publisher", esc(d.publisher) if not rec.doi.startswith("openalex:") else ""),
                  ("issn", ", ".join(d.issn) if kind == "article" else ""),
                  ("shortjournal", esc(d.journal_abbrev) if kind == "article" else ""),
                  ("langid", esc(d.language))]
        if rec.doi.startswith("openalex:"):
            fields.append(("url", f"https://openalex.org/{rec.doi.split(':', 1)[1]}"))
        else:
            fields.append(("doi", rec.doi))
        if note := _flags(d):
            fields.append(("note", bibtex_escape(note)))
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
        entries.append(f"@{kind}{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _papers(corpus: "Corpus", lib: "Library | None" = None) -> list[list[Any]]:
    out = []
    for rec in sorted(corpus.greenlist.values(), key=lambda r: (r.year or 0, r.title)):
        d = rec.bibliographic
        seed = rec.provenance.discovered_by is DiscoveredBy.SEED
        ruled = rec.relevance.decided_by == RULING_DECIDED_BY
        row = [rec.title, _authors(rec), rec.year or "",
               rec.venue or "", rec.doi,
               _link(rec), rec.oa_status or "", _source(rec), _found_by(rec),
               "supplied by you" if seed else
               (f"ruled in by you: {rec.relevance.agent_reason}" if ruled
                else rec.relevance.agent_reason or ""),
               "yes" if rec.relevance.judged_without_abstract else "",
               # Bibliographic detail, and flags with their source.
               d.volume or "", d.issue or "", d.pages or "", d.publication_date or "",
               d.language or "", d.publisher or "",
               f"yes (according to {d.retraction_source or 'the registry'})"
               if d.is_retracted else "",
               ", ".join(d.corrections)]
        if lib is not None:
            row.append("yes" if _in_library(rec, lib) else "")
        out.append(row)
    return out


def _excluded(corpus: "Corpus") -> list[list[Any]]:
    from .summary import CriterionText
    from .years import RULE_DECIDER

    criterion = CriterionText(corpus.run_dir)
    out = []
    for rec in sorted(corpus.excluded.values(), key=lambda r: (r.year or 0, r.title)):
        rel = rec.relevance
        by = {RULE_DECIDER: "year limits (rule)",
              RULING_DECIDED_BY: "you (ruling)"}.get(rel.decided_by, "judge")
        out.append([rec.title, _authors(rec), rec.year or "", rec.venue or "", rec.doi, by,
                    criterion.exclusion(rel.criterion, rel.config_generation),
                    rel.agent_reason or "", len(rel.history)])
    return out


def _uncertain(corpus: "Corpus") -> list[list[Any]]:
    return [[rec.title, _authors(rec), rec.year or "", rec.venue or "", rec.doi,
             rec.relevance.agent_reason or "not yet judged"]
            for rec in sorted(corpus.uncertain.values(), key=lambda r: (r.year or 0, r.title))]


def _records(corpus: "Corpus") -> str:
    recs = [{"pool": pool, **rec.model_dump(mode="json")}
            for pool, recs in (("greenlist", corpus.greenlist), ("uncertain", corpus.uncertain),
                               ("excluded", corpus.excluded))
            for rec in sorted(recs.values(), key=lambda r: r.doi)]
    recs += [{"pool": "local", **rec.model_dump(mode="json")}
             for _id, rec in sorted(corpus.local_records.items())]
    return json.dumps(recs, indent=2, sort_keys=True)


def _rejections(corpus: "Corpus", config: "Config") -> str:
    from .loop import REJECTION_BINS, rejection_bin

    near = config.matching.title_moderate
    out = []
    for e in corpus.audit_events:
        if e.command != "submit":
            continue
        for d in e.decisions:
            key = rejection_bin(d, near)
            if key is not None:
                out.append({"round": e.round, "bin": REJECTION_BINS[key], **d})
    return json.dumps(out, indent=2, sort_keys=True) if out else ""


def _redact(text: str, emails: set[str]) -> str:
    """Every contact address masked, and the home directory (the user's name) written `~`,
    in every spelling a YAML or JSON file can hold."""
    for email in sorted(emails, key=len, reverse=True):
        text = text.replace(email, "<redacted>")
    for home in _home_forms():
        text = text.replace(home, "~")
    return text


def _home_forms() -> list[str]:
    home = Path.home()
    return sorted({str(home), home.as_posix(), str(home).replace("\\", "\\\\")},
                  key=len, reverse=True)


def _contact_emails(run_dir: Path, corpus: "Corpus", config: "Config") -> set[str]:
    """Every contact address the run has used: the current one, the environment's, and each
    configuration generation's own, since a steer can change it."""
    from .config import CONTACT_EMAIL_ENV, generation_path

    emails = {config.contact_email, os.environ.get(CONTACT_EMAIL_ENV)}
    for g in range((corpus.config_generation or 0) + 1):
        src = generation_path(run_dir, g)
        if src.is_file():
            data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
            emails.add(data.get("contact_email") if isinstance(data, dict) else None)
    return {e.strip() for e in emails if isinstance(e, str) and e.strip()}


def _criteria(run_dir: Path, corpus: "Corpus", dest: Path, emails: set[str]) -> None:
    """Each generation, with every contact address masked and its original checksum."""
    from .config import generation_path

    sums: dict[str, str] = {}
    for g in range((corpus.config_generation or 0) + 1):
        src = generation_path(run_dir, g)
        if src.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            raw = src.read_bytes()
            sums[f"gen{g:03d}.yaml"] = hashlib.sha256(raw).hexdigest()
            _text(dest / f"gen{g:03d}.yaml", _redact(raw.decode("utf-8"), emails))
    if sums:
        _text(dest / "checksums.json", json.dumps(sums, indent=2))


def _judge_prompts(corpus: "Corpus", dest: Path) -> None:
    from .loop import JUDGE_PROMPT_FILENAME

    index: dict[str, list[int]] = {}
    for r in sorted(corpus.completed_rounds):
        p = corpus.round_dir(r) / JUDGE_PROMPT_FILENAME
        if not p.is_file():
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest not in index:
            _text(dest / f"{digest[:12]}.md", p.read_text(encoding="utf-8"))
        index.setdefault(digest, []).append(r)
    if index:
        _text(dest / "index.json", json.dumps(index, indent=2))


def _reference_candidates(corpus: "Corpus", dest: Path) -> None:
    """Each round's `references` listing, as `NNN.md` and `NNN.json`."""
    from .references import CANDIDATES_NAME

    for r in sorted(corpus.completed_rounds):
        for suffix in (".md", ".json"):
            p = corpus.round_dir(r) / f"{CANDIDATES_NAME}{suffix}"
            if p.is_file():
                _text(dest / f"{r:03d}{suffix}", p.read_text(encoding="utf-8"))


def _n(count: int, singular: str, plural: str | None = None) -> str:
    """`1 round`, `3 rounds` — text a scientist may paste into a manuscript."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _list(xs: list) -> str:
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else "none"


_NOT_VERIFIED_LABELS = [
    ("doi_does_not_resolve", "DOI resolves to nothing"),
    ("doi_belongs_to_another_paper", "DOI belongs to another paper"),
    ("title_near_a_registered_one", "title near a registered one"),
    ("no_similar_title_registered", "nothing similar registered"),
    ("title_search_similarity_not_recorded", "title search, similarity not recorded"),
    ("duplicate", "refused as a duplicate"),
    ("out_of_scope_type", "out-of-scope type"),
    ("malformed_doi", "malformed DOI"),
    ("registry_gave_up", "registry unavailable, gave up after retries"),
    ("registry_query_rejected", "registry refused the tool's query"),
    ("malformed_entry", "malformed entry"),
]


def _found_line(f: dict[str, Any]) -> str:
    """The header's count of what the tool found, rulings apart."""
    if not f.get("ruled_in"):
        return f"{f['included']} found and screened by the tool"
    return (f"{f['included'] + f['ruled_in']} found by the tool ({f['included']} included by "
            f"the judge, {f['ruled_in']} by your ruling)")


def _flow_table(f: dict[str, Any]) -> str:
    # Rulings get their own row, so "included" is the judge's alone.
    ruled = f.get("ruled_in", 0) + f.get("ruled_out", 0)
    seeds_out = f.get("seeds_ruled_out", 0)
    rows = [("supplied by you (seeds)", f["seeds"], "included without screening"
             + (f"; {seeds_out} more you later ruled out" if seeds_out else "")),
            ("generated by the model", f["generated"], ""),
            ("  not verified", f["not_verified_total"], "")]
    rows += [(f"    {label}", f["not_verified"][k], "")
             for k, label in _NOT_VERIFIED_LABELS if f["not_verified"].get(k)]
    if f["awaiting_retry"]:
        rows.append(("  awaiting a retry (registry error)", f["awaiting_retry"], ""))
    rows += [("  verified", f["verified"], ""),
             ("    excluded by the year limits (rule)", f["excluded_by_year_rule"], ""),
             ("    screened by the judge", f["screened"], ""),
             ("      included", f["included"], ""),
             ("      excluded", f["excluded_by_judge"], ""),
             ("      undecided", f["undecided"], ""),
             *([("      ruled on by you", ruled,
                 f"({f.get('ruled_in', 0)} in, {f.get('ruled_out', 0)} out)")] if ruled else []),
             ("    awaiting judgment", f["awaiting_judgment"], ""),
             ("corpus", f["corpus"],
              (f"({f['seeds']} supplied by you + {f['included']} included by the judge + "
               f"{f['ruled_in']} ruled in by you)") if ruled else
              f"({f['seeds']} supplied by you + {f['included']} screened)")]
    if f["unaccounted"]:
        rows.append(("UNACCOUNTED -- see audit/rejections.json", f["unaccounted"], ""))
    return "\n".join(f"{label:<46}{n:>5}   {note}".rstrip() for label, n, note in rows)


def _span(s: dict[str, Any]) -> str:
    """`on <date>`, or `between A and B`; a same-day run is not a range."""
    first, last = ((s["dates"][k] or "")[:10] for k in ("first", "last"))
    return f"on {first}" if first == last else f"between {first} and {last}"


def _methods(s: dict[str, Any]) -> str:
    f, m = s["flow"], s["models"]
    from .summary import AGENT_LABEL, USER_LABEL

    # Provenance is counted over everything the judge applies, notes included. A per-paper
    # ruling is counted apart; `steer` applies it, not a judge.
    crit = [c for c in s["criteria"] if c["kind"] != "judging note"]
    notes = [c for c in s["criteria"] if c["kind"] == "judging note" and not c.get("ruling")]
    n_user = sum(1 for c in crit + notes if c["provenance"] == USER_LABEL)
    n_agent = sum(1 for c in crit + notes if c["provenance"] == AGENT_LABEL)
    tail = s["supply"]["offered_per_round"][-3:]
    pending = ("" if not s["rejudge_pending"] else
               f" (the re-judge after the last steer was still pending for "
               f"{_n(s['rejudge_pending'], 'paper')} at export)")
    if f["awaiting_judgment"]:
        pending += (f"; {_n(f['awaiting_judgment'], 'paper')} "
                    f"{'was' if f['awaiting_judgment'] == 1 else 'were'} still awaiting "
                    f"judgment at export")
    who = ("Verified candidates within the publication-year limits, which were applied by "
           "rule rather than by the model, were" if s["topic"]["year_limits"] else
           "Verified candidates were")
    # Missing data is said, not printed as a number: a run may lack the per-round supply
    # count or the model identity.
    def model(names: list[str]) -> str:
        return (f"self-reported as {_list(names)}" if names else
                "its identity was not recorded")
    unverified = (f"{f['not_verified_total']} of {f['generated']} could not be verified and "
                  f"were discarded" if f["generated"] else
                  f"{f['not_verified_total']} could not be verified and were discarded")
    supply = (f", and in the final {_n(len(tail), 'round')} the model offered {_list(tail)} "
              f"papers per round" if tail else "")
    origin = ("the model's recall and from the corpus's own reference lists"
              if s.get("references_from_round") is not None else "the model's recall")
    return (
        f"Candidate papers were generated by a language model ({model(m['proposer'])}) over "
        f"{_n(s['rounds'], 'round')} {_span(s)}. Each candidate was verified "
        f"against the OpenAlex and Crossref registries with PaperRaven {s['tool_version']}; "
        f"{unverified}. {who} screened for relevance{pending} by a separate model instance "
        f"({model(m['judge'])}) against "
        f"{_n(len(crit), 'criterion', 'criteria')} and {_n(len(notes), 'judging note')}. "
        f"Of these {len(crit) + len(notes)}, {n_user} "
        f"{'was' if n_user == 1 else 'were'} the author's own (stated or signed off by the "
        f"author) and {n_agent} {'was' if n_agent == 1 else 'were'} written by the tool. "
        f"{_rulings_sentence(f)}"
        f"{_n(f['seeds'], 'paper')} supplied by the author "
        f"{'was' if f['seeds'] == 1 else 'were'} included without screening. The corpus "
        f"contains {_n(f['corpus'], 'paper')}. This was not an exhaustive database search: "
        f"candidates came from {origin}{supply}.{_found_by_sentence(s)}")


def _rulings_sentence(f: dict[str, Any]) -> str:
    """Say that the author ruled on papers directly, when they did."""
    n_in, n_out = f.get("ruled_in", 0), f.get("ruled_out", 0)
    if not n_in + n_out:
        return ""
    return (f"The author also ruled on {_n(n_in + n_out, 'paper')} directly ({n_in} in, "
            f"{n_out} out); those rulings override the judge's verdict. ")


def _found_by_sentence(s: dict[str, Any]) -> str:
    """The proposer's self-reported split by source, when any was reported."""
    fb = s.get("found_by") or {}
    recall, search, refs, unstated = (fb.get(k, 0) for k in
                                      ("recall", "search", "references", "not_stated"))
    if not recall and not search and not refs:
        return ""
    total = recall + search + refs + unstated
    tail = (f"; {unstated} did not say" if unstated else "")
    if not refs:
        return (f" Of the {total} candidates generated, {recall} came from the model's recall "
                f"and {search} from web searches it ran during the run (self-reported){tail}.")
    return (f" Of the {total} candidates generated, {recall} came from the model's recall, "
            f"{search} from web searches it ran during the run and {refs} from the reference "
            f"lists of the corpus's own papers (OpenAlex), from round "
            f"{s['references_from_round']} on (self-reported){tail}.")


def _readme(s: dict[str, Any], dest: Path) -> str:
    f = s["flow"]
    has = lambda name: (dest / name).exists()
    files = [("papers.csv", "every paper in the corpus, one row each, with why it was included"),
             ("papers.bib", "the bibliography"),
             ("worklist.csv", "how to get each PDF, open access first. `Already present` marks "
                              "a PDF already in the folder given with `--pdf-dir`"),
             ("excluded.csv", "what the criteria cost: each excluded paper, who excluded it, "
                              "under which criterion and why. Exclusions close to the "
                              "criteria's boundary are the least reproducible verdicts the "
                              "tool makes; review them before relying on them"),
             ("uncertain.csv", "papers the judge could not decide; yours to rule on"),
             ("citation_network.html", "the citations among the corpus papers, as a network "
                                       "to explore in a browser; it works offline"),
             ("citations.csv", "the same citations as an edge list (citing, cited), keyed "
                               "as in `papers.bib`, for Gephi, networkx or R. From OpenAlex "
                               "reference lists, which some papers lack")]
    lines = [f"# Literature corpus — {_n(f['corpus'], 'paper')}", "",
             f"{_n(f['corpus'], 'paper')}: {f['seeds']} you supplied, {_found_line(f)}. "
             f"Produced by PaperRaven {s['tool_version']}, "
             f"{_span(s)}.", ""]
    if s["rejudge_pending"]:
        lines += [f"**{_n(s['rejudge_pending'], 'paper')} still awaiting re-judging after a "
                  f"steer**, so their verdicts here were made under the earlier criteria.", ""]
    # Retracted and corrected papers: flagged, never removed, always with the registry's name.
    if s.get("retracted") or s.get("corrected"):
        lines += ["## Retracted or corrected papers", "",
                  "These stay in the corpus; decide for yourself whether to cite them. The status "
                  "is the registry's, not the tool's finding, as of verification, and a "
                  "registry can lag.", ""]
        lines += [f"- **Retracted** (according to {src or 'the registry'}): {t}"
                  for t, src in s.get("retracted", [])]
        lines += [f"- **Has a correction** ({notes}): {t}" for t, notes in s.get("corrected", [])]
        lines.append("")
    lib = s.get("library")
    if lib:
        is_are = lambda n: "is" if n == 1 else "are"
        lines += ["## Your library", "",
                  f"Compared with `{Path(lib['file']).name}`: {lib['new']} {is_are(lib['new'])} "
                  f"new to you; {lib['in_library']} {is_are(lib['in_library'])} already in your "
                  f"library (the `In your library` column in `papers.csv`). "
                  f"{_n(lib['without_doi'], 'library entry', 'library entries')} had no DOI "
                  f"and {'was' if lib['without_doi'] == 1 else 'were'} not compared.", ""]
    lines += ["## Files", ""]
    lines += [f"- `{n}` — {d}." for n, d in files if has(n)]
    if has("citations.csv") and not has("citation_network.html"):
        lines.append("- The citation network is too large to draw well in a browser, so it is "
                     "exported only as `citations.csv`; Gephi opens it directly.")
    lines += ["- `audit/` — everything behind the corpus, for checking it; see "
              "`audit/README.md`.", "", "## The topic", ""]
    if s["topic"]["user_brief"]:
        lines += ["Your own words:", "", f"> {s['topic']['user_brief']}", ""]
    if s["topic"]["description"]:
        lines += ["The description the judge used:", "", f"> {s['topic']['description']}", ""]
    lines += ["## The criteria", ""]
    for kind, title in (("inclusion", "In scope"), ("exclusion", "Out of scope"),
                        ("judging note", "Judging notes")):
        items = [c for c in s["criteria"] if c["kind"] == kind]
        if items:
            lines += [f"**{title}**", ""]
            lines += [f"- {c['code']}. {c['text']} — {c['provenance']}" for c in items]
            lines.append("")
    if s["topic"]["year_limits"]:
        lines += [f"**Year limits:** {s['topic']['year_limits']} — applied by rule, not by the "
                  f"judge, against every publication date the registries state.", ""]
    m = s["models"]
    lines += ["## How it was produced", "",
              f"- {_n(s['rounds'], 'round')}, {_n(s['epoch'] + 1, 'epoch')}, final "
              f"configuration generation {s['config_generation']}.",
              f"- Models (self-reported, unverified): proposer "
              f"{_list(m['proposer']) if m['proposer'] else 'not recorded'}; judge "
              f"{_list(m['judge']) if m['judge'] else 'not recorded'}."]
    t = s["target"]
    lines.append(f"- Target: {t['found']} found of a target of {t['target_papers']}"
                 + (f", plus {_n(t['seeds'], 'seed')} of your own." if t["seeds"] else "."))
    if (first := s.get("references_from_round")) is not None:
        lines.append(f"- From round {first}, candidates were also proposed from the reference "
                     f"lists of the corpus's papers (OpenAlex, through `references`): "
                     f"{s['found_by'].get('references', 0)} proposed, "
                     f"{s['references_in_corpus']} of them in the corpus, and "
                     f"{_n(s.get('references_declined', 0), 'listed candidate')} "
                     f"{'was' if s.get('references_declined', 0) == 1 else 'were'} declined by "
                     f"the proposer. The listings are in `audit/reference_candidates/`."
                     + _decline_reasons_lines(s.get("references_decline_reasons") or []))
    if s["steers"]:
        # Steers after the same round share one bullet.
        by_round: dict[int, list[dict[str, Any]]] = {}
        for st in s["steers"]:
            by_round.setdefault(st["after_round"], []).append(st)
        for after, sts in by_round.items():
            times = {1: "", 2: "twice ", 3: "three times "}.get(len(sts), f"{len(sts)} times ")
            changes = "; ".join(ch for st in sts for ch in st["changes"])
            lines.append(f"- Steered {times}after round {after}: {changes}. "
                         + " ".join(_steer_outcome(st) for st in sts))
    else:
        lines.append("- Not steered.")
    lines += ["", "## What came of it", "", "```", _flow_table(f), "```", ""]
    if unresolved := s.get("unresolved_seed_files"):
        # A seed the registries could not match is the user's paper, so it is named.
        lines += [f"**{_n(len(unresolved), 'seed file')} could not be matched to a registry "
                  f"record** and {'is' if len(unresolved) == 1 else 'are'} not in the corpus "
                  f"files: {_list(unresolved)}. Resolve each with `seed` to include it.", ""]
    if s["excluded_by_criterion"]:
        lines += ["**Exclusions by criterion** (the judge's stated basis):", ""]
        lines += [f"- {k} — {_n(n, 'paper')}" for k, n in s["excluded_by_criterion"].items()]
        lines.append("")
    lines += [_unverified_sentence(f)
              + (f" {_n(s['doi_recovered_by_title'], 'proposed DOI')} failed, and the title "
                 f"search found the paper anyway." if s.get("doi_recovered_by_title") else ""),
              "",
              "## Methods (drafted by the tool — adapt before use)", "",
              f"> {_methods(s)}", "",
              "## Limitations", "",
              "- This was not an exhaustive database search. Candidates came from a language "
              "model's recall"
              + (" and from the corpus's own reference lists" if s.get("references_from_round")
                 is not None else "")
              + "; the registries verified that each exists, not that none is missing.",
              # True of every run, so the tool says it.
              "- The model's recall is thinnest for work published close to or after the "
              "model's training cutoff, so the most recent work is likely under-represented; "
              "back it with a database search if recency matters.",
              *(["- Some candidates from the reference lists were declined by the proposer for "
                 "reasons of its own, which may lie outside the criteria; its reasons are under "
                 "How it was produced."]
                if any(g["reason"] for g in s.get("references_decline_reasons") or []) else []),
              _stop_line(s),
              *_seeds_line(s),
              *_europe_pmc_line(s),
              f"- {_n(s['judged_without_abstract'], 'judged paper')} had no registry abstract; "
              f"the judge was told to look each one up.",
              _YEARS_LINE]
    return "\n".join(lines) + "\n"


def _rounds_text(rounds: list[int]) -> str:
    """`round 7`, or `rounds 3–5, 9`: consecutive rounds as ranges."""
    spans: list[list[int]] = []
    for r in sorted(rounds):
        if spans and r == spans[-1][1] + 1:
            spans[-1][1] = r
        else:
            spans.append([r, r])
    text = ", ".join(f"{a}–{b}" if b > a else f"{a}" for a, b in spans)
    return f"{'round' if len(rounds) == 1 else 'rounds'} {text}"


def _md_text(text: str) -> str:
    """Model text shown as text: Markdown's and HTML's special characters escaped."""
    return "".join("\\" + c if c in "\\`*_[]<>#|" else c for c in text)


#: The most reasons for declines the README quotes. A proposer rewords its reason round by
#: round, so the rest are counted and pointed to, not listed.
SHOWN_REASONS = 3


def _steer_outcome(st: dict[str, Any]) -> str:
    if st["rejudge_marked"]:
        return (f"The re-judge moved {st['moved_in']} in, {st['moved_out']} out"
                + (f", {st['moved_other']} between excluded and undecided"
                   if st.get("moved_other") else "") + ".")
    if st.get("ruled"):
        return f"{_n(st['ruled'], 'paper')} ruled on by you; nothing else was re-judged."
    return "No re-judge was run."


def _stop_line(s: dict[str, Any]) -> str:
    """The stop signal in plain words: the reasons when it fired, one line when it had not."""
    if s["stopped"]:
        return f"- The run stopped: {'; '.join(s['stop_reasons'])}."
    return "- No stop condition had been reached at export."


def _decline_reasons_lines(groups: list[dict[str, Any]]) -> str:
    """The proposer's reasons for its declines, by rounds, quoted as its own words: the ones
    covering the most declines, in round order, then where the rest are."""
    reasons = [g for g in groups if g["reason"]]
    if not reasons:
        return ""
    top = sorted(range(len(reasons)), key=lambda i: -reasons[i]["declined"])[:SHOWN_REASONS]
    shown = [reasons[i] for i in sorted(top)]
    rest = [g for g in reasons if g not in shown]
    lines = [" The proposer's reasons, in its own words:"]
    lines += [f"  - {_rounds_text(g['rounds'])} ({g['declined']} declined): "
              f"\"{_md_text(g['reason'])}\"" for g in shown]
    if rest:
        lines.append(f"  - {len(rest)} more reason{'s' if len(rest) != 1 else ''} "
                     f"({sum(g['declined'] for g in rest)} declined) "
                     f"{'are' if len(rest) != 1 else 'is'} in `audit/rounds.jsonl`, as "
                     f"`decline_reason`.")
    for g in groups:
        if not g["reason"]:
            lines.append(f"  - {_n(len(g['rounds']), 'round')} gave no reason "
                         f"({g['declined']} declined).")
    return "\n".join(lines)


def _unverified_sentence(f: dict[str, Any]) -> str:
    """What became of the proposals that were not verified, naming every bin that holds any."""
    held = [f"{label} ({f['not_verified'][k]})" for k, label in _NOT_VERIFIED_LABELS
            if f["not_verified"].get(k)]
    if not held:
        return "Every proposal the model generated was verified; nothing unverified entered the corpus."
    return (f"Of the proposals that were not verified: {'; '.join(held)}. None of these is proof "
            f"that a paper was invented, and nothing unverified entered the corpus.")


def _europe_pmc_line(s: dict[str, Any]) -> list[str]:
    """Say how many abstracts came from Europe PMC rather than the verifying registry."""
    n = s.get("abstracts_from_europe_pmc") or 0
    if not n:
        return []
    return [f"- {_n(n, 'abstract')} came from Europe PMC, looked up because the verifying "
            f"registry had none."]


def _seeds_line(s: dict[str, Any]) -> list[str]:
    so = s.get("seeds_outside_year_limits") or {}
    n, m = so.get("count", 0), so.get("of", 0)
    if not n:
        return []
    years = so.get("years") or []
    span = f"{years[0]}-{years[-1]}" if len(years) > 1 else f"{years[0]}"
    return [f"- {n} of your {m} seed{'s' if m != 1 else ''} "
            f"{'falls' if n == 1 else 'fall'} outside the year limits and "
            f"{'is' if n == 1 else 'are'} kept, because seeds are exempt ({span}): "
            f"`papers.bib` spans those years too."]


_YEARS_LINE = ("- Years are the registry's publication year (for most papers the earliest, usually "
               "online, date). `papers.bib` is UTF-8 — use biber, or convert for 8-bit BibTeX.")


def _changes(s: dict[str, Any]) -> str:
    if not s["steers"]:
        return ""
    lines = ["# Changes between configuration generations", ""]
    lines += [f"- **gen{st['generation']:03d}**, after round {st['after_round']}: "
              f"{'; '.join(st['changes'])}. "
              + (f"{st['rejudge_marked']} records marked for re-judging; the re-judge moved "
                 f"{st['moved_in']} in, {st['moved_out']} out." if st["rejudge_marked"] else
                 "No re-judge was run.")
              for st in s["steers"]]
    return "\n".join(lines) + "\n"


PAPERS_HEADER = ["Title", "Authors", "Year", "Venue", "DOI", "Link", "Open access",
                 "Source", "Found by", "Why included", "No registry abstract",
                 "Volume", "Issue", "Pages", "Publication date", "Language", "Publisher",
                 "Retracted", "Corrections"]
EXCLUDED_HEADER = ["Title", "Authors", "Year", "Venue", "DOI", "Excluded by", "Criterion",
                   "Reason", "Earlier verdicts"]
UNCERTAIN_HEADER = ["Title", "Authors", "Year", "Venue", "DOI", "Why undecided"]
CITATIONS_HEADER = ["Citing", "Cited"]


def write_export(corpus: "Corpus", config: "Config", run_dir: Path, dest: Path,
                 *, pdf_dir: str | Path | None = None,
                 library: str | Path | None = None,
                 reference_lists: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the deliverable. Returns the files written and the run summary.

    Nothing here touches the network: every field is what verification stored, and the
    citation network is drawn from `reference_lists` (`references.reference_lists`, from the
    records alone), or left out when it is `None` or finds no citation among the papers.
    """
    from .summary import run_summary
    from .worklist import worklist_summary, write_worklist

    dest = Path(dest)
    previous = _previous_manifest(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # A stub manifest first, so an export that crashes part-way still marks the folder
    # as the tool's and the next export is not refused. The full manifest is written last.
    (dest / "audit").mkdir(parents=True, exist_ok=True)
    if previous is None:
        (dest / "audit" / "manifest.json").write_text("{}\n", encoding="utf-8")
    _WRITTEN.clear()
    summary = run_summary(corpus, config)
    # Retractions and corrections as recorded at verification.
    records = list(corpus.greenlist.values())
    summary["retracted"] = sorted((r.title, r.bibliographic.retraction_source or "")
                                  for r in records if r.bibliographic.is_retracted)
    summary["corrected"] = sorted((r.title, ", ".join(r.bibliographic.corrections))
                                  for r in records if r.bibliographic.corrections)
    lib = parse_library(library) if library else None
    if lib is not None:
        known = sum(1 for rec in corpus.greenlist.values() if _in_library(rec, lib))
        summary["library"] = {"file": str(library), "in_library": known,
                              "new": len(corpus.greenlist) - known,
                              "entries": lib.entries, "without_doi": lib.without_doi}
    audit = dest / "audit"
    emails = _contact_emails(Path(run_dir), corpus, config)
    files: dict[str, Path | None] = {
        "papers": _csv(dest / "papers.csv",
                       PAPERS_HEADER + (["In your library"] if lib is not None else []),
                       _papers(corpus, lib)),
        "bib": _text(dest / "papers.bib", _bib(corpus)),
        "excluded": _csv(dest / "excluded.csv", EXCLUDED_HEADER, _excluded(corpus)),
        "uncertain": _csv(dest / "uncertain.csv", UNCERTAIN_HEADER, _uncertain(corpus)),
        "audit_readme": _text(audit / "README.md", AUDIT_README),
        "records": _text(audit / "records.json", _redact(_records(corpus), emails)),
        "flow": _text(audit / "flow.json", json.dumps(summary, indent=2, default=str)),
        "rejections": _text(audit / "rejections.json", _rejections(corpus, config)),
    }
    rounds_log = Path(run_dir) / "rounds.jsonl"
    if rounds_log.is_file():
        files["rounds"] = _text(audit / "rounds.jsonl", rounds_log.read_text(encoding="utf-8"))
    _criteria(Path(run_dir), corpus, audit / "criteria", emails)
    # The whole record, with the contact address masked as in `criteria/`.
    files["events"] = _text(audit / "events.jsonl", _redact("".join(
        e.model_dump_json() + "\n" for e in corpus.audit_events), emails))
    files["retry_queue"] = _text(audit / "retry_queue.json",
                                 json.dumps(corpus.retry_queue, indent=2, default=str))
    files["changes"] = _text(audit / "criteria" / "changes.md", _changes(summary))
    _judge_prompts(corpus, audit / "judge_prompts")
    _reference_candidates(corpus, audit / "reference_candidates")
    if reference_lists is not None:
        from .network import DRAW_LIMIT, build_network, citation_rows, network_html
        net = build_network(corpus, reference_lists)
        if net["edges"]:                  # no citation among the papers: nothing to draw
            if len(net["nodes"]) <= DRAW_LIMIT:
                files["network"] = _text(dest / "citation_network.html",
                                         network_html(net, title=Path(run_dir).name))
            files["citations"] = _csv(dest / "citations.csv", CITATIONS_HEADER,
                                      citation_rows(net))

    if corpus.greenlist:                 # a header-only worklist is an empty file
        wl_path, wl_rows = write_worklist(corpus, dest / "worklist.csv", pdf_dir=pdf_dir)
        files["worklist"] = wl_path
        _WRITTEN.append(Path(wl_path))
        worklist = worklist_summary(wl_rows)
    else:
        worklist = "worklist: nothing to acquire -- the corpus is empty"
    # Stale files go before the README, whose file guide lists what the folder holds.
    written = {_relpath(p, dest) for p in _WRITTEN if p} | {"README.md"}
    removed, kept = _remove_stale(dest, previous or {}, written)
    # Last, so its file guide lists only what was actually written.
    files["readme"] = _text(dest / "README.md", _readme(summary, dest))
    from .closeout import write_manifest
    write_manifest(dest, _WRITTEN)
    return {"dest": str(dest), "files": {k: str(v) for k, v in files.items() if v},
            "worklist": worklist, "summary": summary,
            "removed_stale": removed, "kept_edited": kept}


#: The files `export` writes at the top of its folder; a folder holding any of them and no
#: manifest is not taken to be an earlier export.
_EXPORT_NAMES = ("README.md", "papers.csv", "papers.bib", "worklist.csv", "excluded.csv",
                 "uncertain.csv")


def _relpath(path: Path, root: Path) -> str:
    return Path(path).relative_to(root).as_posix()


def _previous_manifest(dest: Path) -> dict[str, str] | None:
    """The previous export's manifest, `None` if there is none; refuses a foreign folder."""
    path = dest / "audit" / "manifest.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}
    clash = [n for n in _EXPORT_NAMES if (dest / n).exists()]
    if clash:
        raise ValueError(
            f"{dest} is not a PaperRaven export (no audit/manifest.json) but holds "
            f"{', '.join(clash)}, which `export` would overwrite. Pass an empty or new folder "
            f"with --dest, or move those files first.")
    return None


def _remove_stale(dest: Path, previous: dict[str, str], written: set[str]
                  ) -> tuple[list[str], list[str]]:
    """Remove files the previous export wrote and this one did not, unless the user edited them.

    Without this, ruling in every undecided paper and exporting again would leave the old
    `uncertain.csv` behind. Only the previous export's own files are candidates, and only
    unedited ones (hash as recorded); nothing else is touched.
    """
    removed, kept = [], []
    for name, digest in sorted(previous.items()):
        path = dest / name
        if name in written or name == "audit/manifest.json" or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            path.unlink()
            removed.append(name)
        else:
            kept.append(name)
    return removed, kept
