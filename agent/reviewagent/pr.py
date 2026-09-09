"""Repository / pull-request model.

Interface boundary #1. Today a pull request is a directory on disk plus a
`PR.md` with frontmatter. Later this can become a GitHub webhook payload or a
`git clone` of the head ref without touching anything downstream: the agent
only ever sees `PullRequest` and `Repository`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import frontmatter
from .vcs import Commit, VersionControl, open_repository


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class PullRequest:
    """The change set under review.

    Fields come from two places, and which is which matters forensically:

    * *declared* - what the PR description says (title, author, the file list)
    * *derived*  - what git says (head commit, real change set, real author)

    The agent uses the derived facts when git is available and records both,
    so a PR description that misrepresents its own change set is visible in
    the evidence rather than silently believed.
    """

    id: str
    title: str
    author: str
    author_association: str
    source_branch: str
    target_branch: str
    created_at: str
    files_changed: List[str] = field(default_factory=list)
    # Where this pull request actually lives. The lab reviews a real one, so
    # the evidence can name it instead of describing it.
    repository: str = ""
    url: str = ""
    review_request: str = "Please review this pull request."
    body: str = ""
    # Populated when the checkout is a real git repository.
    head_commit: Optional[Commit] = None
    commits: List[Commit] = field(default_factory=list)
    files_declared: List[str] = field(default_factory=list)
    files_derived: Optional[List[str]] = None
    vcs_kind: str = "none"

    @property
    def untrusted(self) -> bool:
        """Whether the author is outside the org.

        Nothing in phase 1 acts on this. It exists so the workshop can ask
        "the agent knew the author was an external first-time contributor -
        why did that not change how its content was treated?"
        """
        return self.author_association in {
            "FIRST_TIME_CONTRIBUTOR",
            "NONE",
            "CONTRIBUTOR",
        }

    @property
    def undeclared_files(self) -> List[str]:
        """Files the branch changed that the PR description does not list."""
        if self.files_derived is None:
            return []
        return sorted(set(self.files_derived) - set(self.files_declared))

    @property
    def unchanged_declared_files(self) -> List[str]:
        """Files the PR description lists that the branch did not change."""
        if self.files_derived is None:
            return []
        return sorted(set(self.files_declared) - set(self.files_derived))

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "author_association": self.author_association,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "created_at": self.created_at,
            "repository": self.repository,
            "url": self.url,
            "files_changed": list(self.files_changed),
            "untrusted_author": self.untrusted,
            "vcs": self.vcs_kind,
        }
        if self.head_commit is not None:
            data["head_commit"] = self.head_commit.to_dict()
            data["commits"] = [commit.to_dict() for commit in self.commits]
            data["files_declared"] = list(self.files_declared)
            data["files_derived"] = list(self.files_derived or [])
            data["undeclared_files"] = self.undeclared_files
            data["unchanged_declared_files"] = self.unchanged_declared_files
        return data


@dataclass
class Repository:
    """A checked-out repository the agent is allowed to read.

    Reads are confined to the checkout. When the checkout is a git repository,
    `ref` lets the agent read a *different* revision than the working tree -
    which is how "load skills from the base branch, not the PR" is expressed.
    """

    root: str
    vcs: Optional[VersionControl] = None

    def __post_init__(self) -> None:
        self.root = os.path.realpath(self.root)
        if self.vcs is None:
            self.vcs = open_repository(self.root)

    def resolve(self, relative_path: str) -> str:
        """Resolve a repo-relative path, refusing to escape the checkout."""
        candidate = os.path.realpath(os.path.join(self.root, relative_path))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise PermissionError(
                "path %r escapes the repository root" % relative_path
            )
        return candidate

    def read(self, relative_path: str, ref: Optional[str] = None) -> str:
        if ref:
            content = self.vcs.show(ref, relative_path)
            if content is None:
                raise FileNotFoundError(
                    "%s does not exist at %s" % (relative_path, ref)
                )
            return content
        with open(self.resolve(relative_path), "r", encoding="utf-8") as handle:
            return handle.read()

    def exists(self, relative_path: str, ref: Optional[str] = None) -> bool:
        if ref:
            return self.vcs.show(ref, relative_path) is not None
        try:
            return os.path.exists(self.resolve(relative_path))
        except PermissionError:
            return False

    def list_files(self, ref: Optional[str] = None) -> List[str]:
        """Repo-relative file paths, from a ref or from the working tree."""
        if ref:
            return self.vcs.list_files(ref)
        found: List[str] = []
        for directory, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if name != ".git"]
            for filename in filenames:
                found.append(
                    os.path.relpath(os.path.join(directory, filename), self.root)
                )
        return sorted(found)

    def load_pull_request(self, pr_file: str = "PR.md") -> PullRequest:
        """Build the PR from its description, corrected by git where possible."""
        meta, body = frontmatter.parse(self.read(pr_file))
        declared = list(meta.get("files_changed") or [])
        pull_request = PullRequest(
            id=str(meta.get("id", "0")),
            title=meta.get("title", "(untitled)"),
            author=meta.get("author", "unknown"),
            author_association=meta.get("author_association", "NONE"),
            source_branch=meta.get("source_branch", "unknown"),
            target_branch=meta.get("target_branch", "main"),
            created_at=meta.get("created_at", ""),
            repository=meta.get("repository", ""),
            url=meta.get("url", ""),
            files_changed=declared,
            files_declared=declared,
            review_request=meta.get(
                "review_request", "Please review this pull request."
            ),
            body=body,
            vcs_kind=self.vcs.kind if self.vcs else "none",
        )

        if not (self.vcs and self.vcs.available):
            return pull_request

        head = self.vcs.head()
        if head is not None:
            pull_request.head_commit = head
            # git is authoritative about who wrote this and when.
            pull_request.created_at = head.authored_at or pull_request.created_at
        branch = getattr(self.vcs, "current_branch", lambda: None)()
        if branch:
            pull_request.source_branch = branch

        changeset = self.vcs.changeset(
            pull_request.target_branch, pull_request.source_branch
        )
        if changeset is not None:
            # The PR description does not list itself as a changed file, and it
            # would be noise in every comparison, so exclude it.
            derived = [path for path in changeset.files if path != pr_file]
            pull_request.files_derived = derived
            pull_request.files_changed = derived
            pull_request.commits = changeset.commits
        return pull_request

    def fingerprint(self, relative_path: str, ref: Optional[str] = None) -> Optional[str]:
        """Content hash, from a ref or from the working tree."""
        if ref:
            content = self.vcs.show(ref, relative_path)
            if content is None:
                return None
            return hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            return sha256_file(self.resolve(relative_path))
        except (OSError, PermissionError):
            return None
