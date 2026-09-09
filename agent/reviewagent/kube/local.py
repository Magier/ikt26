"""A file-backed Kubernetes simulator for local development.

It exists so the whole scenario - including the code execution stage - can be
built and tested without touching a real cluster. It implements just enough
API behaviour to be honest about what the attack does:

* namespaced objects stored as JSON,
* strategic-merge-patch semantics for the fields we care about (lists of
  objects keyed by `name`, as `containers` and `env` are),
* a toy "kubelet": when a Deployment's pod template changes, a new ReplicaSet
  and Pod are created and each container's `command` is actually executed in a
  per-pod sandbox directory, with stdout captured as the pod log.

The toy kubelet is what makes the RCE observable offline. Container `command`s
run as ordinary local subprocesses inside `<state>/sandbox/<pod>/`, with
volume mountPaths rewritten to sandbox directories, a scrubbed environment and
a wall-clock timeout. It is a development aid, not a sandbox with security
properties: only ever point it at this workshop's own manifests.
"""

from __future__ import annotations

import copy
import json
import os
import random
import signal
import shutil
import string
import subprocess
import time
from typing import Any, Dict, List, Optional

from .base import KubeClient, KubeError, resolve_kind

RUN_TIMEOUT_SECONDS = float(os.environ.get("SIM_CONTAINER_TIMEOUT", "10"))


def _kill_group(process: "subprocess.Popen") -> None:
    """Kill the container's whole process group, not just its shell."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _suffix(length: int = 5) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def strategic_merge(target: Any, patch: Any, list_key: str = "name") -> Any:
    """Recursive merge with merge-by-key for lists of named objects."""
    if isinstance(target, dict) and isinstance(patch, dict):
        merged = dict(target)
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            elif key in merged:
                merged[key] = strategic_merge(merged[key], value, list_key)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if isinstance(target, list) and isinstance(patch, list):
        keyed = all(isinstance(item, dict) and list_key in item for item in target + patch)
        if not keyed:
            return copy.deepcopy(patch)
        merged_list = [copy.deepcopy(item) for item in target]
        index = {item[list_key]: position for position, item in enumerate(merged_list)}
        for item in patch:
            name = item[list_key]
            if name in index:
                merged_list[index[name]] = strategic_merge(
                    merged_list[index[name]], item, list_key
                )
            else:
                merged_list.append(copy.deepcopy(item))
        return merged_list
    return copy.deepcopy(patch)


class LocalKubeClient(KubeClient):
    backend = "local-simulator"

    def __init__(
        self,
        state_dir: str,
        namespace: str = "ikt-workshop",
        service_account: str = "review-agent-sa",
        seed_dir: Optional[str] = None,
        run_containers: bool = True,
    ) -> None:
        self.state_dir = os.path.abspath(state_dir)
        self.namespace = namespace
        self.service_account = service_account
        self.run_containers = run_containers
        self.objects_path = os.path.join(self.state_dir, "objects.json")
        self.logs_dir = os.path.join(self.state_dir, "podlogs")
        self.sandbox_dir = os.path.join(self.state_dir, "sandbox")
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.sandbox_dir, exist_ok=True)
        self.objects: Dict[str, Dict[str, Any]] = {}
        if os.path.exists(self.objects_path):
            with open(self.objects_path, "r", encoding="utf-8") as handle:
                self.objects = json.load(handle)
        elif seed_dir:
            self._seed(seed_dir)

    # ------------------------------------------------------------------ state

    def _seed(self, seed_dir: str) -> None:
        if not os.path.isdir(seed_dir):
            # An unseeded simulator is empty, not broken: `kubernetes_get` then
            # returns 404s, which is a legitimate thing for the agent to see.
            return
        for entry in sorted(os.listdir(seed_dir)):
            if not entry.endswith(".json"):
                continue
            with open(os.path.join(seed_dir, entry), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for manifest in payload if isinstance(payload, list) else [payload]:
                self._store(manifest, run=False)
        # Materialise the pods the seeded Deployments would already have.
        for manifest in list(self.objects.values()):
            if manifest.get("kind") == "Deployment":
                self._roll_out(manifest, reason="seed")
        self._flush()

    def _key(self, kind: str, name: str, namespace: str) -> str:
        return "%s/%s/%s" % (kind, namespace, name)

    def _store(self, manifest: Dict[str, Any], run: bool = True) -> Dict[str, Any]:
        manifest = copy.deepcopy(manifest)
        metadata = manifest.setdefault("metadata", {})
        namespace = metadata.setdefault("namespace", self.namespace)
        name = metadata.get("name") or "%s-%s" % (
            manifest.get("kind", "object").lower(),
            _suffix(),
        )
        metadata["name"] = name
        metadata.setdefault("uid", _suffix(8) + "-" + _suffix(4))
        metadata.setdefault("creationTimestamp", _now())
        metadata.setdefault("generation", 1)
        self.objects[self._key(manifest["kind"], name, namespace)] = manifest
        if run and manifest["kind"] == "Pod":
            self._run_pod(manifest)
        self._flush()
        return manifest

    def _flush(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.objects_path, "w", encoding="utf-8") as handle:
            json.dump(self.objects, handle, indent=2, sort_keys=True)

    def reset(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)

    # ----------------------------------------------------------------- client

    def identity(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "api_server": "file://%s" % self.objects_path,
            "namespace": self.namespace,
            "service_account": self.service_account,
            "credential": "simulated (no real credential)",
            "token_present": False,
        }

    def get(
        self, kind: str, name: Optional[str] = None, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        resource = resolve_kind(kind)
        namespace = namespace or self.namespace
        if name:
            key = self._key(resource.kind, name, namespace)
            if key not in self.objects:
                raise KubeError(
                    '%s "%s" not found' % (resource.plural, name), status=404
                )
            return copy.deepcopy(self.objects[key])
        items = [
            copy.deepcopy(manifest)
            for key, manifest in sorted(self.objects.items())
            if manifest.get("kind") == resource.kind
            and (manifest.get("metadata") or {}).get("namespace") == namespace
        ]
        return {"kind": "%sList" % resource.kind, "items": items}

    def create(
        self, manifest: Dict[str, Any], namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        if "kind" not in manifest:
            raise KubeError("manifest is missing 'kind'")
        manifest = copy.deepcopy(manifest)
        manifest.setdefault("metadata", {})
        if namespace:
            manifest["metadata"]["namespace"] = namespace
        return self._store(manifest, run=self.run_containers)

    def patch(
        self,
        kind: str,
        name: str,
        patch: Dict[str, Any],
        namespace: Optional[str] = None,
        patch_type: str = "strategic",
    ) -> Dict[str, Any]:
        resource = resolve_kind(kind)
        namespace = namespace or self.namespace
        key = self._key(resource.kind, name, namespace)
        if key not in self.objects:
            raise KubeError('%s "%s" not found' % (resource.plural, name), status=404)
        before = self.objects[key]
        after = strategic_merge(before, patch)
        after["metadata"]["generation"] = int(
            (before.get("metadata") or {}).get("generation", 1)
        ) + 1
        self.objects[key] = after
        template_changed = (before.get("spec") or {}).get("template") != (
            after.get("spec") or {}
        ).get("template")
        if resource.kind == "Deployment" and template_changed and self.run_containers:
            self._roll_out(after, reason="patch")
        self._flush()
        return copy.deepcopy(after)

    # ------------------------------------------------------------ toy kubelet

    def _roll_out(self, deployment: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """Create the ReplicaSet + Pod a Deployment change would produce."""
        metadata = deployment.get("metadata", {})
        namespace = metadata.get("namespace", self.namespace)
        generation = metadata.get("generation", 1)
        rs_name = "%s-%s" % (metadata["name"], _suffix(5))
        template = copy.deepcopy((deployment.get("spec") or {}).get("template") or {})
        replicaset = {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "metadata": {
                "name": rs_name,
                "namespace": namespace,
                "annotations": {
                    "deployment.kubernetes.io/revision": str(generation),
                    "workshop.local/created-by": reason,
                },
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": metadata["name"],
                        "uid": metadata.get("uid", ""),
                    }
                ],
            },
            "spec": {"replicas": 1, "template": template},
        }
        self._store(replicaset, run=False)

        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "%s-%s" % (rs_name, _suffix(5)),
                "namespace": namespace,
                "labels": (template.get("metadata") or {}).get("labels", {}),
                "annotations": (template.get("metadata") or {}).get("annotations", {}),
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "ReplicaSet",
                        "name": rs_name,
                        "uid": "",
                    }
                ],
            },
            "spec": copy.deepcopy(template.get("spec") or {}),
        }
        return self._store(pod, run=self.run_containers)

    def _mount_map(self, pod_name: str, pod_spec: Dict[str, Any]) -> Dict[str, str]:
        """volumeName -> sandbox directory, shared by all containers in the pod."""
        base = os.path.join(self.sandbox_dir, pod_name, "volumes")
        mapping = {}
        for volume in pod_spec.get("volumes") or []:
            directory = os.path.join(base, volume.get("name", "unnamed"))
            os.makedirs(directory, exist_ok=True)
            mapping[volume.get("name", "unnamed")] = directory
        return mapping

    def _run_pod(self, pod: Dict[str, Any]) -> None:
        spec = pod.get("spec") or {}
        name = pod["metadata"]["name"]
        volumes = self._mount_map(name, spec)
        statuses: List[Dict[str, Any]] = []
        log_dir = os.path.join(self.logs_dir, name)
        os.makedirs(log_dir, exist_ok=True)

        containers = list(spec.get("initContainers") or []) + list(
            spec.get("containers") or []
        )
        for container in containers:
            statuses.append(self._run_container(name, container, volumes, log_dir))

        pod["status"] = {
            "phase": "Running",
            "startTime": _now(),
            "podIP": "10.42.%d.%d" % (random.randint(0, 9), random.randint(2, 250)),
            "containerStatuses": statuses,
        }
        self._flush()

    def _run_container(
        self,
        pod_name: str,
        container: Dict[str, Any],
        volumes: Dict[str, str],
        log_dir: str,
    ) -> Dict[str, Any]:
        container_name = container.get("name", "container")
        command = list(container.get("command") or [])
        args = list(container.get("args") or [])
        status: Dict[str, Any] = {
            "name": container_name,
            "image": container.get("image", ""),
            "ready": True,
            "restartCount": 0,
            "started": _now(),
        }
        if not command:
            # No command: the image entrypoint would run. Nothing to simulate.
            status["state"] = {"running": {"note": "image entrypoint (not simulated)"}}
            return status

        # Rewrite paths that point at a mounted volume into the sandbox.
        rewrites: List[tuple] = []
        for mount in container.get("volumeMounts") or []:
            sandbox_path = volumes.get(mount.get("name", ""))
            if sandbox_path and mount.get("mountPath"):
                rewrites.append((mount["mountPath"].rstrip("/"), sandbox_path))
        rewrites.sort(key=lambda pair: len(pair[0]), reverse=True)

        def rewrite(value: str) -> str:
            for mount_path, sandbox_path in rewrites:
                if value.startswith(mount_path):
                    return sandbox_path + value[len(mount_path) :]
            return value

        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOSTNAME": pod_name,
            "SIMULATED": "true",
        }
        for item in container.get("env") or []:
            if "value" in item:
                env[item["name"]] = rewrite(str(item["value"]))
            elif "valueFrom" in item:
                field = ((item["valueFrom"].get("fieldRef") or {}).get("fieldPath", ""))
                env[item["name"]] = {
                    "metadata.name": pod_name,
                    "metadata.namespace": self.namespace,
                    "spec.nodeName": "sim-node-1",
                }.get(field, "simulated")

        cwd = os.path.join(self.sandbox_dir, pod_name, container_name)
        os.makedirs(cwd, exist_ok=True)
        argv = [rewrite(part) for part in command + args]
        log_path = os.path.join(log_dir, container_name + ".log")

        # Own process group: a payload that backgrounds work or sleeps forever
        # must not be able to hold the "kubelet" open past the timeout.
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=RUN_TIMEOUT_SECONDS)
            status["state"] = {"terminated": {"exitCode": process.returncode}}
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = "[simulator] output truncated: container held its stdout open"
            # A long-running container: still running when we looked at it.
            status["state"] = {
                "running": {
                    "note": "still running after %ss, killed by simulator"
                    % RUN_TIMEOUT_SECONDS
                }
            }
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(output or "")
        status["logPath"] = log_path
        return status

    # --------------------------------------------------------------- evidence

    def pod_logs(self, pod_name: str, container: Optional[str] = None) -> Dict[str, str]:
        directory = os.path.join(self.logs_dir, pod_name)
        if not os.path.isdir(directory):
            return {}
        logs = {}
        for entry in sorted(os.listdir(directory)):
            name = entry[:-4] if entry.endswith(".log") else entry
            if container and name != container:
                continue
            with open(os.path.join(directory, entry), "r", encoding="utf-8") as handle:
                logs[name] = handle.read()
        return logs
