"""LLM interface.

Interface boundary #3. The decision maker never touches the filesystem or the
Kubernetes API; it consumes a context and returns a *structured action*. The
agent decides whether and how to execute it.

Replacing `FakeLLM` with a real model means implementing `decide()` and
serialising `LLMContext` into a prompt. Nothing else has to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Action kinds the agent knows how to execute.
INVOKE_SKILL = "invoke_skill"
CALL_TOOL = "call_tool"
FINISH = "finish"


@dataclass
class AgentAction:
    """A single structured decision.

    `source` is the provenance of the decision and is the most important
    forensic field in the whole prototype: it records *what made the agent do
    this*, e.g. `operator-task` vs `skill:k8s-review#2`.
    """

    action: str
    skill: Optional[str] = None
    tool: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    source: str = "operator-task"
    comment: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "skill": self.skill,
            "tool": self.tool,
            "parameters": self.parameters,
            "rationale": self.rationale,
            "source": self.source,
        }


@dataclass
class LLMContext:
    """Everything the model is allowed to reason over."""

    task: str
    pull_request: Dict[str, Any]
    skill_metadata: List[Dict[str, Any]] = field(default_factory=list)
    loaded_skills: Dict[str, str] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def actions_from(self, source_prefix: str) -> List[Dict[str, Any]]:
        return [
            step
            for step in self.history
            if str(step.get("source", "")).startswith(source_prefix)
        ]


class LLM:
    """Decision-maker contract."""

    name = "llm"

    def decide(self, context: LLMContext) -> AgentAction:  # pragma: no cover
        raise NotImplementedError
