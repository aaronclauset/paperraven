---
name: paperraven
description: Build a verified literature corpus from a topic description and a few seed PDFs, or fetch the PDFs for any .bib file. Drives the PaperRaven round loop — you propose papers, Python verifies them against OpenAlex/Crossref, you judge relevance — and hands the user a corpus plus a PDF-acquisition worklist; its fetch command then downloads what it politely can and lists the rest. Use when the user wants a systematic literature search, a paper corpus, "find me the papers on X", or "get the PDFs for my references".
---
<!-- paperraven {{version}} -->

# PaperRaven

You are the LLM in a generate–verify loop. **You generate; deterministic code verifies.**
You propose candidate papers and judge relevance. Python owns every piece of state, checks
every DOI against the registries, and refuses anything it cannot confirm. You never edit
state files by hand and never call a model API — you *are* the model.

The complete procedure is `.claude/skills/paperraven/INSTRUCTIONS.md`, beside this file. This
file is the short form.

## Setup, once

Work from the folder the skill is installed in (the one holding `.claude/`). Check:

```bash
{{python}} -m paperraven check
```

`check` makes no network call. **Tell the user its OpenAlex line before anything else.** If
no key was found, say that OpenAlex's keyless pool allows about 100 searches a day, which one
run can use up, and that they can add a free key now: `OPENALEX_API_KEY=<key>` in a `.env` file in this directory. Never
ask for the key in the chat, and never read, print or copy `.env` yourself.

If that fails, or `check` says the installed skill does not match the tool, do not install
anything yourself. Ask the user to reinstall, in the environment they installed PaperRaven in:
`pip install -U git+https://github.com/aaronclauset/paperraven`, then `paperraven install-skill`
in this folder, then a new Claude Code session.

Every command below is `{{python}} -m paperraven --run-dir runs/<name> …`. If a
command prints that OpenAlex refused service, the tool has already moved on to Crossref and
queued what it could not settle; pass the line on to the user.
Registry access needs a contact address for the polite pool: `contact_email:` in the config
you write (or `PAPERRAVEN_CONTACT_EMAIL` in the user's own environment; one you `export` in a
command does not last to your next command). Ask the user for it in the interview if neither
is set.

## 0. Walk the user through it — and never put words in their mouth

The user may have never used this tool. Do not assume they know what a facet, a seed or a
round is. Tell them, in one sentence each, what you are about to ask and why, ask, and
show them what you produced before you act on it. The steps below are yours to drive;
the decisions — locking the topic description, approving the config, reading the result —
are theirs.

**Provenance is the contract.** Everything in the run is either `[USER]` — the user typed
it, or read it and signed it off — or `[AGENT]` — you wrote it, inferred it, extrapolated
it or chose the wording of it. Every inclusion, exclusion and judging note in the config
carries it as a field, `by: user` or `by: agent`:

```yaml
inclusions:
  - text: One or two sentences stating the rule.
    by: agent          # becomes `user` once the user has read and confirmed it
```

`init` refuses a criterion without `by`. Facets and keywords are not recorded with
provenance; do not tell the user they are. Two consequences:

- **Never write "your X" or "you said X" about anything the user did not type or sign
  off.** When the user picks one of several options you authored, the *choice* is theirs
  and the *wording* is yours: say *"the option you chose"*, set the item to `by: user` only
  if they saw the text and confirmed it, and keep your wording of it `by: agent` until
  they do.
- **Any reinterpretation of what the user typed is said to them, in the conversation,
  before it is acted on** — never resolved silently in a file. "8 hours" is not a number of
  rounds until the user says it is.

## 1. Interview (skip if the user hands you a config)

Ask for: a topic description (prose or a file), **seed PDFs on disk** (5–10; files, not
DOIs), read depth (front matter is the default), **a target paper count —
how many papers the tool should find (seeds do not count)**, which goes in the config as
`budget.target_papers`, and a contact email for the registries' polite pool, which goes in the
config as `contact_email:` (an environment variable set in one of your shell commands does not
last to the next).

**Do not ask for a round budget — derive it.** Set `max_rounds` to the target divided by
`papers_per_round × 0.6`, rounded up, and say so in one line: *"100 papers at 5 a round,
assuming about 60% of proposals end up in the corpus → `max_rounds: 34`. Stopping is reported,
never enforced, so a high ceiling costs nothing if the proposer runs dry first."* The user
approves or changes it with the rest of the config. Take the target as given; do not argue
about it.

**Echo the topic back before anything else.** Take what the user typed, remove filler and
resolve obvious ambiguities *by asking, not by deciding*, and present the distilled
description to them for edits or sign-off. Iterate until they lock it. The locked text is
`[USER]` and goes into `topic.description`; their original, verbatim, goes into
`topic.user_brief` — both reach the judge, so the judge reads the researcher's own words
beside the distillation. Until the user locks it, the description is `[AGENT]`.

Tell the user, before they answer, that you will check their seeds against their
description and ask them which to trust if the two disagree — a seed set broader than the
prose is common, and `init` puts every resolved seed straight into the greenlist, so an
exclusion the seeds would fail has to be settled before it runs, not discovered in round 3.
**Pose scope questions as a concrete paper** — *"a 2011 paper that is purely theoretical,
with no data: in or out?"* — not as an abstract boundary. One named paper gets a decision
in one turn.

Read the seed PDFs yourself. Distil the topic into a config: **2–4 facets** (named keyword
groups — a taxon, a measurement, a method, a setting — 5–25 terms each, synonyms and older
vocabulary included), exclusion facets, inclusion/exclusion prose. Render the prompt that
tells you how, if you want it:

```bash
{{python}} -m paperraven --run-dir runs/<name> --config <draft.yaml> prompt distill
```

**Be conservative**: formalise what the user said; do not invent exclusions. Everything you
add beyond their words is `[AGENT]` and says so.

**A criterion is a rule, in one or two sentences.** "A study that reports no original data is
out of scope." The test cases the user decided ("the 2019 replication
study — in"), the reasoning behind a boundary, and anything about the run's own history belong
in **`judging.notes`**, which the judge reads under its own heading — not inside a criterion,
where they make the README unreadable. **A note keeps the tag of whoever made the decision** — its
`by`: a test case the user ruled on is `by: user` wherever it is written down.

**A year limit is not prose.** If the user states one — "since 1990", "before 2000" — write it as `topic.year_min` / `topic.year_max`, never as an exclusion sentence. Python enforces it against every publication date the registries state; a judge reading a prose year applies it to the earliest *online* date, which can be a year before the print issue.

**Show the user the config — the decisions in full, the vocabulary as a count.** Paste, with their tags: the user's own words, the description, **every inclusion,
exclusion and judging note, and the year limits**. For facets give each name and its number of
keywords — *"generative_models (23 keywords)"* — and offer to show any of them. Show the
keywords only when asked; ask whether the user wants fewer only if they say the lists are
long. A changelog may accompany a later config, never replace the criteria. The user edits or
approves; you do not finalise it. `init` prints the same, and `init --show-topic` the keywords
too, so the record exists either way.
`{{python}} -c "from paperraven.config import Config, canonical_yaml; print(canonical_yaml(Config()))"`
prints the full schema.

**Then argue against it, before they sign off.** Under its own heading, list the ways this
config could fail the user — a handful, each specific, each with the evidence that would show it
happening:

- what the tool cannot deliver for this goal — a request for the newest work meets the
  proposer's thinnest recall; a literature whose older half has no DOIs is verified by title;
- a criterion that could quietly exclude a strand of the literature they want, and the verdicts
  that would reveal it;
- scope that does not fit the target — so wide that the corpus fills with the most-cited papers
  rather than the most useful, or so narrow that it runs dry — and which setting changes it;
- anything in the deliverable that will surprise them, such as seeds outside the year limits,
  which are kept.

Each item is a question for the user, not an edit you make. Nothing runs until they have read it.
**Keep the list, and at each check-in say which of these risks the verdicts so far bear out**,
quoting the verdict that shows it. A risk that comes true is a steer to propose, not a fix to make.

## 2. Initialise

```bash
{{python}} -m paperraven --run-dir runs/<name> --config <config.yaml> init
```

Resolves the seeds into the greenlist, publishes the config as an immutable generation,
writes an editable `config_draft.yaml`, and writes round 1's context. If it prints
`ACTION: read seed_digest.md`, open that file, read each listed seed's page text,
identify its title (and DOI if you can), and finish the seeding — one call per seed,
**before** `context`:

```bash
{{python}} -m paperraven --run-dir runs/<name> seed --file <name>.pdf --title "<title>" [--doi <doi>]
```

## 3. The round loop

```
context  →  you write proposals.json  →  submit  →  judge  →  you write judging_response.json  →  verdicts
```

**Check in with the user after round 1, then every 5 rounds, and at once whenever `status` reports a stop reason, says `references` or `citing` is open, or a registry has refused a request.** A check-in says how many papers are in, undecided and out, quotes the latest shortfall reasons, says which of the pre-run risks the verdicts bear out, and ends with the choice — continue, steer or stop — and waits for the answer. Between check-ins, run the rounds without asking.

- `context` writes `rounds/NNN/context.md`. **Read it** — it carries the topic, the facets,
  the redlist of everything already seen, and the exact output format.
- Propose up to `papers_per_round` works in the scholarly literature (default 5) — journal
  articles, conference papers, preprints, books, theses; never blog posts, news, press
  releases or other web pieces. **Propose from what you know; do not look papers up to
  confirm them** — `submit` checks every one against the registries, which is the tool's job,
  not yours. **Search only to find papers you do not know**, such as recent work. **Give the
  DOI whenever you know it** — a right one is verified exactly — and `null` when you do not; a
  wrong DOI falls back to the title search, so it does not lose the paper. Mark
  each paper's `source` — `"recall"`, `"search"`, `"references"` or `"citing"`, with
  `found_via` for a search or a paper from the `references` or `citing` list. Write
  `rounds/NNN/proposals.json`, with a top-level `model`
  naming your own model identifier — the corpus's methods section depends on it — and have
  the judge subagent do the same in `judging_response.json`. Never construct a DOI.
  **If you cannot fill the round, do not pad it** — return what you have with a one-line
  `"shortfall_reason"` at the top level saying why, and where you are finding it hard to
  name more. `submit` refuses a short round without one. Consecutive thin rounds are how
  the run learns it has reached the end of the literature; a quota met every round tells
  it nothing. **At every check-in, quote the most recent shortfall reasons to the user**
  (`status` lists them): they say *where* the supply is dry, which is what a user needs
  to decide whether to steer or stop.
- **When `status` or `submit` says `references` is open**, your own recall is running short.
  Ask the user once: *"My own recall is running out. I can now propose from the papers the
  corpus cites: PaperRaven lists the works cited by several corpus papers that are not yet in
  the run. Shall I start?"* If they agree, run it before writing each round's proposals:

  ```bash
  {{python}} -m paperraven --run-dir runs/<name> references
  ```

  Read `rounds/NNN/reference_candidates.md` and propose, alongside your own recall, the
  candidates within the criteria, with `"source": "references"` and `found_via` as the list
  gives it. Name the ones you decide against by OpenAlex ID in a top-level
  `"declined_candidates"` list, so they are not listed again, and say why in one line in a
  top-level `"decline_reason"`: if the user asked you to narrow what you propose, say so, and
  say whether that declines papers the criteria admit. The export quotes it. Do not use it
  earlier: the
  ranking needs enough papers you found yourself. If the user asks for it sooner, run it with
  `--now`.
- **When `status` or `submit` says `citing` is open**, the reference lists are running dry too.
  Run it once and ask the user, with the counts it prints: *"The papers your corpus cites are
  running out. PaperRaven can now list the works that cite your corpus and are not yet in it:
  <the counts>. They are ranked by how much of their reference list is your corpus. How many
  papers would you like to add from them? Or give a minimum number of your corpus papers each
  must cite."* A number of papers becomes the run's target: set `budget.target_papers` in
  `runs/<name>/config_draft.yaml` to the papers found so far plus that number, raise
  `budget.max_rounds` if the run would reach it first (allow a round for every 4 papers or so),
  and `steer` (neither change re-judges anything); the run stops at the target as usual. A minimum is
  passed as `--min-cites` from then on. Then run it before writing each round's proposals:

  ```bash
  {{python}} -m paperraven --run-dir runs/<name> citing [--min-cites K]
  ```

  Read `rounds/NNN/citing_candidates.md` and propose the candidates within the criteria, with
  `"source": "citing"` and `found_via` as the list gives it; decline the others as for
  `references`, in the same `"declined_candidates"` list with a `"decline_reason"`. It searches
  OpenAlex once and later rounds rank from that search with no calls; run it with `--refresh`
  to search again, as when it says the search is used up. If the user asks for it sooner, run
  it with `--now`.
- **Follow the corpus's reference lists only through `references`, and the works that cite it
  only through `citing`.** Searching the web stays allowed, including for papers that cite one
  you know. What is not allowed is harvesting candidates from pages: never fetch a paper's
  reference list, or a citation service's pages (such as Semantic Scholar), to mine candidates.
  The commands list them from OpenAlex, which the run already uses.
  **Every fifth round, make one proposal a deliberate boundary probe** — a paper you expect
  the criteria to *exclude* — and say so in its `explanation`. A proposer that offers only
  what it expects to pass never tests the exclusion criteria.
- `submit` verifies. Read its report: hallucinations, duplicates and mismatches are named.
- `judge` writes `rounds/NNN/judging_request.json` and `rounds/NNN/judge_prompt.md`. **Hand
  them to one independent judge subagent, every round** — the **`paperraven-judge`** agent
  type, never a general-purpose agent. It can read, write and search the web, and nothing
  else, which is the point. If your host has no such agent type, give the judge those limits
  in words and tell the user they are not enforced. **Give the judge subagent exactly two files, by their absolute paths, and nothing else — never their contents pasted in:**
  `rounds/NNN/judge_prompt.md` — the judge prompt `judge` rendered for this round, with the
  researcher's own words, the criteria, the facets and the judging notes filled in — and
  `rounds/NNN/judging_request.json`. Not this conversation, not your reasons for proposing, and
  no instructions of your own. **If you think the judge needs guidance, that is a judging note:**
  propose it to the user as a steer (`judging.notes` in the draft config), `by: agent`.
  Once approved it is part of every later prompt, and it re-judges what was already judged. Do not judge inline. When one mind proposes and judges, the include rate
  measures its consistency with itself, not the criteria. The judge writes
  `rounds/NNN/judging_response.json` (its prompt names the file): one verdict per DOI, `include` / `exclude` / `uncertain`, each with a specific reason. No
  confidence scores. **Where a record has no abstract, the judge searches the web for it first**
  and answers `uncertain` if the results do not settle it; where an abstract plainly belongs to a
  different paper, it says so.
- `verdicts` files them. If it reports records still unjudged, run `judge` again for the
  same round — that is your job, not the user's.
- `status` any time: greenlist size, discovery curve, stop signal.

Stopping is **reported, never enforced**. When `status` says `stop:`, tell the user why and
let them decide.

**After you stop or interrupt a command, run `status` before you tell the user the state.** A
stopped command may still commit a moment later, and what you last saw is then out of date.

## 4. Steer, if the papers drift

Edit `runs/<name>/config_draft.yaml`, then `steer` (or `steer <file>` with an edited copy). A
topic change opens a new epoch.

**A criterion or note the user asks for mid-run is `by: user` only in words they have read.**
If their own sentence works as the text, use it verbatim. If you reword it, turn it into a
rule, or add a reason of your own, show them the exact text and write `by: user` only once they
confirm it; until then it is `by: agent`. This holds for every inclusion, exclusion and judging
note a steer adds or changes, not only for rulings on a paper.

**A topic change re-judges everything already generated** under the new criteria — the
greenlist, the excluded pool and the uncertain pool, **seeds exempt**. The greenlist can grow
*or shrink*; the redlist does not change, so nothing is re-proposed. `steer` says how many
records will be re-judged **before** anything is spent; nothing moves until the next
`judge` and `verdicts`, and `verdicts` lists every paper that moved. Give the whole re-judge
to one judge, as every round: there is one request and one response file, so judges run side
by side would overwrite each other. If `verdicts` reports records still unjudged, run `judge`
again; it asks only for those. Year limits are re-applied by rule, not re-judged.
If the user wants the new criteria to shape what is *proposed*, that is a fresh run, not a
steer. **Tell the user the re-judge count `steer` reports before running
`judge`** — it is the cost of the steer.

**When the user rules on a named paper** ("that one is in", "keep that out"), write it down
at once, as a judging note that names the paper:

```yaml
judging:
  notes:
    - text: One sentence, in words the user has read, saying which paper and why.
      by: user
      paper: <the record's identifier, as the CSVs or `status` show it>
      verdict: include        # or exclude
```

Draft the sentence, show it to the user, and write it only once they confirm the words; that
is what `by: user` means. Collect the rulings and apply them in one `steer`. `steer` gives the
paper the user's verdict, keeps it through every later re-judge, and **re-judges nothing
else**. Add `--rejudge all` only if the ruling also changes how the criteria should be read,
and say what that costs first. A ruling can name only a paper already in the run: to add one,
propose it or seed it. Removing the note hands the paper back to the judge. Rulings on
`uncertain.csv` after export work the same way; export again afterwards.

## 5. Export

```bash
{{python}} -m paperraven --run-dir runs/<name> export [--library their.bib] [--pdf-dir DIR]
```

**Ask, once, whether the user has a reference library** (a `.bib` or a list of DOIs) to
compare against — `--library` marks which papers are new to them. It writes `<run-name>-corpus/` **beside `runs/`**, not inside the run — the run directory is
disposable, the corpus is not. What the user gets:

- `README.md` — start here: the topic in their own words, every criterion marked "yours
  (stated or signed off by you)" or "written by the tool", how the run went, the flow table, a methods paragraph to
  adapt, and the limitations.
- `papers.bib` — the bibliography (standard BibTeX: authors, title, venue, year, volume, issue, pages, DOI), with the registry's publication year.
- `papers.csv` — every paper, why it was included, and (with `--library`) which the user
  already has.
- `worklist.csv` — how to get each PDF.
- `excluded.csv` — each excluded paper, who excluded it (the judge or the year rule), under
  which criterion, and why. **Borderline exclusions are the least reproducible verdicts the
  tool makes** — say so.
- `uncertain.csv` — only if something is undecided: the user's to rule on.
- `citation_network.html` — the citations among the corpus papers, drawn as a network to
  explore in a browser (hover, click, drag); it works offline. **Tell the user about it.**
  Written only when some corpus paper cites another.
- `citations.csv` — the same citations as an edge list, for Gephi, networkx or R. Above
  500 papers it is written without the page.
- `audit/` — every record, every config generation, the judge prompts, the rejections. For
  checking, not for use; do not walk the user through it unless asked.

**Lead every report of the export with the outputs**: after `export`, after `fetch`, and in
your last message. Open with a bold **Outputs** heading, then one line each, linked: `README.md`
(start here), `papers.bib` and `papers.csv`, `uncertain.csv` with its count (those papers are
not in `papers.bib` until the user rules on them, by a steer), `citation_network.html` when it
was written (open it in a browser: who cites whom), and after `fetch` the `pdfs/` folder and
`pdfs/click_list.html`.
The close-out's lists (must fix, for the user to decide) come after it, so the deliverables are
not lost behind the decisions. When the network was written, offer to open
`citation_network.html` in the user's browser (`open` on macOS, `xdg-open` on Linux, `start` on
Windows), once, and open it only on a yes.

**Close out before you call the run finished.** Right after `export`:

1. `{{python}} -m paperraven --run-dir runs/<name> closeout --scratch <your scratchpad>`.
   It checks that the export folder holds what the tool wrote and nothing it did not, the
   bibliography's integrity, the tables and README against the corpus, that no contact email
   or key is in the export, that every JSON file parses, and what is left in your scratchpad
   (PDFs, other downloads, extracted text). It reports and changes nothing but its own report,
   `audit/closeout.json`. `export` runs the same check without the scratchpad.
2. Hand the `paperraven-closeout` agent (read-only) the run directory, the export folder,
   your scratchpad path and, in Claude Code, the folder holding this session's subagent
   transcripts. It returns three lists: must fix, for the user to decide, fine.
3. Give the user those lists. **Do not call the run finished until they have seen them.**
   Deleting anything, a file in the scratchpad or a stray file in the export, is the user's
   call, file by file.

Your own notes for the user go in `agent_notes.md` in the export folder, the one file the
check expects from you. Write nothing else there.

**Then offer the PDFs**, as a separate step, as §6 says: first the estimate, then the offer in the
words given there. If they say yes, follow §6.

**Last, when the run is over** (after `fetch`, or after the close-out if the user wants no
PDFs): run the `closeout` command above once more. In Claude Code it also records what the whole
session used in tokens (raw, and weighted by the published cache rates) in `audit/closeout.json`,
in total and per paper returned. Tell the user the per-paper line it prints.

## 6. Fetching PDFs (for this corpus, or any `.bib` the user gives you)

This is separate from building a corpus. A user can ask for it on its own, for any `.bib`.

```bash
{{python}} -m paperraven fetch --bib <file.bib> --contact-email <the run's contact email> [--dest <folder>]
```

`fetch` reads no run config, so pass the contact email (the one in the run's config) with
`--contact-email`; it is used for the registries' polite pool only, never sent to publishers.

**Before offering, get the time** (no network, nothing written):

```bash
{{python}} -m paperraven fetch --bib <corpus>/papers.bib --estimate
```

**Then make the offer in these words**, with N from the estimate and `<dest>`
the destination:

> *"Your corpus is done. I can fetch the PDFs now. It works like a reference manager: it takes
> open-access copies first, then asks each publisher's site for its PDF link, never more than
> once every 10 seconds per publisher, a few publishers at a time, and stops asking a publisher
> that refuses. It bypasses no paywall; on a campus network or VPN it can use your
> subscriptions. For this corpus it should take about N minutes. PDFs go to `<dest>`. Whatever it
> cannot get goes on a click-list, a web page of links you open and download yourself; I can
> then file those downloads in for you. Do you want preprints (submitted or accepted versions)
> when they are all that is openly available, or only the publishers' versions? Publishers'
> versions only means more papers on the click-list for you to download yourself. Shall I
> start?"*

If they want preprints, add: *"Some copies may be preprints; it will say which."* If they want
only the publishers' versions, pass `--published-only`.

**Running it:** Always run `fetch` in the background and report when it ends; your command limit
is shorter than a long fetch, and the estimate cannot know how fast each publisher answers. Never
pipe its output through `tail` or anything that truncates it: its report comes at the end. It
prints its own estimate after the registry lookups, and a progress line every 10 papers.

- **The destination** defaults to a `pdfs/` folder beside the `.bib`, which for an export is
  `<corpus>/pdfs/`. Tell the user where that is, and take another folder if they name one.
  - The command refuses a place git would track: PDFs are copyrighted.
  - `export` has already copied the user's seed PDFs into `<corpus>/pdfs/` (its output says how
    many), so `fetch` counts them as present and never asks the user for a seed. If `export`
    says a seed file was not copied, tell the user which.
- **The command prints its own advice** (campus network or VPN; library licences; Zotero). Pass
  it on.
- **What it does:** it downloads what it can the way a reference manager does: open copies
  first, then the publisher's PDF link. It is polite, goes slowly, and stops asking a publisher
  at its first refusal.
  With `--published-only` it takes only the publishers' versions; papers with only a preprint
  or a copy of unknown version openly available go on the click-list, and the report counts
  them.
- **What it leaves:** `click_list.html` in the destination, for the papers it could not get,
  and, as an optional section, the published versions of papers it fetched as preprints. Open it
  for the user, and tell them how many of the PDFs in the folder are preprints (the report
  says).
- **Collecting:** when they say they have downloaded some, run the same command with
  `--collect <their downloads folder>`. The command's output names the usual one for this
  machine; ask if theirs is elsewhere. It recognises each PDF by its first page and copies it
  in; it never moves or deletes their files.
  - When a paper in the destination is a preprint and they downloaded its published version,
    it puts that in under the paper's name and moves the preprint to `_preprints/`. It does so
    only when the PDF's first page shows the paper's own DOI.
  - If the output says downloads were not swapped in, tell the user those look like another
    copy of the preprint, and ask them to download the version on the publisher's page.
- **Re-running** is safe: it skips every paper already in the destination, whatever the file is
  called. If it says a per-publisher limit was reached, run it again later.
- **Never download papers any other way:** no `curl`, no browser, no scripts.

## Do not

- Run `init` twice on a live run, or two commands on one run at once (the second is refused).
- Edit `state.json`, `rounds.jsonl`, or a `config_genNNN.yaml`. Edit the draft and `steer`.
- Pass `--config` after `init`. State names the live configuration.
- Construct a DOI from a pattern. Give the DOI you remember, or `null`: a wrong one falls
  back to the title search, but a fabricated one is noise in the audit trail.
- Harvest candidates from reference lists on web pages or from citation services' pages.
  Searching the web stays allowed; the corpus's own reference lists come through `references`,
  and the works that cite it through `citing`.
