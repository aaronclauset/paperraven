"""What the run cost in tokens, read from the host's own session transcripts.

Python never sees token counts: the host does. Claude Code keeps each session's transcript under
`~/.claude/projects/<project folder>/`, with its subagents' beside it, and every model call there
carries its usage. `session_usage` reads the sessions that mention the run's folder, and only
their usage figures, and reports:

A session counts when it mentions the run's folder and was still active after the run began
(`since`, the run's first commit): two runs can share a folder name, one after the other. The
whole of such a session counts, the setup before `init` included.

- the raw tokens (cache reads, cache writes, uncached input and output);
- the input **weighted by the published cache rates**, relative to one input token: a cache read
  at 0.05 on Opus 5.5, 0.025 on Fable 5.1 and Mythos 5.1, and 0.1 on other models; a cache write
  at 2, since a subscription keeps its cache for an hour; uncached input at 1. Output is kept
  apart. The provider says cached tokens count less against a plan's limits but publishes no
  plan weights, so these are the API's.

Another host, or a folder with no transcripts, gives `None` figures and a note saying where it
looked.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

#: Cache-read weight relative to an input token, by model; any other model reads at 0.1.
CACHE_READ_WEIGHT = {"opus-5-5": 0.05, "fable-5-1": 0.025, "mythos-5-1": 0.025}
DEFAULT_CACHE_READ_WEIGHT = 0.1
#: A 1-hour cache write, relative to an input token.
CACHE_WRITE_WEIGHT = 2.0


def project_folder(projects: Path, cwd: Path) -> Path:
    """Where Claude Code keeps this folder's sessions: its path with every character that is
    not a letter or digit written as `-`."""
    return Path(projects) / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def _read_weight(model: str | None) -> float:
    return next((w for key, w in CACHE_READ_WEIGHT.items() if key in (model or "")),
                DEFAULT_CACHE_READ_WEIGHT)


def _calls(path: Path) -> dict[str, tuple[str | None, dict[str, Any]]]:
    """Each model call's final usage, by message id (a call spans several transcript rows)."""
    calls = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        if row.get("type") == "assistant" and isinstance(msg, dict) and msg.get("id") \
                and isinstance(msg.get("usage"), dict):
            calls[msg["id"]] = (msg.get("model"), msg["usage"])
    return calls


def session_usage(run_dir: Path, papers: int, *, projects: Path | None = None,
                  cwd: Path | None = None, since: datetime | None = None) -> dict[str, Any]:
    cwd = Path(cwd or Path.cwd()).resolve()
    folder = project_folder(projects or Path.home() / ".claude" / "projects", cwd)
    run = Path(run_dir).resolve()
    names = {str(run), run.as_posix()}
    try:
        names.add(Path(os.path.relpath(run, cwd)).as_posix())
    except ValueError:                                # another drive, on Windows
        pass
    recent = [p for p in sorted(folder.glob("*.jsonl")) if folder.is_dir()
              and (since is None or p.stat().st_mtime >= since.timestamp())]
    sessions = [p for p in recent
                if any(n in p.read_text(encoding="utf-8", errors="replace") for n in names)]
    if not sessions:
        return {"raw_tokens": None, "input_equivalent": None, "output_tokens": None,
                "per_paper": None, "sessions": [], "papers": papers,
                "note": f"no session transcript mentioning {run.name} in {folder}"}
    t = {"cache_read": 0, "cache_write": 0, "input": 0, "output": 0, "input_equivalent": 0.0}
    calls = 0
    for s in sessions:
        for f in [s, *sorted((s.parent / s.stem / "subagents").glob("*.jsonl"))]:
            for model, u in _calls(f).values():
                calls += 1
                cr, cw, inp, out = (int(u.get(k) or 0) for k in (
                    "cache_read_input_tokens", "cache_creation_input_tokens",
                    "input_tokens", "output_tokens"))
                t["cache_read"] += cr
                t["cache_write"] += cw
                t["input"] += inp
                t["output"] += out
                t["input_equivalent"] += cr * _read_weight(model) + cw * CACHE_WRITE_WEIGHT + inp
    raw = t["cache_read"] + t["cache_write"] + t["input"] + t["output"]
    return {"raw_tokens": raw, "input_equivalent": t["input_equivalent"],
            "output_tokens": t["output"], "cache_read": t["cache_read"],
            "cache_write": t["cache_write"], "uncached_input": t["input"], "calls": calls,
            "per_paper": ({"raw_tokens": raw / papers,
                           "input_equivalent": t["input_equivalent"] / papers,
                           "output_tokens": t["output"] / papers} if papers else None),
            "papers": papers, "sessions": [s.stem for s in sessions],
            "note": "input weighted by the published cache rates; output apart"}
