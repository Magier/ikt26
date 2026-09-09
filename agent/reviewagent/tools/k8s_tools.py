"""Kubernetes-facing tools.

Each tool is a thin wrapper over `KubeClient`. Two agent-side behaviours are
deliberately implemented here rather than in the skill:

* mutating calls stamp provenance annotations
  (`ai-review.workshop/...`) so the change is attributable in
  `kubectl describe` / `kubectl rollout history` even without audit logs;
* nothing is filtered or validated, because an agent that "reviews" manifests
  has no reason to expect its own writes to be hostile. That missing
  validation is one of the workshop's findings, not an oversight.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from ..kube.base import KubeError
from .registry import Tool, ToolError, ToolRegistry

ANNOTATION_PREFIX = "ai-review.workshop"


def _summarise(obj: Dict[str, Any]) -> Dict[str, Any]:
    metadata = obj.get("metadata") or {}
    summary: Dict[str, Any] = {
        "kind": obj.get("kind"),
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "resourceVersion": metadata.get("resourceVersion"),
        "generation": metadata.get("generation"),
        "uid": metadata.get("uid"),
    }
    if obj.get("kind", "").endswith("List"):
        summary["items"] = [
            (item.get("metadata") or {}).get("name") for item in obj.get("items", [])
        ]
        summary["count"] = len(obj.get("items", []))
    spec = obj.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec")) or spec
    containers = template_spec.get("containers")
    if containers:
        summary["containers"] = [
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "command": container.get("command"),
            }
            for container in containers
        ]
    status = obj.get("status") or {}
    if status.get("phase"):
        summary["phase"] = status["phase"]
    return summary


def register_k8s_tools(registry: ToolRegistry, client, run_id: str = "unknown") -> None:
    def kubernetes_get(
        kind: str, name: Optional[str] = None, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        try:
            obj = client.get(kind, name=name, namespace=namespace)
        except KubeError as exc:
            raise ToolError(str(exc))
        return {"summary": _summarise(obj), "object": obj}

    def kubernetes_create(
        manifest: Dict[str, Any], namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ToolError("kubernetes_create expects a manifest object")
        manifest = _stamp(copy.deepcopy(manifest), client, run_id)
        try:
            created = client.create(manifest, namespace=namespace)
        except KubeError as exc:
            raise ToolError(str(exc))
        return {"summary": _summarise(created), "object": created}

    def kubernetes_patch(
        kind: str,
        name: str,
        patch: Dict[str, Any],
        namespace: Optional[str] = None,
        patch_type: str = "strategic",
    ) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            raise ToolError("kubernetes_patch expects a patch object")
        patch = _stamp(copy.deepcopy(patch), client, run_id)
        try:
            before = client.get(kind, name=name, namespace=namespace)
        except KubeError:
            before = {}
        try:
            after = client.patch(
                kind, name, patch, namespace=namespace, patch_type=patch_type
            )
        except KubeError as exc:
            raise ToolError(str(exc))
        return {
            "summary": _summarise(after),
            "before": _summarise(before),
            "diff": _container_diff(before, after),
            "object": after,
        }

    registry.register(
        Tool(
            name="kubernetes_get",
            description="Read a Kubernetes object or list objects of a kind.",
            handler=kubernetes_get,
            parameters=["kind", "name", "namespace"],
            risk="Read-only; discloses workload configuration to the model.",
        )
    )
    registry.register(
        Tool(
            name="kubernetes_create",
            description="Create a Kubernetes object from a manifest.",
            handler=kubernetes_create,
            parameters=["manifest", "namespace"],
            mutating=True,
            risk="Can schedule attacker-chosen images and commands.",
        )
    )
    registry.register(
        Tool(
            name="kubernetes_patch",
            description="Patch an existing Kubernetes object.",
            handler=kubernetes_patch,
            parameters=["kind", "name", "patch", "namespace", "patch_type"],
            mutating=True,
            risk=(
                "Patching a workload's pod template is arbitrary code "
                "execution in that workload. This is the review -> execution "
                "boundary crossing."
            ),
        )
    )


def _stamp(manifest: Dict[str, Any], client, run_id: str = "unknown") -> Dict[str, Any]:
    """Annotate agent-authored mutations so they stay attributable."""
    identity = client.identity()
    annotations = {
        "%s/modified-by" % ANNOTATION_PREFIX: "ai-pr-review-agent",
        "%s/service-account" % ANNOTATION_PREFIX: str(
            identity.get("service_account", "unknown")
        ),
        "%s/run-id" % ANNOTATION_PREFIX: run_id,
        # The standard annotation, so `kubectl rollout history` names the agent
        # and the run id an investigator can grep for in the audit log.
        "kubernetes.io/change-cause": (
            "ai-pr-review-agent automated review (run=%s, sa=%s)"
            % (run_id, identity.get("service_account", "unknown"))
        ),
    }
    metadata = manifest.setdefault("metadata", {})
    metadata.setdefault("annotations", {}).update(annotations)
    # Deployments: also stamp the pod template so the resulting pods carry it.
    template = ((manifest.get("spec") or {}).get("template"))
    if isinstance(template, dict):
        template.setdefault("metadata", {}).setdefault("annotations", {}).update(
            annotations
        )
    return manifest


def _container_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    def containers(obj: Dict[str, Any]) -> Dict[str, Any]:
        spec = obj.get("spec") or {}
        template_spec = ((spec.get("template") or {}).get("spec")) or spec
        return {
            container.get("name"): container
            for container in (template_spec.get("containers") or [])
        }

    old, new = containers(before), containers(after)
    return {
        "containers_added": sorted(set(new) - set(old)),
        "containers_removed": sorted(set(old) - set(new)),
        "containers_changed": sorted(
            name for name in set(old) & set(new) if old[name] != new[name]
        ),
    }
