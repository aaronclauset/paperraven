"""Tiered title-agreement evaluation.

## Why tiers rather than one threshold

A single cut-off has to serve two incompatible jobs at once: catching a fabricated
pairing (real DOI, wrong paper) and tolerating legitimate title drift. Set it high and
real papers are lost; set it low and wrong papers are admitted.

The resolution is **graduated corroboration**: high title similarity needs little
supporting evidence, low similarity needs a lot. Year and author agreement do the
corroborating. Every outcome — accept, accept-with-drift, and reject — carries a labelled
condition so the *rule that fired* is recoverable from the log, not just its category.

## Title drift is accepted, not rejected

The goal is the right set of papers, not exact titles. So a resolving DOI whose registry
title has drifted from the proposal is **accepted with the registry's title and a drift
flag**, provided something else corroborates it being the same paper. Nothing
corroborating means it is probably a wrong pairing, and that is still rejected — accepting
it would admit the wrong paper.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from enum import Enum

from .normalize import normalize_title, title_similarity
from .registry.base import RegistryWork


class AgreementVerdict(str, Enum):
    ACCEPT = "accept"
    #: Same paper, drifted title. Accepted with the registry's title; the proposal's
    #: title is retained in provenance and the record is flagged.
    ACCEPT_WITH_DRIFT = "accept_with_drift"
    REJECT = "reject"


@dataclass(frozen=True)
class TitleAgreement:
    verdict: AgreementVerdict
    condition: str
    similarity: float
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict in (AgreementVerdict.ACCEPT, AgreementVerdict.ACCEPT_WITH_DRIFT)

    @property
    def drifted(self) -> bool:
        return self.verdict is AgreementVerdict.ACCEPT_WITH_DRIFT


# --------------------------------------------------------------------------------------
# Corroborating signals
# --------------------------------------------------------------------------------------

#: Publication-year window, in years, relative to the proposal's year. **Asymmetric on
#: purpose**: a registry record can postdate the work by more than it can predate it —
#: a conference paper presented one year appears in archival proceedings a year or two
#: later. A symmetric window would reject those correct matches.
YEAR_WINDOW_EARLIER = -1
YEAR_WINDOW_LATER = 2

#: Only the first two author surnames are checked. Later authors are unreliable in
#: model-supplied lists, and requiring the full list to agree would reject correct matches
#: over an author the model omitted.
AUTHOR_CHECK_DEPTH = 2

#: A title with fewer content tokens than this is treated as **generic** and must match
#: near-exactly (Condition G): a short record title otherwise matches unrelated longer
#: papers and takes their metadata.
GENERIC_TITLE_MAX_TOKENS = 4
#: The same test for CJK and Thai titles, in characters.
GENERIC_TITLE_MAX_CHARS_UNSPACED = 8


def _unspaced_script(ch: str) -> bool:
    """CJK ideographs, Hiragana, Katakana, Hangul syllables, Thai."""
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0x3040 <= o <= 0x30FF
            or 0xAC00 <= o <= 0xD7AF or 0x0E00 <= o <= 0x0E7F)


#: Tri-state signal outcomes. With a bare bool, "missing data" and "actually agrees"
#: would both be ``True``, and a match with no year and no authors anywhere would be
#: reported as *corroborated*. Absence of contradiction is not evidence of agreement.
AGREE = "agree"
UNVERIFIABLE = "unverifiable"
DISAGREE = "disagree"


def years_agree(proposed: int | None, registry_years: list[int]) -> tuple[str, str]:
    """Tri-state comparison of publication years.

    Missing data yields ``UNVERIFIABLE``, never ``AGREE``: an absent year is no evidence
    of disagreement (older records are sparse) but it is equally no evidence *for* a
    match, and only the latter may be used to corroborate.
    """
    if proposed is None or not registry_years:
        return UNVERIFIABLE, "year unverifiable"
    for y in registry_years:
        diff = y - proposed
        if YEAR_WINDOW_EARLIER <= diff <= YEAR_WINDOW_LATER:
            return AGREE, f"year {y} within window of {proposed}"
    return DISAGREE, f"year {registry_years} outside window of {proposed}"


_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
_FOLD = str.maketrans({"ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l",
                       "ı": "i"})


def _surname(name: str) -> str:
    """Extract a surname from either "Ada Lovelace" or "Lovelace, Ada".

    Comma form puts the surname *first*, so the two orders cannot be handled by the same
    positional rule. "et al." and name suffixes (Jr., III) are not surnames, and
    surnames compare with accents folded (Erdős = Erdos), so a correct paper is not refused
    over how its author was written.
    """
    import unicodedata

    text = re.sub(r"\bet\.?\s*al\b\.?", " ", name, flags=re.I)
    head = text.split(",", 1)[0] if "," in text else text
    parts = [p.strip(".") for p in head.split() if p.strip(".")]
    while len(parts) > 1 and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    if not parts:
        return ""
    folded = unicodedata.normalize("NFKD", parts[-1].lower().translate(_FOLD))
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def surname(name: str) -> str:
    """The comparable surname of an author string (public: the verifier's duplicate check)."""
    return _surname(name)


def authors_agree(
    proposed: list[str], registry: list[str], depth: int = AUTHOR_CHECK_DEPTH
) -> tuple[str, str]:
    """Tri-state comparison of author surnames.

    Set intersection over the registry's first ``depth`` surnames rather than a positional
    comparison: author order is not reliably reproduced, and one shared leading surname is
    strong evidence for a title that has merely drifted.

    Missing data on either side yields ``UNVERIFIABLE``, for the same reason as years.
    """
    if not proposed or not registry:
        return UNVERIFIABLE, "authors unverifiable"
    reg = {_surname(a) for a in registry[:depth] if _surname(a)}
    prop = {_surname(a) for a in proposed if _surname(a)}
    if not reg or not prop:
        return UNVERIFIABLE, "authors unverifiable"
    shared = reg & prop
    if shared:
        return AGREE, f"author surname match: {sorted(shared)}"
    return DISAGREE, f"no author overlap (registry: {sorted(reg)})"


def is_generic_title(title: str | None) -> bool:
    """Whether a title is too unspecific to identify a paper on its own.

    Short titles are the tractable case. A title consisting only of stop words is caught
    too, but that is rare in practice.
    """
    n = normalize_title(title)
    if not n:
        return True
    # Scripts written without spaces between words are measured in characters: a whole
    # Japanese title is one "token", so would always be generic.
    letters = [ch for ch in n if ch.isalpha()]
    unspaced = sum(1 for ch in letters if _unspaced_script(ch))
    if letters and unspaced * 2 > len(letters):
        return unspaced <= GENERIC_TITLE_MAX_CHARS_UNSPACED
    tokens = n.split()
    if len(tokens) <= GENERIC_TITLE_MAX_TOKENS:
        return True
    stop = {"a", "an", "the", "of", "on", "in", "and", "for", "to", "with", "from"}
    return all(t in stop for t in tokens)


def _registry_years(work: RegistryWork) -> list[int]:
    return [work.year] if isinstance(work.year, int) else []


def _accept(sim: float, high: float, condition: str, detail: str) -> "TitleAgreement":
    """Accept, flagging drift when the titles meaningfully disagree.

    Drift is a property of the *titles*, not of which condition admitted the record — so
    it is decided here rather than per-branch. A drifted title is accepted and flagged,
    and a reviewer needs the flag whichever rule fired.
    """
    verdict = (
        AgreementVerdict.ACCEPT if sim >= high else AgreementVerdict.ACCEPT_WITH_DRIFT
    )
    return TitleAgreement(verdict, condition, sim, detail)


# --------------------------------------------------------------------------------------
# The tiered evaluator
# --------------------------------------------------------------------------------------


def evaluate_title_agreement(
    proposed_title: str,
    work: RegistryWork,
    *,
    proposed_year: int | None = None,
    proposed_authors: list[str] | None = None,
    near_exact: float = 0.95,
    high: float = 0.85,
    moderate: float = 0.60,
) -> TitleAgreement:
    """Decide whether ``work`` is the paper ``proposed_title`` describes.

    Conditions are evaluated in order; the first to apply decides. Guards come first
    precisely because they exist to override an otherwise-passing similarity score.

    | condition | rule |
    |---|---|
    | **G** | registry title is generic → require near-exact agreement |
    | **F** | registry record has no authors → require near-exact agreement |
    | **A** | similarity > high, year agrees → accept |
    | **B** | similarity > moderate, year agrees, authors agree → accept |
    | **C** | similarity > moderate but one signal disagrees → accept with drift |
    | **D** | similarity ≤ moderate, year **and** authors both agree → accept with drift |
    | **E** | otherwise → reject |
    """
    sim = title_similarity(proposed_title, work.title)
    year_state, year_detail = years_agree(proposed_year, _registry_years(work))
    auth_state, auth_detail = authors_agree(proposed_authors or [], work.authors)

    # Only a positive AGREE may corroborate. UNVERIFIABLE means "we could not check",
    # which is neither support nor objection.
    contradicted = DISAGREE in (year_state, auth_state)
    corroborations = [year_state, auth_state].count(AGREE)
    signals = f"{year_detail}; {auth_detail}"

    # -- G: a generic registry title cannot identify a paper on its own. Checked before
    #       everything else because a short title inflates similarity against anything
    #       containing its words.
    if is_generic_title(work.title):
        if sim >= near_exact and year_state == auth_state == DISAGREE:
            # A short title is where two papers share one, so year and authors both
            # contradicting refuses it.
            return TitleAgreement(AgreementVerdict.REJECT,
                                  "G: generic title, near-exact, year and authors contradict",
                                  sim, signals)
        if sim >= near_exact:
            return TitleAgreement(AgreementVerdict.ACCEPT, "G: generic title, near-exact",
                                  sim, f"registry title is generic; {sim:.3f} >= {near_exact}")
        return TitleAgreement(
            AgreementVerdict.REJECT, "G: generic title without near-exact agreement", sim,
            f"registry title {work.title!r} is too generic to identify a paper at {sim:.3f}",
        )

    # -- F: author-less records are disproportionately errata, retraction notices,
    #       directory entries and review-aggregator stubs. Without an author list there is
    #       no corroborating signal at all, so the title must carry the whole burden.
    if not work.authors:
        if sim >= near_exact:
            return TitleAgreement(AgreementVerdict.ACCEPT, "F: no-author work, near-exact",
                                  sim, f"no registry authors; {sim:.3f} >= {near_exact}")
        return TitleAgreement(
            AgreementVerdict.REJECT, "F: no-author work with imperfect title", sim,
            f"no registry authors and similarity only {sim:.3f}",
        )

    # -- A: high similarity is strong enough to stand on its own, so an unverifiable
    #       signal does not block it. A positive contradiction does — unless the other
    #       signal positively agrees, which makes the contradiction much weaker evidence.
    if sim > high:
        if not contradicted:
            return _accept(sim, high, "A: high similarity", f"{sim:.3f} > {high}; {signals}")
        if corroborations >= 1:
            return TitleAgreement(
                AgreementVerdict.ACCEPT_WITH_DRIFT,
                "A: high similarity, one signal contradicted but another agrees", sim, signals,
            )
        return TitleAgreement(AgreementVerdict.REJECT,
                              "A: high similarity but a signal contradicts and none agree",
                              sim, signals)

    # -- B / C: moderate similarity is NOT self-sufficient, so at least one signal must
    #       positively agree: a moderate match with nothing checkable is rejected rather
    #       than reported as corroborated.
    if sim > moderate:
        if corroborations == 0:
            return TitleAgreement(
                AgreementVerdict.REJECT, "C: moderate similarity, nothing corroborates", sim,
                f"{sim:.3f} > {moderate} but no signal could confirm it; {signals}",
            )
        if not contradicted:
            return _accept(sim, high, "B: moderate similarity, corroborated",
                           f"{sim:.3f} > {moderate}; {signals}")
        return TitleAgreement(
            AgreementVerdict.ACCEPT_WITH_DRIFT,
            "C: moderate similarity, partly corroborated", sim,
            f"{sim:.3f} > {moderate}; {signals}",
        )

    # -- D: low similarity is acceptable *only* when both signals positively agree. This
    #       is the title-drift case: the same paper cited under a different title, with
    #       year and authorship still lining up. Rejecting it would lose real papers.
    if corroborations == 2:
        return TitleAgreement(
            AgreementVerdict.ACCEPT_WITH_DRIFT, "D: title drift, year and authors agree", sim,
            f"low similarity {sim:.3f} but {signals}",
        )

    # -- E: nothing supports this being the same paper.
    return TitleAgreement(
        AgreementVerdict.REJECT, "E: low similarity, uncorroborated", sim,
        f"{sim:.3f} <= {moderate}; {signals}",
    )
