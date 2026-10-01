"""The verification cascade — the *verify* half of generate–verify.

The agent is never trusted about whether a paper exists; this module is never asked
whether a paper is interesting.

The central inversion: our input already carries a DOI, so the primary path
is an **exact key lookup**, not fuzzy title matching. Fuzzy matching is the *fallback*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .matching import (
    AGREE,
    AgreementVerdict,
    TitleAgreement,
    authors_agree,
    evaluate_title_agreement,
    is_generic_title,
    years_agree,
)
from .matching import surname as matching_surname
from .models import (
    Bibliographic,
    type_in_scope,
    DiscoveredBy,
    PaperProposal,
    PaperRecord,
    Provenance,
    Rejection,
    RegistrySource,
    RejectionReason,
    VerificationOutcome,
    VerificationPath,
)
from .normalize import REGISTRY_ID_PREFIX, normalize_doi, normalize_title, title_similarity
from .registry.base import (AuthRefused, NotFound, PoliteSession, QueryRejected, Registry,
                            RegistryUnavailable, RegistryWork)
from .versions import resolve_version_of_record


def record_identifier(work: RegistryWork) -> str | None:
    """The identifier a record built from ``work`` is keyed on: its normalized DOI, else
    ``openalex:W...``, else ``None``."""
    doi = normalize_doi(work.doi)
    if doi is not None:
        return doi
    if work.registry_id:
        return REGISTRY_ID_PREFIX + work.registry_id
    return None


#: The DOI failures the title search can recover from, and how the record says so.
_DOI_FAILURES = {RejectionReason.DOI_UNRESOLVED: "unresolved",
                 RejectionReason.DOI_TITLE_MISMATCH: "mismatch",
                 RejectionReason.MALFORMED_DOI: "malformed"}


@dataclass
class KnownState:
    """What the corpus already holds, for duplicate detection.

    Titles are carried as well as DOIs because unresolvable seed papers are kept with a
    local id and a redlist entry by title, and must not be re-admitted.
    """

    dois: set[str]
    normalized_titles: set[str]

    @classmethod
    def empty(cls) -> "KnownState":
        return cls(dois=set(), normalized_titles=set())

    def has_doi(self, doi: str | None) -> bool:
        return doi is not None and doi in self.dois

    def has_title(self, title: str | None) -> bool:
        n = normalize_title(title)
        return bool(n) and n in self.normalized_titles


#: Why a generic-titled candidate found by title search, with no year or author agreeing,
#: was refused.
_GENERIC_UNSUPPORTED = "G: generic title found by title search, nothing agrees"
#: Refusals that look at the title alone, not the year or authors.
_TITLE_ONLY_REFUSALS = ("G: generic title without near-exact agreement",
                        "F: no-author work with imperfect title")


def _with_refusals(detail: str | None, attempts: list[dict] | None) -> str | None:
    """`detail`, with why each candidate the title search found was refused and how to recover:
    the agent sees `detail`, not the attempts."""
    notes: list[str] = []
    for attempt in attempts or []:
        for r in attempt.get("refused", []):
            reason = r["condition"].split(": ", 1)[-1]
            fix = ("give its year, its authors or its DOI" if r["condition"] == _GENERIC_UNSUPPORTED
                   else "give its exact title or its DOI"
                   if r["condition"].startswith(_TITLE_ONLY_REFUSALS)
                   else "check its year and authors, or give its DOI")
            if (note := f"refused: {reason} ({fix})") not in notes:
                notes.append(note)
    return "; ".join([detail, *notes] if detail else notes) or detail


class Verifier:
    """Runs one proposal through the verification cascade.

    ``failover`` answers when ``primary`` cannot, and is the title search's last step.
    It is **not** a cross-check: OpenAlex contains ~99% of Crossref and Crossref ~90%
    of OpenAlex, so agreement between them is weak evidence of existence.

    ``abstracts`` is where a round looks up the abstracts its new records lack (Europe
    PMC). It verifies nothing; it rides here so its session is reported with the others.
    """

    def __init__(
        self,
        primary: Registry,
        config: Config,
        failover: Registry | None = None,
        abstracts: Any = None,
    ) -> None:
        self.primary = primary
        self.failover = failover
        self.abstracts = abstracts
        self.config = config

    def registry_report(self) -> dict[str, Any]:
        """Each live registry session's report, keyed by registry name."""
        return {reg.name: reg.session.report()
                for reg in (self.primary, self.failover, self.abstracts)
                if isinstance(getattr(reg, "session", None), PoliteSession)}

    # ----------------------------------------------------------------------------------

    def verify(
        self,
        proposal: PaperProposal,
        *,
        round_index: int,
        known: KnownState | None = None,
        discovered_by: DiscoveredBy = DiscoveredBy.AGENT_PROPOSAL,
    ) -> VerificationOutcome:
        known = known or KnownState.empty()
        kw = dict(round_index=round_index, known=known, discovered_by=discovered_by)

        if not proposal.has_doi:
            return self._verify_by_title(proposal, **kw)
        # A DOI that is malformed, does not resolve, or belongs to another paper does not
        # cost the paper -- the title is searched. This is what lets the proposer give DOIs
        # without risk.
        exclude: set[str] = set()
        if proposal.normalized_doi is None:
            doi_rejection = self._reject(proposal, RejectionReason.MALFORMED_DOI, round_index,
                                         detail=f"does not match DOI syntax: {proposal.doi!r}"
                                         ).rejection
        else:
            outcome = self._verify_by_doi(proposal, **kw)
            if outcome.accepted or outcome.rejection.reason not in _DOI_FAILURES:
                return outcome
            doi_rejection = outcome.rejection
            if doi_rejection.reason is RejectionReason.DOI_TITLE_MISMATCH:
                exclude.add(proposal.normalized_doi)   # the DOI path refused it
        by_title = self._verify_by_title(proposal, exclude=exclude, **kw)
        # The DOI failure rides along whatever the title found, so it is counted
        # and (for an unresolved DOI) redlisted by the round.
        by_title.doi_failure = doi_rejection
        if by_title.accepted:
            by_title.record.provenance.proposed_doi_failed = _DOI_FAILURES[doi_rejection.reason]
            return by_title
        if by_title.rejection.reason is not RejectionReason.NO_DOI_NO_TITLE_MATCH:
            return by_title          # a duplicate, an out-of-scope type or an outage: the real answer
        doi_rejection.title_attempts = by_title.rejection.title_attempts
        doi_rejection.detail = _with_refusals(doi_rejection.detail, doi_rejection.title_attempts)
        return VerificationOutcome(rejection=doi_rejection)

    # ----------------------------------------------------------------------------------
    # Primary path: exact DOI lookup
    # ----------------------------------------------------------------------------------

    def _verify_by_doi(
        self,
        proposal: PaperProposal,
        *,
        round_index: int,
        known: KnownState,
        discovered_by: DiscoveredBy,
    ) -> VerificationOutcome:
        doi = proposal.normalized_doi

        if known.has_doi(doi):
            return self._reject(proposal, RejectionReason.DUPLICATE, round_index, detail=doi)

        try:
            work, source = self._fetch(doi)
        except NotFound:
            # A definitive answer: the paper does not exist. Likely fabricated.
            return self._reject(proposal, RejectionReason.DOI_UNRESOLVED, round_index, detail=doi)
        except RegistryUnavailable as exc:
            # NOT a rejection of the paper — our infrastructure failed. Must be retried,
            # or a registry outage silently shrinks the corpus.
            return self._reject(
                proposal, RejectionReason.REGISTRY_ERROR, round_index, detail=exc.detail
            )

        if not self._type_in_scope(work):
            return self._reject(proposal, RejectionReason.OUT_OF_SCOPE_TYPE, round_index,
                                detail=f"type={work.type!r}", registry_identifier=doi)

        # The hallucinated-pairing guard: a resolving DOI proves *a* paper exists, not
        # *the* paper the model described. Evaluated by the tiered matcher, so
        # legitimate title drift is accepted-and-flagged while an uncorroborated mismatch
        # is still rejected.
        agreement = self._evaluate_agreement(proposal, work)
        disagreement: str | None = None

        if not agreement.accepted and source is RegistrySource.OPENALEX and self.failover:
            # Agreement between registries is weak evidence and is not sought: Crossref is
            # a failover, not a cross-check. *Disagreement* is strong evidence that one
            # registry's record is corrupt, so on a mismatch — and only then — the failover
            # is asked. OpenAlex can attach a correct DOI to the wrong work (a discussion
            # piece by another author, say) while Crossref has it right.
            try:
                alt = self.failover.fetch_by_doi(doi)
            except (NotFound, RegistryUnavailable):
                alt = None
            if alt is not None and self._type_in_scope(alt):
                alt_agreement = self._evaluate_agreement(proposal, alt)
                if alt_agreement.accepted:
                    disagreement = work.title
                    work, source, agreement = alt, RegistrySource.CROSSREF, alt_agreement

        if not agreement.accepted:
            return VerificationOutcome(
                rejection=Rejection(
                    reason=RejectionReason.DOI_TITLE_MISMATCH,
                    proposal_title=proposal.title,
                    proposed_doi=doi,
                    registry_title=work.title,
                    registry_identifier=record_identifier(work),
                    detail=f"{agreement.condition} (similarity={agreement.similarity:.3f}): "
                           f"{agreement.detail}",
                    round=round_index,
                )
            )

        # The registry's record may be keyed on another DOI than the one proposed; if the run
        # holds that one, this is a duplicate, as on the title path.
        if known.has_doi(identifier := record_identifier(work)):
            return self._reject(proposal, RejectionReason.DUPLICATE, round_index,
                                detail=identifier)

        record = self._to_record(
            work,
            proposal=proposal,
            round_index=round_index,
            source=source,
            path=VerificationPath.DOI_EXACT,
            discovered_by=discovered_by,
            agreement=agreement,
            vor=self._resolve_vor(work),
        )
        if disagreement is not None:
            record.provenance.registry_disagreement = disagreement
        return VerificationOutcome(record=record)

    # ----------------------------------------------------------------------------------
    # Fallback path: title search
    # ----------------------------------------------------------------------------------

    def _verify_by_title(
        self,
        proposal: PaperProposal,
        *,
        round_index: int,
        known: KnownState,
        discovered_by: DiscoveredBy,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> VerificationOutcome:
        """The title search: for a proposal without a usable DOI, and for one whose
        DOI failed. ``exclude`` holds identifiers the DOI path already refused.

        This is what makes the nullable DOI safe: the prompt can tell the model to set
        ``doi: null`` rather than invent one, because there is a real path here.
        """
        if known.has_title(proposal.title):
            return self._reject(
                proposal, RejectionReason.DUPLICATE, round_index, detail="title already known"
            )

        # OpenAlex's title filter, then OpenAlex's general search, then Crossref -- moving on
        # whenever a step yields no accepted match, not only when it returns nothing (a wrong
        # first answer must not end the search).
        limit = self.config.matching.title_search_candidates
        steps = [("filter", RegistrySource.OPENALEX, self.primary.search_by_title)]
        general = getattr(self.primary, "search_general", None)
        if general is not None:
            steps.append(("search", RegistrySource.OPENALEX, general))
        if self.failover is not None:
            steps.append(("crossref", RegistrySource.CROSSREF, self.failover.search_by_title))

        attempts: list[dict] = []
        for step, source, search in steps:
            try:
                candidates = search(proposal.title, limit=limit)
            except QueryRejected as exc:
                # Our query was refused -- a failed step, not an outage to retry.
                attempts.append({"step": step, "query_rejected": exc.detail,
                                 "candidates": 0, "best_similarity": None})
                continue
            except RegistryUnavailable as exc:
                # A step that could not answer is recorded, and makes the result a
                # retry -- never a title redlisted after only some steps searched it.
                attempts.append({"step": step, "outage": exc.detail})
                continue
            except NotFound:
                candidates = []
            candidates = [w for w in candidates if record_identifier(w) not in exclude]
            scored = self._collapse_duplicate_records(self._score_candidates(proposal, candidates))
            best_sim = max((sim for _w, sim, _c in scored), default=None)
            attempts.append({"step": step, "candidates": len(candidates),
                             "best_similarity": best_sim})
            best, refused = self._agreeing_best(
                proposal, scored, seed=discovered_by is DiscoveredBy.SEED)
            if refused:
                attempts[-1]["refused"] = refused
            if best is not None and record_identifier(best[0]) is None:
                best = None                    # nothing stable to key it on; keep looking
            if best is None:
                continue

            work, score = best
            identifier = record_identifier(work)
            if not self._type_in_scope(work):
                # Recorded, and the cascade moves on -- the in-scope work a dataset or
                # figure record hangs off may be found by the next step.
                attempts[-1].update(out_of_scope=work.type, identifier=identifier)
                continue
            if known.has_doi(identifier):
                return self._reject(proposal, RejectionReason.DUPLICATE, round_index,
                                    detail=identifier)
            outcome = VerificationOutcome(record=self._to_record(
                work, proposal=proposal, round_index=round_index, source=source,
                path=VerificationPath.TITLE_FALLBACK, discovered_by=discovered_by,
                match_score=score, vor=self._resolve_vor(work)))
            outcome.record.provenance.title_search_step = step
            return outcome

        if outages := [a for a in attempts if "outage" in a]:
            out = self._reject(proposal, RejectionReason.REGISTRY_ERROR, round_index,
                               detail="; ".join(f"{a['step']}: {a['outage']}" for a in outages))
            out.rejection.title_attempts = attempts
            return out
        if refused := [a for a in attempts if "query_rejected" in a]:
            out = self._reject(proposal, RejectionReason.REGISTRY_QUERY_REJECTED, round_index,
                               detail="; ".join(f"{a['step']}: query refused, "
                                                f"{a['query_rejected']}" for a in refused))
            out.rejection.title_attempts = attempts
            return out
        if oos := next((a for a in attempts if "out_of_scope" in a), None):
            out = self._reject(proposal, RejectionReason.OUT_OF_SCOPE_TYPE, round_index,
                               detail=f"type={oos['out_of_scope']!r}",
                               registry_identifier=oos["identifier"])
            out.rejection.title_attempts = attempts
            return out
        sims = [a["best_similarity"] for a in attempts if a["best_similarity"] is not None]
        return self._reject(
            proposal,
            RejectionReason.NO_DOI_NO_TITLE_MATCH,
            round_index,
            # "No clear match" only when no candidate was refused (a refused one can be an
            # exact title).
            detail=_with_refusals(
                "; ".join(f"{a['step']}: {a['candidates']} candidate(s)" for a in attempts)
                + ("" if any(a.get("refused") for a in attempts)
                   else ", no clear match above threshold"), attempts),
            # What the search found, so the report can tell "a similar title is
            # registered" from "nothing like it is", across every step.
            candidates=sum(a["candidates"] for a in attempts),
            best_similarity=max(sims, default=None),
            title_attempts=attempts,
        )

    def _fetch(self, doi: str) -> tuple[RegistryWork, RegistrySource]:
        """Primary lookup with failover on *unresponsiveness only*.

        A 404 propagates as ``NotFound``: it is a real answer meaning the paper does not
        exist, and retrying it against a second registry would be treating a definitive
        negative as a transport failure.
        """
        try:
            return self.primary.fetch_by_doi(doi), RegistrySource.OPENALEX
        except RegistryUnavailable as exc:
            if self.failover is None:
                raise
            try:
                return self.failover.fetch_by_doi(doi), RegistrySource.CROSSREF
            except NotFound:
                # Crossref not knowing a DOI is no answer while OpenAlex is down
                # (Crossref holds no DataCite DOIs), so it is retried, never redlisted.
                raise RegistryUnavailable(exc.service, f"{exc.detail}; Crossref has no "
                                                       f"record of the DOI") from None

    def _resolve_vor(self, work: RegistryWork):
        """Attempt version-of-record resolution.

        Runs only for preprints, so a normal article costs no extra call. On failure the
        non-VoR record is kept and flagged rather than dropped — preprints are acceptable,
        but **provenance for extracted data must be tracked**, because data extracted from
        a figure depends on the version, and figures get revised in review.

        A registry failure here must not fail the whole verification: the paper is real
        and verified, we simply could not find its published counterpart.
        """
        if not self.config.matching.resolve_version_of_record:
            return None
        try:
            return resolve_version_of_record(
                work, self.primary,
                title_threshold=self.config.matching.title_fallback_threshold,
                limit=self.config.matching.title_search_candidates,
            )
        except AuthRefused:
            raise
        except Exception:  # noqa: BLE001 - VoR is enrichment, never a gate
            return None

    def _evaluate_agreement(
        self, proposal: PaperProposal, work: RegistryWork
    ) -> TitleAgreement:
        m = self.config.matching
        return evaluate_title_agreement(
            proposal.title,
            work,
            proposed_year=proposal.year,
            proposed_authors=proposal.authors,
            near_exact=m.title_near_exact,
            high=m.title_high,
            moderate=m.title_moderate,
        )

    def _type_in_scope(self, work: RegistryWork) -> bool:
        """Blocklist, not allowlist — see `models.OUT_OF_SCOPE_TYPES`.

        Only definitive non-documents are excluded here. Whether an erratum or an
        encyclopedia entry is *wanted* is a relevance question, and relevance belongs to
        the judge.
        """
        return type_in_scope(work.type)

    def _score_candidates(
        self, proposal: PaperProposal, candidates: list[RegistryWork]
    ) -> list[tuple[RegistryWork, float, int]]:
        """Rank title candidates by ``(similarity, corroboration)``.

        Two **separate** dimensions, not one blended number. A bonus added to the
        similarity and clamped to 1.0 would vanish whenever the base similarity is already
        1.0, i.e. exactly the identical-title tie where a tie-break is the entire point.

        Lexicographic ranking keeps similarity primary and lets corroboration decide only
        genuine ties — the tie-breaker role reserved for model-supplied year and author
        data.
        """
        out: list[tuple[RegistryWork, float, int]] = []
        for work in candidates:
            sim = title_similarity(proposal.title, work.title)
            corroboration = 0
            year_state, _ = years_agree(proposal.year, [work.year] if work.year else [])
            if year_state == AGREE:
                corroboration += 1
            auth_state, _ = authors_agree(proposal.authors, work.authors)
            if auth_state == AGREE:
                corroboration += 1
            out.append((work, sim, corroboration))
        out.sort(key=lambda t: (t[1], t[2]), reverse=True)
        return out

    def _agreeing_best(self, proposal: PaperProposal,
                       scored: list[tuple[RegistryWork, float, int]], *, seed: bool = False
                       ) -> tuple[tuple[RegistryWork, float] | None, list[dict]]:
        """The clear best candidate that also passes the DOI path's agreement rule.

        A best candidate whose year and authors contradict the proposal is refused, recorded,
        and dropped, and the next is considered: the right paper for a refused match can be
        further down the list.

        A title search finds a paper by its title alone, so a generic title
        (`is_generic_title`) needs its year or one of its first two authors to agree; a
        distinctive title stands on its own. Seeds are exempt: the user supplied the PDF,
        and seed verification passes no year or authors. Refused, not redlisted.
        """
        refused: list[dict] = []
        corroboration = {id(w): c for w, _s, c in scored}
        while (best := self._clear_best(scored, proposal.title)) is not None:
            agreement = self._evaluate_agreement(proposal, best[0])
            # Either title generic (the searched one or the found one).
            generic = is_generic_title(best[0].title) or is_generic_title(proposal.title)
            if agreement.accepted and not seed and generic and not corroboration[id(best[0])]:
                agreement = TitleAgreement(AgreementVerdict.REJECT, _GENERIC_UNSUPPORTED,
                                           agreement.similarity,
                                           "neither its year nor its authors agree")
            if agreement.accepted:
                return best, refused
            refused.append({"identifier": record_identifier(best[0]),
                            "condition": agreement.condition})
            scored = [t for t in scored if t[0] is not best[0]]
        return None, refused

    @staticmethod
    def _collapse_duplicate_records(
        scored: list[tuple[RegistryWork, float, int]]
    ) -> list[tuple[RegistryWork, float, int]]:
        """Two registry records of **one work** are one candidate.

        OpenAlex can hold one paper twice, for example under two DOI schemes of the same
        publisher, with most citations on one record. Both match the title exactly, so
        `_clear_best` would see a tie with equal corroboration and refuse. Same normalized
        title, same year, same first-author surname is the same work; keep the record with
        the most citations, which is the one the literature actually points at. Genuinely
        different works that share a title differ in year or author and are left alone.
        """
        kept: dict[tuple, tuple[RegistryWork, float, int]] = {}
        order: list[tuple] = []
        for work, sim, corr in scored:
            surname = (matching_surname(work.authors[0]) or None) if work.authors else None
            if work.year is None or surname is None:
                # Title alone is not identity evidence. Two same-title records with no
                # year or author to compare stay two candidates, and `_clear_best`
                # refuses the tie -- the rule is title *and* year *and* author.
                key = ("uncollapsed", id(work))
            else:
                key = (normalize_title(work.title or ""), work.year, surname)
            if key not in kept:
                kept[key] = (work, sim, corr)
                order.append(key)
                continue
            # One work, so either record's agreement is evidence.
            best = max(corr, kept[key][2])
            if (work.cited_by_count or 0) > (kept[key][0].cited_by_count or 0):
                kept[key] = (work, sim, best)
            else:
                kept[key] = (kept[key][0], kept[key][1], best)
        return [kept[k] for k in order]

    def _clear_best(
        self, scored: list[tuple[RegistryWork, float, int]], proposal_title: str | None = None
    ) -> tuple[RegistryWork, float] | None:
        """Accept only a *clear* best match above threshold.

        "Clear" means one of two things, and the second matters:

        1. the best beats the runner-up on **similarity** by the configured margin; or
        2. similarities are effectively tied but the best is **better corroborated** —
           year and authors agree where the runner-up's do not.

        Case 2 is the identical-title tie. Treating it as unclear would reject a match we
        can actually distinguish; treating it as clear *without* the corroboration check
        would pick arbitrarily. An arbitrary accept is worse than a rejection the agent
        can retry with a DOI.
        """
        if not scored:
            return None
        m = self.config.matching
        best_work, best_sim, best_corr = scored[0]
        if best_sim < m.title_fallback_threshold:
            return None
        if len(scored) > 1:
            _, next_sim, next_corr = scored[1]
            if (best_sim - next_sim) < m.title_fallback_margin:
                # Similarities are effectively tied: only strictly better corroboration
                # makes this a distinguishable best.
                if best_corr <= next_corr:
                    # Citations, then the version, can still pick one among equally
                    # corroborated identical titles: a version of record beats its own
                    # preprint, as in `versions.py` and the export.
                    tied = [s for s in scored
                            if (best_sim - s[1]) < m.title_fallback_margin and s[2] == best_corr]
                    return self._narrow_tie(tied, proposal_title)
        return best_work, best_sim

    def _narrow_tie(self, tied: list[tuple[RegistryWork, float, int]],
                    proposal_title: str | None) -> tuple[RegistryWork, float] | None:
        """The tie-break is for **identical** titles.

        The similarity margin can tie "…, part I" with "…, part II" (similarity 0.99), and
        the more-cited part must not win when the proposal names the other exactly. So a
        candidate whose normalised title is the proposal's wins; otherwise the most-cited
        rule applies only if every tied title is the same, and anything else is refused.
        """
        if proposal_title:
            want = normalize_title(proposal_title)
            exact = [t for t in tied if normalize_title(t[0].title or "") == want]
            if len(exact) == 1:
                return exact[0][0], exact[0][1]
            if exact:
                tied = exact
        if len({normalize_title(t[0].title or "") for t in tied}) > 1:
            return None
        return self._break_tie(tied)

    #: Version rank for the tie fallback: the version a citation would normally give.
    _VERSION_RANK = {"article": 3, "journal-article": 3, "conference-paper": 2,
                     "proceedings-article": 2, "preprint": 0, "posted-content": 0}

    def _break_tie(
        self, tied: list[tuple[RegistryWork, float, int]]
    ) -> tuple[RegistryWork, float] | None:
        """Choose among equally corroborated identical-title candidates, or refuse.

        Most-cited wins, with ranked versions as the fallback: a journal article tied with
        its own conference version or preprint is the one kept. Anything still tied is
        refused — an arbitrary accept is worse than a rejection the agent can retry with a
        DOI.
        """
        if len(tied) < 2:
            return None
        for key in (lambda t: t[0].cited_by_count if t[0].cited_by_count is not None else -1,
                    lambda t: self._VERSION_RANK.get((t[0].type or "").lower(), 1)):
            top = max(key(t) for t in tied)
            tied = [t for t in tied if key(t) == top]
            if len(tied) == 1:
                return tied[0][0], tied[0][1]
        # Every survivor names the same work: one DOI can be registered twice.
        if len({t[0].doi for t in tied if t[0].doi}) == 1 and all(t[0].doi for t in tied):
            return tied[0][0], tied[0][1]
        return None

    def _reject(
        self,
        proposal: PaperProposal,
        reason: RejectionReason,
        round_index: int,
        detail: str | None = None,
        **found,
    ) -> VerificationOutcome:
        return VerificationOutcome(
            rejection=Rejection(
                reason=reason,
                proposal_title=proposal.title,
                proposed_doi=proposal.doi,
                detail=detail,
                round=round_index,
                **found,
            )
        )

    def _to_record(
        self,
        work: RegistryWork,
        *,
        proposal: PaperProposal,
        round_index: int,
        source: RegistrySource,
        path: VerificationPath,
        discovered_by: DiscoveredBy,
        match_score: float | None = None,
        agreement: TitleAgreement | None = None,
        vor: Any = None,
    ) -> PaperRecord:
        """Build a record. Registry values win over anything the model supplied."""
        registry_title = work.title or proposal.title
        proposed_title = (
            proposal.title
            if normalize_title(proposal.title) != normalize_title(registry_title)
            else None
        )
        return PaperRecord(
            doi=record_identifier(work) or str(work.doi),
            title=registry_title,
            authors=work.authors,
            year=work.year,
            venue=work.venue,
            type=work.type,
            oa_status=work.oa_status,
            oa_url=work.oa_url,
            oa_pdf_url=work.best_oa_pdf_url,
            oa_version=work.best_oa_version,
            oa_license=work.best_oa_license,
            cited_by_count=work.cited_by_count,
            openalex_id=work.registry_id,
            references=list(work.references),
            abstract=work.abstract,
            abstract_source=source.value if work.abstract else None,
            bibliographic=Bibliographic(**work.bibliographic),
            extra_fields=dict(proposal.extra_fields),
            provenance=Provenance(
                discovered_by=discovered_by,
                discovered_round=round_index,
                source=source,
                verification_path=path,
                proposed_title=proposed_title,
                title_drift=bool(agreement and agreement.drifted),
                match_condition=agreement.condition if agreement else None,
                match_similarity=(
                    agreement.similarity if agreement is not None else match_score
                ),
                is_version_of_record=(vor.is_version_of_record if vor else None),
                preprint_doi=(vor.preprint_doi if vor else None),
                version_of_record_doi=(vor.vor_doi if vor else None),
            ),
        )
