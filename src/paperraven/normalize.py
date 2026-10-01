"""DOI and title normalization.

Pure and side-effect free by design: this is where the subtle bugs live, so it must be
testable without a network.
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import unquote

# --------------------------------------------------------------------------------------
# DOI
# --------------------------------------------------------------------------------------

#: A DOI is `10.` + a 4-9 digit registrant code + `/` + a non-empty suffix.
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")

#: URL and scheme prefixes stripped before validation.
_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "https://www.doi.org/",
    "http://www.doi.org/",
    "doi.org/",
    "dx.doi.org/",
    "doi:",
    "doi ",
)

#: Trailing punctuation that commonly rides along when a DOI is copied out of prose.
#: NB: `)` is deliberately excluded — some real DOIs contain balanced parentheses,
#: as the SICI-style DOIs of older journal articles do.
_DOI_TRAILING_JUNK = ".,;:'\"<>]}"


_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
_URL_ONLY = re.compile(r"\s*(?:https?://|www\.)\S+\s*")


def clean_text(raw: str | None) -> str | None:
    """Registry text as a reader should see it: entities unescaped, tags stripped,
    whitespace collapsed. Registries hand back titles like ``<i>Genus species</i>`` or
    ``&lt;p&gt;…``; unescaped twice, because some arrive escaped twice."""
    if raw is None:
        return None
    text = _TAG.sub(" ", html.unescape(html.unescape(str(raw))))
    return " ".join(text.split()) or None


def usable_abstract(abstract: str | None, title: str | None) -> str | None:
    """The abstract, or `None` when the field holds no abstract: a bare URL, or a
    citation string -- the paper's own title with little else around it."""
    if not abstract or _URL_ONLY.fullmatch(abstract):
        return None
    nt, na = normalize_title(title), normalize_title(abstract)
    # Whole words, and a title long enough that containing it means something.
    if (len(nt.split()) >= 4 and f" {nt} " in f" {na} "
            and len(na.split()) <= len(nt.split()) + 25):
        return None
    return abstract


def normalize_doi(raw: str | None) -> str | None:
    """Normalize a DOI to canonical comparable form, or return ``None`` if malformed.

    Idempotent — ``normalize_doi(normalize_doi(x)) == normalize_doi(x)``. Applied before
    every registry lookup *and* before every dedup comparison, because
    inconsistent case is a genuine source of duplicate records.

    Returns ``None`` for anything failing ``DOI_PATTERN``; the caller records that as
    ``malformed_doi`` and makes **no network call**.
    """
    if raw is None:
        return None

    s = str(raw).strip()
    if not s:
        return None

    # Normalize unicode first: DOIs copied from PDFs pick up non-breaking spaces and
    # full-width characters that would otherwise defeat both stripping and the regex.
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if not ch.isspace() or ch == " ").strip()

    # Surrounding angle brackets, as used by BibTeX and some metadata exports.
    while s.startswith("<") and s.endswith(">") and len(s) > 2:
        s = s[1:-1].strip()

    # Scheme / host prefixes, case-insensitively and repeatedly: real-world strings
    # occasionally carry doubled prefixes such as "doi:https://doi.org/10.1/x".
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for prefix in _DOI_PREFIXES:
            if low.startswith(prefix):
                s = s[len(prefix):].strip()
                changed = True
                break

    # A DOI copied from a URL arrives percent-encoded; decode it once, so it is
    # neither double-encoded in a lookup nor missed as a duplicate of its decoded form.
    if re.search(r"%[0-9A-Fa-f]{2}", s):
        s = unquote(s)

    s = s.lower()

    # Internal whitespace is never valid in a DOI; a DOI that still contains any is damaged.
    if not s or any(c.isspace() for c in s):
        return None

    s = _strip_trailing_junk(s)
    return s if s and DOI_PATTERN.match(s) else None


def _strip_trailing_junk(s: str) -> str:
    """Strip punctuation a DOI picked up from surrounding prose.

    ``DOI_PATTERN``'s suffix is ``\\S+``, so it accepts trailing punctuation happily and
    cannot arbitrate this — the stripping heuristic has to.

    Brackets are handled by **balance**, not by blanket removal: a trailing ``]`` whose
    opening ``[`` appears earlier is DOI content, whereas an unmatched one was added by a
    citation. This keeps real structured DOIs intact — SICI-style DOIs of older journal
    articles carry balanced ``(...)`` and ``[...]`` — while still cleaning ``10.1000/182]``.
    """
    pairs = {"]": "[", "}": "{", ">": "<", ")": "("}
    while s:
        last = s[-1]
        if last in pairs:
            # Balanced -> content, stop stripping. Unbalanced -> junk, remove it.
            if s.count(pairs[last]) >= s.count(last):
                break
            s = s[:-1]
        elif last in ".,;:'\"":
            s = s[:-1]
        else:
            break
    return s.strip()


def is_valid_doi(raw: str | None) -> bool:
    """True when ``raw`` normalizes to a syntactically valid DOI."""
    return normalize_doi(raw) is not None


# --------------------------------------------------------------------------------------
# Title
# --------------------------------------------------------------------------------------

_MARKUP = re.compile(r"<[^>]*>")
_DASHES = re.compile(r"[-‐‑‒–—―−]+")
_TRAILING_FOOTNOTE = re.compile(r"([a-z])[1-9]$")
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)


def normalize_title(raw: str | None) -> str:
    """Normalize a title for comparison.

    Steps, in order: unescape HTML entities; strip markup; lowercase; collapse dashes and
    whitespace; drop a trailing footnote digit.

    Motivated by real damage seen in registry titles: escaped quotes (``Erratum [to \\"...``), stray trailing quotes, a lost leading
    character, and MathML-style ``<i>`` markup in Crossref titles.
    """
    if raw is None:
        return ""

    s = str(raw)

    # Entities may be doubly escaped in Crossref payloads ("&amp;lt;" -> "&lt;" -> "<").
    for _ in range(3):
        unescaped = html.unescape(s)
        if unescaped == s:
            break
        s = unescaped

    s = _MARKUP.sub(" ", s)
    s = unicodedata.normalize("NFKC", s).lower()
    s = _DASHES.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = " ".join(s.split()).strip()

    # A trailing single digit glued to a word is a footnote marker, not part of the title.
    # Applied after punctuation stripping so "title.1" and "title1" behave alike.
    s = _TRAILING_FOOTNOTE.sub(r"\1", s)

    return " ".join(s.split()).strip()


def title_similarity(a: str | None, b: str | None) -> float:
    """Symmetric similarity of two titles in ``[0.0, 1.0]`` after normalization.

    **Deliberately symmetric.** A token-*set* ratio, which scores 1.0 whenever one title's
    tokens are wholly contained in the other's, is not used: it gives false-perfect matches,
    such as a short generic registry title matching unrelated longer papers, or a proposal
    matching an *erratum* whose title merely contains the same words.

    Subtitle tolerance is provided instead by graduated corroboration in
    :mod:`paperraven.matching`, which accepts a lower similarity when the year and authors
    agree. That distinguishes "same paper, different title" from "different paper,
    overlapping words", which no string measure can do.
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    try:
        from rapidfuzz import fuzz  # type: ignore
    except ImportError:  # pragma: no cover - rapidfuzz is a declared dependency
        import difflib

        return difflib.SequenceMatcher(None, na, nb).ratio()

    # token_sort tolerates word-order differences; ratio is the character-level measure.
    # Both are symmetric, so neither rewards one title merely containing the other.
    return max(fuzz.token_sort_ratio(na, nb), fuzz.ratio(na, nb)) / 100.0


# ---------------------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Record identifiers
# --------------------------------------------------------------------------------------

#: A work OpenAlex indexes **without a DOI** is identified by its OpenAlex ID under this
#: scheme, e.g. ``openalex:W123456789``. An OpenAlex ID is a stable identifier, so such a
#: work can still be deduplicated; the prefix keeps the two kinds visibly distinct downstream.
REGISTRY_ID_PREFIX = "openalex:"


def is_record_identifier(value: str | None) -> bool:
    """A normalized DOI, or an ``openalex:W...`` identifier."""
    if not value:
        return False
    if value.startswith(REGISTRY_ID_PREFIX):
        return bool(re.fullmatch(r"W\d+", value[len(REGISTRY_ID_PREFIX):]))
    return normalize_doi(value) is not None


#: A first word that makes a paper *about* another paper.
_RESPONSE_WORDS = frozenset({"comment", "comments", "commentary", "correction", "corrigendum",
                             "erratum", "reply", "response", "retraction", "retracted",
                             "addendum"})
_ROMAN = frozenset({"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"})


def titles_conflict(a: str | None, b: str | None) -> bool:
    """Whether two similar-looking titles name different papers.

    Two signals that title similarity cannot see: a number or roman numeral that differs
    ("…, part I" / "…, part II"; "2018 update" / "2019 update"), and a response word that
    opens one title and not the other ("Comment on X", "Correction: X" / "X").
    """
    # A digit glued to the title's last word is a footnote mark ("…Networks)1"), not a
    # number; "part 2", with its space, is kept.
    unmark = lambda t: re.sub(r"(?<=[A-Za-z)\]])\d$", "", (t or "").strip())
    ta = re.findall(r"[a-z0-9]+", normalize_title(unmark(a)))
    tb = re.findall(r"[a-z0-9]+", normalize_title(unmark(b)))
    if not ta or not tb:
        return False
    if (ta[0] in _RESPONSE_WORDS) != (tb[0] in _RESPONSE_WORDS) or (
            ta[0] in _RESPONSE_WORDS and ta[0] != tb[0]):
        return True
    marks = lambda t: sorted(w for w in t if w.isdigit() or w in _ROMAN)
    return marks(ta) != marks(tb)


def doi_for_url(doi: str) -> str:
    """A DOI made safe for a URL path: `%`, `#` and `?` are percent-encoded.

    Unencoded, `#` starts a fragment and `?` a query string, so a DOI containing one (some old
    Wiley SICI DOIs end in `#`) would be looked up truncated and taken for a fabrication. Every
    other DOI character is left as registries and doi.org expect it.
    """
    return doi.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


def identifier_url(value: str) -> str:
    """The landing URL for a record identifier of either kind."""
    if value.startswith(REGISTRY_ID_PREFIX):
        return f"https://openalex.org/{value[len(REGISTRY_ID_PREFIX):]}"
    return f"https://doi.org/{doi_for_url(value)}"

