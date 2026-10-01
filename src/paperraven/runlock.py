"""The run lock — a kernel-held exclusive lock over one run directory.

## Why this module is ten lines of mechanism and not a subsystem

The operating assumption is **one user, one agent session, one run directory**. Concurrency
is not a supported mode; it is detected and refused, never defended against. Two concurrent
runs use two directories.

So there are no ownership tokens, pid liveness checks, `unlock` command or `--force` flag:
forcing a lock open would let the displaced writer commit over its replacement.

What the module relies on is one property of `flock`: **the kernel releases the lock when the
process exits, by any means.** So there are no stale locks — and with no stale locks there
is nothing to recover, nothing to force, and no liveness to guess. Breaking a lock means
killing the process, which is also precisely what makes it unable to commit.

## Writers and readers

Writers take the lock for the whole command, network I/O included; that eliminates
the lost-update class rather than detecting it. Readers take nothing, because `os.replace`
hands them a coherent generation for free.

- **writers**: `init`, `submit`, `judge`, `verdicts`, `steer`, `seed`
- **readers**: `status`, `context`, `export`, `prompt`

`judge` is the one that looks like a reader and is not: it emits a file and mutates no
corpus record, but it records the active request's digest in state. Any
command that records an identity other commands later compare against is a writer.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

# `fcntl` does not exist on Windows, which locks a byte range with `msvcrt.locking` instead.
if os.name == "nt":                                      # pragma: no cover - Windows only
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None

#: The byte Windows locks: far past the owner metadata at the start of the file, because a
#: Windows lock is mandatory and would stop another process reading who holds it. Windows
#: allows locking beyond the end of a file.
WINDOWS_LOCK_OFFSET = 1 << 30

#: Inside the run directory. Created on first acquisition and never removed — see
#: :meth:`RunLock.release` for why unlinking it would be a bug rather than tidiness.
LOCK_FILENAME = ".lock"


class RunLocked(RuntimeError):
    """Raised when another process holds the run lock.

    Carries the holder's metadata rather than only the fact of refusal, so the user can
    see *who* holds the lock.
    """

    def __init__(self, run_dir: Path, owner: dict[str, Any]) -> None:
        self.run_dir = Path(run_dir)
        #: The holder's recorded metadata, or `{}` when it could not be read.
        self.owner = owner
        if owner:
            held = (
                f"{owner.get('command', '?')!r} (pid {owner.get('pid', '?')} on "
                f"{owner.get('hostname', '?')}, since {owner.get('acquired_at', '?')})"
            )
        else:
            held = "another process (owner metadata unreadable)"
        super().__init__(
            f"run {str(self.run_dir)!r} is locked by {held}.\n"
            f"Only one command may write to a run at a time. Wait for it to finish, or "
            f"stop that process — the lock is released automatically when it exits (on "
            f"Windows this can take a few seconds: try again)."
        )


class RunLock:
    """Exclusive lock over one run directory, held for the whole command.

    Usable as a context manager or through :meth:`acquire` / :meth:`release`.
    Acquisition never blocks: it succeeds or raises :class:`RunLocked` immediately, so a
    second writer learns at once instead of hanging behind a round that may take minutes.
    """

    def __init__(self, run_dir: str | Path, *, command: str) -> None:
        self.run_dir = Path(run_dir)
        self.command = command
        self.path = self.run_dir / LOCK_FILENAME
        self._fd: int | None = None

    # ----------------------------------------------------------------------------------

    def acquire(self) -> "RunLock":
        if self._fd is not None:
            raise RuntimeError("this RunLock is already held")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            # Held by someone else. Close *our* descriptor before reading the metadata:
            # the open fd is harmless, but leaking one per refusal is not.
            os.close(fd)
            raise RunLocked(self.run_dir, self._read_owner()) from None
        self._fd = fd
        try:
            self._write_owner()
        except BaseException:
            # An open descriptor *is* the lock. Failing to record who holds it must not
            # leave it held by a process that is about to unwind.
            self.release()
            raise
        return self

    def release(self) -> None:
        """Release the lock. Idempotent.

        The lock **file is deliberately not removed.** Unlinking it would be a race, not
        tidiness: another process may already hold that inode flocked, and deleting the
        name lets a third process create a fresh file and lock *that* — two holders, each
        correctly locked, on different inodes. The file's existence means nothing; only
        the kernel's lock on the open description does.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    # ----------------------------------------------------------------------------------

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    # ----------------------------------------------------------------------------------

    def _write_owner(self) -> None:
        """Record who holds the lock, for another process's refusal message.

        Informational only — `flock` is the mechanism. Not fsynced: a metadata line that
        is lost to a crash costs a clearer error message, and the lock it described is
        already gone with the process.
        """
        assert self._fd is not None
        payload = json.dumps({
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "command": self.command,
        }, indent=2, sort_keys=True)
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, payload.encode("utf-8"))

    def _read_owner(self) -> dict[str, Any]:
        """The holder's metadata, or `{}`.

        Every failure mode returns `{}` rather than propagating: we are already refusing,
        and the reason for refusal is the `flock`, not this file. A holder caught between
        `ftruncate` and `write` yields an empty or partial read, which is a worse error
        message and not a different outcome.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def _try_lock(fd: int) -> None:
    """Take the exclusive lock without waiting; `OSError` if another process holds it."""
    if msvcrt is not None:
        os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if msvcrt is not None:
        os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
