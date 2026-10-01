"""Audit events — authoritative provenance, committed with the state it describes.

## Why events live in state rather than beside it

`rounds/NNN/verified.json` is not needed for future *control flow* — its effects (redlist
entries, the retry queue) already live in state. But it is **not** regenerable from state
alone: rejection details, the registry observations behind them, malformed-entry
accounting, verification denominators and null-DOI diagnostics exist nowhere else.

The distinction matters more here than it would elsewhere, because this tool's claim is
**verified provenance**, not merely resumability. A corpus can stay operationally correct
while its audit trail is permanently incomplete.

So a commit carries its own provenance: the complete semantic event goes into the same
atomic write as the mutation it describes, and `rounds.jsonl` and `rounds/NNN/verified.json`
are projections of it.

This is an **outbox, not a journal** — no second file, no pending/committed markers, no
recovery state machine.

## What is preserved, and what is deliberately not

**Preserved as exact bytes**: the agent's `proposals.json` and `judging_response.json`.
Without the literal output you cannot distinguish *"the model never said it"* from *"our
parser silently dropped it"* — for example, valid papers discarded because a field arrived
in an unexpected shape. For a tool whose thesis is that the LLM generates and deterministic
code verifies, being unable to reconstruct what the LLM generated undercuts the claim.

**Deliberately not stored:**

* **Seed PDFs** — publisher copyright, and large. Digest, path and extracted evidence only.
* **Raw registry responses** — OpenAlex payloads carry researcher emails and affiliations.
  The normalized fields verification actually used, the source, a retrieval timestamp and
  a response digest.

**Provenance means decision-relevant evidence, not indiscriminate archiving.**
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import __version__


class InboundArtifact(BaseModel):
    """Exactly what a host handed us, kept verbatim.

    `text` rather than bytes because both handoff files are JSON, which is UTF-8 by
    definition; `sha256` is over the encoded bytes so it can be compared against a file on
    disk without re-encoding assumptions.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    sha256: str
    text: str


def inbound_from_path(path: "Path") -> InboundArtifact:
    """Capture a handoff file as the **literal bytes the host wrote**.

    Reads bytes and decodes explicitly, rather than `read_text`, because text mode applies
    universal-newline translation: a CRLF file would be stored and digested as LF, so the
    artifact would not be the file and its digest could not verify what was submitted.
    "Exact model output" has to mean exact, or the forensic question
    it exists to answer — did the model say this, or did our parser lose it — is answered
    against a reconstruction.
    """
    data = Path(path).read_bytes()
    return InboundArtifact(
        name=Path(path).name,
        sha256=hashlib.sha256(data).hexdigest(),
        text=data.decode("utf-8"),
    )


class AuditEvent(BaseModel):
    """One state-mutating command, recorded completely.

    Identity is :attr:`event_id`, which equals the `state_generation` the event committed
    at. The generation is already unique per commit; epoch, round and operation are
    attributes, and `init`, `steer` and future migrations have no natural round to put in
    a key. If a single commit ever needs several events, the identity extends to
    `(state_generation, event_index)` — not a wider composite.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stamped by `Corpus.commit`. Zero means "not yet committed".
    event_id: int = 0
    generation_before: int = 0

    timestamp: str
    command: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    epoch: int | None = None
    round: int | None = None

    #: The configuration generation the event ran under; the digest proves that file
    #: unchanged.
    config_generation: int | None = None
    config_digest: str | None = None

    #: The host's literal output for this command, where there was one.
    inbound: InboundArtifact | None = None

    #: Counts *and* their denominators — a rate whose denominator is unrecoverable is not
    #: provenance, it is a number.
    counts: dict[str, int] = Field(default_factory=dict)

    #: Per-item outcomes with the evidence behind them: which papers were rejected, for
    #: what reason, against which registry observation. This content exists nowhere else
    #: in state.
    decisions: list[dict[str, Any]] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)

    #: Which code produced this event. Deliberately separate from
    #: `Politeness.user_agent_version`, which is config identifying the tool to a
    #: registry; the two must agree, and are kept apart rather than coupled.
    tool_version: str = __version__


def new_event(*, command: str, **fields: Any) -> AuditEvent:
    """An event stamped with the current time and the running version."""
    return AuditEvent(
        command=command,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **fields,
    )


# --------------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------------

#: Which events become a line in `rounds.jsonl`. Only `submit` advances a round, so only
#: `submit` has a round log.
PROJECTED_COMMAND = "submit"

OUTCOME_FILENAME = "verified.json"
JUDGING_OUTCOME_FILENAME = "judging_outcome.json"


def _round_line(event: AuditEvent) -> dict[str, Any]:
    return {"event_id": event.event_id, **event.result}


def _round_line_json(line: dict[str, Any]) -> str:
    """The **one** serializer for a `rounds.jsonl` line.

    One spelling, one place, so a line is always written the same way.
    """
    return json.dumps(line, sort_keys=True) + "\n"


def _projection_json(payload: dict[str, Any]) -> str:
    """The **one** serializer for per-round projection files.

    `sort_keys=True` is load-bearing: `state.json` is written sorted, so a decision dict
    comes back from disk in sorted key order while the in-memory one is in model-field
    order. Without sorting here, the same event would serialize to two different texts.
    """
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _verified_payload(event: AuditEvent,
                      superseded: list[AuditEvent]) -> dict[str, Any]:
    return {
        "round": event.round,
        "accepted": event.counts.get("verified", 0),
        "rejections": [{k: v for k, v in d.items() if k != "kind"}
                       for d in event.decisions if d.get("kind") == "rejected"],
    }


def _judging_payload(event: AuditEvent,
                     superseded: list[AuditEvent]) -> dict[str, Any]:
    """The newest verdicts pass for a round, saying so.

    A round may legitimately be judged more than once — `verdicts` reports records left
    unjudged and tells the user to re-run `judge`. Newest-wins is the right meaning for a
    file called `judging_outcome.json`, and the two trailing keys buy back the only thing
    it gives up: a reader's knowledge that they are looking at a view, and where the
    passes it replaced can be found (they are intact in state, which is authoritative).
    """
    return {
        "round": event.round,
        "verdicts_applied": event.counts.get("verdicts_applied", 0),
        "dropped": [{"raw": d.get("raw"), "error": d.get("error")}
                    for d in event.decisions if d.get("kind") == "dropped"],
        "unmatched_dois": event.result.get("unmatched_dois", []),
        "unrequested_dois": event.result.get("unrequested_dois", []),
        "unjudged_dois": event.result.get("unjudged_dois", []),
        # The skill's instructions tell the agent to read this. Empty for older events.
        "duplicate_dois": event.result.get("duplicate_dois", []),
        "summary": event.result.get("summary"),
        "judging_pass": len(superseded) + 1,
        "earlier_passes": [e.event_id for e in superseded],
    }


#: Per-round files, by the command whose event is their sole source, so each can be
#: regenerated from state.
PER_ROUND_PROJECTIONS = {
    "submit": (OUTCOME_FILENAME, _verified_payload),
    "verdicts": (JUDGING_OUTCOME_FILENAME, _judging_payload),
}


def _expected_files(corpus: Any) -> list[tuple[Any, "Path", str]]:
    """Every per-round projection the committed events imply, with its exact content.

    **One entry per path, always.** Events are grouped by `(command, round)` and the
    newest wins; the ones it supersedes are handed to the payload builder so the file can
    name them.

    `submit` has one event per round, which `completed_rounds` enforces, but `verdicts`
    can have several, because the CLI tells the user to re-run it when records are left
    unjudged. Without newest-wins, two events would name one path with different content.
    """
    groups: dict[tuple[str, int], list[AuditEvent]] = {}
    for event in corpus.audit_events:
        if event.command not in PER_ROUND_PROJECTIONS or event.round is None:
            continue
        groups.setdefault((event.command, event.round), []).append(event)

    out = []
    for (command, round_index), events in groups.items():
        # `audit_events` is already in commit order, so the last is the newest.
        *superseded, newest = events
        name, build = PER_ROUND_PROJECTIONS[command]
        out.append((newest, corpus.round_dir(round_index) / name,
                    _projection_json(build(newest, superseded))))
    # Ordered by the event that produced each file, so messages are stable.
    return sorted(out, key=lambda row: row[0].event_id)


def write_projections(corpus: Any) -> None:
    """Rewrite every projection from state. Writers call this after each commit.

    **This is the whole mechanism.** `rounds.jsonl` is at most `max_rounds` lines long, so
    it is rewritten whole rather than maintained incrementally, and there is no gap
    detection or repair.

    Properties, and the only ones that matter: idempotent, byte-identical whether the corpus
    is fresh or reloaded from disk, and atomic per file. **Readers never call it** — they
    take no lock, so a reader that wrote could race a writer; state is authoritative,
    and a stale view costs nothing until the next writer replaces it.

    `atomic_write_text` is imported here rather than at module scope because `corpus`
    imports this module; a top-level import would be a cycle.
    """
    from .corpus import atomic_write_text

    submits = [e for e in corpus.audit_events if e.command == PROJECTED_COMMAND]
    atomic_write_text(corpus.rounds_log_path,
                      "".join(_round_line_json(_round_line(e)) for e in submits))
    for _event, path, content in _expected_files(corpus):
        atomic_write_text(path, content)
    # Written from the committed event, so a refused `submit` leaves no stale file.
    for e in submits:
        if dropped := [{k: v for k, v in d.items() if k != "kind"}
                       for d in e.decisions if d.get("kind") == "dropped"]:
            atomic_write_text(corpus.round_dir(e.round) / "dropped_proposals.json",
                              json.dumps(dropped, indent=2, default=str))
