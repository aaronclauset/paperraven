"""A `.bib` reader for any tool's export, written in-house with no parsing dependency.

It reads what reference managers and hand-written files produce: nested braces, quoted values,
multi-line fields, `@string` macros with `#` concatenation, bare month names, `@comment` and
`@preamble`, and DOIs given as a field or inside a URL. It extracts only what fetching needs:
the key, DOI (or OpenAlex id), title, authors, year and venue. A malformed entry is skipped,
not raised: one bad entry must not cost the rest of a bibliography.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..normalize import normalize_doi

_MONTHS = {m: m for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                          "nov", "dec")}
_ACCENTS = {'"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
            "c": "\u0327", "v": "\u030c", "=": "\u0304", ".": "\u0307", "u": "\u0306", "H": "\u030b"}
_OPENALEX = re.compile(r"openalex\.org/(W\d+)", re.I)
_DOI_URL = re.compile(r"doi\.org/(10\.[^\s}\",?#]+)", re.I)
#: Where an entry's own identifier may be written. A DOI in `note` or `abstract` is
#: usually another paper's (a commented paper, a dataset).
_ID_FIELDS = ("url", "eprint", "howpublished")


@dataclass
class BibEntry:
    key: str
    type: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def doi(self) -> str | None:
        if self.fields.get("doi"):
            # `\_`, `\#`, `\%` are BibTeX escapes (Mendeley, JabRef), not DOI text.
            raw = re.sub(r"\\([_#%&])", r"\1", self.fields["doi"])
            if doi := normalize_doi(raw):
                return doi
        for name in _ID_FIELDS:                    # a malformed `doi` falls back to these
            if m := _DOI_URL.search(self.fields.get(name, "")):
                return normalize_doi(re.sub(r"\\([_#%&])", r"\1", m.group(1)))
        return None

    @property
    def openalex_id(self) -> str | None:
        for name in _ID_FIELDS:
            if m := _OPENALEX.search(self.fields.get(name, "")):
                return m.group(1)
        return None

    @property
    def title(self) -> str | None:
        return clean(self.fields.get("title")) or None

    @property
    def authors(self) -> list[str]:
        raw = self.fields.get("author") or ""
        out = []
        for name in re.split(r"\s+and\s+", raw):
            name = clean(name)
            if not name:
                continue
            if "," in name:                                # "Surname, Given" -> "Given Surname"
                last, _, first = name.partition(",")
                name = f"{first.strip()} {last.strip()}".strip()
            out.append(name)
        return out

    @property
    def year(self) -> int | None:
        m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", self.fields.get("year") or self.fields.get("date") or "")
        return int(m.group(1)) if m else None

    @property
    def venue(self) -> str | None:
        for f in ("journal", "journaltitle", "booktitle", "publisher", "school", "institution"):
            if self.fields.get(f):
                return clean(self.fields[f])
        return None


def clean(value: str | None) -> str:
    """TeX to plain text, enough for matching and file names: accents, commands, braces."""
    if not value:
        return ""
    text = re.sub(r"\\([\"'`^~=.]|[cvuH](?![a-zA-Z]))\s*\{?\\?([a-zA-Z])\}?",
                  lambda m: m.group(2) + _ACCENTS[m.group(1)], value)
    text = re.sub(r"\\[a-zA-Z]+\*?\s*", "", text)          # \textit, \emph ... keep the argument
    text = text.replace("{", "").replace("}", "").replace("~", " ").replace("--", "-")
    return unicodedata.normalize("NFC", " ".join(text.split()))


def parse_bib(text: str, problems: list[str] | None = None) -> list[BibEntry]:
    """Every well-formed entry in `text`, in order. `@string` macros are expanded.

    An entry that is never closed is skipped and noted in `problems` (with its line), and
    reading resumes at the next `@` that starts a line, so one bad entry does not end the file.
    """
    macros: dict[str, str] = dict(_MONTHS)
    entries: list[BibEntry] = []
    i, n = 0, len(text)
    while (at := text.find("@", i)) != -1:
        m = re.compile(r"@\s*(\w+)\s*([{(])").match(text, at)
        if not m:
            i = at + 1
            continue
        kind, opener = m.group(1).lower(), m.group(2)
        body_start = m.end()
        end = _matching(text, body_start - 1, opener)
        if end is None:
            if problems is not None:
                problems.append(f"line {text.count(chr(10), 0, at) + 1}: an entry that is "
                                f"never closed was skipped")
            nxt = re.compile(r"\n[ \t]*@").search(text, at + 1)
            if nxt is None:
                break
            i = nxt.start() + 1
            continue
        body = text[body_start:end]
        i = end + 1
        if kind in ("comment", "preamble"):
            continue
        if kind == "string":
            for name, value in _fields(body, macros).items():
                macros[name] = value
            continue
        key, _, rest = body.partition(",")
        if not key.strip() or "=" in key:
            continue
        entries.append(BibEntry(key=key.strip(), type=kind, fields=_fields(rest, macros)))
    return entries


def _matching(text: str, open_at: int, opener: str) -> int | None:
    close = "}" if opener == "{" else ")"
    depth, j, in_quote = 0, open_at, False
    while j < len(text):
        ch = text[j]
        if ch == '"' and opener == "(" and text[j - 1] != "\\":
            in_quote = not in_quote
        elif not in_quote and ch == opener:
            depth += 1
        elif not in_quote and ch == close:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _fields(body: str, macros: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    j, n = 0, len(body)
    while j < n:
        m = re.compile(r"\s*,?\s*([A-Za-z][\w\-:.]*)\s*=\s*").match(body, j)
        if not m:
            break
        name, j = m.group(1).lower(), m.end()
        parts = []
        while j < n:
            ch = body[j]
            if ch == "{":
                end = _matching(body, j, "{")
                if end is None:
                    return out
                parts.append(body[j + 1:end])
                j = end + 1
            elif ch == '"':
                k = j + 1
                depth = 0
                while k < n and not (body[k] == '"' and depth == 0 and body[k - 1] != "\\"):
                    depth += (body[k] == "{") - (body[k] == "}")
                    k += 1
                parts.append(body[j + 1:k])
                j = k + 1
            else:
                mm = re.compile(r"[^\s,#}]+").match(body, j)
                if not mm:
                    break
                word = mm.group(0)
                parts.append(macros.get(word.lower(), word))
                j = mm.end()
            ws = re.compile(r"\s*#\s*").match(body, j)
            if ws:
                j = ws.end()
                continue
            break
        out[name] = "".join(parts)
    return out
