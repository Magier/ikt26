"""Version control access.

Interface boundary #1a. `Repository` describes *what the agent may read*;
this module describes *where the content came from* - which branch, which
commit, written by whom, when.

The implementation shells out to the `git` binary. No GitPython, no dulwich:
the agent runs commands an investigator can re-run by hand, and the commands
appear verbatim in the exported trace.

`NoVersionControl` is the honest fallback for a plain directory. Everything
downstream must work without git - it just answers fewer questions.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

GIT_TIMEOUT = 15.0


class VcsError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    short_sha: str
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committed_at: str
    subject: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "short_sha": self.short_sha,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "authored_at": self.authored_at,
            "committer_name": self.committer_name,
            "committed_at": self.committed_at,
            "subject": self.subject,
        }


@dataclass
class ChangeSet:
    """What a branch actually changed, as opposed to what its PR claims."""

    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    files: List[str] = field(default_factory=list)
    commits: List[Commit] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "files": list(self.files),
            "commits": [commit.to_dict() for commit in self.commits],
        }


class VersionControl:
    """Contract used by the rest of the agent."""

    available = False
    kind = "none"

    def head(self, ref: str = "HEAD") -> Optional[Commit]:
        return None

    def resolve(self, ref: str) -> Optional[str]:
        return None

    def changed_files(self, base_ref: str, head_ref: str = "HEAD") -> List[str]:
        return []

    def changeset(self, base_ref: str, head_ref: str = "HEAD") -> Optional[ChangeSet]:
        return None

    def introducing_commit(self, path: str, head_ref: str = "HEAD") -> Optional[Commit]:
        """The commit that added `path`."""
        return None

    def list_files(self, ref: str) -> List[str]:
        return []

    def show(self, ref: str, path: str) -> Optional[str]:
        return None

    def diff(self, base_ref: str, head_ref: str = "HEAD", path: Optional[str] = None) -> str:
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "available": self.available}


class NoVersionControl(VersionControl):
    """A plain directory. Every question about provenance answers 'unknown'."""


# Stable, parseable commit formatting: unit-separated fields, one commit per line.
_SEP = "\x1f"
_FORMAT = _SEP.join(["%H", "%h", "%an", "%ae", "%aI", "%cn", "%cI", "%s"])


class GitRepository(VersionControl):
    kind = "git"

    def __init__(self, root: str) -> None:
        self.root = root
        self.available = self._detect()

    # -------------------------------------------------------------- plumbing

    def _run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", self.root] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=GIT_TIMEOUT,
                universal_newlines=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VcsError("git %s: %s" % (" ".join(args), exc))
        if completed.returncode != 0:
            raise VcsError(
                "git %s failed (%d): %s"
                % (" ".join(args), completed.returncode, completed.stderr.strip())
            )
        return completed.stdout

    def _try(self, *args: str) -> Optional[str]:
        try:
            return self._run(*args)
        except VcsError:
            return None

    def _detect(self) -> bool:
        if not os.path.isdir(self.root):
            return False
        return (self._try("rev-parse", "--git-dir") or "").strip() != ""

    @staticmethod
    def _parse_commit(line: str) -> Optional[Commit]:
        parts = line.rstrip("\n").split(_SEP)
        if len(parts) != 8:
            return None
        return Commit(
            sha=parts[0],
            short_sha=parts[1],
            author_name=parts[2],
            author_email=parts[3],
            authored_at=parts[4],
            committer_name=parts[5],
            committed_at=parts[6],
            subject=parts[7],
        )

    # ----------------------------------------------------------------- reads

    def head(self, ref: str = "HEAD") -> Optional[Commit]:
        out = self._try("log", "-1", "--format=" + _FORMAT, ref)
        return self._parse_commit(out.splitlines()[0]) if out and out.strip() else None

    def resolve(self, ref: str) -> Optional[str]:
        out = self._try("rev-parse", "--verify", "--quiet", ref)
        return out.strip() if out and out.strip() else None

    def current_branch(self) -> Optional[str]:
        out = self._try("rev-parse", "--abbrev-ref", "HEAD")
        name = (out or "").strip()
        return name if name and name != "HEAD" else None

    def changed_files(self, base_ref: str, head_ref: str = "HEAD") -> List[str]:
        # Three-dot: what the head branch changed since it diverged from base,
        # which is what a PR shows - not everything that moved on base since.
        out = self._try("diff", "--name-only", "%s...%s" % (base_ref, head_ref))
        if out is None:
            out = self._try("diff", "--name-only", "%s..%s" % (base_ref, head_ref)) or ""
        return [line for line in out.splitlines() if line.strip()]

    def commits_between(self, base_ref: str, head_ref: str = "HEAD") -> List[Commit]:
        out = self._try("log", "--format=" + _FORMAT, "%s..%s" % (base_ref, head_ref)) or ""
        commits = [self._parse_commit(line) for line in out.splitlines() if line.strip()]
        return [commit for commit in commits if commit]

    def changeset(self, base_ref: str, head_ref: str = "HEAD") -> Optional[ChangeSet]:
        base_sha, head_sha = self.resolve(base_ref), self.resolve(head_ref)
        if not base_sha or not head_sha:
            return None
        return ChangeSet(
            base_ref=base_ref,
            head_ref=head_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            files=self.changed_files(base_ref, head_ref),
            commits=self.commits_between(base_ref, head_ref),
        )

    def introducing_commit(self, path: str, head_ref: str = "HEAD") -> Optional[Commit]:
        out = self._try(
            "log", "--diff-filter=A", "-1", "--format=" + _FORMAT, head_ref, "--", path
        )
        if not out or not out.strip():
            return None
        return self._parse_commit(out.splitlines()[0])

    def list_files(self, ref: str) -> List[str]:
        out = self._try("ls-tree", "-r", "--name-only", ref) or ""
        return [line for line in out.splitlines() if line.strip()]

    def show(self, ref: str, path: str) -> Optional[str]:
        return self._try("show", "%s:%s" % (ref, path))

    def diff(self, base_ref: str, head_ref: str = "HEAD", path: Optional[str] = None) -> str:
        args = ["diff", "%s...%s" % (base_ref, head_ref)]
        if path:
            args += ["--", path]
        return self._try(*args) or ""

    def to_dict(self) -> Dict[str, Any]:
        data = {"kind": self.kind, "available": self.available, "root": self.root}
        if self.available:
            head = self.head()
            data["branch"] = self.current_branch()
            data["head"] = head.to_dict() if head else None
        return data


def open_repository(root: str) -> VersionControl:
    """Return a git-backed VCS if `root` is a checkout, else the null one."""
    git = GitRepository(root)
    return git if git.available else NoVersionControl()
