"""Skill discovery and representation.

Interface boundary #2. Two deliberately separate steps, mirroring how real
Agent Skills work:

    discover()      cheap: walk `.agents/skills/*/SKILL.md`, read *metadata only*
    load(name)      expensive: read the full instruction body

The agent has no hardcoded list of trusted skills. Whatever the pull request
ships is what the model gets to choose from - that is the whole point of the
workshop.

Discovery reads a *revision*: the working tree by default (which, for a PR
checkout, is the head ref), or any git ref via `ref=`. Pointing discovery at
the base branch instead of the PR is the cheapest real mitigation this lab can
demonstrate, so it is a first-class parameter rather than a patch.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import frontmatter
from .pr import Repository

# Codex, Claude Code, Gemini CLI and the rest of the Agent Skills ecosystem
# all discover skills here, scanning from the working directory up to the
# repository root. A PR that adds a directory under it is auto-discovered.
SKILLS_DIR = os.path.join(".agents", "skills")
SKILL_FILE = "SKILL.md"

# A skill body may contain fenced reference tool calls:
#
#     ```agent-action
#     {"tool": "kubernetes_get", "parameters": {...}}
#     ```
#
# A real LLM would read the prose and emit equivalent tool calls; the fenced
# block is how the FakeLLM "understands" the same instruction deterministically.
DIRECTIVE_BLOCK = re.compile(
    r"```agent-action\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)


@dataclass
class SkillMetadata:
    """What the model sees *before* choosing a skill."""

    name: str
    description: str
    path: str
    source: str = "repository"
    sha256: Optional[str] = None
    size_bytes: int = 0
    ref: Optional[str] = None
    # The commit that added this skill, when the repository is under git.
    introduced_by: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "source": self.source,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "ref": self.ref or "working-tree",
        }
        if self.introduced_by:
            data["introduced_by"] = self.introduced_by
        return data


@dataclass
class Directive:
    """One tool call requested by a skill's instructions."""

    tool: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    index: int = 0


@dataclass
class Skill:
    """A fully loaded skill: metadata + instruction body."""

    metadata: SkillMetadata
    body: str

    @property
    def name(self) -> str:
        return self.metadata.name

    def directives(self) -> List[Directive]:
        directives: List[Directive] = []
        for position, match in enumerate(DIRECTIVE_BLOCK.finditer(self.body)):
            raw = match.group(1).strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "tool" not in payload:
                continue
            directives.append(
                Directive(
                    tool=str(payload["tool"]),
                    parameters=dict(payload.get("parameters") or {}),
                    rationale=str(payload.get("rationale", "")),
                    index=position,
                )
            )
        return directives


class SkillRegistry:
    """Discovers skills in a repository and loads them on demand.

    Discovery is metadata-only. `load()` is the moment untrusted repository
    content enters the model's context, so it is a separate, logged step.
    """

    def __init__(
        self,
        repository: Repository,
        skills_dir: str = SKILLS_DIR,
        ref: Optional[str] = None,
    ) -> None:
        self.repository = repository
        self.skills_dir = skills_dir
        # None means the working tree; a ref means "read skills from there".
        self.ref = ref
        self._metadata: Dict[str, SkillMetadata] = {}
        self._loaded: Dict[str, Skill] = {}

    def _skill_files(self) -> List[str]:
        """Repo-relative SKILL.md paths, from the configured revision."""
        prefix = self.skills_dir.rstrip("/") + "/"
        candidates = []
        for path in self.repository.list_files(self.ref):
            normalised = path.replace(os.sep, "/")
            if not normalised.startswith(prefix) or not normalised.endswith(
                "/" + SKILL_FILE
            ):
                continue
            # .agents/skills/<name>/SKILL.md - one directory deep, no nesting.
            if normalised.count("/") != prefix.count("/") + 1:
                continue
            candidates.append(normalised)
        return sorted(candidates)

    def discover(self) -> List[SkillMetadata]:
        """Find `.agents/skills/*/SKILL.md` and parse their frontmatter."""
        self._metadata = {}
        self._loaded = {}
        vcs = getattr(self.repository, "vcs", None)

        for relative in self._skill_files():
            try:
                content = self.repository.read(relative, ref=self.ref)
            except (OSError, PermissionError):
                continue
            meta, _ = frontmatter.parse(content)
            directory = relative.split("/")[-2]
            name = str(meta.get("name") or directory)
            introduced = None
            if vcs is not None and vcs.available:
                commit = vcs.introducing_commit(relative, self.ref or "HEAD")
                introduced = commit.to_dict() if commit else None
            self._metadata[name] = SkillMetadata(
                name=name,
                description=str(meta.get("description", "")),
                path=relative,
                source="repository",
                sha256=self.repository.fingerprint(relative, ref=self.ref),
                size_bytes=len(content.encode("utf-8")),
                ref=self.ref,
                introduced_by=introduced,
            )
        return list(self._metadata.values())

    def metadata(self) -> List[SkillMetadata]:
        return list(self._metadata.values())

    def get_metadata(self, name: str) -> Optional[SkillMetadata]:
        return self._metadata.get(name)

    def load(self, name: str) -> Skill:
        """Read the full instruction body for a discovered skill."""
        if name in self._loaded:
            return self._loaded[name]
        meta = self._metadata.get(name)
        if meta is None:
            raise KeyError("skill %r was not discovered in this repository" % name)
        _, body = frontmatter.parse(self.repository.read(meta.path, ref=self.ref))
        skill = Skill(metadata=meta, body=body)
        self._loaded[name] = skill
        return skill

    def loaded(self) -> Dict[str, Skill]:
        return dict(self._loaded)
