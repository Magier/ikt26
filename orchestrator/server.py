"""agent-orchestrator - the control loop that spawns agent-worker pods.

This is the platform component the whole workshop turns on. Its job is to keep a
small pool of ephemeral worker pods alive: it reconciles what is running against
a desired count and creates a worker pod whenever one is missing. That is the
entire function - and it is exactly why this workload's ServiceAccount holds
`create pods`. The permission is not contrived: something whose job is to create
pods has to be allowed to create pods.

It talks to the Kubernetes API directly over HTTPS with its mounted
ServiceAccount token - no client library, standard library only, the same way
every in-cluster caller ultimately authenticates. Watching this process create
a pod is watching a ServiceAccount token turn into a running workload. That is
the primitive an attacker inherits if they ever stand in this pod's shoes.

No dependencies - Python's standard library only, like the console next door.
"""

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))

# How many worker pods to keep alive. Small on purpose: the playground nodes are
# 4G and every worker is a full agentbox image.
WORKER_REPLICAS = int(os.environ.get("WORKER_REPLICAS", "1"))
WORKER_IMAGE = os.environ.get(
    "WORKER_IMAGE", "ghcr.io/magier/ikt26/agentbox:latest"
)
# The SA the workers run as - deliberately separate from, and weaker than, the
# orchestrator's own. A worker that abuses its own identity should get nowhere;
# the escalation is in reaching this orchestrator's identity, not the worker's.
WORKER_SA = os.environ.get("WORKER_SA", "agent-worker")
RECONCILE_SECONDS = float(os.environ.get("RECONCILE_SECONDS", "15"))

# The in-cluster API server, addressed by DNS so the mounted CA validates (the
# cert has no SAN for the service IP). The three files below are what every pod
# with a ServiceAccount gets mounted, and together they are a full API identity.
API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = os.path.join(SA_DIR, "token")
CA_PATH = os.path.join(SA_DIR, "ca.crt")
NS_PATH = os.path.join(SA_DIR, "namespace")

# label set stamped on every pod this loop owns, so it can find its own workers
# again without tracking state.
OWNED = {"app": "agent-worker", "managed-by": "agent-orchestrator"}
SELECTOR = ",".join("%s=%s" % kv for kv in OWNED.items())

_state = {"workers": [], "last_reconcile": None, "last_error": None}
_lock = threading.Lock()


def _read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


NAMESPACE = os.environ.get("POD_NAMESPACE") or _read(NS_PATH, "agent-system")


def api(method, path, body=None):
    """One Kubernetes API call, authenticated with the mounted SA token.

    Returns the decoded JSON response. Raises on transport errors; a non-2xx
    is surfaced as an HTTPError the caller can inspect (404 is expected and
    handled when a pod has already gone).
    """
    token = _read(TOKEN_PATH)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context(cafile=CA_PATH)
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def worker_manifest():
    """The pod this loop stamps out. Resources are small - these are stand-in
    workers for the lab, not agents actually burning tokens, so the 3Gi ceiling
    the agentbox image asks for elsewhere would only keep them from scheduling.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"generateName": "agent-worker-", "labels": dict(OWNED)},
        "spec": {
            "serviceAccountName": WORKER_SA,
            "containers": [
                {
                    "name": "worker",
                    "image": WORKER_IMAGE,
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "128Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                }
            ],
            # A bare, self-managed pod, not a Deployment: each worker is a task
            # runner the orchestrator owns the lifecycle of. Reinventing a slice
            # of a ReplicaSet is the point - it is why the SA needs create pods.
            "restartPolicy": "Always",
        },
    }


def list_workers():
    path = "/api/v1/namespaces/%s/pods?labelSelector=%s" % (NAMESPACE, SELECTOR)
    items = api("GET", path).get("items", [])
    return [
        {
            "name": p["metadata"]["name"],
            "phase": p.get("status", {}).get("phase", "Unknown"),
        }
        for p in items
    ]


def create_worker():
    path = "/api/v1/namespaces/%s/pods" % NAMESPACE
    made = api("POST", path, worker_manifest())
    name = made["metadata"]["name"]
    print("created worker %s" % name, flush=True)
    return name


def reconcile():
    """Bring the live worker count up to WORKER_REPLICAS. Only ever creates -
    workers that die are replaced on the next pass; nothing here deletes, so a
    pod an attacker spawns by hand is left alone rather than fought over.
    """
    workers = list_workers()
    alive = [w for w in workers if w["phase"] in ("Pending", "Running")]
    for _ in range(WORKER_REPLICAS - len(alive)):
        create_worker()
        workers.append({"name": "(creating)", "phase": "Pending"})
    return workers


def loop():
    while True:
        try:
            workers = reconcile()
            with _lock:
                _state.update(
                    workers=workers,
                    last_reconcile=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    last_error=None,
                )
        except urllib.error.HTTPError as exc:
            detail = "%s %s" % (exc.code, exc.reason)
            print("reconcile failed: %s" % detail, flush=True)
            with _lock:
                _state["last_error"] = detail
        except Exception as exc:
            print("reconcile failed: %s" % exc, flush=True)
            with _lock:
                _state["last_error"] = str(exc)
        time.sleep(RECONCILE_SECONDS)


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-orchestrator/0.1"

    def _send(self, status, body, content_type="application/json"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # Does not touch the API server, so a failing probe means this process
        # is wedged - not that the cluster is briefly unreachable.
        if self.path.startswith("/healthz"):
            return self._send(200, "ok\n", "text/plain; charset=utf-8")
        if self.path == "/" or self.path.startswith("/?") or self.path.startswith("/status"):
            with _lock:
                snapshot = dict(_state)
            snapshot["namespace"] = NAMESPACE
            snapshot["desired"] = WORKER_REPLICAS
            return self._send(200, json.dumps(snapshot, indent=2) + "\n")
        return self._send(404, "not found\n", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # the create/reconcile lines are the log worth keeping


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    print("listening on 0.0.0.0:%d, namespace %s" % (PORT, NAMESPACE), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
