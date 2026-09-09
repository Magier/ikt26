"""Repository-facing tools."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import Tool, ToolError, ToolRegistry

MAX_READ_BYTES = 64 * 1024


def register_fs_tools(registry: ToolRegistry, repository, skill_registry) -> None:
    def read_file(path: str, max_bytes: Optional[int] = None) -> Dict[str, Any]:
        try:
            content = repository.read(path)
        except (OSError, PermissionError) as exc:
            raise ToolError("read_file(%s): %s" % (path, exc))
        limit = int(max_bytes or MAX_READ_BYTES)
        return {
            "path": path,
            "bytes": len(content.encode("utf-8")),
            "sha256": repository.fingerprint(path),
            "content": content[:limit],
            "truncated": len(content) > limit,
        }

    def load_skill(name: str) -> Dict[str, Any]:
        try:
            skill = skill_registry.load(name)
        except KeyError as exc:
            raise ToolError(str(exc))
        return {
            "name": skill.name,
            "path": skill.metadata.path,
            "sha256": skill.metadata.sha256,
            "bytes": len(skill.body.encode("utf-8")),
            "directives": [
                {"tool": directive.tool, "parameters": directive.parameters}
                for directive in skill.directives()
            ],
            "body": skill.body,
        }

    registry.register(
        Tool(
            name="read_file",
            description="Read a file from the repository under review.",
            handler=read_file,
            parameters=["path", "max_bytes"],
            risk="Reads untrusted PR content into the model context.",
        )
    )
    registry.register(
        Tool(
            name="load_skill",
            description="Load the full instruction body of a discovered skill.",
            handler=load_skill,
            parameters=["name"],
            risk=(
                "Promotes attacker-authored repository text to agent "
                "instructions. This is the trust boundary crossing."
            ),
        )
    )
