"""Relevance judging: the file-based handoff to the host agent's subagents.

**Python holds no credentials.** In host-agent mode the host agent *is* the LLM, so this
module cannot spawn a judging subagent and does not try. It writes a request file, and it
validates a response file. Everything between those two points is the agent's job. That is
the same shape the proposal loop uses, for the same reason.

    1. Python        -> rounds/NNN/judging_request.json
    2. host agent    -> fans out Sonnet-tier subagents
    3. subagents     -> rounds/NNN/judging_response.json
    4. Python        -> validates, files verdicts into the four-way partition

Two consequences of that split are worth stating because they are easy to erode later:

- **No prefilter**. Sonnet is cheap enough to judge every verified record, so there
  is no keyword scorer standing in front of the judge.
- **The judge decides relevance, never existence.** It is handed only records the
  verification cascade already confirmed, with their verified metadata; it may search the web
  for a missing abstract, but nothing it finds changes whether a paper exists. Keeping the two
  questions apart is what makes the verdict interpretable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .abstracts import has_abstract
from .config import Config, Facet, texts
from .corpus import Corpus, atomic_write_text
from .models import DiscoveredBy, PaperRecord, Verdict
from .normalize import normalize_doi

JUDGING_REQUEST_FILENAME = "judging_request.json"
JUDGING_RESPONSE_FILENAME = "judging_response.json"


# --------------------------------------------------------------------------------------
# What the judge is asked
# --------------------------------------------------------------------------------------


class JudgingItem(BaseModel):
    """One paper presented for judgment.

    Deliberately not a `PaperRecord`: the judge needs what it can reason about and nothing
    else. Sending the whole record would put provenance, OA links and match diagnostics in
    front of a model whose only question is topical relevance.
    """

    model_config = ConfigDict(extra="forbid")

    doi: str
    title: str
    year: int | None = None
    venue: str | None = None
    type: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None

    #: Stated explicitly rather than left implicit by a null abstract, so the judge can
    #: answer `uncertain` instead of guessing from a title. Missing abstracts are around
    #: a fifth of a corpus and *publisher-biased* (Springer and Elsevier withhold them), so
    #: this is a systematic slice, not a rare edge case, and a judge that quietly guessed
    #: on it would produce confidently wrong verdicts.
    abstract_available: bool = True
    #: Where the abstract came from: `openalex`, `crossref` or `europe_pmc`.
    abstract_source: str | None = None


class JudgingRequest(BaseModel):
    """The full work order for one round."""

    model_config = ConfigDict(extra="forbid")

    round: int
    epoch: int = 0
    #: The configuration generation the request was written under. `verdicts` refuses a
    #: response when the run's generation has moved on since (the criteria the judge read
    #: are no longer the run's).
    config_generation: int | None = None
    topic_description: str = ""
    inclusions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    #: Worked examples of "relevant here": the user's own seed papers.
    seed_examples: list[str] = Field(default_factory=list)
    #: The topic's **dimensions**. A paper that touches one facet and misses another is
    #: the canonical false positive, and the facets encode that conjunction by
    #: construction. Framed in the prompt as axes a relevant paper must touch, not as terms
    #: to find — the framing is what stops a model pattern-matching titles to a word list.
    facets: list[Facet] = Field(default_factory=list)
    items: list[JudgingItem] = Field(default_factory=list)
    #: The rendered judge prompt written beside this request, and its
    #: SHA-256. The pair is what Python issued for this round; `verdicts` re-reads the file
    #: and records whether it still matches. Python cannot see what the agent passes to its
    #: subagent, so this establishes the instructions *issued*, not those *received*.
    prompt_file: str | None = None
    prompt_sha256: str | None = None

    #: One request per round; the host agent decides its own fan-out. At ~295 tokens per
    #: abstract a five-paper round is ~1475 tokens, so sharding in Python would add a knob
    #: and buy nothing.
    note: str = (
        # Must agree with the judge prompt's rules: every judge reads this file.
        "Judge each item independently. Return one verdict per DOI. "
        # By web search only, not by fetching the paper.
        "Where abstract_available is false, look the paper up first: search the web for it, "
        "judge only from what the results show of the paper itself, and name the source; "
        "answer 'uncertain' if they do not settle it."
    )


def build_request(
    corpus: Corpus, config: Config, *, round_index: int
) -> JudgingRequest:
    """Assemble the round's judging request from records awaiting a verdict.

    Only unjudged records: re-judging a settled paper wastes a call and, worse, would let
    a second opinion silently overwrite the first without the audit trail saying why.
    """
    seeds = [
        rec.title
        for rec in corpus.uncertain.values()
        if rec.provenance.discovered_by.value == "seed"
    ] + [rec.title for rec in corpus.greenlist.values()
         if rec.provenance.discovered_by.value == "seed"]

    items = [
        JudgingItem(
            doi=rec.doi,
            title=rec.title,
            year=rec.year,
            venue=rec.venue,
            type=rec.type,
            authors=list(rec.authors),
            abstract=rec.abstract,
            abstract_available=has_abstract(rec),
            abstract_source=(rec.abstract_source or rec.provenance.source.value)
            if has_abstract(rec) else None,
        )
        for rec in _awaiting_verdict(corpus)
    ]

    topic = config.topic
    return JudgingRequest(
        round=round_index,
        epoch=corpus.epoch,
        config_generation=corpus.config_generation,
        topic_description=topic.resolved_description() or "",
        inclusions=texts(topic.inclusions),
        exclusions=texts(topic.exclusions),
        seed_examples=sorted(set(seeds))[:10],
        facets=list(topic.facets),
        items=items,
    )


def _awaiting_verdict(corpus: Corpus) -> list[PaperRecord]:
    """Records the judge should see: never judged, or marked for re-judging.

    An unjudged record lives in the `uncertain` pool by construction: it is held
    there rather than admitted to the greenlist, because the greenlist means "judged
    relevant" and defaulting to inclusion would inflate the deliverable. A record marked
    for re-judging by a topic steer may be in **any** pool — it stays where it is until its new
    verdict arrives.
    """
    fresh = [rec for rec in corpus.uncertain.values() if rec.relevance.agent_verdict is None]
    seen = {rec.doi for rec in fresh}
    pending = []
    for doi in sorted(corpus.rejudge_pending - seen):
        rec = (corpus.greenlist.get(doi) or corpus.uncertain.get(doi)
               or corpus.excluded.get(doi))
        # A seed is the user's, never the judge's, even if it is marked. (Ruled records are not filtered here: an unpinned paper keeps
        # `decided_by` "user (ruling)" and must be judged again.)
        if rec is not None and rec.provenance.discovered_by is not DiscoveredBy.SEED:
            pending.append(rec)
    return fresh + pending


def write_request(
    corpus: Corpus, config: Config, *, round_index: int,
    prompt_file: str | None = None, prompt_sha256: str | None = None,
) -> tuple[Path, JudgingRequest]:
    """Write the request file and return its path alongside the request."""
    request = build_request(corpus, config, round_index=round_index)
    request.prompt_file, request.prompt_sha256 = prompt_file, prompt_sha256
    path = corpus.round_dir(round_index) / JUDGING_REQUEST_FILENAME
    atomic_write_text(path, json.dumps(request.model_dump(mode="json"), indent=2))
    return path, request


# --------------------------------------------------------------------------------------
# What comes back
# --------------------------------------------------------------------------------------


class JudgeVerdict(BaseModel):
    """One verdict from the judge.

    `extra="forbid"` on purpose: a field we do not model is a field we would silently
    ignore, and a judge inventing structure is worth surfacing rather than absorbing.
    """

    model_config = ConfigDict(extra="forbid")

    doi: str
    verdict: Verdict
    #: Required. A verdict with no reason cannot be audited.
    reason: str = Field(min_length=1)
    #: The numbered inclusion or exclusion criterion that decided it, as the
    #: judge states it — its stated basis, not a verified one. Optional: a verdict without
    #: one is still valid, and simply cannot be grouped.
    criterion: str | None = None

    # NO confidence field. LLMs cannot emit confidence scores that correctly assess their
    # internal model states; only a similarity score against external data would be
    # useful.
    #
    # `extra="forbid"` means a judge that emits one is *rejected and reported* rather than
    # silently ignored — which is the intended behaviour: a number nobody should trust
    # must not quietly enter the audit trail where a later reader might threshold it.
    # The three verdicts plus a reason are the whole signal.


#: What the audit trail records when a file does not state its model.
NOT_STATED = "not stated"


@dataclass
class DroppedEntry:
    """A response entry that failed validation, kept rather than discarded.

    Kept so the round log counts what was emitted, not only what *parsed*, and a parser
    failure stays distinguishable from a model that under-produced. Proposal files are
    read the same way.
    """

    raw: Any
    error: str


@dataclass
class Moved:
    """A re-judged record whose verdict changed: what `verdicts` reports."""

    doi: str
    title: str
    before: str
    after: str


@dataclass
class JudgingOutcome:
    """Verdicts, plus an explicit account of everything that did not become one."""

    verdicts: list[JudgeVerdict] = field(default_factory=list)
    dropped: list[DroppedEntry] = field(default_factory=list)
    #: Verdicts naming a DOI the corpus does not hold — usually a stale response file.
    unmatched_dois: list[str] = field(default_factory=list)
    #: Verdicts naming a record the request did not ask about, such as a seed. Not
    #: applied: only the judging request's records are the judge's to rule on.
    unrequested_dois: list[str] = field(default_factory=list)
    #: Records that were awaiting a verdict and did not get one.
    unjudged_dois: list[str] = field(default_factory=list)
    #: DOIs given more than once in the response, each listed once; only a DOI's first
    #: verdict counts, and `repeats` counts the verdicts not applied.
    duplicate_dois: list[str] = field(default_factory=list)
    repeats: int = 0
    #: Exclusions applied without naming the rule they meet or fail.
    no_criterion_dois: list[str] = field(default_factory=list)
    #: Records that already had a verdict and received another.
    rejudged: int = 0
    #: …and the ones whose verdict changed, so the user sees exactly what a steer moved.
    moved: list[Moved] = field(default_factory=list)
    #: Discovery rounds of the records judged for the **first** time — the only rounds
    #: whose `included_per_round` entry this batch may change.
    first_judged_rounds: set[int] = field(default_factory=set)
    #: The judge's own statement of its model, self-reported.
    judge_model: str = NOT_STATED

    @property
    def applied(self) -> int:
        return (len(self.verdicts) - len(self.unmatched_dois) - len(self.unrequested_dois)
                - self.repeats)

    def summary(self) -> str:
        parts = [f"{len(self.verdicts)} verdicts"]
        if self.dropped:
            parts.append(f"{len(self.dropped)} malformed entries DROPPED")
        if self.unmatched_dois:
            parts.append(f"{len(self.unmatched_dois)} for unknown DOIs")
        if self.unrequested_dois:
            parts.append(f"{len(self.unrequested_dois)} for records not in the request, "
                         f"ignored")
        if self.duplicate_dois:
            parts.append(f"{len(self.duplicate_dois)} given twice or more (the first verdict "
                         f"counts)")
        if self.unjudged_dois:
            parts.append(f"{len(self.unjudged_dois)} records left unjudged")
        if self.rejudged:
            parts.append(f"{self.rejudged} re-judged, {len(self.moved)} moved")
        return "judging: " + ", ".join(parts)


def stated_model(payload: Any) -> str:
    """The model a proposals or judging file says wrote it — **self-reported**.

    Python cannot verify it, and every place that reports it says so. A missing or empty
    value is recorded as "not stated" rather than refused.
    """
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return NOT_STATED


def parse_response(payload: Any) -> tuple[list[JudgeVerdict], list[DroppedEntry]]:
    """Validate a judging response, reporting what failed instead of dropping it.

    Accepts either a bare list or an object carrying a `verdicts` key, matching the
    tolerance `read_proposals` already extends to proposal files: the agent writes these
    by hand and both shapes are natural.
    """
    if isinstance(payload, dict):
        key = next((k for k in ("verdicts", "results") if k in payload), None)
        if key is None:
            # Reported as a dropped entry (for `{"judgments": [...]}` or one bare verdict), so
            # the agent fixes the file's format rather than judging again.
            return [], [DroppedEntry(raw=payload, error="no `verdicts` list in the response "
                                                        "(expected {\"verdicts\": [...]})")]
        entries = payload[key]
    else:
        entries = payload

    if not isinstance(entries, list):
        return [], [DroppedEntry(raw=payload, error="`verdicts` is not a list of verdicts "
                                                    "(one object per paper)")]

    verdicts: list[JudgeVerdict] = []
    dropped: list[DroppedEntry] = []
    for entry in entries:
        try:
            verdict = JudgeVerdict.model_validate(entry)
        except ValidationError as exc:
            dropped.append(DroppedEntry(raw=entry, error=_terse(exc)))
            continue
        # `https://doi.org/10.1000/X` is the record `10.1000/x`.
        if (doi := normalize_doi(verdict.doi)) is not None:
            verdict = verdict.model_copy(update={"doi": doi})
        verdicts.append(verdict)
    return verdicts, dropped


def _terse(exc: ValidationError) -> str:
    """One short line per validation problem — enough to fix the prompt."""
    bits = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        bits.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(bits)


def read_response(
    path: str | Path,
) -> tuple[list[JudgeVerdict], list[DroppedEntry], str]:
    """Read and validate a response file: verdicts, drops, and the model it says wrote it.

    A missing or unparseable file raises: that is a workflow failure the agent must see,
    not a data problem to be averaged away. The model is self-reported.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    verdicts, dropped = parse_response(payload)
    return verdicts, dropped, stated_model(payload)


# --------------------------------------------------------------------------------------
# Applying verdicts
# --------------------------------------------------------------------------------------


def apply_verdicts(
    corpus: Corpus,
    verdicts: Sequence[JudgeVerdict],
    *,
    dropped: Iterable[DroppedEntry] = (),
) -> JudgingOutcome:
    """File verdicts into the four-way partition.

    Records the judge's reason on the record (an earlier verdict and reason are preserved
    as history), then routes via `Corpus.add_record`, which owns the partition and keeps it
    a partition.
    """
    outcome = JudgingOutcome(verdicts=list(verdicts), dropped=list(dropped))
    awaiting = {rec.doi for rec in _awaiting_verdict(corpus)}
    seen: set[str] = set()

    for v in verdicts:
        record = (
            corpus.uncertain.get(v.doi)
            or corpus.greenlist.get(v.doi)
            or corpus.excluded.get(v.doi)
        )
        if record is None:
            outcome.unmatched_dois.append(v.doi)
            continue
        if v.doi not in awaiting:
            outcome.unrequested_dois.append(v.doi)
            continue
        if v.doi in seen:
            outcome.repeats += 1
            if v.doi not in outcome.duplicate_dois:
                outcome.duplicate_dois.append(v.doi)
            continue
        seen.add(v.doi)
        before = record.relevance.agent_verdict
        if before is None:
            outcome.first_judged_rounds.add(record.provenance.discovered_round)
        else:
            # The later judgment replaces the earlier, which is kept.
            outcome.rejudged += 1
            if before != v.verdict:
                outcome.moved.append(Moved(doi=v.doi, title=record.title,
                                           before=before.value, after=v.verdict.value))
            record.relevance.supersede()
        corpus.rejudge_pending.discard(v.doi)
        record.relevance.agent_verdict = v.verdict
        record.relevance.agent_reason = v.reason
        record.relevance.criterion = (v.criterion or "").strip() or None
        record.relevance.decided_by = "judge"
        record.relevance.config_generation = corpus.config_generation
        # Mark verdicts reached on a title alone, so a weaker judgment is visible in the
        # record rather than indistinguishable from an abstract-backed one. This is around
        # a fifth of a corpus and publisher-biased.
        record.relevance.judged_without_abstract = not has_abstract(record)
        if v.verdict is Verdict.EXCLUDE and not record.relevance.criterion:
            outcome.no_criterion_dois.append(v.doi)
        corpus.add_record(record, verdict=v.verdict)

    outcome.unjudged_dois = sorted(awaiting - seen)
    return outcome


def judge_round(
    corpus: Corpus, response_path: str | Path
) -> JudgingOutcome:
    """Read a response file and apply it: step 4 of the handoff."""
    verdicts, dropped, model = read_response(response_path)
    outcome = apply_verdicts(corpus, verdicts, dropped=dropped)
    outcome.judge_model = model
    return outcome
