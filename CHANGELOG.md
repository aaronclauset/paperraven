# Changelog

All notable changes to PaperRaven are listed here, newest first.

## 0.2.0

**Added**
- `citing`: late in a run, PaperRaven can now generate proposals from the works that cite the
  corpus. This feature helps fill in more recent work on a topic.
- `SECURITY.md`.

**Changed**
- The demo deck follows a new run, using all four sources.

**Fixed**
- `fetch` no longer puts seed papers on the click-list: `export` copies the seed PDFs into
  `pdfs/`.
- A code comment described a setting the release no longer has.

## 0.1.0 — first release

- **Corpus building in Claude Code** (`/paperraven`): an interview that turns your topic and
  seed papers into criteria you approve, then rounds in which the LLM proposes papers, the code
  verifies each against OpenAlex and Crossref, and a separate judge applies your criteria.
- **Steering mid-run:** change the criteria, or rule on a named paper, and the papers already
  judged are re-judged; PaperRaven says how many first.
- **Proposing from the corpus's reference lists** (`references`): once the LLM's own supply
  thins, and with your OK, PaperRaven ranks the works the corpus's papers cite that are not yet
  in it, from OpenAlex, and the LLM proposes from that list; the export says how many came that
  way.
- **Export:** `papers.bib`, `papers.csv`, `worklist.csv`, `excluded.csv`, `uncertain.csv`, a
  README with a flow table and a methods paragraph, and a full audit trail with checksums.
- **The citation network** among the corpus papers: `citation_network.html`, interactive and
  self-contained, and `citations.csv`, an edge list.
- **Close-out:** a check of the export and the session's scratchpad, a read-only review, and
  the run's token usage per paper.
- **`fetch`:** the PDFs for any `.bib`, fetched politely, with a click-list for the rest and
  `--collect` to file your own downloads.
- **`paperraven install-skill`** and **`paperraven --version`**.
