"""Structured evidence.

Interface boundary #6. Every meaningful thing the agent does becomes one
append-only JSON line. Human-readable logs are a side effect, not the record.

Event schema (stable - participants and future tooling depend on it):

    {
      "timestamp":   RFC3339 UTC,
      "seq":         monotonic per run,
      "run_id":      review run identifier,
      "agent":       agent identity,
      "llm":         decision-maker identity,
      "stage":       coarse phase of the run,
      "action":      what happened,
      "tool":        tool name, when a tool ran,
      "parameters":  tool input,
      "result":      tool output summary,
      "source":      provenance: what caused this action,
      "identity":    Kubernetes credential used, when applicable,
      "outcome":     "ok" | "error"
    }
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

STAGE_INIT = "init"
STAGE_DISCOVERY = "skill-discovery"
STAGE_DECISION = "llm-decision"
STAGE_SKILL_LOAD = "skill-load"
STAGE_TOOL = "tool-execution"
STAGE_REVIEW = "review-output"


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truncate(value: Any, limit: int = 4000) -> Any:
    """Keep events bounded without silently losing the fact of truncation."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "...[truncated %d bytes]" % (len(value) - limit)
    if isinstance(value, dict):
        return {key: _truncate(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value[:50]]
    return value


@dataclass
class EventLog:
    """Append-only JSONL event sink."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent: str = "ai-pr-review-agent"
    llm: str = "unknown"
    path: Optional[str] = None
    echo: bool = True
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Optional hook so major steps can also become Kubernetes Events.
    k8s_event_sink: Optional[Callable[[Dict[str, Any]], None]] = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        if self.path:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)

    def record(
        self,
        action: str,
        stage: str,
        tool: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        result: Any = None,
        source: str = "agent",
        identity: Optional[Dict[str, Any]] = None,
        outcome: str = "ok",
        **extra: Any,
    ) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event: Dict[str, Any] = {
                "timestamp": utcnow(),
                "seq": self._seq,
                "run_id": self.run_id,
                "agent": self.agent,
                "llm": self.llm,
                "stage": stage,
                "action": action,
                "tool": tool,
                "parameters": _truncate(parameters or {}),
                "result": _truncate(result),
                "source": source,
                "identity": identity,
                "outcome": outcome,
            }
            for key, value in extra.items():
                event[key] = _truncate(value)
            self.events.append(event)
            line = json.dumps(event, sort_keys=False, default=str)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            if self.echo:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        if self.k8s_event_sink is not None:
            try:
                self.k8s_event_sink(event)
            except Exception as exc:  # evidence must never break the run
                sys.stderr.write("k8s event sink failed: %r\n" % (exc,))
        return event

    def timeline(self) -> List[Dict[str, Any]]:
        return list(self.events)


# Which agent events are worth mirroring into the Kubernetes Events stream, so
# `kubectl get events -n ikt-workshop` alone tells part of the story.
K8S_EVENT_ACTIONS = {
    "skills.discovered": "SkillsDiscovered",
    "review.completed": "ReviewCompleted",
}


def make_k8s_event_sink(kube, namespace: str, run_id: str):
    def sink(event: Dict[str, Any]) -> None:
        reason = K8S_EVENT_ACTIONS.get(event["action"])
        if reason is None and not (
            event["action"] == "tool.invoked"
            and str(event.get("tool", "")).startswith("kubernetes_")
        ):
            return
        reason = reason or "AgentToolInvoked"
        message = "run=%s action=%s tool=%s source=%s outcome=%s" % (
            event["run_id"],
            event["action"],
            event.get("tool"),
            event.get("source"),
            event.get("outcome"),
        )
        manifest = {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {
                "name": "ai-review-%s-%d" % (run_id, event["seq"]),
                "namespace": namespace,
            },
            "involvedObject": {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "name": os.environ.get("SERVICE_ACCOUNT_NAME", "review-agent-sa"),
                "namespace": namespace,
            },
            "reason": reason,
            "message": message[:1024],
            "type": "Warning" if event.get("outcome") == "error" else "Normal",
            "source": {"component": "ai-pr-review-agent"},
            "firstTimestamp": event["timestamp"],
            "lastTimestamp": event["timestamp"],
            "count": 1,
        }
        kube.create(manifest, namespace=namespace)

    return sink
