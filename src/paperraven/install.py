"""`paperraven install-skill`: put the skill and its two agents where Claude Code finds them.

The skill ships inside the package (`skill/`). This copies it into the project folder's
`.claude/`, the only place a project-level skill and its agents are read from, and fills in
two placeholders as it goes:

- `{{python}}`: the quoted path of the interpreter running this command, so every command in
  the skill runs the installed package whatever the agent's shell has on its PATH;
- `{{version}}`: the package version, which `check` compares with the installed skill.

Running it again rewrites only what differs, and says which files it replaced. It refuses to
run in the home directory, whose `.claude/` is Claude Code's own settings folder (the skill and
agents would then apply in every folder), and to write through a symlink, which would change
the file the link points to.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__

SKILL_DIR = Path(__file__).resolve().parent / "skill"

#: Packaged file -> where it goes in the project.
TARGETS = {
    "SKILL.md": ".claude/skills/paperraven/SKILL.md",
    "INSTRUCTIONS.md": ".claude/skills/paperraven/INSTRUCTIONS.md",
    "agents/paperraven-judge.md": ".claude/agents/paperraven-judge.md",
    "agents/paperraven-closeout.md": ".claude/agents/paperraven-closeout.md",
}
INSTALLED_SKILL = TARGETS["SKILL.md"]
VERSION_LINE = re.compile(r"<!-- paperraven (\S+) -->")


class InstallRefused(Exception):
    """The folder is not one to install into; nothing was written."""


def python_command(executable: str | None = None) -> str:
    """The interpreter as a shell word. Forward slashes, which Windows also accepts, keep a
    Windows path intact in a bash command."""
    return shlex.quote(str(executable or sys.executable).replace("\\", "/"))


def shell_word(value: object) -> str:
    """One argument of a printed command, quoted for the shell it will be pasted into."""
    if os.name == "nt":
        return subprocess.list2cmdline([str(value)])
    return shlex.quote(str(value))


def render(text: str, executable: str | None = None) -> str:
    return (text.replace("{{python}}", python_command(executable))
                .replace("{{version}}", __version__))


def install_skill(project: Path, *, home: Path | None = None,
                  executable: str | None = None) -> list[tuple[str, str]]:
    """Install into `project`; return each target with `written`, `replaced` or `unchanged`."""
    project = Path(project).resolve()
    if project == Path(home or Path.home()).resolve():
        raise InstallRefused(
            f"Not installing into your home folder ({project}): its .claude/ is Claude Code's "
            f"own settings folder, so the skill and agents would apply everywhere. Run "
            f"`paperraven install-skill` in the folder you will work in.")
    links = [target for target in TARGETS.values()
             if any(p.is_symlink() for p in [project / target, *(project / target).parents]
                    if p != project and project in p.parents)]
    if links:
        raise InstallRefused(
            f"Not installing: {', '.join(links)} is reached through a symlink, and writing "
            f"through it would change the file it points to. Remove the link and run "
            f"`paperraven install-skill` again.")
    report = []
    for source, target in TARGETS.items():
        text = render((SKILL_DIR / source).read_text(encoding="utf-8"), executable)
        path = project / target
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if old == text:
            report.append((target, "unchanged"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        report.append((target, "written" if old is None else "replaced"))
    return report


def installed_version(project: Path) -> str | None:
    """The version line of the skill installed in `project`, or None if there is none."""
    path = Path(project) / INSTALLED_SKILL
    if not path.is_file():
        return None
    m = VERSION_LINE.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else "unknown"
