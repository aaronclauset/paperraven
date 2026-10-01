"""The discovery curve — what the tool can honestly report about its own progress.

Two counts per round, neither resting on any model or assumption:

* **new papers verified per round** — whether the proposer is naming papers that exist;
* **papers judged relevant per round** — whether they are the right papers.

The gap between the two is the off-topic signal, and the saturation rule reads the judged
curve: a proposer that has drifted off-topic keeps naming real papers, so the verified curve
stays healthy while the greenlist does not move. What makes the curve a *stopping* signal
rather than a quota readout is that the proposer may return fewer than `papers_per_round`,
saying why, and a run of thin rounds is a stop reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .corpus import Corpus
from .models import DiscoveredBy


# --------------------------------------------------------------------------------------
# The discovery curve — assumption-free, always reported
# --------------------------------------------------------------------------------------


@dataclass
class DiscoveryCurve:
    """New finds per round and the cumulative total.

    It rests on no assumptions and cannot mislead, so it is always reported.
    """

    new_per_round: list[int] = field(default_factory=list)
    #: Papers judged relevant per round. `None` when nothing has been judged.
    included_per_round: list[int] = field(default_factory=list)
    #: Where the verified papers are now, so the gap is named part by part.
    split: dict[str, int] | None = None

    @property
    def rounds(self) -> int:
        return len(self.new_per_round)

    @property
    def total_found(self) -> int:
        return sum(self.new_per_round)

    def marginal_yield(self, window: int = 3) -> float | None:
        """Mean new finds over the last ``window`` rounds.

        ``None`` before there are enough rounds to average, rather than a figure computed
        from one round and presented as a trend.
        """
        if window <= 0 or len(self.new_per_round) < window:
            return None
        recent = self.new_per_round[-window:]
        return sum(recent) / len(recent)

    def summary(self) -> str:
        if not self.new_per_round:
            return "discovery curve: no rounds yet"
        marginal = self.marginal_yield()
        line = (
            f"discovery: {self.total_found} verified over {self.rounds} round(s); "
            f"per round {self.new_per_round}"
        )
        if marginal is not None:
            line += f"; last-3 mean {marginal:.1f}/round"
        if self.included_per_round:
            line += (
                f"\n  judged relevant: {sum(self.included_per_round)} "
                f"per round {self.included_per_round}"
            )
        if self.split:
            s = self.split
            line += (f"\n  where they are now: {s['relevant']} judged relevant, "
                     f"{s['excluded_by_judge']} excluded by the judge, {s['excluded_by_rule']} "
                     f"by the year rule, {s['uncertain']} judged uncertain, {s['awaiting']} "
                     f"awaiting judgment"
                     + (f"; ruled on by you: {s['ruled_in']} in, {s['ruled_out']} out"
                        if s.get("ruled_in") or s.get("ruled_out") else ""))
        return line


def discovery_curve(corpus: Corpus) -> DiscoveryCurve:
    from .models import RULING_DECIDED_BY
    from .years import RULE_DECIDER

    found = lambda pool: [r for r in pool.values()
                          if r.provenance.discovered_by is not DiscoveredBy.SEED]
    ruled = lambda r: r.relevance.decided_by == RULING_DECIDED_BY
    greenlist, excluded = found(corpus.greenlist), found(corpus.excluded)
    uncertain = found(corpus.uncertain)
    # The judge's counts leave out papers the user ruled on, as `flow.json` does.
    split = {"relevant": sum(not ruled(r) for r in greenlist),
             "excluded_by_judge": sum(r.relevance.decided_by not in (RULE_DECIDER,
                                                                      RULING_DECIDED_BY)
                                      for r in excluded),
             "excluded_by_rule": sum(r.relevance.decided_by == RULE_DECIDER for r in excluded),
             "uncertain": sum(r.relevance.agent_verdict is not None for r in uncertain),
             "awaiting": sum(r.relevance.agent_verdict is None for r in uncertain),
             "ruled_in": sum(ruled(r) for r in greenlist),
             "ruled_out": sum(ruled(r) for r in excluded)}
    return DiscoveryCurve(new_per_round=list(corpus.new_per_round),
                          included_per_round=list(corpus.included_per_round),
                          split=split if any(split.values()) else None)


# --------------------------------------------------------------------------------------
# Combined report
# --------------------------------------------------------------------------------------


@dataclass
class CoverageReport:
    """What the user is shown about progress. The curve, and nothing modelled."""

    curve: DiscoveryCurve
    epoch: int = 0

    def lines(self) -> list[str]:
        out = [self.curve.summary()]
        if self.epoch:
            out.append(f"epoch {self.epoch}: the topic was steered {self.epoch} time(s); the "
                       f"curve counts every round, the supply signal only this epoch's")
        return out

    def summary(self) -> str:
        return "\n".join(self.lines())


def coverage_report(corpus: Corpus) -> CoverageReport:
    """Build the progress report for a corpus."""
    return CoverageReport(curve=discovery_curve(corpus), epoch=corpus.epoch)
