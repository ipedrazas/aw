"""Git helpers for the workspace repository.

The workspace is normally its own repository. When it is a directory inside a
larger repository (as in this checkout), the enclosing repository is used. When
there is no git at all, every function degrades to ``None`` and the run records
the file's content hash instead of a commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip()


def is_repo(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree") == "true"


def head_commit(root: Path) -> str | None:
    return _git(root, "rev-parse", "HEAD") or None


def file_commit(root: Path, rel: str) -> str | None:
    """The last commit that touched ``rel``; None if untracked or no git."""
    out = _git(root, "log", "-n", "1", "--format=%H", "--", rel)
    return out or None


def is_dirty(root: Path, rel: str) -> bool:
    out = _git(root, "status", "--porcelain", "--", rel)
    return bool(out)


def commit_paths(
    root: Path,
    rels: list[str],
    message: str,
    author_name: str | None = None,
    author_email: str | None = None,
) -> str | None:
    """Stage and commit the given paths, attributing the change to the author. Returns the commit."""
    if not is_repo(root):
        return None
    if _git(root, "add", "--", *rels) is None:
        return None
    args = ["commit", "-m", message, "--", *rels]
    if author_name and author_email:
        args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", *args]
    if _git(root, *args) is None:
        return None
    return head_commit(root)


def file_history(root: Path, rel: str, limit: int = 30) -> list[dict[str, str]]:
    """The commits that changed ``rel``, newest first: what each was, when and by whom.
    Empty when there is no git."""
    out = _git(root, "log", f"-n{limit}", "--format=%H%x1f%aI%x1f%an%x1f%s", "--follow", "--", rel)
    rows = []
    for line in (out or "").splitlines():
        commit, when, who, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        rows.append({"commit": commit, "when": when, "who": who, "subject": subject})
    return rows


def file_at(root: Path, rel: str, commit: str) -> str | None:
    """``rel`` as it was at ``commit`` (relative to ``root``, which may sit inside a larger
    repository), or None if it did not exist there."""
    out = _git(root, "show", f"{commit}:./{rel}")
    return None if out is None else out + "\n"
