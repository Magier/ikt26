"""Kubernetes credentials + API surface.

Interface boundary #5. Two implementations:

    rest.RestKubeClient    in-cluster, ServiceAccount token, stdlib urllib
    local.LocalKubeClient  a file-backed simulator for local development

The agent code path is identical for both, so the attack can be developed and
tested without a cluster and then run unchanged inside one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


class KubeError(Exception):
    """Any API failure. Carries the HTTP status when there is one."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Resource:
    """Mapping from a friendly kind to an API path."""

    kind: str
    plural: str
    group: str = ""
    version: str = "v1"
    namespaced: bool = True

    @property
    def api_prefix(self) -> str:
        if not self.group:
            return "/api/%s" % self.version
        return "/apis/%s/%s" % (self.group, self.version)


RESOURCES: Dict[str, Resource] = {
    "pod": Resource("Pod", "pods"),
    "service": Resource("Service", "services"),
    "configmap": Resource("ConfigMap", "configmaps"),
    "serviceaccount": Resource("ServiceAccount", "serviceaccounts"),
    "event": Resource("Event", "events"),
    "deployment": Resource("Deployment", "deployments", group="apps"),
    "replicaset": Resource("ReplicaSet", "replicasets", group="apps"),
}


def resolve_kind(kind: str) -> Resource:
    key = kind.lower().rstrip("s") if kind.lower() not in RESOURCES else kind.lower()
    if key not in RESOURCES:
        # accept plural forms too ("deployments")
        for resource in RESOURCES.values():
            if kind.lower() in (resource.plural, resource.kind.lower()):
                return resource
        raise KubeError("unsupported kind %r for this agent" % kind)
    return RESOURCES[key]


class KubeClient:
    """Minimal Kubernetes client contract used by the agent's tools."""

    backend = "abstract"

    def identity(self) -> Dict[str, Any]:  # pragma: no cover
        """Who the client authenticates as (for evidence)."""
        raise NotImplementedError

    def get(
        self, kind: str, name: Optional[str] = None, namespace: Optional[str] = None
    ) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def create(
        self, manifest: Dict[str, Any], namespace: Optional[str] = None
    ) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def patch(
        self,
        kind: str,
        name: str,
        patch: Dict[str, Any],
        namespace: Optional[str] = None,
        patch_type: str = "strategic",
    ) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError
