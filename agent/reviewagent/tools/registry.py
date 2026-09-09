"""Tool execution.

Interface boundary #4. Tools are explicit, named, individually registered and
individually logged. The agent can only do what a registered tool exposes, so
the registry doubles as the agent's capability inventory - which is what the
workshop asks participants to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    """A tool refused or failed. Recorded as an evidence event either way."""


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    parameters: List[str] = field(default_factory=list)
    mutating: bool = False
    # Free-text note used by the README/instructor material to explain why the
    # capability exists and what it costs.
    risk: str = ""

    def __call__(self, **kwargs: Any) -> Any:
        unknown = set(kwargs) - set(self.parameters)
        if unknown:
            raise ToolError(
                "tool %s got unexpected parameter(s): %s"
                % (self.name, ", ".join(sorted(unknown)))
            )
        return self.handler(**kwargs)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError("no such tool: %s" % name)
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return sorted(self._tools)

    def inventory(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "mutating": tool.mutating,
                "risk": tool.risk,
            }
            for tool in (self._tools[name] for name in self.names())
        ]


def build_default_registry(repository, skill_registry, kube_client, event_log) -> ToolRegistry:
    """Wire the concrete tool set for phase 1."""
    from .fs_tools import register_fs_tools
    from .k8s_tools import register_k8s_tools

    registry = ToolRegistry()
    register_fs_tools(registry, repository, skill_registry)
    if kube_client is not None:
        register_k8s_tools(
            registry, kube_client, getattr(event_log, "run_id", "unknown")
        )
    return registry
