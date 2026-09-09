"""In-cluster Kubernetes client built on the standard library.

Uses the pod's projected ServiceAccount token and the cluster CA. No
`kubernetes` package, no `requests`. Every call the agent makes therefore also
appears in the Kubernetes API server audit log under the agent's
ServiceAccount identity - which is exactly the evidence the workshop wants.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .base import KubeClient, KubeError, resolve_kind

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

PATCH_CONTENT_TYPES = {
    "strategic": "application/strategic-merge-patch+json",
    "merge": "application/merge-patch+json",
    "json": "application/json-patch+json",
}


class RestKubeClient(KubeClient):
    backend = "in-cluster-rest"

    def __init__(
        self,
        api_server: Optional[str] = None,
        token: Optional[str] = None,
        ca_path: Optional[str] = None,
        namespace: Optional[str] = None,
        service_account: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api_server = api_server or "https://%s:%s" % (host, port)
        self.token = token or self._read(os.path.join(SA_DIR, "token"))
        self.namespace = (
            namespace
            or self._read(os.path.join(SA_DIR, "namespace"))
            or os.environ.get("POD_NAMESPACE")
            or "default"
        )
        self.service_account = service_account or os.environ.get(
            "SERVICE_ACCOUNT_NAME", "unknown"
        )
        self.timeout = timeout
        ca_path = ca_path or os.path.join(SA_DIR, "ca.crt")
        if os.path.exists(ca_path):
            self._ssl = ssl.create_default_context(cafile=ca_path)
        else:  # pragma: no cover - only when run outside a pod
            self._ssl = ssl.create_default_context()

    @staticmethod
    def _read(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    # ------------------------------------------------------------------

    def identity(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "api_server": self.api_server,
            "namespace": self.namespace,
            "service_account": self.service_account,
            "credential": "projected ServiceAccount token (%s/token)" % SA_DIR,
            "token_present": bool(self.token),
        }

    def _url(self, kind: str, name: Optional[str], namespace: Optional[str]) -> str:
        resource = resolve_kind(kind)
        namespace = namespace or self.namespace
        path = resource.api_prefix
        if resource.namespaced:
            path += "/namespaces/%s" % namespace
        path += "/%s" % resource.plural
        if name:
            path += "/%s" % name
        return self.api_server + path

    def _request(
        self,
        method: str,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        content_type: str = "application/json",
    ) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url=url, data=data, method=method)
        request.add_header("Authorization", "Bearer %s" % (self.token or ""))
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl
            ) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise KubeError(
                "%s %s -> HTTP %s" % (method, url, exc.code),
                status=exc.code,
                body=detail,
            )
        except urllib.error.URLError as exc:
            raise KubeError("%s %s -> %s" % (method, url, exc.reason))
        return json.loads(payload) if payload else {}

    # ------------------------------------------------------------------

    def get(
        self, kind: str, name: Optional[str] = None, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request("GET", self._url(kind, name, namespace))

    def create(
        self, manifest: Dict[str, Any], namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        kind = manifest.get("kind")
        if not kind:
            raise KubeError("manifest is missing 'kind'")
        namespace = (
            namespace
            or (manifest.get("metadata") or {}).get("namespace")
            or self.namespace
        )
        return self._request("POST", self._url(kind, None, namespace), body=manifest)

    def patch(
        self,
        kind: str,
        name: str,
        patch: Dict[str, Any],
        namespace: Optional[str] = None,
        patch_type: str = "strategic",
    ) -> Dict[str, Any]:
        content_type = PATCH_CONTENT_TYPES.get(patch_type)
        if content_type is None:
            raise KubeError("unsupported patch type %r" % patch_type)
        return self._request(
            "PATCH",
            self._url(kind, name, namespace),
            body=patch,
            content_type=content_type,
        )
