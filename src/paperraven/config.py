"""Configuration for a PaperRaven run.

The config is written by the startup interview and is **the reproducible record of a
run**: interview once, then resume and re-run from the file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import (BaseModel, ConfigDict, Field, field_validator, model_serializer,
                      model_validator)

#: Environment variable that overrides `contact_email`.
#: The address is not a secret — both registries receive it in the clear, which is the
#: point of the polite pool — but it is PII, and configs get shared and committed.
CONTACT_EMAIL_ENV = "PAPERRAVEN_CONTACT_EMAIL"

#: Read depth for seed PDFs. The agent **asks** rather than deciding silently, because
#: full-text reading is expensive and the cost is the user's to accept.
READ_DEPTH_CHOICES = ("front_matter", "full")


#: Said once, wherever a criterion is not `{text, by}`.
CRITERION_SHAPE = (
    "a criterion is written as {text: ..., by: user|agent} (structured provenance); "
    "a plain string, with or without a [USER]/[AGENT] prefix, is not one. Write it as "
    "{text, by}")


class Criterion(BaseModel):
    """An inclusion, an exclusion or a judging note, with who it belongs to.

    `by: user` — typed or signed off by the user; `by: agent` — written, inferred or worded by
    the tool (the product's `[USER]`/`[AGENT]` contract). A field, not a tag inside the text or
    a YAML comment, so rewriting the config cannot drop it. Required, so an untagged config
    cannot run.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    by: Literal["user", "agent"]

    @model_validator(mode="before")
    @classmethod
    def _refuse_plain_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            raise ValueError(f"{CRITERION_SHAPE}; got {data[:70]!r}")
        return data

    @field_validator("text")
    @classmethod
    def _text_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a criterion's text must be non-empty")
        return v.strip()


class Note(Criterion):
    """A judging note, which may carry the user's ruling on one paper.

    A ruling on one paper is recorded as a refinement of the main config text. The
    text still reaches the judge as a test case; `paper` and `verdict` let `steer` apply the
    ruling to that record itself, so it holds through later re-judges. A ruling is the user's
    by definition. The two fields are left out of the serialisation when unset, so a plain
    note's canonical form -- and every existing generation's digest -- is unchanged.
    """

    paper: str | None = None
    verdict: Literal["include", "exclude"] | None = None

    @model_validator(mode="after")
    def _a_ruling_is_whole_and_the_users(self) -> "Note":
        if (self.paper is None) != (self.verdict is None):
            raise ValueError("a ruling needs both `paper` and `verdict`")
        if self.paper is not None and self.by != "user":
            raise ValueError("a ruling on a paper is the user's: `by: user`")
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_ruling(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items()
                if not (k in ("paper", "verdict") and v is None)}


def texts(items: Iterable[Criterion]) -> list[str]:
    """The criteria as the judge and the proposer read them: text only."""
    return [c.text for c in items]


def content_dump(model: BaseModel) -> dict[str, Any]:
    """`model_dump()` with every criterion reduced to its text.

    What `steer` compares: a change to `by` alone is a provenance change — it opens no epoch
    and re-judges nothing — while a change to any text is what it always was.
    """
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            if set(v) == {"text", "by"}:
                return v["text"]
            if set(v) == {"text", "by", "paper", "verdict"}:        # a ruling
                return {"text": v["text"], "paper": v["paper"], "verdict": v["verdict"]}
            return {k: strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return strip(model.model_dump())


class Facet(BaseModel):
    """A named keyword group.

    There is no `required` / `enriching` flag: encoding a conjunction mechanically matters
    only for a numeric similarity score, and the LLM judge reads the exclusion prose
    directly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    keywords: list[str] = Field(default_factory=list)


class ExtractionField(BaseModel):
    """An extra field the agent should extract per paper.

    This is the mechanism that keeps domain-specific fields out of the package: the *topic
    config* declares domain fields, the code stays domain-agnostic.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    description_file: str | None = None
    #: The user's brief **verbatim** -- what they actually typed, unedited. `description`
    #: is the distillation the user signed off; this is its source. Both reach the judge,
    #: so extrapolation in the distillation is visible against the words it was distilled
    #: from.
    user_brief: str | None = None
    facets: list[Facet] = Field(default_factory=list)
    exclusion_facets: list[Facet] = Field(default_factory=list)
    inclusions: list[Criterion] = Field(default_factory=list)
    exclusions: list[Criterion] = Field(default_factory=list)
    #: Publication-year limits, inclusive, **enforced by Python**, not by the judge. A paper
    #: is admitted if *any* of its publication dates is inside them, because both registries
    #: report the earliest *online* date by design, which can be a year before the print
    #: issue. Part of `topic`, so changing a limit is a topic change.
    year_min: int | None = None
    year_max: int | None = None

    @model_validator(mode="after")
    def _ordered_years(self) -> "Topic":
        if self.year_min is not None and self.year_max is not None \
                and self.year_min > self.year_max:
            raise ValueError(f"topic.year_min ({self.year_min}) must not exceed "
                             f"topic.year_max ({self.year_max})")
        return self

    def resolved_description(self, base_dir: Path | None = None) -> str:
        """Inline description, or the contents of `description_file`."""
        if self.description:
            return self.description
        if self.description_file:
            p = Path(self.description_file)
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            return p.read_text(encoding="utf-8")
        return ""


class Judging(BaseModel):
    """Operational guidance to the judge.

    Guidance that changes verdicts is configuration, so it is recorded rather than living
    only in a judge subagent's brief: the agent proposes a note, the user approves it
    through `steer`, it carries `by: user|agent` like any criterion, and its text is
    rendered into the judge prompt Python writes.

    **Its own section, not `topic`**: a changed note re-judges every judged
    record, since verdicts under the old note are stale, but opens **no** epoch — it changes
    how the criteria are read, not which papers the proposer samples.
    """

    model_config = ConfigDict(extra="forbid")

    notes: list[Note] = Field(default_factory=list)


class Seeds(BaseModel):
    """Seed papers are **PDF files on disk**, not DOIs — DOIs would mean fetching, which
    means publisher paywalls.

    Every PDF in `pdf_dir`, plus each of `files` (extra seeds). A relative
    `files` entry is looked for in `pdf_dir` first, then in the directory `init` runs from;
    each PDF is read once.
    """

    model_config = ConfigDict(extra="forbid")

    pdf_dir: str | None = None
    files: list[str] = Field(default_factory=list)
    read_depth: str = "front_matter"

    @field_validator("read_depth")
    @classmethod
    def _known_depth(cls, v: str) -> str:
        if v not in READ_DEPTH_CHOICES:
            raise ValueError(f"read_depth must be one of {READ_DEPTH_CHOICES}, got {v!r}")
        return v


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: **Default 5, and that is a measured value, not a style preference.** Prior
    #: experiments found that asking for more than 5 per round encourages hallucination.
    #: Do not raise this for throughput.
    papers_per_round: int = 5
    max_rounds: int = Field(default=40, ge=1)
    #: Papers the tool should **find** — seeds do not count.
    target_papers: int = Field(default=100, ge=1)

    @field_validator("papers_per_round")
    @classmethod
    def _sane_round_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("papers_per_round must be >= 1")
        return v


class Stopping(BaseModel):
    """Multiple conditions, first to trigger, reason reported. Never enforced silently —
    the user decides when to stop."""

    model_config = ConfigDict(extra="forbid")

    min_new_per_round: int = Field(default=1, ge=0)
    patience_rounds: int = Field(default=3, ge=1)
    #: A round is **thin** when the proposer offers at most
    #: `floor(thin_round_fraction x papers_per_round)` papers, and `patience_rounds` thin
    #: rounds in a row are the "running dry" stop reason. It reads the supply the proposer
    #: actually offered rather than its self-assessment, which can miss under-production.
    #: The default 0.3 is at most 1 of 5.
    thin_round_fraction: float = Field(default=0.3, gt=0.0, lt=1.0)
    #: Secondary trigger only: a very high uncertain fraction suggests the
    #: clearly-relevant material is exhausted. Not primary — that would couple the
    #: stopping rule to the judge's uncalibrated confidence.
    uncertain_ratio_trigger: float | None = 0.90


class Politeness(BaseModel):
    """Polite-pool settings. No throttle database — a semaphore and a delay are adequate
    at ~5 records per round."""

    model_config = ConfigDict(extra="forbid")

    request_delay_seconds: float = Field(default=0.1, ge=0.0)
    max_attempts: int = Field(default=4, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    user_agent_product: str = "PaperRaven"
    user_agent_version: str = "0.1.0"


class Matching(BaseModel):
    """Title-agreement settings.

    Every cut point here is **uncalibrated**. They are meant to be calibrated against a
    canonical greenlist, not guessed: an unvalidated rule such as
    `score > 90 or substring` is the kind of untested constant that silently costs recall.

    There is deliberately **no** general-purpose `title_match_threshold`:
    `title_fallback_threshold` does that work. A threshold that
    can be tuned without effect is worse than no threshold: it invites a calibration run
    that concludes the tool is insensitive to its own parameter.
    """

    model_config = ConfigDict(extra="forbid")

    #: Tiered cut points for `matching.evaluate_title_agreement`.
    #: Graduated corroboration: high similarity needs little support, low similarity needs
    #: year *and* author agreement. The structure follows patterns from prior
    #: production work; the values differ because our comparison base is a bare title
    #: rather than full citation rawtext, and remain to be calibrated.
    title_near_exact: float = Field(default=0.95, ge=0.0, le=1.0)
    title_high: float = Field(default=0.85, ge=0.0, le=1.0)
    title_moderate: float = Field(default=0.60, ge=0.0, le=1.0)
    #: How many candidates the title-only fallback retrieves. Deliberately > 1: with
    #: `rows: 1`, any paper whose top hit is wrong is silently lost.
    title_search_candidates: int = 5
    title_fallback_threshold: float = Field(default=0.93, ge=0.0, le=1.0)
    #: Margin the best title candidate must beat the runner-up by, so only a *clear* best
    #: match is accepted. The numeric value is uncalibrated. With several
    #: candidates scoring alike, picking the top one is arbitrary, and an arbitrary
    #: accept is worse than a rejection the agent can retry with a DOI.
    title_fallback_margin: float = Field(default=0.05, ge=0.0, le=1.0)
    #: Attempt version-of-record resolution on accepted preprints. Costs one extra
    #: title search **per preprint only** — articles are unaffected. Disable for offline
    #: runs, where the extra lookup is noise.
    resolve_version_of_record: bool = True


class Config(BaseModel):
    """A complete run configuration."""

    model_config = ConfigDict(extra="forbid")

    topic: Topic = Field(default_factory=Topic)
    judging: Judging = Field(default_factory=Judging)
    seeds: Seeds = Field(default_factory=Seeds)
    extraction_fields: list[ExtractionField] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    stopping: Stopping = Field(default_factory=Stopping)
    politeness: Politeness = Field(default_factory=Politeness)
    matching: Matching = Field(default_factory=Matching)

    contact_email: str | None = None

    # ---------------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Load from YAML, applying the environment override for `contact_email`."""
        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        # A list, a bare value, or `topic: some text` gets a message, not a traceback.
        if not isinstance(data, dict):
            raise ValueError(f"{p}: a configuration must be a mapping of keys to values, not "
                             f"a {type(data).__name__}")
        for name, info in cls.model_fields.items():
            kind = info.annotation
            if (isinstance(kind, type) and issubclass(kind, BaseModel)
                    and data.get(name) is not None and not isinstance(data[name], dict)):
                raise ValueError(f"{p}: `{name}` must be a section of keys (for example "
                                 f"`{name}:` then indented `key: value` lines), not a "
                                 f"{type(data[name]).__name__}")
        # A description file's text is inlined, so the published generation pins it
        # and an edit to the file cannot change a run silently. Relative to the config file.
        topic = data.get("topic") or {}
        if topic.get("description_file") and not topic.get("description"):
            src = Path(topic["description_file"])
            topic["description"] = (src if src.is_absolute() else p.parent / src).read_text(
                encoding="utf-8")
        cfg = cls.model_validate(data)
        cfg = cfg.with_env_overrides()
        return cfg

    def with_env_overrides(self) -> "Config":
        """Return a copy with environment overrides applied.

        The env var **wins** over the config field, so a shared config never forces one
        person's address onto another's requests.
        """
        env_email = os.environ.get(CONTACT_EMAIL_ENV)
        if env_email:
            return self.model_copy(update={"contact_email": env_email.strip()})
        return self

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json", exclude_none=False), sort_keys=False),
            encoding="utf-8",
        )

    # ---------------------------------------------------------------------------------

    def require_contact_email(self) -> str:
        """The polite-pool contact address, or raise.

        Both registries grant faster, more reliable service to identified clients, and
        the address must be configuration rather than hardcoded.
        """
        if not self.contact_email or "@" not in self.contact_email:
            # The one place that answers "do we have a usable contact address?", so every
            # caller gives the same message and treats it as misuse (exit 2), not a failure.
            raise ValueError(
                "no contact email. Both registries give identified clients faster and "
                "more reliable service, so it is required: set "
                f"{CONTACT_EMAIL_ENV} or `contact_email` in the config."
            )
        return self.contact_email

    @property
    def user_agent(self) -> str:
        """A real User-Agent carrying product, version and contact."""
        contact = self.contact_email or "unknown"
        return (
            f"{self.politeness.user_agent_product}/"
            f"{self.politeness.user_agent_version} (mailto:{contact})"
        )

# --------------------------------------------------------------------------------------
# Config generations
# --------------------------------------------------------------------------------------

#: The editable input. It is named as a draft so no one mistakes it for the live
#: configuration: an edit here takes effect only through `steer`.
DRAFT_FILENAME = "config_draft.yaml"


class ConfigMissing(FileNotFoundError):
    """State names a generation whose file is gone.

    Deliberately **not** a digest failure. A missing file has no content to hash, so
    reporting it as corruption would send the user looking for an edit that never happened
    instead of a file that was moved or deleted.
    """


class ConfigIntegrityError(RuntimeError):
    """A committed generation's bytes no longer match the digest state recorded.

    Integrity against accidental mutation, **not authentication** — anyone who can edit
    both the file and state can recompute the digest. That is adequate for this threat
    model and must be described as what it is.
    """


def canonical_yaml(config: "Config") -> str:
    """The one serialization a generation is written and hashed from.

    Hashing the *user's* draft bytes and reserializing later would let comments, key order
    and line endings make logically identical configurations produce different
    generations. So publication normalizes once: parse, validate, serialize canonically,
    hash those bytes, write exactly those bytes.
    """
    return yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=False),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )


#: Excluded from the digest. `contact_email` is a courtesy identifier for registry rate
#: limits — it cannot change which papers are found — and is deliberately per-operator
#: via an environment variable. Attesting it would put
#: the digest in permanent disagreement with the effective configuration on any machine
#: whose env var differs, which is a lie about everything else the digest covers.
#:
#: The cost, accepted: editing the address inside a committed generation is not detected.
DIGEST_EXCLUDED_FIELDS = ("contact_email",)


def _digest_payload(config: "Config") -> str:
    data = config.model_dump(mode="json", exclude_none=False)
    for field in DIGEST_EXCLUDED_FIELDS:
        data.pop(field, None)
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False,
                          allow_unicode=True)


def canonical_digest(config: "Config") -> str:
    """Digest of everything in a configuration that can change results.

    Computed from the **parsed** configuration rather than the file's bytes, so
    reformatting a generation is tolerated while changing a value is not. That is the
    property worth having: the question is whether the configuration that produced these
    results changed, not whether someone re-indented it.
    """
    return hashlib.sha256(_digest_payload(config).encode("utf-8")).hexdigest()


def draft_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / DRAFT_FILENAME


def write_draft(config: "Config", run_dir: str | Path) -> Path:
    """Create the editable draft, **never overwriting an existing one**.

    Refusing to overwrite is the whole contract: the draft holds
    unsubmitted edits, which are the user's work and not ours to discard.
    """
    path = draft_path(run_dir)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_yaml(config), encoding="utf-8")
    return path


def generation_path(run_dir: str | Path, generation: int) -> Path:
    return Path(run_dir) / f"config_gen{generation:03d}.yaml"


def publish_generation(
    config: "Config", run_dir: str | Path, generation: int
) -> tuple[Path, str, int]:
    """Write an immutable generation, returning its path, digest, **and the generation
    actually allocated** — which may be past the one requested.

    Returning the allocated number is not a convenience. Callers record it in state, and a
    caller that assumed it got the number it asked for would write state naming an
    *abandoned* orphan while the real configuration sat one file along — recreating by
    accident the config/state disagreement this whole design exists to prevent.

    Allocation walks forward past **orphans** — generation files left by a `steer` that
    crashed before its state commit. An orphan whose **bytes** are identical is reused;
    anything else is left exactly where it is and the next number is taken. A committed or
    orphaned generation is **never overwritten**, because a file whose bytes can change is
    not a generation, whatever its name says.

    **The comparison is on bytes, and the orphan is never parsed.** A crash between
    `O_EXCL` create and write leaves *zero bytes*, which `yaml.safe_load` turns into `{}`
    and pydantic into a **default** `Config` — so a parsed comparison would adopt an empty
    file as the generation of a default configuration, with every later integrity check
    passing over a config that is not there. And parsing an unparseable orphan would raise
    `yaml.ParserError` or a validation error out of this function, which has no handler,
    on every subsequent `init` and `steer`.

    Byte equality is both sufficient and what the immutability sentence above actually
    means. The property given up is recognising an orphan that is semantically equal but
    textually different; `canonical_yaml` is deterministic, so producing one requires a
    serialization change between two runs of the same directory, and taking a fresh number
    there is correct.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    text = canonical_yaml(config)
    digest = canonical_digest(config)

    n = generation
    while True:
        path = generation_path(run_dir, n)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            if path.read_bytes() == text.encode("utf-8"):
                return path, digest, n  # byte-identical orphan of this config; reuse it
            n += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # Synced before the state that names it: the asymmetry that lets a crash orphan a
        # file but never leave state pointing at one that does not exist.
        return path, digest, n


def load_generation(
    run_dir: str | Path, generation: int, expected_digest: str | None = None,
    file_sha256: str | None = None,
) -> "Config":
    path = generation_path(run_dir, generation)
    if not path.is_file():
        raise ConfigMissing(
            f"the configuration this run committed is gone: {path} no longer exists. "
            f"Generation {generation} is named by the run's state, so it cannot simply be "
            f"skipped — restore the file, or steer a new generation from "
            f"{DRAFT_FILENAME}."
        )
    text = path.read_text(encoding="utf-8")
    parsed = Config.model_validate(yaml.safe_load(text) or {})
    found = canonical_digest(parsed)
    if expected_digest is not None and found != expected_digest:
        # The digest is over the config *as this version's schema reads it*, so a
        # schema change alters it with the file untouched. The file's own bytes
        # say which happened — and the message must not accuse the user of an edit.
        import hashlib

        from . import __version__

        raw = hashlib.sha256(path.read_bytes()).hexdigest()
        if file_sha256 is not None and raw == file_sha256:
            raise ConfigIntegrityError(
                f"{path} is unchanged since it was committed, but it no longer reads the same "
                f"under PaperRaven {__version__}: the configuration schema has changed since "
                f"this run was made. A run does not survive a schema change. Its committed "
                f"state and records are intact; export them with the version that made the "
                f"run, or start a fresh run."
            )
        if file_sha256 is None:
            raise ConfigIntegrityError(
                f"{path} does not match the fingerprint recorded when it was committed. "
                f"Either the file was edited after it was committed, or this run was made by "
                f"a different version of PaperRaven, whose configuration schema differs. "
                f"Committed generations are "
                f"immutable: to change the configuration, edit {DRAFT_FILENAME} and steer."
            )
        raise ConfigIntegrityError(
            f"{path} has changed since it was committed: digest {found[:16]}… does not "
            f"match the {expected_digest[:16]}… recorded in state. Committed generations "
            f"are immutable — edit {DRAFT_FILENAME} and steer instead of editing a "
            f"generation in place."
        )
    return parsed.with_env_overrides()


class ConfigOverrideRefused(RuntimeError):
    """An explicit `--config` was given for a run that is already initialised.

    A command run with an override executes against one configuration while its audit
    event names the committed generation — a lie in the trail, and provenance is the whole
    reason audit events are authoritative. Recording the override instead would preserve
    the lie in a more elaborate form, because state would still name a different live
    configuration. `steer` exists for mid-run change; this refuses the alternative.
    """


def load_snapshot(
    run_dir: str | Path, explicit: str | Path | None = None
) -> tuple[Any, "Config"]:
    """**One** loader returning a consistent `(corpus, config)` pair.

    Loading state and config separately would let a commit landing between the two loads
    produce a Runner holding old config with new state. Readers deliberately take no lock,
    which makes that overlap possible; one snapshot removes it.
    """
    from .corpus import Corpus

    corpus = Corpus.load(run_dir)
    if explicit:
        if corpus.initialized:
            raise ConfigOverrideRefused(
                f"{run_dir} is already initialised, so --config is refused: the command "
                f"would run against one configuration while its audit trail named the "
                f"committed generation {corpus.config_generation}. Use `steer` to change "
                f"the configuration of a live run."
            )
        return corpus, Config.load(explicit)
    if corpus.config_generation is None:
        raise ConfigMissing(
            f"{run_dir} has no committed configuration: pass --config to start a run, "
            f"or run `init` first if you meant to resume one."
        )
    return corpus, load_generation(
        run_dir, corpus.config_generation, corpus.config_digest,
        corpus.config_file_sha256)

