"""The command surface — what the host agent actually invokes.

    paperraven <command> [options]

## Why a CLI at all, when the agent could import the library

Because the contract is that **Python owns all state and the agent contributes exactly
two things**: k candidate papers, and verdicts. A library invites the agent to reach into
`Corpus` and mutate it, and every such reach is a way for state to drift from what is on
disk. A command surface makes the contract enforceable: each command reads state from
disk, does one thing, writes state back, and prints what happened.

It is also what makes the tool **host-neutral**. Claude Code and Codex share no API, but
both can run a shell command and read a file, so the command surface is the whole
portability story.

## The contract each command honours

* **Reads state from disk, writes state to disk.** No command assumes another ran in the
  same process. That is what stateless rounds mean, and it is why a crash, a
  `/clear`, or a week's gap between rounds costs nothing.
* **Prints a short human-readable report, and `--json` for a machine.** The agent reads
  the text; a driver script reads the JSON.
* **Exit status is meaningful.** `0` success, `1` a real failure, `2` misuse. An agent can
  branch on it without parsing prose.
* **Never silently succeeds at nothing.** `submit` on an empty proposals file says so.

## The loop, as commands

    check     first     the OpenAlex key and the contact email; no network
    init      once      resolve seeds, write round 1's context
    seed      as needed finish a seed the cascade could not, from the agent's reading
    context   per round render the prompt the agent answers  -> context.md
    submit    per round verify the agent's proposals         <- proposals.json
    judge     per round emit the judging worklist            -> judging_request.json
    verdicts  per round apply the judge's answers            <- judging_response.json
    status    anytime   discovery curve, coverage, stop signal
    export    at end    greenlist / uncertain / excluded / worklist.csv
    steer     anytime   apply a config change, open a new epoch

`context` and `judge` write files for the agent to answer; `submit` and `verdicts` read
files the agent wrote. Nothing else crosses the boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from .audit import write_projections
from . import __version__
from .config import (
    CONTACT_EMAIL_ENV,
    Config,
    ConfigIntegrityError,
    ConfigOverrideRefused,
    draft_path,
    load_snapshot,
)
from .corpus import StateVersionError
from .install import InstallRefused, install_skill, installed_version
from .fetch.clicklist import collect_command
from .models import RejectionReason
from .runlock import RunLock, RunLocked
from .loop import (JUDGE_PROMPT_FILENAME, PROMPTS, PROPOSALS_FILENAME, Runner, load_prompt,
                   registry_lines)
from .relevance import JUDGING_REQUEST_FILENAME, JUDGING_RESPONSE_FILENAME
from .registry.base import AuthRefused, PoliteSession
from .registry.crossref import CrossrefRegistry
from .registry.europepmc import REQUEST_DELAY_SECONDS as EUROPE_PMC_DELAY, EuropePMC
from .registry.openalex import OPENALEX_KEY_NAME, OpenAlexRegistry, find_api_key
from .fetch.run import FetchRefused
from .verify import Verifier

EXIT_OK, EXIT_FAIL, EXIT_MISUSE = 0, 1, 2

#: Commands that mutate authoritative state and therefore hold the run lock for their
#: whole duration. Readers take nothing, because `os.replace` hands them a coherent
#: generation for free.
#:
#: **`judge` is the trap.** It emits a file and mutates no corpus record, so it reads as a
#: writer of *outputs* rather than of state — but it writes the judge prompt that `verdicts`
#: later fingerprints.
#: The rule: any command that records an identity other commands later compare against is
#: a writer, whatever else it does.
WRITER_COMMANDS = {"init", "submit", "judge", "verdicts", "steer", "seed"}

#: `PROMPTS` — which shipped prompt each command renders — lives in `loop.py`, shared with the
#: renderer, so the path `judge` prints and the prompt it writes cannot disagree.


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------



def build_registries(config: Config) -> tuple[Any, Any, Any]:
    """Construct the live registry clients: ``(primary, failover, abstracts)``.

    Factored out as the **only** place the CLI touches the network, so the offline test
    suite can substitute stubs by patching this one name. There is deliberately no
    `--offline` flag: a user-facing switch that silently swapped in fake data would be a
    way to produce a corpus that looks real and is not.
    """
    # `Config.require_contact_email` is the one authority for this check.
    contact = config.require_contact_email()
    pol = config.politeness
    ua = f"{pol.user_agent_product}/{pol.user_agent_version} (mailto:{contact})"
    # The user's own OpenAlex key when there is one, read here and handed to the
    # OpenAlex session only -- never to `Config`, which is published and exported.
    key, where = find_api_key()

    def session(service: str, auth: dict[str, str] | None = None) -> PoliteSession:
        return PoliteSession(
            service, contact, ua,
            max_attempts=pol.max_attempts,
            timeout_seconds=pol.timeout_seconds,
            request_delay_seconds=pol.request_delay_seconds,
            auth_params=auth, auth_source=where if auth else None,
        )

    # Europe PMC gets no contact email and a second between requests.
    europe_pmc = EuropePMC(PoliteSession(
        "europepmc", None, f"{pol.user_agent_product}/{pol.user_agent_version}",
        max_attempts=pol.max_attempts, timeout_seconds=pol.timeout_seconds,
        request_delay_seconds=EUROPE_PMC_DELAY))

    # Crossref is a **failover**, not a cross-check: OpenAlex contains ~99% of Crossref
    # and Crossref ~90% of OpenAlex, so agreement between them is weak evidence and
    # treating it as corroboration would be a mistake.
    return (OpenAlexRegistry(session("openalex", {"api_key": key} if key else None)),
            CrossrefRegistry(session("crossref")),
            europe_pmc)


def _openalex_line(runner: Runner) -> str | None:
    """`OpenAlex: keyed (from .env)` or `keyless …`; `None` for a stub registry."""
    s = getattr(runner.verifier.primary, "session", None)
    if not isinstance(s, PoliteSession):
        return None
    return (f"keyed (from {s.auth_source})" if s.auth_params
            else f"keyless (no {OPENALEX_KEY_NAME} found; see `check`)")


def _runner(args: argparse.Namespace) -> Runner:
    run_dir = Path(args.run_dir)
    corpus, config = load_snapshot(run_dir, getattr(args, "config", None))
    primary, failover, abstracts = build_registries(config)
    runner = Runner(run_dir, config,
                    Verifier(primary, config, failover=failover, abstracts=abstracts),
                    corpus=corpus)
    _reconcile_projections(runner, getattr(args, "command", ""))
    return runner


def _reconcile_projections(runner: Runner, command: str) -> None:
    """Writers rewrite `rounds.jsonl` and the per-round files from state before their own
    work; readers do nothing.

    Asymmetric by design: readers take no lock, so a reader that wrote a projection could
    race a writer. State is authoritative and a stale view costs nothing until the next
    writer replaces it. Nothing here reads a projection to decide anything: control flow
    is never derived from the audit log.
    """
    if command in WRITER_COMMANDS:
        write_projections(runner.corpus)


def _emit(payload: dict[str, Any], lines: Sequence[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in lines:
            print(line)


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_check(args) -> int:
    """Readiness before any registry call. Makes no network call."""
    key, where = find_api_key()
    email_set = bool(os.environ.get(CONTACT_EMAIL_ENV))
    lines = [
        f"PaperRaven {__version__}",
        f"OpenAlex API key: found (from {where}); OpenAlex requests will use it" if key else
        f"OpenAlex API key: not found — the keyless pool will be used. It allows about 100 "
        f"searches a day per network address, which one run can use up. To use a key, put "
        f"{OPENALEX_KEY_NAME}=<key> in a .env file in this directory (a free key is at "
        f"openalex.org). Never paste the key into the chat.",
        f"contact email: set ({CONTACT_EMAIL_ENV})" if email_set else
        f"contact email: not set in the environment — give it as `contact_email` in the "
        f"config, or set {CONTACT_EMAIL_ENV}",
    ]
    skill = installed_version(Path.cwd())
    if skill is None:
        lines.append("skill: not installed in this folder; run `paperraven install-skill` here")
    elif skill != __version__:
        lines.append(f"skill: the installed skill is version {skill}, the tool {__version__}. "
                     f"Ask the user to run `paperraven install-skill` in this folder, then "
                     f"start a new Claude Code session")
    _emit({"version": __version__, "openalex_key": where, "contact_email_set": email_set,
           "skill_version": skill}, lines, args.json)
    return EXIT_OK


def cmd_install_skill(args) -> int:
    """Copy the skill and its agents into this folder's `.claude/`."""
    report = install_skill(Path.cwd())
    lines = [f"{status:9}  {target}" for target, status in report]
    replaced = [t for t, s in report if s == "replaced"]
    if replaced:
        lines.append(f"replaced {len(replaced)} file(s) that differed from this version")
    lines.append("Start Claude Code in this folder and type /paperraven.")
    _emit({"version": __version__, "files": dict(report)}, lines, args.json)
    return EXIT_OK


def cmd_init(args) -> int:
    runner = _runner(args)
    report = runner.init(
        seed_dois=args.seed_doi or None,
        base_dir=Path(args.base_dir) if args.base_dir else None,
    )
    seeds = report["seeds"]
    lines = [
        f"initialised {report['run_dir']}",
        *([f"  OpenAlex: {auth}"] if (auth := _openalex_line(runner)) else []),
        *registry_lines(report.get("registry")),
        f"  seeds: {seeds['resolved']} resolved, {seeds['local_only']} kept locally "
        f"without a DOI, {seeds['needs_agent']} need reading, {seeds['failed']} unreadable"
        + (f", {len(seeds['missing'])} not found" if seeds.get("missing") else ""),
    ]
    for name in seeds.get("missing") or []:
        # A seed named in the config that exists nowhere. `init` carries on without it.
        where = (seeds.get("missing_looked_in") or {}).get(name) or []
        lines.append(f"  not found: {name}"
                     + (f" (looked in {', '.join(where)})" if where else "")
                     + "; the run continues without it")
    if seeds.get("unchecked"):
        lines.append(f"  {seeds['unchecked']} seed(s) could not be checked: a registry was "
                     f"unavailable. They are kept, not redlisted; resolve each with `seed` later")
    if seeds["resolved"]:
        # Resolved seeds are greenlist members on the user's authority.
        lines.append(
            f"  {seeds['resolved']} seed papers are in the greenlist, decided_by "
            f'"user (seed)" — not judged, because you supplied them as relevant'
        )
    if report.get("seed_digest"):
        lines.append(
            f"  ACTION: read {report['seed_digest']} — extraction found neither a DOI nor "
            f"a usable title for those files"
        )
    # The topic block this run is publishing, printed in full. The skill requires the agent
    # to show the config before `init`; printing it here puts it in the conversation whether
    # or not the agent did.
    topic = runner.config.topic
    report["topic"] = topic.model_dump(mode="json")
    lines += ["", "  published topic (generation "
              f"{runner.corpus.config_generation}) — this is what the judge will read:"]
    if topic.user_brief:
        lines += ["    the user's own words:", *_indent(topic.user_brief, 6)]
    lines += ["    description:", *_indent(topic.resolved_description() or "(none)", 6)]
    # The decisions in full, the vocabulary as a count: the keyword lists are long, and
    # `--show-topic` prints them.
    full = args.show_topic
    for label, facets in (("facets", topic.facets), ("exclusion facets", topic.exclusion_facets)):
        if facets:
            lines.append(f"    {label}:")
            lines += [f"      {f.name}: {', '.join(f.keywords)}" if full else
                      f"      {f.name} ({len(f.keywords)} keywords)" for f in facets]
    for label, items in (("inclusions", topic.inclusions), ("exclusions", topic.exclusions),
                         ("judging notes", runner.config.judging.notes)):
        if items:
            lines.append(f"    {label}:")
            # Provenance is a field, printed as the conversation names it.
            lines += [f"      - [{x.by.upper()}] {x.text}" for x in items]
    if not full and (topic.facets or topic.exclusion_facets):
        lines.append(f"    (keywords: `init --show-topic`, or "
                     f"config_gen{runner.corpus.config_generation:03d}.yaml in the run directory)")
    lines.append(f"  next: write the round-1 context with `context`")
    _emit(report, lines, args.json)
    return EXIT_OK


def _indent(text: str, n: int) -> list[str]:
    pad = " " * n
    return [pad + line for line in text.strip().splitlines()] or [pad + "(none)"]


def cmd_context(args) -> int:
    runner = _runner(args)
    template = None if args.no_prompt else load_prompt(PROMPTS["propose"])
    path = runner.write_context(prompt_template=template)
    r = runner.next_round
    payload = {"round": r, "context": str(path),
               "proposals_expected": runner.config.budget.papers_per_round,
               "write_proposals_to": str(path.parent / PROPOSALS_FILENAME)}
    _emit(payload, [
        f"round {r} context written to {path}",
        f"  read it, then write up to {payload['proposals_expected']} proposals to "
        f"{payload['write_proposals_to']}",
        f"  fewer is a valid answer: say why in `shortfall_reason` rather than padding",
        f"  then run `submit`",
    ], args.json)
    return EXIT_OK


def cmd_submit(args) -> int:
    runner = _runner(args)
    r = runner.next_round
    path = Path(args.proposals) if args.proposals else (
        runner.corpus.round_dir(r) / PROPOSALS_FILENAME)
    if not path.is_file():
        print(f"no proposals at {path}. Run `context` first, then write the file.",
              file=sys.stderr)
        return EXIT_MISUSE

    proposals = runner.read_proposals(path)
    dropped = list(getattr(runner, "last_dropped", ()) or ())
    reason = getattr(runner, "last_shortfall_reason", None)
    k = runner.config.budget.papers_per_round
    if not proposals and not reason:
        # Never silently succeed at nothing: an empty round looks identical to a
        # successful one in the log, and the user would be none the wiser. A stated
        # `shortfall_reason` is different -- that is the proposer saying why.
        print(f"{path} yielded no valid proposals "
              f"({len(dropped)} entries failed validation). Nothing submitted. "
              f"If you genuinely cannot name more papers, say why: add a one-line "
              f"`shortfall_reason` at the top level of the file.",
              file=sys.stderr)
        return EXIT_FAIL
    offered = len(proposals) + len(dropped)
    if offered < k and not reason:
        # A short round must say why. The reason is the
        # most informative sentence a thinning run produces -- it says where the supply
        # is dry -- and a count alone cannot tell caution from exhaustion.
        print(f"{path} offers {offered} of the {k} papers asked for and gives no "
              f"`shortfall_reason`. Nothing submitted. Fewer is a valid answer, but say "
              f"why: add a one-line `shortfall_reason` at the top level of the file.",
              file=sys.stderr)
        return EXIT_FAIL

    model = getattr(runner, "last_proposer_model", "not stated")
    outcome = runner.run_round(proposals, shortfall_reason=reason, proposer_model=model)
    payload = {
        "round": outcome.round_index,
        "proposals": outcome.log.proposals,
        "proposals_dropped": len(dropped),
        "verified": outcome.accepted,
        "new": outcome.log.new,
        "duplicates": outcome.duplicates,
        "rejected": dict(outcome.log.rejected or {}),
        "stop": str(outcome.stop),
    }
    lines = [outcome.summary]
    if model == "not stated":
        # Recorded, never refused, and said to the agent rather than the user.
        lines.append("  note: proposals.json does not state `model`; recorded as \"not "
                     "stated\". Add your model identifier at the top level next round")
    if reason:
        payload["offered"] = offered
        payload["shortfall_reason"] = reason
        lines.append(f"  short round ({offered} of {k}), in the proposer's words: {reason}")
    if outcome.abstracts is not None and outcome.abstracts.attempted:
        lines.append(f"  {outcome.abstracts.summary()}")
        payload["abstracts"] = {"service": "europe_pmc",
                                "attempted": outcome.abstracts.attempted,
                                "recovered": outcome.abstracts.recovered,
                                "outcomes": dict(outcome.abstracts.outcomes)}
    lines += _rejection_lines(outcome.rejections)
    payload["rejections"] = _rejection_payload(outcome.rejections)
    if dropped:
        lines.append(
            f"  WARNING: {len(dropped)} emitted entries failed validation and are NOT "
            f"counted as proposals — see "
            f"{runner.corpus.round_dir(outcome.round_index) / 'dropped_proposals.json'}"
        )
    # The proposer's self-reported recall/search split, one line.
    log = outcome.log
    lines.append(f"  found by: {log.from_recall or 0} recall, {log.from_search or 0} search, "
                 + (f"{log.from_references} references, " if log.from_references else "")
                 + (f"{log.from_citing} citing, " if log.from_citing else "")
                 + f"{log.source_not_stated or 0} not stated")
    payload["found_by"] = {"recall": log.from_recall or 0, "search": log.from_search or 0,
                           "references": log.from_references or 0,
                           "citing": log.from_citing or 0,
                           "not_stated": log.source_not_stated or 0}
    lines += registry_lines(log.registry)
    payload["registry"] = log.registry
    lines.append("  next: `judge` to have the new records judged for relevance")
    _emit(payload, lines, args.json)
    return EXIT_OK


#: Not the paper's fault: a registry was unreachable or refused the tool's query. Listed apart
#: from the rejections, since nothing is redlisted.
_NOT_CHECKED = (RejectionReason.REGISTRY_ERROR, RejectionReason.REGISTRY_GAVE_UP,
                RejectionReason.REGISTRY_QUERY_REJECTED)


def _rejection_lines(rejections) -> list[str]:
    """One line per rejected proposal, duplicates left to the count.

    Printed so the agent need not read `verified.json` by hand. Registry failures follow
    under their own heading: they are not rejections of the paper.
    """
    shown = [x for x in rejections if x.reason is not RejectionReason.DUPLICATE]
    rejected = [x for x in shown if x.reason not in _NOT_CHECKED]
    unchecked = [x for x in shown if x.reason in _NOT_CHECKED]
    out: list[str] = []
    if rejected:
        out.append(f"  rejected ({len(rejected)}):")
        out += [_one_rejection(x) for x in rejected]
    if unchecked:
        out.append(f"  not checked ({len(unchecked)}): a registry was unavailable or refused the "
                   f"query; not the paper's fault, nothing redlisted"
                   + ("; `registry_error` ones are retried next round"
                      if any(x.reason is RejectionReason.REGISTRY_ERROR for x in unchecked)
                      else ""))
        out += [_one_rejection(x) for x in unchecked]
    return out


def _one_rejection(x) -> str:
    title = x.proposal_title if len(x.proposal_title) <= 90 else x.proposal_title[:87] + "..."
    # A refused candidate's note is the agent's way forward, so it is kept whole;
    # only the text before it is shortened.
    detail, sep, notes = (x.detail or "").strip().partition("refused: ")
    detail = detail if len(detail) <= 120 else detail[:117] + "..." + ("; " if sep else "")
    detail += sep + notes
    best = (f" | best title similarity {x.best_similarity:.2f}"
            if x.best_similarity is not None else "")
    return f"    - {title} | {x.reason.value}" + (f" | {detail}" if detail else "") + best


def _rejection_payload(rejections) -> list[dict]:
    """The same rejections as data, duplicates left to the count."""
    return [{"title": x.proposal_title, "proposed_doi": x.proposed_doi,
             "reason": x.reason.value, "detail": x.detail,
             "best_similarity": x.best_similarity}
            for x in rejections if x.reason is not RejectionReason.DUPLICATE]


def cmd_judge(args) -> int:
    runner = _runner(args)
    # Resolved by the runner, not here: a default computed in the CLI and passed as an
    # explicit round would be indistinguishable from a user's `--round`.
    r = runner.judging_round(args.round)
    path = runner.write_judging_request(args.round)
    request = json.loads(path.read_text(encoding="utf-8"))
    n = len(request["items"])
    no_abstract = sum(1 for i in request["items"] if not i.get("abstract_available", True))

    payload = {"round": r, "request": str(path), "items": n,
               "without_abstract": no_abstract,
               "write_response_to": str(path.parent / JUDGING_RESPONSE_FILENAME),
               "prompt": str(path.parent / JUDGE_PROMPT_FILENAME)}
    # An earlier response in the folder was set aside, never to be applied: no recovery
    # advice is given, and `verdicts` refuses a set-aside file.
    aside = []
    if runner.last_superseded is not None:
        payload["superseded_response"] = str(runner.last_superseded)
        # The advice to hand the request on only when there is one to hand.
        aside = [f"  an earlier {JUDGING_RESPONSE_FILENAME} was set aside as "
                 f"{runner.last_superseded.name}, kept as a record; it is never applied"
                 + (". Hand the request above to a judge" if n else "")]
    if n == 0:
        _emit(payload, [f"round {r}: nothing awaiting judgment.", *aside], args.json)
        return EXIT_OK

    lines = [
        f"round {r}: {n} records awaiting judgment -> {path}",
        # The *rendered* prompt, not the raw template, so the judge sees the user's own words.
        # The `paperraven-judge` agent type can read, write and search the web and nothing
        # else; a general-purpose agent can also download files and browse.
        f"  give the `paperraven-judge` subagent (never a general-purpose one) exactly these "
        f"two files, by their absolute paths, not their contents: {payload['prompt']} and the "
        f"request above. Guidance of your own is a judging note to propose to the user, not "
        f"an addition to the brief",
        f"  the judge writes its verdicts to {payload['write_response_to']}; then run "
        f"`verdicts`",
        *aside,
    ]
    if no_abstract:
        lines.append(
            f"  {no_abstract} of {n} have NO abstract: the judge must search the web for each "
            f"one before judging, and answer `uncertain` if the results do not settle it"
        )
    _emit(payload, lines, args.json)
    return EXIT_OK


def cmd_verdicts(args) -> int:
    runner = _runner(args)
    r = runner.judging_round(args.round)
    # Always the round folder's response, where `judge` tells the judge
    # to write it; `--round` chooses the round.
    path = runner.corpus.round_dir(r) / JUDGING_RESPONSE_FILENAME
    if not path.is_file():
        # After `judge`, the file is the judge's to write.
        asked = (runner.corpus.round_dir(r) / JUDGING_REQUEST_FILENAME).is_file()
        waiting = [f"a response not yet applied is waiting in rounds/{n:03d}/: run "
                   f"`verdicts --round {n}`" for n in _waiting_responses(runner)]
        print(f"no judging response at {path}. "
              + ("Hand the request to a judge (it writes this file), then run `verdicts`."
                 if asked else "Run `judge` first.")
              + "".join(f" ({w})" for w in waiting), file=sys.stderr)
        return EXIT_MISUSE

    outcome = runner.apply_judging(path, round_index=args.round)
    payload = {
        "round": r,
        "applied": outcome.applied,
        "dropped": len(outcome.dropped),
        "unmatched_dois": outcome.unmatched_dois,
        "unrequested_dois": outcome.unrequested_dois,
        "unjudged_dois": outcome.unjudged_dois,
        "duplicate_dois": outcome.duplicate_dois,
        "greenlist": runner.corpus.corpus_size,
    }
    lines = [outcome.summary(), f"  greenlist now {runner.corpus.corpus_size}"]
    ev = runner.corpus.audit_events[-1].result
    if outcome.judge_model == "not stated":
        lines.append("  note: judging_response.json does not state `model`; recorded as "
                     "\"not stated\". The judge should add its model identifier")
    if ev.get("judge_prompt_matches_request") is False:
        lines.append(f"  NOTE: {JUDGE_PROMPT_FILENAME} changed after `judge` wrote it; the "
                     f"audit trail records both fingerprints")
    if outcome.rejudged:
        # Exactly what the steer moved, so the user can see it.
        payload["rejudged"] = outcome.rejudged
        payload["moved"] = [{"doi": m.doi, "before": m.before, "after": m.after}
                            for m in outcome.moved]
        lines.append(f"  re-judged {outcome.rejudged}: {len(outcome.moved)} moved, "
                     f"{outcome.rejudged - len(outcome.moved)} unchanged")
        lines += [f"    {m.before} -> {m.after}: {m.title}" for m in outcome.moved]
    if outcome.dropped:
        lines.append(f"  WARNING: {len(outcome.dropped)} malformed verdicts — see "
                     f"{runner.corpus.round_dir(r) / 'judging_outcome.json'}")
    if outcome.unjudged_dois:
        lines.append(f"  {len(outcome.unjudged_dois)} records still have no verdict; "
                     f"re-run `judge` to retry them")
    if outcome.no_criterion_dois:
        payload["no_criterion_dois"] = outcome.no_criterion_dois
        lines.append(f"  {len(outcome.no_criterion_dois)} exclusion(s) name no criterion, "
                     f"though the judge prompt asks each to name the rule it meets or fails: "
                     + ", ".join(outcome.no_criterion_dois))
    _emit(payload, lines, args.json)
    return EXIT_FAIL if (outcome.dropped and not outcome.applied) else EXIT_OK


def _waiting_responses(runner) -> list[int]:
    """Completed rounds whose own `judging_response.json` no `verdicts` has applied (its bytes
    match no applied response in the audit trail) and `verdicts --round N` would accept: its
    request is current. An unreadable file is passed over."""
    import hashlib
    corpus = runner.corpus
    applied = {e.inbound.sha256 for e in corpus.audit_events
               if e.command == "verdicts" and e.inbound is not None}
    out = []
    for n in sorted(corpus.completed_rounds):
        try:
            data = (corpus.round_dir(n) / JUDGING_RESPONSE_FILENAME).read_bytes()
            runner._check_request_is_current(n)
        except (OSError, ValueError):
            continue
        if hashlib.sha256(data).hexdigest() not in applied:
            out.append(n)
    return out


def cmd_references(args) -> int:
    from .references import list_candidates, write_candidates
    runner = _runner(args)
    listing = list_candidates(runner.corpus, runner.config, runner.verifier.primary,
                              now=args.now, min_cited_by=args.min_cited_by, limit=args.limit)
    r = runner.next_round
    listing = {"round": r, **listing}
    md, _js = write_candidates(runner.corpus.round_dir(r), listing)
    n = listing["candidates_total"]
    _emit({**listing, "file": str(md)}, [
        f"{n} candidate{'' if n == 1 else 's'} cited by at least {args.min_cited_by} corpus "
        f"papers; top {len(listing['listed'])} listed in {md}",
        "  propose those within the criteria with \"source\": \"references\"; list the "
        "others under declined_candidates",
        *([f"  {listing['note']}"] if listing.get("note") else []),
    ], args.json)
    return EXIT_OK


def cmd_citing(args) -> int:
    from .references import CITING_NAME, list_citing, write_candidates
    runner = _runner(args)
    listing = list_citing(runner.corpus, runner.config, runner.verifier.primary,
                          now=args.now, min_cites=args.min_cites, limit=args.limit,
                          refresh=args.refresh)
    r = runner.next_round
    listing = {"round": r, **listing}
    md, _js = write_candidates(runner.corpus.round_dir(r), listing, name=CITING_NAME)
    n = listing["candidates_total"]
    counts = " · ".join(f"cite {t} or more: {c}" for t, c in listing["counts"].items())
    _emit({**listing, "file": str(md)}, [
        f"{n} work{'' if n == 1 else 's'} citing at least {listing['min_cites']} corpus papers "
        f"and not yet in the run; top {len(listing['listed'])} listed in {md}",
        *([f"  {counts}"] if counts else []),
        *([f"  {listing['note']}"] if listing.get("note") else []),
        f"  {listing['openalex_calls']} OpenAlex calls",
        "  propose those within the criteria with \"source\": \"citing\"; list the others "
        "under declined_candidates",
    ], args.json)
    return EXIT_OK


def cmd_status(args) -> int:
    runner = _runner(args)
    st = runner.status()
    sup = st["supply"]
    lines = [
        f"rounds completed : {st['rounds_completed']}   epoch {st['epoch']}",
        f"target           : {st['target']['found']} found of {st['target']['target_papers']}"
        f" (corpus {st['target']['corpus_size']} with {st['target']['seeds']} seeds)",
        f"greenlist        : {st['corpus_size']}",
        f"uncertain pool   : {st['uncertain_pool']}   excluded: {st['excluded']}",
        _rejection_line(st["rejections"]),
        f"models           : proposer {', '.join(st['models']['proposer']) or '-'}; "
        f"judge {', '.join(st['models']['judge']) or '-'}  (self-reported, unverified)",
        f"awaiting judgment: {st['awaiting_judgment']}"
        + (f"   (of which {st['rejudge_pending']} re-judge after a steer)"
           if st["rejudge_pending"] else ""),
        *st["report"],
        f"proposals / round: {sup['offered_per_round']}   (thin = at most "
        f"{sup['thin_max']} of {sup['papers_per_round']}; this epoch)",
        *[f"  shortfall, round {r}: {why}" for r, why in sup["reasons"]],
        st["abstract_coverage"]["summary"],
        f"stop signal      : {st['stop']}",
        *([f"OpenAlex         : {auth}"] if (auth := _openalex_line(runner)) else []),
    ]
    if st["retry_queue"]:
        lines.append(f"retry queue      : {st['retry_queue']} proposal(s) held after a "
                     f"registry error; they retry at the start of the next round")
    _emit(st, lines, args.json)
    return EXIT_OK


def _rejection_line(r: dict) -> str:
    """The rejection split, worded for what each bin suggests and no more."""
    return (f"rejections       : {r['doi_does_not_resolve']} DOI(s) resolve to nothing, "
            f"{r['doi_belongs_to_another_paper']} DOI(s) belong to another paper, "
            f"{r['title_near_a_registered_one']} title(s) near a registered one, "
            f"{r['no_similar_title_registered']} title(s) with nothing similar registered "
            f"(similarity < {r['near_threshold']:.2f}) — none of these is proof a paper was "
            f"invented")


def _open_line(dest: Path, names: list[str]) -> str:
    """What the user should open first: the README, and the network page when there is one."""
    parts = [f"{dest / 'README.md'} (start here)"]
    if "citation_network.html" in names:
        parts.append(f"{dest / 'citation_network.html'} (in a browser: who cites whom among the "
                     f"papers)")
    return "open: " + "; ".join(parts)


def cmd_export(args) -> int:
    runner = _runner(args)
    report = runner.export(args.dest, pdf_dir=args.pdf_dir, library=args.library)
    dest = Path(report["dest"])
    # The files a reader opens, then one line for the audit trail: discoverable, not in the
    # user's face.
    # The files this export wrote, not a kept file the user edited.
    top = sorted(Path(f).name for f in report["files"].values() if Path(f).parent == dest)
    n_audit = sum(1 for p in (dest / "audit").rglob("*") if p.is_file()) \
        if (dest / "audit").is_dir() else 0
    lines = [f"wrote the corpus to {dest}", *[f"  {name}" for name in top],
             f"  audit/  ({n_audit} files: every record, the criteria, the judge prompts, "
             f"the rejections -- for checking, not for use)", _open_line(dest, top)]
    if report["rejudge_pending"]:
        lines.insert(0, f"WARNING: {report['rejudge_pending']} record(s) are still awaiting "
                        f"re-judging after a steer; their verdicts in this export were made "
                        f"under the earlier criteria. Run `judge` and `verdicts` first.")
    lib = report["summary"].get("library")
    if lib:
        lines.append(f"library: {lib['new']} new to you, {lib['in_library']} already in "
                     f"{Path(lib['file']).name}; {lib['without_doi']} library entries had no "
                     f"DOI and were not compared")
    lines += [report["worklist"], report["abstracts"], *seed_pdf_lines(report["seed_pdfs"])]
    # The previous export's files, removed or kept, said.
    removed, kept = report.get("removed_stale") or [], report.get("kept_edited") or []
    if removed:
        lines.append(f"removed {len(removed)} file{'s' if len(removed) != 1 else ''} a previous "
                     f"export wrote (unedited): {', '.join(removed)}")
    if kept:
        lines.append(f"kept {len(kept)} file{'s' if len(kept) != 1 else ''} you edited since the "
                     f"last export: {', '.join(kept)}")
    if report["summary"].get("retracted"):
        lines.append(f"  {len(report['summary']['retracted'])} paper(s) marked retracted by a "
                     f"registry: listed in README.md; they stay in the corpus")
    # The deterministic close-out check, run on what was just written.
    from .closeout import closeout, report_lines
    lines += report_lines(closeout(runner.corpus, runner.config, runner.run_dir, dest,
                                   api_key=_openalex_key(), transcripts=_transcripts()))
    _emit(report, lines, args.json)
    return EXIT_OK


def seed_pdf_lines(seed: dict) -> list[str]:
    """What `export` did with the user's seed PDFs, said only when there is something to say."""
    lines = []
    if seed["listed"]:
        lines.append(f"pdfs/: {seed['listed']} of your seed PDFs are there ({seed['copied']} "
                     f"copied now), so `fetch` will not ask you for them")
    if seed["refused"]:
        lines.append(f"your seed PDFs were not copied: {seed['refused']}")
    if seed["name_taken"]:
        lines.append(f"not copied, another file already has the paper's name in pdfs/: "
                     f"{', '.join(seed['name_taken'])}")
    if seed["unplaced"]:
        lines.append(f"seed files not copied (no paper of the corpus, or a second file for "
                     f"one): "
                     f"{', '.join(seed['unplaced'])}")
    return lines


FETCH_ADVICE = (
    "  Before it starts: if you have access through a university or institute library, run "
    "this from its network or VPN.\n"
    "  Library licences usually forbid bulk automated downloading, so this goes slowly (never "
    "more than one request every 10 s to a publisher, up to 4 publishers at a time, at most 25 "
    "PDFs per publisher) and stops asking a publisher at its first refusal. What it cannot get "
    "is left on a click-list for you.\n"
    "  Using Zotero? Importing the .bib and choosing \"Find Available PDF\" loads each paper "
    "with your access and shows you any CAPTCHA.")


def build_fetch_client():
    """The fetch command's HTTP client: the one place `fetch` downloads."""
    from .fetch.client import PoliteClient
    return PoliteClient(version=__version__)


def cmd_fetch(args) -> int:
    """PDFs for a bibliography, as a reference manager fetches them."""
    from .fetch.bibfile import parse_bib
    from .fetch.run import downloads_folder, fetch_bibliography
    from .models import PaperProposal

    bib = Path(args.bib).expanduser()
    if not bib.is_file():
        print(f"no such .bib: {bib}", file=sys.stderr)
        return EXIT_MISUSE
    problems: list[str] = []
    entries = parse_bib(bib.read_text(encoding="utf-8", errors="replace"), problems)
    if not entries:
        print(f"{bib} holds no BibTeX entries"
              + (f" that could be read ({'; '.join(problems)})" if problems else ""),
              file=sys.stderr)
        return EXIT_MISUSE
    if args.estimate:
        # How long, before the user is asked. From the .bib alone; nothing is fetched.
        from .fetch.client import PoliteClient
        from .fetch.run import _minutes, estimate_from_bib
        seconds = estimate_from_bib(entries, PoliteClient(version=__version__))
        _emit({"entries": len(entries), "seconds": round(seconds)},
              [f"fetch estimate for {len(entries)} entries: about {_minutes(seconds)}, registry "
               f"lookups included (up to 4 publishers at a time; less if publishers refuse). "
               f"Nothing was downloaded; papers already in the folder take no time"], args.json)
        return EXIT_OK
    # Nothing machine-specific. The default is a `pdfs/` folder beside the .bib (an export's
    # corpus folder), and the downloads folder is the home directory's, as an absolute path.
    dest = Path(args.dest).expanduser() if args.dest else bib.parent / "pdfs"
    downloads = downloads_folder()
    collect = Path(args.collect).expanduser() if args.collect else None
    if collect is not None and not collect.is_dir():
        print(f"no such folder to collect from: {collect}", file=sys.stderr)
        return EXIT_MISUSE
    config = Config(contact_email=args.contact_email or os.environ.get(CONTACT_EMAIL_ENV))
    if not config.contact_email or "@" not in config.contact_email:
        # `fetch` reads no run config, so the message names the flag and the variable only.
        print(f"fetch needs a contact email for the registries' polite pool: pass "
              f"--contact-email <address> (the one in the run's config), or set "
              f"{CONTACT_EMAIL_ENV}", file=sys.stderr)
        return EXIT_MISUSE
    openalex, crossref, europe_pmc = build_registries(config)
    verifier = Verifier(openalex, config, failover=crossref)

    def resolve(entry) -> str | None:
        """A DOI-less entry's identifier, by the same title cascade proposals go through."""
        outcome = verifier.verify(PaperProposal(
            title=entry.title, year=entry.year, authors=entry.authors,
            explanation="an entry in the user's bibliography", proposed_in_round=0),
            round_index=0)
        return outcome.record.doi if outcome.accepted else None

    if not args.json:
        print(f"fetching PDFs for {len(entries)} entries of {bib} into {dest}")
        print(FETCH_ADVICE, flush=True)
    report = fetch_bibliography(entries, dest, bib_path=bib, client=build_fetch_client(),
                                openalex=openalex, europe_pmc=europe_pmc, resolve=resolve,
                                collect=collect,
                                # The estimate before any download, then progress.
                                progress=None if args.json else
                                (lambda message: print(message, flush=True)),
                                published_only=args.published_only)
    lines = [
        f"fetch: {report['entries']} entries — {report['already_present']} already present, "
        f"{report['collected']} collected, {report['fetched']} fetched, "
        f"{report['click_list']} on the click-list"
        + (f" ({report['unverified']} downloaded PDFs did not match their paper: see "
           f"_unverified/)" if report["unverified"] else ""),
    ]
    if report.get("published_collected"):
        lines.append(f"  {report['published_collected']} published version(s) collected; their "
                     f"preprints were moved to _preprints/")
    if report.get("only_preprint"):
        # The user asked for publishers' versions; these had only a preprint open.
        lines.append(f"  {report['only_preprint']} paper(s) had only a preprint "
                     f"or a copy of unknown version openly available and are on the click-list "
                     f"(you asked for publishers' versions only)")
    if report.get("not_swapped"):
        lines.append(f"  {report['not_swapped']} downloaded PDF(s) look like another copy of a "
                     f"preprint already here (page 1 shows no DOI of the paper): not swapped in")
    if report.get("preprints"):
        # Said, so the user can decide whether the published versions are needed.
        # Counted over the folder, as a rerun keeps the record.
        lines.append(f"  {report['preprints']} of the PDFs in the folder are submitted or "
                     f"accepted versions (preprints)"
                     + (f"; the published versions of {report['optional_rows']} are listed as "
                        f"optional on the click-list" if report.get("optional_rows") else ""))
    if report["hosts_refused"]:
        lines.append(f"  publishers that refused an automated request (their papers are on the "
                     f"click-list): {', '.join(report['hosts_refused'])}")
    if report["run_again"]:
        lines.append(f"  the per-publisher limit was reached for {', '.join(report['hosts_capped'])}: "
                     f"run the same command again later to continue")
    lines += [f"  {bib.name}, {p}; the rest of the file was read" for p in problems]
    lines.append(f"  click-list: {report['click_list_html']}")
    lines.append(f"open: {report['click_list_html']} (the papers to download yourself); the PDFs "
                 f"are in {dest}")
    if report["click_list"]:
        email = " --contact-email <address>" if args.contact_email else ""
        mode = " --published-only" if args.published_only else ""
        lines.append(f"  after downloading some yourself: "
                     f"{collect_command(bib, dest, downloads, email, mode)}")
    _emit(report, lines, args.json)
    return EXIT_OK


def cmd_closeout(args) -> int:
    """Check the deliverable and the scratchpad before the run is called finished."""
    from .closeout import closeout, report_lines
    from .export import default_export_dir
    runner = _runner(args)
    dest = Path(args.dest) if args.dest else default_export_dir(runner.run_dir)
    if not dest.is_dir():
        print(f"no export at {dest}: run `export` first", file=sys.stderr)
        return EXIT_MISUSE
    report = closeout(runner.corpus, runner.config, runner.run_dir, dest,
                      scratch=Path(args.scratch) if args.scratch else None,
                      api_key=_openalex_key(), transcripts=_transcripts())
    _emit(report, report_lines(report), args.json)
    return EXIT_OK if report["ok"] else EXIT_FAIL


def _transcripts() -> Path:
    """Where Claude Code keeps its session transcripts, for the run's token usage."""
    return Path.home() / ".claude" / "projects"


def _openalex_key() -> str | None:
    """The key, only to check the export does not contain it; never printed."""
    from .registry.openalex import find_api_key
    return find_api_key()[0]


def cmd_steer(args) -> int:
    runner = _runner(args)
    # Defaults to the run's editable draft, the file the config error messages tell the user
    # to edit before running `steer`.
    source = Path(args.config_file) if args.config_file else draft_path(args.run_dir)
    if not source.is_file():
        print(f"no configuration at {source}. Pass one explicitly, or edit the run's "
              f"{draft_path(args.run_dir).name}.", file=sys.stderr)
        return EXIT_MISUSE
    if not source.read_text(encoding="utf-8").strip():
        print(f"{source} is empty; nothing was changed. Edit it (it should hold the whole "
              f"configuration), then run `steer` again.", file=sys.stderr)
        return EXIT_MISUSE
    new_config = Config.load(source)
    from .loop import NothingToSteer, UnknownRuledPaper
    try:
        epoch = runner.steer(new_config, rejudge_all=args.rejudge == "all")
    except UnknownRuledPaper as exc:
        print(f"nothing changed: {exc}", file=sys.stderr)
        return EXIT_MISUSE
    except NothingToSteer as exc:
        _emit({"changed": False}, [f"nothing changed: {exc}; nothing was written"], args.json)
        return EXIT_OK
    event = runner.corpus.audit_events[-1]
    counts = event.counts
    from .summary import _diff, generation_yaml
    changes = _diff(generation_yaml(runner.run_dir, event.result["replaced_generation"]),
                    generation_yaml(runner.run_dir, runner.corpus.config_generation))
    pending = counts.get("rejudge_pending", 0)
    payload = {"epoch": epoch, "greenlist": runner.corpus.corpus_size,
               "rejudge_pending": pending,
               "year_ruled_out": counts.get("year_ruled_out", 0),
               "year_readmitted": counts.get("year_readmitted", 0)}
    lines = [
        f"steered; epoch is now {epoch}",
        ("  a topic change opens a new epoch: rounds before and after it sampled "
         "different populations" if (event.result or {}).get("topic_changed") else
         "  no topic change; the epoch is unchanged"),
        f"  changed: {'; '.join(changes)}",
        f"  corpus and redlist carried over untouched ({runner.corpus.corpus_size} papers)",
    ]
    ruled, unruled = event.result.get("ruled") or {}, event.result.get("unruled") or []
    if ruled:
        lines.append(f"  your ruling applied to {len(ruled)} paper(s): "
                     + ", ".join(f"{d} {v}d" for d, v in ruled.items())
                     + ("" if pending else "; nothing else will be re-judged"))
    if unruled:
        lines.append(f"  ruling removed for {', '.join(unruled)}: the judge decides "
                     f"{'it' if len(unruled) == 1 else 'them'} again at the next `judge`")
    if pinned := event.result.get("pinned"):
        lines.append(f"  the topic changed; {len(pinned)} paper(s) keep the verdict you ruled. "
                     f"Check whether the new topic changes that: {', '.join(pinned)}")
    if pending:
        # The cost is stated before anything is spent.
        lines.append(f"  topic changed: {pending} record(s) will be re-judged under the new "
                     f"criteria at the next `judge` (seeds exempt). Nothing moves until "
                     f"then")
    if payload["year_ruled_out"] or payload["year_readmitted"]:
        lines.append(f"  year limits re-applied by rule: {payload['year_ruled_out']} excluded, "
                     f"{payload['year_readmitted']} readmitted for judging")
    _emit(payload, lines, args.json)
    return EXIT_OK


def cmd_seed(args) -> int:
    """Finish seeding one paper the mechanical cascade could not."""
    runner = _runner(args)
    result = runner.resolve_seed(args.file, title=args.title, doi=args.doi)
    if hasattr(result, "accepted") and not result.accepted:
        rej = result.rejection
        print(f"{Path(args.file).name}: {rej.reason.value} — {rej.detail}. Nothing changed. "
              f"Try a different title, or pass --doi if you can find one.", file=sys.stderr)
        return EXIT_FAIL
    record = result.record if hasattr(result, "record") else result
    found_in = runner.corpus.audit_events[-1].result.get("superseded_found_in_round")
    payload = {"file": args.file, "doi": record.doi, "title": record.title,
               "greenlist": runner.corpus.corpus_size, "superseded_found_in_round": found_in}
    lines = [f"seed resolved: {record.doi}  {record.title}",
             f"  greenlist now {runner.corpus.corpus_size}"]
    if found_in is not None:
        lines.append(f"  the tool had already found this paper in round {found_in}; it is now "
                     f"your seed, so the found count is {runner.corpus.found_size}")
    _emit(payload, lines, args.json)
    return EXIT_OK


def cmd_prompt(args) -> int:
    """Print a shipped prompt, rendered against the run's current state."""
    runner = _runner(args)
    from .loop import render_prompt

    name = PROMPTS.get(args.which, args.which)
    text = render_prompt(load_prompt(name), runner.corpus, runner.config,
                         round_index=runner.next_round)
    print(text)
    return EXIT_OK


# --------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------


def _at_least_one(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="paperraven",
        description="Keywords to a verified literature corpus. "
                    "Python owns state; the agent proposes papers and judges relevance.",
    )
    ap.add_argument("--run-dir", default="runs/current",
                    help="where this run's state lives (default: runs/current)")
    ap.add_argument("--config", help="config YAML; defaults to the copy inside --run-dir")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=f"paperraven {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install-skill", help="copy the skill and its agents into this "
                                             "folder's .claude/")
    p.set_defaults(func=cmd_install_skill)

    p = sub.add_parser("check", help="the OpenAlex key and the contact email, before any "
                                     "registry call (no network)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("init", help="resolve seeds and write round 1's context")
    p.add_argument("--seed-doi", action="append",
                   help="headless seeding from a DOI (repeatable). Ordinary seeding "
                        "uses PDFs on disk; this is for re-runs from a known list")
    p.add_argument("--base-dir", help="root for relative seed paths (default: cwd)")
    p.add_argument("--show-topic", action="store_true",
                   help="print every facet's keywords, not just their counts")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("context", help="write the round's context for the agent")
    p.add_argument("--no-prompt", action="store_true",
                   help="emit the structured briefing instead of the proposal prompt")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("submit", help="verify the agent's proposals")
    p.add_argument("--proposals", help="path to proposals.json")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("judge", help="write the judging request for this round")
    p.add_argument("--round", type=int)
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("verdicts", help="apply the judge's verdicts")
    p.add_argument("--round", type=int)
    p.set_defaults(func=cmd_verdicts)

    p = sub.add_parser("references", help="list the works the corpus cites that are not yet "
                                          "in the run, most-cited first (once supply thins)")
    p.add_argument("--min-cited-by", type=_at_least_one, default=2,
                   help="list only works cited by at least this many corpus papers (default 2)")
    p.add_argument("--limit", type=_at_least_one, default=30,
                   help="how many candidates to list (default 30)")
    p.add_argument("--now", action="store_true",
                   help="list before supply has thinned; only when the user asked for it")
    p.set_defaults(func=cmd_references)

    p = sub.add_parser("citing", help="list the works citing the corpus that are not yet in "
                                      "the run, ranked by how much of their reference list "
                                      "the corpus is (late in a run)")
    p.add_argument("--min-cites", type=_at_least_one, default=2,
                   help="list only works citing at least this many corpus papers (default 2)")
    p.add_argument("--limit", type=_at_least_one, default=30,
                   help="how many candidates to list (default 30)")
    p.add_argument("--now", action="store_true",
                   help="list before it has opened; only when the user asked for it")
    p.add_argument("--refresh", action="store_true",
                   help="search OpenAlex again rather than rank from the saved search")
    p.set_defaults(func=cmd_citing)

    p = sub.add_parser("status", help="discovery curve, supply, stop signal")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="write the deliverables and the worklist")
    p.add_argument("--dest", help="output directory (default: <run-name>-corpus beside runs/, "
                                  "so it outlives the run)")
    p.add_argument("--pdf-dir", help="mark papers already downloaded here")
    p.add_argument("--library",
                   help="your reference library -- a .bib file or a text file of DOIs -- to "
                        "mark which corpus papers you already have (matched by DOI)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("closeout", help="check the deliverable and the scratchpad before the "
                                        "run is called finished (reports; changes nothing)")
    p.add_argument("--dest", help="the export folder (default: the one `export` writes)")
    p.add_argument("--scratch", help="the agent's scratchpad directory, to list downloads and "
                                     "extracted text left there")
    p.set_defaults(func=cmd_closeout)

    p = sub.add_parser("fetch", help="fetch the PDFs for a .bib into a folder, as a reference "
                                     "manager does; list the rest for you to open")
    p.add_argument("--bib", required=True, help="any .bib, e.g. an export's papers.bib")
    p.add_argument("--dest", help="the folder for the PDFs (default: a pdfs/ folder beside the "
                                  ".bib); never a place git would track")
    p.add_argument("--collect", help="a folder of PDFs you downloaded (your browser's downloads "
                                     "folder): the ones recognised as entries are copied into "
                                     "--dest")
    p.add_argument("--contact-email", help="for the registries' polite pool (or set "
                                           "PAPERRAVEN_CONTACT_EMAIL)")
    p.add_argument("--estimate", action="store_true",
                   help="print how long the fetch should take, from the .bib alone, and stop "
                        "(no network, nothing written)")
    p.add_argument("--published-only", action="store_true",
                   help="fetch only the publishers' versions: skip preprints (submitted and "
                        "accepted versions) and copies of unknown version; those papers go on "
                        "the click-list")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("steer", help="apply a config change (a topic change opens a new "
                                     "epoch and re-judges)")
    p.add_argument("config_file", nargs="?",
                   help="the revised config YAML (default: the run's config_draft.yaml)")
    p.add_argument("--rejudge", choices=["all"], default=None,
                   help="with a change made only of rulings on papers, re-judge every other "
                        "record too (by default nothing else is re-judged)")
    p.set_defaults(func=cmd_steer)

    p = sub.add_parser("seed", help="resolve a seed PDF the mechanical cascade could not, "
                                    "from the title (and DOI) the agent read off it")
    p.add_argument("--file", required=True, help="the seed PDF named in seed_digest.md")
    p.add_argument("--title", required=True, help="the paper's title as read from the PDF")
    p.add_argument("--doi", help="the paper's DOI, if the agent found one")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("prompt", help="print a shipped prompt rendered against this run")
    p.add_argument("which", choices=sorted(PROMPTS) + list(PROMPTS.values()))
    p.set_defaults(func=cmd_prompt)

    # The global flags also work after the subcommand (`status --json`), as agents
    # type them. `SUPPRESS` matters: a subcommand default would overwrite a value given before
    # the subcommand (`--run-dir X --json status` parsed as `json=False`).
    for sp in sub.choices.values():
        sp.add_argument("--run-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in WRITER_COMMANDS:
            # Held for the whole command, network I/O included. That eliminates the
            # lost-update class rather than detecting it, and costs nothing real: the
            # operating assumption is one user, one session, one run directory, so a
            # second writer is a mistake worth refusing rather than queueing behind.
            with RunLock(Path(args.run_dir), command=args.command):
                return args.func(args)
        return args.func(args)
    except (RunLocked, ConfigOverrideRefused, AuthRefused, FetchRefused, InstallRefused) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MISUSE
    # `ConfigIntegrityError` and `StateVersionError` are bare `RuntimeError` subclasses, so
    # they are caught by name: their messages explain an edited generation or unmigrated
    # state and must not reach the user as tracebacks. `ConfigMissing` is caught as a
    # `FileNotFoundError`. There is deliberately no common base class — catching a base
    # class also catches the failures nobody has considered yet, which is the pattern that
    # hides bugs — so the names are listed; every exception the package defines is either
    # listed here or deliberately fatal.
    except (ConfigIntegrityError, StateVersionError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MISUSE
    except yaml.YAMLError as exc:
        print(f"the configuration file could not be parsed as YAML: {exc}",
              file=sys.stderr)
        return EXIT_MISUSE
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MISUSE
    except OSError as exc:
        # A directory given as a file, an unreadable config, a read-only folder, a
        # full disk. The path and the reason, not a traceback.
        where = f"{exc.filename}: " if getattr(exc, "filename", None) else ""
        print(f"{where}{exc.strerror or exc}", file=sys.stderr)
        return EXIT_MISUSE
    except KeyboardInterrupt:
        # True of every command (fetch and export write outside the run).
        # `verdicts`, `submit`, `seed` and `steer` may have saved already.
        print("interrupted. If the command had not saved yet, the run is as it was; run "
              "`status` to see where it stands. Files a command writes outside the run (an "
              "export, a fetch folder) may be partly written: run it again.", file=sys.stderr)
        return 130
    except ValueError as exc:
        # `init` on an existing run, a duplicate round index, and similar refusals. These
        # are the tool declining to corrupt state, so they are misuse rather than failure.
        print(str(exc), file=sys.stderr)
        return EXIT_MISUSE


if __name__ == "__main__":
    raise SystemExit(main())
