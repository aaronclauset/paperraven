"""Data model for PaperRaven.

Pydantic is used deliberately: `PaperProposal` is **untrusted input** written by an LLM,
so it needs real validation rather than duck typing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .normalize import REGISTRY_ID_PREFIX, is_record_identifier, normalize_doi


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


#: `decided_by` on a record the user ruled on through a judging note. Like a seed, it
#: is left out of every re-judge until the note is removed.
RULING_DECIDED_BY = "user (ruling)"


class RejectionReason(str, Enum):
    """Every rejection carries exactly one reason; reasons are never merged."""

    MALFORMED_DOI = "malformed_doi"
    DOI_UNRESOLVED = "doi_unresolved"
    DOI_TITLE_MISMATCH = "doi_title_mismatch"
    NO_DOI_NO_TITLE_MATCH = "no_doi_no_title_match"
    OUT_OF_SCOPE_TYPE = "out_of_scope_type"
    DUPLICATE = "duplicate"
    #: NOT a rejection of the paper — a failure of our infrastructure. Goes to a retry
    #: queue. Conflating it with a real rejection would silently shrink the corpus
    #: during a registry outage.
    REGISTRY_ERROR = "registry_error"
    #: A registry error that persisted through `MAX_RETRIES` retries:
    #: final, but still not the paper's fault, so nothing is redlisted.
    REGISTRY_GAVE_UP = "registry_gave_up"
    #: A registry refused the tool's own query (HTTP 400) at a title-search step and no other
    #: step found the paper. Not retried -- the same query fails the same way -- and,
    #: like the two above, not the paper's fault, so nothing is redlisted.
    REGISTRY_QUERY_REJECTED = "registry_query_rejected"

    @property
    def is_retryable(self) -> bool:
        return self is RejectionReason.REGISTRY_ERROR


class Verdict(str, Enum):
    """Relevance judgement. `UNCERTAIN` is a real outcome, never coerced."""

    INCLUDE = "include"
    EXCLUDE = "exclude"
    UNCERTAIN = "uncertain"


class VerificationPath(str, Enum):
    """Which cascade branch accepted the record."""

    DOI_EXACT = "doi_exact"
    TITLE_FALLBACK = "title_fallback"


class DiscoveredBy(str, Enum):
    """How the record entered the corpus.

    Citation-graph snowballing was discarded as a design, so no snowball values exist.
    """

    AGENT_PROPOSAL = "agent_proposal"
    SEED = "seed"


class RegistrySource(str, Enum):
    OPENALEX = "openalex"
    CROSSREF = "crossref"


#: Work types that are **definitively not literature**. Anything resolving to one of these
#: is rejected as `out_of_scope_type`; everything else proceeds to the relevance judge.
#:
#: A blocklist rather than an allowlist, because an allowlist does **relevance** work that
#: belongs to the judge. "Is an erratum a paper we want?" is a relevance question, not an
#: existence question, and an erratum to a data paper may well carry corrected measurements.
#:
#: Only genuine non-documents are listed. An allowlist also invites silent vocabulary
#: mismatches: OpenAlex's `conference-paper` and Crossref's `proceedings-article` name the
#: same thing.
OUT_OF_SCOPE_TYPES: frozenset[str] = frozenset(
    {
        "dataset",
        "component",       # a figure or supplement registered with its own DOI
        "peer-review",
        "grant",
        "paratext",        # front matter, mastheads, tables of contents
        "database",
        "standard",
        # `other` is deliberately **not** here. It is a publisher's registration string, not
        # a scope fact: a publisher may type a book chapter `other` while the same author's
        # monograph passes as `monograph`. Relevance is the judge's job; `submit`'s job is
        # existence.
    }
)


def type_in_scope(work_type: str | None) -> bool:
    """Whether a resolved work type may proceed to relevance judging.

    Absent type is in scope: older records are sparse, and a missing field is no evidence
    of being a dataset.
    """
    if not work_type:
        return True
    return work_type.strip().lower() not in OUT_OF_SCOPE_TYPES


# --------------------------------------------------------------------------------------
# Proposal — what the agent emits
# --------------------------------------------------------------------------------------


#: How the proposer came to name a paper, self-reported: from its own knowledge, from a
#: search it ran, or from the candidates the `references` command listed.
ProposerSource = Literal["recall", "search", "references"]
PROPOSER_SOURCES: tuple[str, ...] = get_args(ProposerSource)


class PaperProposal(BaseModel):
    """A candidate paper proposed by the agent.

    `authors`, `venue` and `year` are kept as proposed but **the registry value wins** —
    they exist for the title-fallback path and for audit, never as authority.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None

    #: Declared by the topic config as an extra extraction field.
    #: A domain config declares its own fields (e.g. an organism name); the package
    #: itself stays domain-agnostic.
    extra_fields: dict[str, Any] = Field(default_factory=dict)

    #: Model-only judgement. Kept because it makes human review fast and is auditable.
    explanation: str

    #: How the proposer came to name the paper, self-reported: `"recall"` from its own
    #: knowledge, `"search"` from a search it ran this round, `"references"` from the
    #: `references` list, `None` when unstated or unrecognised. Disclosure only: NEVER
    #: trusted downstream and no code may branch on it.
    source: ProposerSource | None = None
    #: For `source == "search"`: the query and the page where the paper turned up; for
    #: `"references"`, the corpus papers that cite it.
    found_via: str | None = None

    proposed_in_round: int

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must be non-empty")
        return v.strip()

    @field_validator("explanation")
    @classmethod
    def _explanation_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("explanation is required")
        return v.strip()

    @property
    def normalized_doi(self) -> str | None:
        """The proposal's DOI in canonical form, or ``None`` if absent or malformed."""
        return normalize_doi(self.doi)

    @property
    def has_doi(self) -> bool:
        """Whether a DOI was supplied at all.

        Distinct from ``normalized_doi is not None``: a supplied-but-malformed DOI is
        ``malformed_doi``, whereas a genuinely absent one routes to the title fallback.
        A nullable DOI is deliberate — it removes the structural incentive to fabricate
        one.
        """
        return self.doi is not None and str(self.doi).strip() != ""


# --------------------------------------------------------------------------------------
# Record — what Python emits
# --------------------------------------------------------------------------------------


class AuthorName(BaseModel):
    """One author as the registry splits the name (Crossref), or whole (`name`) when it does not,
    as for an organisation. Used to write a standard BibTeX author list."""

    # Unknown keys are ignored, as in `PaperRecord`, so a field added or removed later never
    # makes an existing state unloadable.
    model_config = ConfigDict(extra="ignore")

    given: str | None = None
    family: str | None = None
    name: str | None = None


class Bibliographic(BaseModel):
    """What a citation needs beyond title, authors and year, as the registries state it.

    Every field is the registry's text; nothing is inferred. `pages` is the page range, or the
    article number when there is no range. Retraction is reported with its source and never
    removes a paper.
    """

    model_config = ConfigDict(extra="ignore")      # as `AuthorName`

    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None
    issn: list[str] = Field(default_factory=list)
    journal_abbrev: str | None = None
    language: str | None = None
    publication_date: str | None = None
    author_names: list[AuthorName] = Field(default_factory=list)
    is_retracted: bool | None = None
    #: Which registry says it is retracted ("OpenAlex", "Crossref").
    retraction_source: str | None = None
    #: DOIs of retraction notices and corrections the publisher deposited (Crossref `updated-by`).
    retraction_notices: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    #: Crossref `relation`, by type: `{"has-preprint": ["10.1101/…"]}`.
    relations: dict[str, list[str]] = Field(default_factory=dict)


class Provenance(BaseModel):
    """Not optional decoration: without it a run cannot say which mechanism found a
    paper."""

    model_config = ConfigDict(extra="forbid")

    discovered_by: DiscoveredBy
    discovered_round: int
    source: RegistrySource
    verification_path: VerificationPath
    #: What the agent claimed, retained when it differs from the registry's canonical title.
    proposed_title: str | None = None
    #: True when the proposal's title disagreed with the registry's but other signals
    #: (year, authors) corroborated it being the same paper. The goal is to identify the
    #: right papers, not to police titles — so drift is accepted and flagged rather than
    #: rejected. The registry's title wins; the proposal's is kept above.
    title_drift: bool = False
    #: The labelled condition that decided the match (e.g. "A: high similarity"). Recorded
    #: so the rule that fired is recoverable from the log, not merely its category.
    match_condition: str | None = None
    match_similarity: float | None = None
    #: Set when the record is a preprint whose version of record could not be found,
    #: or was found. Provenance for extracted data must be traceable.
    is_version_of_record: bool | None = None
    #: Set when the primary registry's record for this DOI disagreed with the proposal and
    #: the failover's agreed. Holds the primary's title, so the corruption is on the
    #: record rather than silently corrected around.
    registry_disagreement: str | None = None
    preprint_doi: str | None = None
    version_of_record_doi: str | None = None
    #: The proposer's self-reported `source` and `found_via`. Disclosure only.
    proposer_source: ProposerSource | None = None
    found_via: str | None = None
    #: Which step of the title search found it — OpenAlex's title filter, OpenAlex's
    #: general `search=`, or Crossref.
    title_search_step: Literal["filter", "search", "crossref"] | None = None
    #: The proposer gave a DOI that failed and the title recovered the paper. The record
    #: carries the registry's DOI; this says what happened to the proposer's.
    proposed_doi_failed: Literal["unresolved", "mismatch", "malformed"] | None = None
    #: The proposer's own DOI, whenever it failed or differs from `doi`.
    proposed_doi: str | None = None
    #: Why the proposer named this paper, in its words (audit only).
    proposer_explanation: str | None = None


class PriorVerdict(BaseModel):
    """A verdict a later one replaced.

    On re-judging, the later judgment replaces the earlier, and the earlier is kept for
    debugging — with the config generation it was made under, so a reader can
    tell a criteria change from judge noise.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict | None = None
    reason: str | None = None
    decided_by: str | None = None
    config_generation: int | None = None
    criterion: str | None = None


class Relevance(BaseModel):
    """Judge output. Original verdict and reason are preserved rather than overwritten —
    for audit, not for feedback. A replaced verdict goes to `history`."""

    model_config = ConfigDict(extra="forbid")

    agent_verdict: Verdict | None = None
    agent_reason: str | None = None
    #: The numbered inclusion or exclusion criterion the judge says decided it, or `"year
    #: limits"` for a rule exclusion. Resolved to its text against `config_generation`.
    criterion: str | None = None
    #: Who decided. A seed enters the greenlist because the **user**
    #: supplied it as a relevant paper, not because a judge ruled on it — and the audit
    #: trail must say which, since the two carry very different weight. A user assertion
    #: is the strongest relevance evidence available; a judge verdict is an opinion.
    #: `None` means unjudged.
    decided_by: str | None = None
    #: True when judged on title alone because no abstract was available.
    judged_without_abstract: bool = False
    #: The config generation the current verdict was made under. After the last steer every judged record carries the final generation,
    #: which is what makes "all candidates were judged against the final criteria" checkable.
    config_generation: int | None = None
    #: Verdicts this one replaced, oldest first.
    history: list[PriorVerdict] = Field(default_factory=list)

    def supersede(self) -> None:
        """Move the current verdict into `history`, if there is one."""
        if self.agent_verdict is not None:
            self.history.append(PriorVerdict(
                verdict=self.agent_verdict, reason=self.agent_reason,
                decided_by=self.decided_by, config_generation=self.config_generation,
                criterion=self.criterion))


class PaperRecord(BaseModel):
    """A verified paper.

    `doi` is the record's **identifier**: a normalized DOI, or -- for a work the registry
    indexes without one -- ``openalex:W...`` (`normalize.REGISTRY_ID_PREFIX`). Every place that keys on `doi` therefore works for both
    kinds, and :attr:`has_doi` says which this is, so an export never passes a registry ID
    off as a DOI.
    """

    model_config = ConfigDict(extra="ignore")

    doi: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    #: The print or issue year, recorded when the year limits had to consult it.
    year_print: int | None = None
    #: How the year limits decided this record, when they had to look past `year`:
    #: `"print"` or `"online"` (admitted on that Crossref date), `"all dates"` (Crossref
    #: consulted, every date outside), `"earliest only"` (no other date was available — no
    #: DOI, or no Crossref), `"unknown"` (no year at all; sent to the judge rather than
    #: excluded on missing data), or `None` (passed on `year`, or no limits set).
    year_basis: str | None = None
    venue: str | None = None
    type: str | None = None

    oa_status: str | None = None
    oa_url: str | None = None
    oa_pdf_url: str | None = None
    oa_version: str | None = None
    oa_license: str | None = None

    cited_by_count: int | None = None
    #: The OpenAlex work ID (``W...``), and the work IDs its reference list cites. Set when
    #: the record was built from an OpenAlex work; `None` and empty from Crossref, and on
    #: records made before reference lists were kept.
    openalex_id: str | None = None
    references: list[str] = Field(default_factory=list)
    abstract: str | None = None
    #: Where `abstract` came from: the verifying registry (`openalex`, `crossref`) or
    #: `europe_pmc`, which the round asks when the registry had none. `None` when there is
    #: no abstract, and on older records, where it was the verifying registry.
    abstract_source: str | None = None

    extra_fields: dict[str, Any] = Field(default_factory=dict)
    #: Volume, issue, pages, publisher, retraction and the rest; empty on older records,
    #: which `export` fills from the registries.
    bibliographic: Bibliographic = Field(default_factory=Bibliographic)

    provenance: Provenance
    relevance: Relevance = Field(default_factory=Relevance)

    @field_validator("doi")
    @classmethod
    def _doi_normalized(cls, v: str) -> str:
        if v.startswith(REGISTRY_ID_PREFIX):
            if not is_record_identifier(v):
                raise ValueError(f"PaperRecord.doi: malformed registry identifier {v!r}")
            return v
        n = normalize_doi(v)
        if n is None:
            raise ValueError(
                f"PaperRecord.doi must be a valid DOI or an {REGISTRY_ID_PREFIX}W... "
                f"identifier, got {v!r}")
        return n

    @property
    def has_doi(self) -> bool:
        return not self.doi.startswith(REGISTRY_ID_PREFIX)


class LocalRecord(BaseModel):
    """A seed paper whose title resolved to no DOI.

    Kept rather than dropped: discarding it would let the model re-propose a paper the
    user personally supplied, which would look broken. Carries a local id and enters the
    redlist by title, but cannot be DOI-matched and so is excluded from scoring.
    """

    model_config = ConfigDict(extra="ignore")

    local_id: str
    #: `None` for a seed with no usable title: such a record exists for
    #: the digest and the `seed` command, not for matching, and a filename must never
    #: stand in for a title — nothing generated or rejected ever has that shape.
    title: str | None
    source_path: str | None = None
    provenance: Provenance


# --------------------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------------------


class Rejection(BaseModel):
    """A proposal that did not become a record."""

    model_config = ConfigDict(extra="forbid")

    reason: RejectionReason
    proposal_title: str
    proposed_doi: str | None = None
    #: For `doi_title_mismatch`, the registry's title — both strings are recorded so the
    #: mismatch can be re-routed as a fresh proposal rather than silently discarded.
    registry_title: str | None = None
    #: The registry work actually evaluated: what an out-of-scope rejection redlists,
    #: and, for a mismatch, the record the proposer's DOI belongs to.
    registry_identifier: str | None = None
    detail: str | None = None
    round: int | None = None
    #: For a failed title search: how many candidates the registry
    #: returned, and the best title similarity among them. What separates "nothing like it
    #: is registered" from "a similar title is" — the only evidence about fabrication the
    #: tool can hold, and not proof of it either way.
    candidates: int | None = None
    best_similarity: float | None = None
    #: The proposer's self-reported `source`: whether rejections differ between
    #: papers it recalled and papers it found by searching.
    proposer_source: ProposerSource | None = None
    #: Every title-search step tried, as `{step, candidates, best_similarity}`. Set on a
    #: failed title search, and on a DOI rejection whose title fallback also failed.
    title_attempts: list[dict[str, Any]] | None = None


class VerificationOutcome(BaseModel):
    """Result of running one proposal through the verification cascade. Exactly one of
    ``record`` / ``rejection`` is set."""

    model_config = ConfigDict(extra="forbid")

    record: PaperRecord | None = None
    rejection: Rejection | None = None
    #: The proposer's DOI failed but the title recovered the paper. Carried so the loop can
    #: apply the DOI's redlisting (an unresolved DOI) without counting a rejection.
    doi_failure: Rejection | None = None

    @property
    def accepted(self) -> bool:
        return self.record is not None

    def model_post_init(self, __context: Any) -> None:
        if (self.record is None) == (self.rejection is None):
            raise ValueError("exactly one of record / rejection must be set")


# --------------------------------------------------------------------------------------
# Round log
# --------------------------------------------------------------------------------------


class RoundLog(BaseModel):
    """One append-only JSONL line per round.

    The run's audit trail. Note `proposals` counts every proposal seen **before** dedup.
    """

    model_config = ConfigDict(extra="forbid")

    round: int
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    epoch: int = 0

    proposals: int = 0
    #: Entries the agent emitted that failed schema validation and never became
    #: proposals. Recorded because `proposals` counts what *parsed*: without this the
    #: model looks like it under-produced when in fact the parser rejected its output, and
    #: a **parser** failure would be read as **model** behaviour.
    #: `proposals + proposals_dropped` is what the agent actually emitted.
    proposals_dropped: int = 0
    #: Proposals re-verified after a registry error. `proposals + retried` is what
    #: this round verified: `verified + sum(rejected)`.
    retried: int = 0
    verified: int = 0
    new: int = 0
    duplicates: int = 0
    rejected: dict[str, int] = Field(default_factory=dict)

    corpus_size: int = 0
    uncertain_size: int = 0
    excluded_size: int = 0

    new_per_round_curve: list[int] = Field(default_factory=list)
    #: Papers the judge accepted each round. Reported alongside the verification
    #: curve because the gap between them is the off-topic-drift signal.
    included_per_round_curve: list[int] = Field(default_factory=list)

    #: Diagnostics for the generate half: how often the model had a paper but no DOI, and
    #: how often it supplied an unparseable one. The prompt asks for `null` whenever the
    #: model does not know a DOI exactly, so the null rate measures how often it is unsure
    #: of one.
    null_doi_rate: float | None = None
    malformed_doi_rate: float | None = None
    abstract_coverage: float | None = None
    certainty_ratio: float | None = None

    config_changed: bool = False
    #: What the proposer offered this round: parsed plus dropped entries, excluding
    #: retries from the queue. `proposals` counts what was
    #: *verified against*, which includes retries; the supply signal needs what the model
    #: itself produced.
    offered: int | None = None
    #: Why the proposer returned fewer than `papers_per_round`, in its own words. Required
    #: by `submit` for a short round. Asked as *why fewer?* rather than as a yes/no flag,
    #: which a model reads literally; this question can always be answered truthfully.
    shortfall_reason: str | None = None
    #: Why the proposer declined this round's `references` candidates, in its own words:
    #: the export quotes it, so a scope choice outside the criteria is visible there.
    decline_reason: str | None = None
    #: The model the proposals file says wrote it —
    #: **self-reported**, unverified; "not stated" when absent.
    proposer_model: str | None = None
    #: Proposals whose DOI failed and whose title recovered the paper.
    doi_recovered_by_title: int | None = None
    #: This round's parsed proposals by their self-reported `source`.
    from_recall: int | None = None
    from_search: int | None = None
    from_references: int | None = None
    source_not_stated: int | None = None
    #: Each registry session's report for the command (auth, requests, a trip).
    registry: dict[str, Any] | None = None
