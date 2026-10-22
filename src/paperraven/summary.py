"""The run summary — one computation, read by every report.

The README's flow table, `audit/flow.json` and `status` all read `run_summary`, so their
numbers cannot disagree.

**The flow accounts for every proposal** the model generated: verified, not verified for a
stated reason, or awaiting a retry after a registry error. `unaccounted` is the difference
and should be zero; a report shows it rather than hiding a discrepancy.

Computed from committed audit events and the final pools, never from projections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import __version__
from .models import PROPOSER_SOURCES, RULING_DECIDED_BY, DiscoveredBy

if TYPE_CHECKING:
    from .config import Config
    from .corpus import Corpus

def _models(corpus: "Corpus") -> dict[str, Any]:
    """Every model the run's files stated, as stated. Unverified."""
    def seen(command: str, key: str) -> list[str]:
        return sorted({e.result.get(key) for e in corpus.audit_events
                       if e.command == command and e.result.get(key)})
    return {"proposer": seen("submit", "proposer_model"),
            "judge": seen("verdicts", "judge_model"), "basis": "self-reported"}


def generation_yaml(run_dir, generation: int | None) -> dict[str, Any]:
    """A committed config generation as plain YAML — readable even under a later schema."""
    import yaml

    from .config import generation_path

    try:
        return yaml.safe_load(generation_path(run_dir, generation or 0)
                              .read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


#: Worded to claim exactly what the provenance contract's tags mean and no more:
#: `[USER]` is "typed or signed off by the user", `[AGENT]` is "written, inferred or worded by
#: the tool". A label such as "stated by you" would claim the user typed text they had only
#: signed off. Keyed by `Criterion.by`.
PROVENANCE = {"user": "yours (stated or signed off by you)",
              "agent": "written by the tool"}
USER_LABEL, AGENT_LABEL = PROVENANCE["user"], PROVENANCE["agent"]


def _criteria(config: "Config") -> list[dict[str, str]]:
    topic, out = config.topic, []
    for kind, prefix, items in (("inclusion", "I", topic.inclusions),
                                ("exclusion", "E", topic.exclusions),
                                ("judging note", "N", config.judging.notes)):
        for n, item in enumerate(items, start=1):
            ruling = getattr(item, "paper", None)
            text = (f"{item.text} (your ruling: {ruling} is {item.verdict}d)" if ruling
                    else item.text)
            out.append({"kind": kind, "code": f"{prefix}{n}", "text": text,
                        "provenance": PROVENANCE[item.by],
                        **({"ruling": True} if ruling else {})})
    return out


def _items(raw: Any) -> list[dict[str, Any]]:
    """A generation file's criteria as `{text, by}` dicts. Tolerates plain strings (`by`
    unknown), because this reads raw YAML, never a loadable config."""
    return [i if isinstance(i, dict) else {"text": str(i), "by": None}
            for i in raw or [] if (i.get("text") if isinstance(i, dict) else str(i).strip())]


def _provenance_changes(old: list[dict], new: list[dict], prefix: str) -> list[str]:
    """`"<code> now yours"` for a criterion whose text is unchanged and whose `by` changed."""
    before = {i["text"]: i.get("by") for i in old}
    return [f"{prefix}{n} now {'yours' if i.get('by') == 'user' else 'written by the tool'}"
            for n, i in enumerate(new, start=1)
            if i["text"] in before and before[i["text"]] != i.get("by")]


def _counted(added: set, removed: set) -> str:
    parts = ([f"{len(added)} added"] if added else []) + \
            ([f"{len(removed)} removed"] if removed else [])
    return ", ".join(parts)


def _diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What a steer changed, in a line a reader can take in."""
    out = []
    ot, nt = old.get("topic") or {}, new.get("topic") or {}
    moved = []                                       # provenance-only changes
    for key, prefix in (("inclusions", "I"), ("exclusions", "E")):
        oi, ni = _items(ot.get(key)), _items(nt.get(key))
        a, b = {i["text"] for i in oi}, {i["text"] for i in ni}
        if a != b:
            out.append(f"{key}: {_counted(b - a, a - b)}")
        moved += _provenance_changes(oi, ni, prefix)
    for key, label in (("description", "description reworded"),
                       ("user_brief", "the researcher's own words changed"),
                       ("facets", "facets changed"),
                       ("exclusion_facets", "exclusion facets changed")):
        if ot.get(key) != nt.get(key):
            out.append(label)
    if (ot.get("year_min"), ot.get("year_max")) != (nt.get("year_min"), nt.get("year_max")):
        out.append(f"year limits: {ot.get('year_min')}-{ot.get('year_max')} -> "
                   f"{nt.get('year_min')}-{nt.get('year_max')}")
    on = _items((old.get("judging") or {}).get("notes"))
    nn = _items((new.get("judging") or {}).get("notes"))
    a, b = {i["text"] for i in on}, {i["text"] for i in nn}
    if a != b:
        out.append(f"judging notes: {_counted(b - a, a - b)}")
    moved += _provenance_changes(on, nn, "N")
    if moved:
        out.append(f"provenance: {', '.join(moved)}")
    for section in ("budget", "stopping"):
        o, n = old.get(section) or {}, new.get(section) or {}
        changed = [f"{k} {o.get(k)} -> {n.get(k)}" for k in sorted(set(o) | set(n))
                   if o.get(k) != n.get(k)]
        if changed:
            out.append(f"{section}: {', '.join(changed)}")
    return out or ["no change to the criteria"]


def _steers(corpus: "Corpus") -> list[dict[str, Any]]:
    events = sorted(corpus.audit_events, key=lambda e: e.event_id)
    steers = [e for e in events if e.command == "steer"]
    out = []
    for i, e in enumerate(steers):
        g = (e.result or {}).get("config_generation")
        nxt = steers[i + 1].event_id if i + 1 < len(steers) else float("inf")
        moved = [m for v in events if v.command == "verdicts" and e.event_id < v.event_id < nxt
                 for m in (v.result or {}).get("moved") or []]
        prior = [s.round for s in events if s.command == "submit" and s.event_id < e.event_id]
        out.append({
            "generation": g,
            "after_round": max(prior) if prior else 0,
            "changes": _diff(generation_yaml(corpus.run_dir,
                                             e.result.get("replaced_generation", (g or 1) - 1)),
                             generation_yaml(corpus.run_dir, g)),
            "rejudge_marked": e.counts.get("rejudge_marked", 0),
            "ruled": len((e.result or {}).get("ruled") or {}),
            "moved_in": sum(1 for m in moved if m.get("after") == "include"),
            "moved_out": sum(1 for m in moved if m.get("before") == "include"),
            # Between exclude and uncertain, which neither of the above counts.
            "moved_other": sum(1 for m in moved if "include" not in (m.get("before"),
                                                                     m.get("after"))),
        })
    return out


class CriterionText:
    """Resolve a verdict's criterion code (`I` or `E` and a number) to its text.

    Against the config generation the verdict was made under, read from the run's committed
    generation files — numbering can shift after a steer, so the current config is not the
    right one to read. Parsed as plain YAML, so a generation written under an earlier schema
    still resolves.
    """

    def __init__(self, run_dir):
        self.run_dir, self._gens = run_dir, {}

    def _criteria(self, generation: int | None) -> dict[str, str]:
        if generation not in self._gens:
            out: dict[str, str] = {}
            topic = generation_yaml(self.run_dir, generation).get("topic") or {}
            for prefix, key in (("I", "inclusions"), ("E", "exclusions")):
                out.update({f"{prefix}{n}": i["text"]
                            for n, i in enumerate(_items(topic.get(key)), start=1)})
            self._gens[generation] = out
        return self._gens[generation]

    def exclusion(self, code: str | None, generation: int | None) -> str:
        """An exclusion's basis: an inclusion code means the paper *fails* it."""
        text = self.label(code, generation)
        return f"fails {text}" if text and code.strip().upper().startswith("I") else text

    def label(self, code: str | None, generation: int | None) -> str:
        """`"<code>: <text>"`, the code alone if it cannot be resolved, or `""`."""
        if not code:
            return ""
        if code == "year limits":
            return code
        text = self._criteria(generation).get(code.strip().upper())
        # Provenance is stated once, in the README's criteria list.
        return f"{code}: {text}" if text else code


def _seeds_outside(seeds: list, topic) -> dict[str, Any]:
    lo, hi = topic.year_min, topic.year_max
    if lo is None and hi is None:
        return {"count": 0, "of": len(seeds), "years": []}
    out = [r.year for r in seeds if r.year is not None
           and ((lo is not None and r.year < lo) or (hi is not None and r.year > hi))]
    return {"count": len(out), "of": len(seeds), "years": sorted(out)}


def _declined_from(d: dict[str, Any], listing: str) -> bool:
    """Whether a decision declined a candidate of `listing` (`references` declines carry no
    `listing`)."""
    return d.get("kind") == "declined_candidate" and d.get("listing", "references") == listing


def _decline_reasons(submits: list, listing: str = "references") -> list[dict[str, Any]]:
    """The proposer's reasons for its declines from `listing`, each with the rounds that gave it
    and how many candidates they declined; rounds that gave none come last, under `None`."""
    groups: dict[str | None, dict[str, Any]] = {}
    for e in submits:
        n = sum(1 for d in e.decisions if _declined_from(d, listing))
        if n:
            g = groups.setdefault((e.result or {}).get("decline_reason"),
                                  {"rounds": [], "declined": 0})
            g["rounds"].append(e.round)
            g["declined"] += n
    return [{"reason": k, **groups[k]} for k in sorted(groups, key=lambda k: k is None)]


def run_summary(corpus: "Corpus", config: "Config") -> dict[str, Any]:
    """Everything a report needs to say about the run, computed once."""
    from .loop import evaluate_stopping, rejection_split, supply_this_epoch, thin_round_limit
    from .years import RULE_DECIDER, limits_text

    submits = [e for e in corpus.audit_events if e.command == "submit"]
    decisions = [d for e in submits for d in e.decisions]
    rejected = [d for d in decisions if d.get("kind") == "rejected"]
    bins = rejection_split(corpus, config)

    records = [r for pool in (corpus.greenlist, corpus.uncertain, corpus.excluded)
               for r in pool.values()]
    is_seed = lambda r: r.provenance.discovered_by is DiscoveredBy.SEED
    seeds = [r for r in records if is_seed(r)]
    verified = [r for r in records if r.provenance.discovered_by is not DiscoveredBy.SEED]
    by_rule = [r for r in verified if r.relevance.decided_by == RULE_DECIDER]
    # A paper the user ruled on is counted as ruled, whatever the judge said of it, so
    # `included` and `excluded_by_judge` are the judge's alone.
    # A ruling on a seed is on the user's own paper, not on one the tool found: seeds are
    # counted as seeds (`seeds` those in the corpus, `seeds_ruled_out` the rest).
    ruled_in = [r for r in corpus.greenlist.values()
                if r.relevance.decided_by == RULING_DECIDED_BY and not is_seed(r)]
    ruled_out = [r for r in corpus.excluded.values()
                 if r.relevance.decided_by == RULING_DECIDED_BY and not is_seed(r)]
    included = [r for r in corpus.greenlist.values()
                if r.provenance.discovered_by is not DiscoveredBy.SEED
                and r.relevance.decided_by != RULING_DECIDED_BY]
    excluded_by_judge = [r for r in corpus.excluded.values()
                         if r.relevance.decided_by not in (RULE_DECIDER, RULING_DECIDED_BY)
                         and r.provenance.discovered_by is not DiscoveredBy.SEED]
    undecided = [r for r in corpus.uncertain.values()
                 if r.provenance.discovered_by is not DiscoveredBy.SEED]
    # A record no judge has seen is awaiting judgment, not "screened" or "undecided".
    awaiting = [r for r in undecided if r.relevance.agent_verdict is None]

    def count(reason: str) -> int:
        return sum(1 for d in rejected if d.get("reason") == reason)

    not_verified = {
        "doi_does_not_resolve": bins["doi_does_not_resolve"],
        "doi_belongs_to_another_paper": bins["doi_belongs_to_another_paper"],
        "title_near_a_registered_one": bins["title_near_a_registered_one"],
        "no_similar_title_registered": bins["no_similar_title_registered"],
        "title_search_similarity_not_recorded": bins["title_search_similarity_not_recorded"],
        "duplicate": count("duplicate"),
        "out_of_scope_type": count("out_of_scope_type"),
        "malformed_doi": count("malformed_doi"),
        "registry_gave_up": count("registry_gave_up"),
        "registry_query_rejected": count("registry_query_rejected"),
        "malformed_entry": sum(1 for d in decisions if d.get("kind") == "dropped"),
    }
    # `offered` only. `proposals` also counts retries; falling back to it would hide exactly
    # the mismatch `unaccounted` exists to show.
    generated = sum(e.counts.get("offered", 0) for e in submits)
    # Candidates by the proposer's self-reported `source`. Parsed proposals only; a
    # malformed entry has no source to report.
    found_by = {k: sum(e.counts.get(f"found_by_{k}", 0) for e in submits)
                for k in (*PROPOSER_SOURCES, "not_stated")}
    not_verified_total = sum(not_verified.values())
    awaiting_retry = len(corpus.retry_queue)

    b, s = config.budget, config.stopping
    supply = supply_this_epoch(corpus)
    timestamps = sorted(e.timestamp for e in corpus.audit_events if e.timestamp)

    stop_signal = evaluate_stopping(corpus, config, round_index=len(corpus.completed_rounds))
    return {
        "tool_version": __version__,
        "dates": {"first": timestamps[0] if timestamps else None,
                  "last": timestamps[-1] if timestamps else None},
        "models": _models(corpus),
        "rounds": len(corpus.completed_rounds),
        "epoch": corpus.epoch,
        "config_generation": corpus.config_generation,
        "flow": {
            "seeds": sum(1 for r in seeds if r.doi in corpus.greenlist),
            "seeds_ruled_out": sum(1 for r in seeds if r.doi not in corpus.greenlist),
            "seed_files_unresolved": len(corpus.local_records),
            "generated": generated,
            "not_verified": not_verified,
            "not_verified_total": not_verified_total,
            "awaiting_retry": awaiting_retry,
            "verified": len(verified),
            "excluded_by_year_rule": len(by_rule),
            "screened": len(verified) - len(by_rule) - len(awaiting),
            "awaiting_judgment": len(awaiting),
            "included": len(included),
            "excluded_by_judge": len(excluded_by_judge),
            "ruled_in": len(ruled_in),
            "ruled_out": len(ruled_out),
            "undecided": len(undecided) - len(awaiting),
            "corpus": len(corpus.greenlist),
            "unaccounted": generated - len(verified) - not_verified_total - awaiting_retry,
        },
        "rejections": bins,
        "doi_recovered_by_title": sum((e.result or {}).get("doi_recovered_by_title") or 0
                                      for e in submits),
        "unresolved_seed_files": sorted(k.removeprefix("seed:") for k in corpus.local_records),
        "found_by": found_by,
        # When the proposer first proposed from the `references` list, and how many of the
        # corpus's papers came that way.
        "references_from_round": min((e.round for e in submits
                                      if e.counts.get("found_by_references")), default=None),
        "references_in_corpus": sum(1 for r in corpus.greenlist.values()
                                    if r.provenance.proposer_source == "references"),
        "references_declined": sum(1 for d in decisions if _declined_from(d, "references")),
        "references_decline_reasons": _decline_reasons(submits),
        # The same for the works that cite the corpus (`citing`).
        "citing_from_round": min((e.round for e in submits
                                  if e.counts.get("found_by_citing")), default=None),
        "citing_in_corpus": sum(1 for r in corpus.greenlist.values()
                                if r.provenance.proposer_source == "citing"),
        "citing_declined": sum(1 for d in decisions if _declined_from(d, "citing")),
        "citing_decline_reasons": _decline_reasons(submits, "citing"),
        "judged_without_abstract": sum(1 for r in verified
                                       if r.relevance.judged_without_abstract),
        # Abstracts Europe PMC supplied for verified papers the registry had none for.
        "abstracts_from_europe_pmc": sum(1 for r in verified
                                         if r.abstract_source == "europe_pmc"),
        "target": {"corpus_size": corpus.corpus_size, "target_papers": b.target_papers,
                   "found": corpus.found_size, "seeds": corpus.seeds_in_corpus},
        # Seeds are exempt from the year limits, so say when they fall outside them.
        "seeds_outside_year_limits": _seeds_outside(seeds, config.topic),
        "supply": {
            "offered_per_round": [n for _, n, _ in supply],
            "thin_max": thin_round_limit(s.thin_round_fraction, b.papers_per_round),
            "papers_per_round": b.papers_per_round,
            "reasons": [(r, why) for r, _, why in supply if why][-s.patience_rounds:],
        },
        "stop": str(stop_signal),
        "stopped": stop_signal.should_stop,
        "stop_reasons": list(stop_signal.reasons),
        # What the README says about the criteria and how they changed.
        "topic": {"user_brief": config.topic.user_brief,
                  "description": config.topic.resolved_description(),
                  "year_limits": limits_text(config.topic)},
        "criteria": _criteria(config),
        "excluded_by_criterion": _by_criterion(corpus),
        "steers": _steers(corpus),
        "rejudge_pending": len(corpus.rejudge_pending),
    }


def _by_criterion(corpus: "Corpus") -> dict[str, int]:
    """Exclusions counted by the criterion that decided them, most frequent first. A user's
    ruling is not a criterion, and is counted as a ruling in the flow."""
    label = CriterionText(corpus.run_dir)
    counts: dict[str, int] = {}
    for rec in corpus.excluded.values():
        if rec.relevance.decided_by == RULING_DECIDED_BY:
            continue
        key = (label.exclusion(rec.relevance.criterion, rec.relevance.config_generation)
               or "no criterion named")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
