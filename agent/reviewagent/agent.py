"""The review agent loop.

    initialise -> discover skills -> present metadata to the LLM
              -> receive a structured action -> execute it -> record evidence
              -> repeat until the LLM finishes or the step budget runs out

The loop owns *execution*; the LLM owns *decisions*; the tools own *effects*.
Keeping those apart is what makes the LLM replaceable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .evidence import (
    STAGE_DECISION,
    STAGE_DISCOVERY,
    STAGE_INIT,
    STAGE_REVIEW,
    STAGE_SKILL_LOAD,
    STAGE_TOOL,
    EventLog,
)
from .llm.base import CALL_TOOL, FINISH, INVOKE_SKILL, AgentAction, LLMContext
from .pr import PullRequest, Repository
from .skills import SkillRegistry
from .tools.registry import ToolError, ToolRegistry

DEFAULT_MAX_STEPS = 12


@dataclass
class ReviewResult:
    run_id: str
    pull_request: Dict[str, Any]
    skills_discovered: List[Dict[str, Any]]
    skills_loaded: List[str]
    steps: List[Dict[str, Any]]
    comment: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    stopped_because: str = "finished"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pull_request": self.pull_request,
            "skills_discovered": self.skills_discovered,
            "skills_loaded": self.skills_loaded,
            "steps": self.steps,
            "review_comment": self.comment,
            "stopped_because": self.stopped_because,
            "event_count": len(self.events),
        }


class ReviewAgent:
    def __init__(
        self,
        repository: Repository,
        llm,
        event_log: EventLog,
        kube_client=None,
        tools: Optional[ToolRegistry] = None,
        skill_registry: Optional[SkillRegistry] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        agent_name: str = "ai-pr-review-agent",
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.log = event_log
        self.log.llm = getattr(llm, "name", "unknown")
        self.log.agent = agent_name
        self.kube = kube_client
        self.skills = skill_registry or SkillRegistry(repository)
        self.tools = tools or self._default_tools()
        self.max_steps = max_steps
        # Mirror significant events into the Kubernetes Events stream so the
        # cluster carries its own copy of the narrative. Wired here (not in the
        # CLI) so every embedding of the agent produces the same evidence.
        if self.kube is not None and self.log.k8s_event_sink is None:
            from .evidence import make_k8s_event_sink

            self.log.k8s_event_sink = make_k8s_event_sink(
                self.kube,
                getattr(self.kube, "namespace", "default"),
                self.log.run_id,
            )

    def _default_tools(self) -> ToolRegistry:
        from .tools.registry import build_default_registry

        return build_default_registry(
            self.repository, self.skills, self.kube, self.log
        )

    # ------------------------------------------------------------------ run

    def review(self, pr_file: str = "PR.md", task: Optional[str] = None) -> ReviewResult:
        identity = self.kube.identity() if self.kube else None
        vcs = getattr(self.repository, "vcs", None)
        self.log.record(
            "agent.start",
            STAGE_INIT,
            result={
                "repository": self.repository.root,
                "vcs": vcs.to_dict() if vcs else {"kind": "none"},
                "skills_ref": self.skills.ref or "working-tree",
                "tools": self.tools.names(),
                "max_steps": self.max_steps,
            },
            identity=identity,
            source="operator-task",
        )

        pull_request = self.repository.load_pull_request(pr_file)
        self.log.record(
            "pr.loaded",
            STAGE_INIT,
            result=pull_request.to_dict(),
            source="operator-task",
        )

        discovered = self.skills.discover()
        self.log.record(
            "skills.discovered",
            STAGE_DISCOVERY,
            result={
                "count": len(discovered),
                "ref": self.skills.ref or "working-tree",
                "skills": [meta.to_dict() for meta in discovered],
                # `files_changed` is git-derived when the checkout is a git
                # repository, so this attribution no longer depends on the PR
                # description telling the truth.
                "introduced_by_this_pr": [
                    meta.name
                    for meta in discovered
                    if meta.path in pull_request.files_changed
                ],
            },
            source="agent",
        )

        effective_task = task or pull_request.review_request
        steps: List[Dict[str, Any]] = []
        comment = ""
        stopped = "finished"

        for step_number in range(1, self.max_steps + 1):
            context = LLMContext(
                task=effective_task,
                pull_request=pull_request.to_dict(),
                skill_metadata=[meta.to_dict() for meta in self.skills.metadata()],
                loaded_skills={
                    name: skill.body for name, skill in self.skills.loaded().items()
                },
                history=steps,
            )
            action = self.llm.decide(context)
            self.log.record(
                "llm.decision",
                STAGE_DECISION,
                result=action.to_dict(),
                source=action.source,
                step=step_number,
            )

            if action.action == FINISH:
                comment = action.comment
                break

            record = self._execute(action, step_number, identity)
            steps.append(record)
        else:
            stopped = "step budget exhausted"

        self.log.record(
            "review.completed",
            STAGE_REVIEW,
            result={
                "steps": len(steps),
                "skills_loaded": sorted(self.skills.loaded()),
                "stopped_because": stopped,
                "comment": comment,
            },
            source="agent",
        )

        return ReviewResult(
            run_id=self.log.run_id,
            pull_request=pull_request.to_dict(),
            skills_discovered=[meta.to_dict() for meta in discovered],
            skills_loaded=sorted(self.skills.loaded()),
            steps=steps,
            comment=comment,
            events=self.log.timeline(),
            stopped_because=stopped,
        )

    # -------------------------------------------------------------- execute

    def _execute(
        self, action: AgentAction, step_number: int, identity: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Translate a decision into a tool call and record what happened."""
        if action.action == INVOKE_SKILL:
            tool_name, parameters = "load_skill", {"name": action.skill}
            stage = STAGE_SKILL_LOAD
        elif action.action == CALL_TOOL:
            tool_name, parameters = action.tool or "", dict(action.parameters)
            stage = STAGE_TOOL
        else:
            self.log.record(
                "action.unsupported",
                STAGE_DECISION,
                result={"action": action.action},
                source=action.source,
                outcome="error",
            )
            return {
                "step": step_number,
                "action": action.action,
                "source": action.source,
                "outcome": "error",
                "result": "unsupported action",
            }

        tool_identity = identity if tool_name.startswith("kubernetes_") else None
        try:
            result = self.tools.get(tool_name)(**parameters)
            outcome = "ok"
        except ToolError as exc:
            result = {"error": str(exc)}
            outcome = "error"

        # Keep the event payload readable: full bodies live in the tool result,
        # summaries live in the event.
        loggable = result
        if isinstance(result, dict):
            loggable = {
                key: value
                for key, value in result.items()
                if key not in {"object", "body", "content"}
            }
            if tool_name == "load_skill":
                loggable["instructions_note"] = (
                    "untrusted repository text entered the model context from %s"
                    % result.get("path")
                )
                loggable["instruction_excerpt"] = str(result.get("body", ""))[:1200]

        self.log.record(
            "tool.invoked",
            stage,
            tool=tool_name,
            parameters=parameters,
            result=loggable,
            source=action.source,
            identity=tool_identity,
            outcome=outcome,
            rationale=action.rationale,
            step=step_number,
        )

        return {
            "step": step_number,
            "action": action.action,
            "tool": tool_name,
            "parameters": parameters,
            "source": action.source,
            "rationale": action.rationale,
            "outcome": outcome,
            "result": loggable,
        }
