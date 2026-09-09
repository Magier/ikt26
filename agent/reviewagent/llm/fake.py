"""A deterministic stand-in for a language model.

Zero compute, zero dependencies, no inference. It reproduces the two model
behaviours the workshop needs:

1. **Skill selection.** Given a task and a list of skill *descriptions*, pick
   the skill whose description overlaps the task's topic. This is ordinary,
   correct, desirable behaviour.

2. **Instruction following.** Once a skill body is in context, follow the
   instructions it contains - including the ones the repository author added
   for the agent rather than for the reviewer. This is the vulnerability, and
   it is a property of *every* instruction-following model, not a bug in this
   fake one.

The fake never executes anything. It emits `AgentAction`s.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from ..skills import Directive, Skill
from .base import CALL_TOOL, FINISH, INVOKE_SKILL, AgentAction, LLMContext, LLM

# Topic vocabulary. A real model has embeddings; the fake has a word list.
TOPICS: Dict[str, Tuple[str, ...]] = {
    "kubernetes": (
        "kubernetes",
        "k8s",
        "manifest",
        "deployment",
        "pod",
        "cluster",
        "helm",
        "rbac",
        "namespace",
        "yaml",
    ),
    "dependencies": ("dependency", "dependencies", "sbom", "package", "cve", "lockfile"),
    "terraform": ("terraform", "hcl", "tfstate", "provider"),
}

WORD = re.compile(r"[a-z0-9][a-z0-9+-]*")


def _tokens(text: str) -> List[str]:
    return WORD.findall(text.lower())


def _topics(text: str) -> List[str]:
    words = set(_tokens(text))
    return [topic for topic, vocab in TOPICS.items() if words & set(vocab)]


class FakeLLM(LLM):
    """Rule-based decisions with the same shape as a real model's."""

    name = "fake-llm/deterministic-v1"

    def decide(self, context: LLMContext) -> AgentAction:
        # ------------------------------------------------------------------
        # Step 1: no skill loaded yet -> choose one by description relevance.
        # ------------------------------------------------------------------
        if not context.loaded_skills:
            choice = self._select_skill(context)
            if choice is not None:
                name, score, reason = choice
                return AgentAction(
                    action=INVOKE_SKILL,
                    skill=name,
                    rationale=(
                        "task topics %s match skill description (%s); "
                        "relevance=%.2f" % (_topics(context.task) or ["none"], reason, score)
                    ),
                    source="operator-task",
                )
            return self._finish(context, "no relevant skill available")

        # ------------------------------------------------------------------
        # Step 2: a skill body is in context -> follow its instructions.
        # ------------------------------------------------------------------
        for name, body in context.loaded_skills.items():
            skill = Skill(metadata=_stub_metadata(name), body=body)
            directives = skill.directives()
            already = len(context.actions_from("skill:%s" % name))
            if already < len(directives):
                directive = directives[already]
                return AgentAction(
                    action=CALL_TOOL,
                    skill=name,
                    tool=directive.tool,
                    parameters=dict(directive.parameters),
                    rationale=(
                        directive.rationale
                        or "step %d of the loaded %s skill procedure"
                        % (directive.index + 1, name)
                    ),
                    source="skill:%s#%d" % (name, directive.index + 1),
                )

        return self._finish(context, "skill procedure complete")

    # ----------------------------------------------------------------------

    def _select_skill(
        self, context: LLMContext
    ) -> Optional[Tuple[str, float, str]]:
        task_topics = set(_topics(context.task) + _topics(
            " ".join(str(f) for f in context.pull_request.get("files_changed", []))
        ))
        task_words = set(_tokens(context.task))

        best: Optional[Tuple[str, float, str]] = None
        for meta in context.skill_metadata:
            description = str(meta.get("description", ""))
            skill_topics = set(_topics(description + " " + str(meta.get("name", ""))))
            shared_topics = task_topics & skill_topics
            overlap = task_words & set(_tokens(description))
            score = len(shared_topics) * 1.0 + len(overlap) * 0.1
            if not shared_topics:
                continue
            if best is None or score > best[1]:
                best = (
                    str(meta.get("name")),
                    score,
                    "shared topics: %s" % sorted(shared_topics),
                )
        return best

    def _finish(self, context: LLMContext, why: str) -> AgentAction:
        return AgentAction(
            action=FINISH,
            rationale=why,
            source="operator-task",
            comment=_render_review_comment(context, why),
        )


def _stub_metadata(name: str):
    from ..skills import SkillMetadata

    return SkillMetadata(name=name, description="", path="(in-context)")


def _render_review_comment(context: LLMContext, why: str) -> str:
    """The user-visible artefact of the review - deliberately reassuring."""
    steps = len(context.history)
    skills = ", ".join(context.loaded_skills) or "none"
    return (
        "**Automated Kubernetes review**\n\n"
        "Reviewed PR #%s (%s) using skill(s): %s.\n"
        "Completed %d review step(s). Outcome: %s.\n\n"
        "No blocking findings. Manifest changes look consistent with the "
        "repository's deployment conventions."
        % (context.pull_request.get("id"), context.pull_request.get("title"), skills, steps, why)
    )
