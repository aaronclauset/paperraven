"""Run state: the four-way partition, dedup, and the append-only round log.

Every proposal is logged *before* dedup, including duplicates, so the round log records
everything the proposer offered and not only what was new.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import (
    DiscoveredBy,
    LocalRecord,
    PaperProposal,
    PaperRecord,
    Rejection,
    RejectionReason,
    RoundLog,
    Verdict,
)
from .audit import AuditEvent
from .normalize import normalize_doi, normalize_title
from .verify import KnownState

STATE_FILENAME = "state.json"
ROUNDS_LOG_FILENAME = "rounds.jsonl"

#: On-disk shape of `state.json`. Bump when the shape changes, and
#: decide explicitly what happens to existing state — never let a missing key be absorbed
#: by a default, which is how a round gets skipped or repeated.
SCHEMA_VERSION = 1
#: Every key `state.json` holds. `_save` writes all of them and `load` refuses a file missing
#: any, so a damaged file is never read as an empty corpus.
STATE_KEYS = (
    "schema_version", "state_generation", "completed_rounds", "audit_events",
    "config_generation", "config_digest", "config_file_sha256", "epoch",
    "greenlist", "uncertain", "excluded", "local_records", "redlist_dois", "redlist_titles",
    "rejudge_pending", "new_per_round", "included_per_round", "retry_queue",
)


class StateVersionError(RuntimeError):
    """Persisted state is present but not loadable at this schema version.

    Deliberately distinct from "no state at all". :meth:`Corpus.load` returns an empty
    corpus when `state.json` is absent, which is right for *this run has not been
    initialised* and catastrophic for *this run's state is unreadable* — the second would
    silently start a fresh corpus over an existing run directory and discard everything
    it had cost to build. The two must never share a code path.
    """


def _fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable.

    Best-effort: a filesystem that refuses to open a directory read-only, or to sync one,
    costs durability of the *rename* and nothing else — the data is already synced and the
    replace already happened. Failing the write here would turn a weaker guarantee into a
    lost round, which is the worse trade.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, newline: str | None = None) -> None:
    """Write ``text`` to ``path`` atomically.

    Writes to a temporary file in the same directory and then ``os.replace``, which is
    atomic on POSIX. An interrupted run therefore leaves the previous state intact rather
    than a half-written file — the corpus is the only irreplaceable thing here, since
    every round costs real model calls.

    **Three syncs, three different jobs.** Fsyncing the temp file makes
    its *bytes* durable; ``os.replace`` makes the swap *atomic*; fsyncing the *directory*
    makes the rename itself durable. Without the third, an OS crash can lose a rename whose
    data was already safely on disk — the file would still hold the old contents.

    The claim this buys is scoped deliberately: state survives **process death and OS
    crash**. It is **not** claimed to survive power loss on hardware with a volatile write
    cache, and an unscoped durability claim is worse than a modest one because someone
    eventually relies on it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    mask = os.umask(0)
    os.umask(mask)
    os.chmod(tmp, 0o666 & ~mask)        # an ordinary file's mode, not mkstemp's 0600
    try:
        # `newline=""` for CSV text, which carries its own `\r\n`: translating it again on
        # Windows would give `\r\r\n`, blank rows in Excel, and wrong row counts.
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        # Leave no partial file behind, and leave the original untouched.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@dataclass
class Corpus:
    """The four-way state partition, persisted under a run directory.

    | set | contents | purpose |
    |---|---|---|
    | greenlist | `include` verdicts | the deliverable |
    | uncertain | `uncertain` verdicts | a **separate pool**, not part of the corpus |
    | excluded  | `exclude` verdicts, with reason | audit trail |
    | redlist   | everything seen | prevents re-proposal |

    `uncertain` deliberately does **not** count toward the corpus: discarding it would be
    an irreversible call made by the weakest component in the pipeline, and promoting it
    would inflate the deliverable.
    """

    run_dir: Path

    greenlist: dict[str, PaperRecord] = field(default_factory=dict)
    uncertain: dict[str, PaperRecord] = field(default_factory=dict)
    excluded: dict[str, PaperRecord] = field(default_factory=dict)
    local_records: dict[str, LocalRecord] = field(default_factory=dict)

    #: Redlist = everything seen, including seeds and excluded papers. An excluded paper
    #: must stay here or it gets re-proposed every round forever.
    redlist_dois: set[str] = field(default_factory=set)
    redlist_titles: set[str] = field(default_factory=set)



    #: Bumped when the user steers the topic, because rounds before and after a steer
    #: sample different populations; the supply signal counts only this epoch's rounds.
    epoch: int = 0

    #: Papers that passed **verification** each round. This is what the round loop can
    #: count at the moment a round ends, because judging happens afterwards.
    new_per_round: list[int] = field(default_factory=list)

    #: Papers the judge **accepted into the greenlist** each round.
    #:
    #: Two curves, not one, because they answer different questions and the *gap between
    #: them is itself a signal*. `new_per_round` measures whether the proposer is naming
    #: papers that exist; `included_per_round` measures whether they are the right papers.
    #: A run where the first stays at 5 and the second sits at 0 is a proposer drifting
    #: off-topic. Set by `recount_included`, called when verdicts are applied
    #: rather than when verification finishes, and only for rounds with a first verdict in
    #: the batch — a re-judge never rewrites an earlier round's entry.
    included_per_round: list[int] = field(default_factory=list)

    #: `registry_error` is our failure, not the paper's, so it is retried rather than
    #: rejected.
    retry_queue: list[dict[str, Any]] = field(default_factory=list)
    #: Records a topic steer marked for re-judging
    #: under the new criteria. **Marked, not moved**: each stays in its pool, so the
    #: greenlist the user has been reading is intact until the new verdicts arrive.
    rejudge_pending: set[str] = field(default_factory=set)

    #: Monotonic commit counter. It orders audit events and makes
    #: their ids unique — it does **no** concurrency work, because the whole-command lock
    #: means nothing can interleave. Recorded here so a later reader does not mistake it
    #: for a compare-and-swap guard and "simplify" the lock away.
    state_generation: int = 0

    #: Which immutable config generation this run is on, and the digest proving that
    #: file unchanged. **State names the live configuration**; nothing
    #: reads a generation state does not name. `None` before `init` commits.
    config_generation: int | None = None
    config_digest: str | None = None
    #: The SHA-256 of the committed generation file's **bytes**, beside
    #: `config_digest`, which is over the config *as the schema reads it*. With both, a
    #: mismatch can be told apart: bytes unchanged means the schema changed, not the file.
    config_file_sha256: str | None = None

    #: Provenance for every commit, in the same atomic write as the mutation it
    #: describes. `rounds.jsonl` and `rounds/NNN/verified.json` are projections of this,
    #: which is what makes them genuinely regenerable.
    audit_events: list[AuditEvent] = field(default_factory=list)

    #: Rounds whose commit succeeded. **The authority for round progression.**
    #: Counting rounds in the log instead would make the *audit log* load-bearing for
    #: control flow: a crash after the log append but before the state write would leave
    #: the log claiming a round that state knew nothing about, and restart would skip it.
    #: One field, derived from nothing.
    completed_rounds: set[int] = field(default_factory=set)


    # ---------------------------------------------------------------------------------
    # Paths
    # ---------------------------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.run_dir / STATE_FILENAME

    @property
    def rounds_log_path(self) -> Path:
        return self.run_dir / ROUNDS_LOG_FILENAME

    def round_dir(self, round_index: int) -> Path:
        return self.run_dir / "rounds" / f"{round_index:03d}"

    @property
    def next_round(self) -> int:
        """The round a fresh `submit` would run.

        Derived from :attr:`completed_rounds` rather than stored alongside it: two fields
        that must agree are two fields that can disagree.
        """
        return max(self.completed_rounds, default=0) + 1

    @property
    def initialized(self) -> bool:
        """Whether state has ever been committed for this run.

        `state_generation == 0` means no commit has happened, which is what distinguishes
        an uninitialised run from one whose state could not be read.
        """
        return self.state_generation > 0

    def complete_round(self, round_index: int) -> None:
        """Mark a round committed. Idempotent — a set, so a retry cannot double-count."""
        self.completed_rounds.add(round_index)

    def commit(self, event: AuditEvent) -> AuditEvent:
        """Persist state together with the event describing why it changed.

        The transaction boundary: the mutation and its provenance land
        in one :func:`atomic_write_text`, so a crash takes both or neither. Callers that
        mutate state should use this rather than :meth:`save`, which is the lower-level
        primitive and records nothing.

        The event is stamped **here** rather than by the caller, because its id is the
        generation it commits at and only this method knows that. A failed write leaves
        the event unrecorded and the counter unmoved — contiguous ids are what make a
        projection with a gap in it detectable.

        .. warning::
           **A failed commit poisons this object.** The rollback undoes the appended
           event, not the caller's preceding in-memory mutations — `run_round` has already
           called `complete_round`, for instance. Disk is correct and a fresh
           :meth:`load` is correct; *this* corpus is not. Callers that catch the exception
           must reload rather than reuse. Nothing in the CLI does, because a failed commit
           ends the command.
        """
        # Stamped here rather than by callers so an event cannot be written without
        # naming the configuration it ran under. Four call sites each remembering to do
        # this is four chances to forget.
        event.config_generation = self.config_generation
        event.config_digest = self.config_digest
        event.generation_before = self.state_generation
        event.event_id = self.state_generation + 1
        self.audit_events.append(event)
        try:
            self._save()
        except BaseException:
            self.audit_events.pop()
            event.event_id = 0
            raise
        return event

    # ---------------------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------------------

    def known_state(self) -> KnownState:
        """What the verifier should treat as already seen."""
        return KnownState(dois=set(self.redlist_dois),
                          normalized_titles=set(self.redlist_titles))

    @property
    def corpus_size(self) -> int:
        """Greenlist only. `uncertain` is a separate pool and is not counted."""
        return len(self.greenlist)

    @property
    def seeds_in_corpus(self) -> int:
        """Greenlisted papers the user supplied as seeds."""
        return sum(1 for r in self.greenlist.values()
                   if r.provenance.discovered_by is DiscoveredBy.SEED)

    @property
    def found_size(self) -> int:
        """Greenlisted papers the tool found — what `target_papers` counts."""
        return self.corpus_size - self.seeds_in_corpus

    @property
    def judged_count(self) -> int:
        """Records the relevance judge has actually ruled on.

        Distinct from pool membership: an **unjudged** record is held in the uncertain
        pool (the greenlist means "judged relevant", so defaulting to inclusion would
        inflate the deliverable), but it has no verdict yet and must not be counted as
        though the judge had called it uncertain.
        """
        return sum(
            1
            for pool in (self.greenlist, self.uncertain, self.excluded)
            for rec in pool.values()
            if rec.relevance.agent_verdict is not None
        )

    @property
    def certainty_ratio(self) -> float | None:
        """Fraction of **judged** records that were certain.

        Tracked because a *rising* uncertain fraction is independent evidence that the
        clearly-relevant material is running out — a saturation signal that rests on no
        assumptions about sampling.

        Returns ``None`` until something has been judged. The denominator is judged
        records, not pool membership: counting unjudged records as judged-uncertain would
        read 100% uncertain on round 1 and fire the secondary stop trigger before the judge
        had run at all.
        """
        judged = self.judged_count
        if judged == 0:
            return None
        certain = sum(
            1
            for pool in (self.greenlist, self.excluded)
            for rec in pool.values()
            if rec.relevance.agent_verdict is not None
        )
        return certain / judged

    # ---------------------------------------------------------------------------------
    # Mutation
    # ---------------------------------------------------------------------------------

    def add_record(self, record: PaperRecord, verdict: Verdict | None = None) -> str:
        """File a verified record into the partition its verdict implies.

        An unjudged record is held in `uncertain` rather than admitted to the greenlist:
        the greenlist means "judged relevant", and defaulting to inclusion would inflate
        the deliverable with unreviewed papers.
        """
        doi = record.doi
        self.redlist_dois.add(doi)
        nt = normalize_title(record.title)
        if nt:
            self.redlist_titles.add(nt)

        effective = verdict or record.relevance.agent_verdict
        # Persist an explicitly-passed verdict onto the record, not just into the routing
        # decision. Otherwise there are two ways to express a verdict and the metrics see
        # only one: a record filed with `verdict=INCLUDE` lands in the greenlist while
        # `judged_count` still reads it as unjudged, so `certainty_ratio` reports `None`
        # on a fully judged corpus.
        if verdict is not None:
            record.relevance.agent_verdict = verdict

        if effective == Verdict.INCLUDE:
            self.greenlist[doi] = record
            self.uncertain.pop(doi, None)
            self.excluded.pop(doi, None)
            return "greenlist"
        if effective == Verdict.EXCLUDE:
            self.excluded[doi] = record
            self.greenlist.pop(doi, None)
            self.uncertain.pop(doi, None)
            return "excluded"
        # Clear the other pools here too, as the INCLUDE and EXCLUDE branches above do, so
        # the four-way partition stays a partition. Otherwise a paper re-judged from INCLUDE
        # or EXCLUDE back to UNCERTAIN would sit in two pools at once: `judged_count`
        # iterates all three pools, so it would be counted **twice** and inflate
        # `certainty_ratio`'s denominator, and `corpus_size` would go on counting it as a
        # deliverable.
        self.uncertain[doi] = record
        self.greenlist.pop(doi, None)
        self.excluded.pop(doi, None)
        return "uncertain"

    def recount_included(self, rounds) -> None:
        """Set `included_per_round` for each round, from what that round discovered.

        A **recount**, not a delta: crediting the round being judged with the net change in
        greenlist size would let a re-judge of earlier rounds' records, arriving in the same
        request, corrupt the entry the saturation rule reads. Callers pass only rounds with
        a **first** verdict in the batch: a re-judge never rewrites an earlier round's
        entry, which records what that round's judging found at the time. Padded, so a round in which judging never
        ran records a 0 rather than shifting every later entry.
        """
        for r in sorted(set(rounds)):
            if r is None or r < 1:
                continue
            while len(self.included_per_round) < r:
                self.included_per_round.append(0)
            self.included_per_round[r - 1] = sum(
                1 for rec in self.greenlist.values()
                if rec.provenance.discovered_round == r
                and rec.provenance.discovered_by is not DiscoveredBy.SEED)

    def add_local_record(self, record: LocalRecord, redlist: bool = True) -> None:
        """Register a seed paper whose title resolved to no DOI.

        Kept rather than dropped: discarding it would let the model re-propose a paper
        the user personally supplied, which would look broken. It enters the redlist by
        title but cannot be DOI-matched, so scoring must exclude it.
        """
        self.local_records[record.local_id] = record
        nt = normalize_title(record.title) if record.title and redlist else ""
        if nt:
            self.redlist_titles.add(nt)

    def find_record(self, identifier: str) -> PaperRecord | None:
        """The record with this identifier, in whichever pool it is."""
        return (self.greenlist.get(identifier) or self.uncertain.get(identifier)
                or self.excluded.get(identifier))

    def mark_for_rejudge(self) -> int:
        """Mark every judged record except seeds for re-judging.

        Every pool is marked, not only `uncertain`; seeds are exempt.
        Rule verdicts (`rule (year)`) are not marked: they are re-applied by rule.
        Returns how many were marked.
        """
        from .models import RULING_DECIDED_BY
        from .seeds import SEED_DECIDED_BY
        from .years import RULE_DECIDER

        before = len(self.rejudge_pending)
        for pool in (self.greenlist, self.uncertain, self.excluded):
            for rec in pool.values():
                if (rec.relevance.agent_verdict is not None
                        and rec.relevance.decided_by not in (SEED_DECIDED_BY, RULE_DECIDER,
                                                             RULING_DECIDED_BY)
                        and rec.provenance.discovered_by is not DiscoveredBy.SEED):
                    self.rejudge_pending.add(rec.doi)
        return len(self.rejudge_pending) - before

    def remove_local_record(self, local_id: str) -> "LocalRecord | None":
        """Drop a local record once its paper has been resolved (the `seed` command)."""
        return self.local_records.pop(local_id, None)

    def note_rejection(
        self, rejection: Rejection, proposal: PaperProposal | None = None, retries: int = 0
    ) -> None:
        """Record a rejection's effect on state.

        Rejected-but-real papers still enter the redlist so they are not re-proposed.
        `registry_error` is the exception: it is our outage, the paper was never judged,
        so it goes to the retry queue and is deliberately **not** redlisted.

        The **whole proposal** is queued when available, not just the rejection. Queuing
        only the rejection dropped `year` and `authors`, so a requeued proposal arrived
        without the signals the matcher needs to corroborate a drifted title — it could
        fail on retry where the original would have succeeded.
        """
        if rejection.reason.is_retryable:
            self.retry_queue.append({
                "rejection": rejection.model_dump(mode="json"),
                "proposal": proposal.model_dump(mode="json") if proposal else None,
                "retries": retries,
            })
            return
        if rejection.reason in (RejectionReason.REGISTRY_GAVE_UP,
                                RejectionReason.REGISTRY_QUERY_REJECTED):
            return                      # our failure, not the paper's: nothing is redlisted
        # A rejection redlists **what was checked and found wanting**. A DOI-based
        # rejection tested the DOI; the title was never searched, so it stays proposable
        # through `doi: null` — the recovery route the skill recommends. Redlisting it would
        # make a paper refused for a corrupt registry record permanently unrecoverable. A
        # failed *title search* did test the title, and redlisting it is the
        # anti-hallucination case. A malformed DOI tested nothing.
        #
        # **A DOI/title mismatch redlists nothing.** The mismatch shows the DOI is real and
        # belongs to a *different* paper, one the tool has not evaluated; the redlist means
        # "already seen", and it was not. Redlisting the DOI would refuse a later proposal of
        # the paper it belongs to as a duplicate. An unresolved DOI still redlists the DOI:
        # no paper has it. An out-of-scope type redlists **the work evaluated**, never the
        # proposer's DOI: after a mismatch, that DOI belongs to some other, unevaluated paper.
        R = RejectionReason
        if rejection.reason is R.DOI_UNRESOLVED:
            d = normalize_doi(rejection.proposed_doi)
            if d:
                self.redlist_dois.add(d)
        if rejection.reason is R.OUT_OF_SCOPE_TYPE and rejection.registry_identifier:
            ident = rejection.registry_identifier
            self.redlist_dois.add(normalize_doi(ident) or ident)
        # A failed title search redlists the title -- including a DOI rejection whose
        # title fallback failed too: the title was checked, every step, and found wanting.
        # Not when a registered record matched the title but its year and authors
        # contradicted the proposal -- the title is real, and redlisting it would refuse the
        # paper that does carry it as a duplicate.
        doi_failed = (R.DOI_UNRESOLVED, R.DOI_TITLE_MISMATCH, R.MALFORMED_DOI)
        refused = any(a.get("refused") for a in rejection.title_attempts or [])
        if not refused and (rejection.reason is R.NO_DOI_NO_TITLE_MATCH
                            or (rejection.reason in doi_failed and rejection.title_attempts)):
            nt = normalize_title(rejection.proposal_title)
            if nt:
                self.redlist_titles.add(nt)

    def open_new_epoch(self) -> int:
        """Steering opens a new epoch.

        Corpus and redlist carry over untouched — only the per-epoch supply signal
        restarts, because rounds before and after a steer sample different populations.
        """
        self.epoch += 1
        return self.epoch

    # ---------------------------------------------------------------------------------
    # Round log
    # ---------------------------------------------------------------------------------

    # `rounds.jsonl` is a **projection** of committed audit events. `audit.write_projections`
    # is its only writer, and it rewrites the file wholesale.

    def read_round_logs(self) -> list[dict[str, Any]]:
        if not self.rounds_log_path.exists():
            return []
        out = []
        for line in self.rounds_log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def build_round_log(
        self,
        round_index: int,
        *,
        proposals: int,
        proposals_dropped: int = 0,
        retried: int = 0,
        verified: int,
        new: int,
        duplicates: int,
        rejected: dict[str, int],
        null_doi_rate: float | None = None,
        malformed_doi_rate: float | None = None,
        config_changed: bool = False,
        offered: int | None = None,
        shortfall_reason: str | None = None,
        decline_reason: str | None = None,
        proposer_model: str | None = None,
        found_by: dict[str, int] | None = None,
        doi_recovered_by_title: int | None = None,
        registry: dict[str, Any] | None = None,
    ) -> RoundLog:
        """Build this round's log line."""
        found_by = found_by or {}
        return RoundLog(
            round=round_index,
            epoch=self.epoch,
            proposals=proposals,
            proposals_dropped=proposals_dropped,
            retried=retried,
            verified=verified,
            new=new,
            duplicates=duplicates,
            rejected=dict(rejected),
            corpus_size=self.corpus_size,
            uncertain_size=len(self.uncertain),
            excluded_size=len(self.excluded),
            new_per_round_curve=list(self.new_per_round),
            included_per_round_curve=list(self.included_per_round),
            null_doi_rate=null_doi_rate,
            malformed_doi_rate=malformed_doi_rate,
            certainty_ratio=self.certainty_ratio,
            config_changed=config_changed,
            offered=offered,
            shortfall_reason=shortfall_reason,
            decline_reason=decline_reason,
            proposer_model=proposer_model,
            from_recall=found_by.get("recall"),
            from_search=found_by.get("search"),
            from_references=found_by.get("references"),
            source_not_stated=found_by.get("not stated"),
            doi_recovered_by_title=doi_recovered_by_title,
            registry=registry,
        )

    # ---------------------------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------------------------

    def _save(self) -> None:
        """Persist state atomically. **Private: use :meth:`commit`.**

        A public save would let a state generation advance with no audit event, which is
        how provenance silently becomes incomplete. `commit` is the only way to persist a mutation; this is the primitive underneath it.
        """
        payload = {
            "epoch": self.epoch,
            "greenlist": {k: v.model_dump(mode="json") for k, v in self.greenlist.items()},
            "uncertain": {k: v.model_dump(mode="json") for k, v in self.uncertain.items()},
            "excluded": {k: v.model_dump(mode="json") for k, v in self.excluded.items()},
            "local_records": {k: v.model_dump(mode="json")
                              for k, v in self.local_records.items()},
            "redlist_dois": sorted(self.redlist_dois),
            "rejudge_pending": sorted(self.rejudge_pending),
            "redlist_titles": sorted(self.redlist_titles),
            "new_per_round": list(self.new_per_round),
            "included_per_round": list(self.included_per_round),
            "retry_queue": list(self.retry_queue),
            "schema_version": SCHEMA_VERSION,
            "state_generation": self.state_generation + 1,
            "completed_rounds": sorted(self.completed_rounds),
            "audit_events": [e.model_dump(mode="json") for e in self.audit_events],
            "config_generation": self.config_generation,
            "config_digest": self.config_digest,
            "config_file_sha256": self.config_file_sha256,
        }
        atomic_write_text(self.state_path, json.dumps(payload, indent=2, sort_keys=True))
        # Only after the write lands: a failed commit must not advance the counter, or the
        # next successful one would leave a gap in the audit event ids.
        self.state_generation += 1

    @classmethod
    def load(cls, run_dir: str | Path) -> "Corpus":
        """Load state, or return an empty corpus if the run is new.

        Resumability is the point: a crash, a `/clear`, or a closed laptop
        should cost nothing, and rounds may be days apart.
        """
        run_dir = Path(run_dir)
        corpus = cls(run_dir=run_dir)
        if not corpus.state_path.exists():
            # Not initialised. `state_generation` stays 0, so `initialized` is False and
            # the caller can tell this apart from a state file it refused to read.
            return corpus

        data = json.loads(corpus.state_path.read_text(encoding="utf-8"))
        found = data.get("schema_version")
        if found != SCHEMA_VERSION:
            raise StateVersionError(
                f"{corpus.state_path} has schema_version {found!r}, expected "
                f"{SCHEMA_VERSION}. This run's state was written by a different version "
                f"of PaperRaven. Migration is deliberate, "
                f"not automatic — no state is silently reinterpreted."
            )
        missing = [key for key in STATE_KEYS if key not in data]
        if missing:
            raise StateVersionError(
                f"{corpus.state_path} declares schema_version {SCHEMA_VERSION} but is missing "
                f"{', '.join(map(repr, missing))}. A damaged state file is refused, never read "
                f"as an empty corpus: guessing the missing part is how papers, verdicts or a "
                f"round are silently lost. Restore the file from a backup, or start a new run."
            )
        corpus.state_generation = int(data["state_generation"])
        corpus.completed_rounds = {int(r) for r in data["completed_rounds"]}
        corpus.audit_events = [AuditEvent.model_validate(e)
                               for e in (data.get("audit_events") or [])]
        corpus.config_generation = data.get("config_generation")
        corpus.config_digest = data.get("config_digest")
        corpus.config_file_sha256 = data.get("config_file_sha256")
        corpus.epoch = int(data.get("epoch", 0))
        corpus.greenlist = {k: PaperRecord.model_validate(v)
                            for k, v in (data.get("greenlist") or {}).items()}
        corpus.uncertain = {k: PaperRecord.model_validate(v)
                            for k, v in (data.get("uncertain") or {}).items()}
        corpus.excluded = {k: PaperRecord.model_validate(v)
                           for k, v in (data.get("excluded") or {}).items()}
        corpus.local_records = {k: LocalRecord.model_validate(v)
                                for k, v in (data.get("local_records") or {}).items()}
        corpus.redlist_dois = set(data.get("redlist_dois") or [])
        corpus.rejudge_pending = set(data.get("rejudge_pending") or [])
        corpus.redlist_titles = set(data.get("redlist_titles") or [])
        corpus.new_per_round = list(data.get("new_per_round") or [])
        corpus.included_per_round = list(data.get("included_per_round") or [])
        corpus.retry_queue = list(data.get("retry_queue") or [])
        # Applied at the one place every count reads from: a seed is never awaiting
        # re-judging, whatever version wrote the state, nor is a DOI in no pool. Saved by
        # the next command that commits.
        corpus.rejudge_pending -= {
            d for d in corpus.rejudge_pending
            if (rec := corpus.find_record(d)) is None
            or rec.provenance.discovered_by is DiscoveredBy.SEED}
        return corpus

    # ---------------------------------------------------------------------------------
    # Export
    # ---------------------------------------------------------------------------------

    # Export lives in `export.write_export`: the three pools reach the reader as
    # `papers.csv`, `excluded.csv` and `uncertain.csv`, and every record, labelled by
    # pool, reaches `audit/records.json`.
