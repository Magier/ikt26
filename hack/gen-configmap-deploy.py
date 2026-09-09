#!/usr/bin/env python3
"""Generate a registry-free deployment of the review agent.

`deploy/30-review-agent.yaml` needs an image built from `agent/Dockerfile` and
a registry (or `kind load`) to get it onto the nodes. On a playground or
managed cluster you often have neither, so this script emits an equivalent
Deployment that runs the stock `python:3.12-alpine` image with the agent
package and the repository under review mounted from ConfigMaps.

    hack/fetch-pr-repo.sh                     # clone the PR from GitHub first
    python3 hack/gen-configmap-deploy.py > /tmp/agent-configmap-mode.yaml
    kubectl apply -f /tmp/agent-configmap-mode.yaml

Identical in every way that matters to the workshop: same ServiceAccount, same
Role, same code, same audit log. ConfigMap volume `items[].path` may contain
subdirectories, so the package tree is reconstructed exactly.

The repository under review travels as a **git bundle** (a single file holding
real objects and refs) in a ConfigMap, which an init container clones into a
shared volume. The agent therefore sees a genuine checkout with real commits,
branches and authorship - not a directory of files that merely claims to be a
pull request. The bundle is made by hack/fetch-pr-repo.sh from the real
repository, so the commits in the cluster are the commits on GitHub.

If the cluster has egress to GitHub, set WORKSHOP_CLONE_URL and the init
container clones the pull request straight from the remote instead - the same
`refs/pull/<n>/head` fetch a CI runner does, no ConfigMap in the middle:

    WORKSHOP_CLONE_URL=https://github.com/Magier/ikt26.git \
      python3 hack/gen-configmap-deploy.py | kubectl apply -f -
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMESPACE = os.environ.get("WORKSHOP_NAMESPACE", "ikt-workshop")

# (configmap name, source directory, mount path)
BUNDLES = [
    ("review-agent-code", os.path.join(ROOT, "agent", "reviewagent"), "/app/reviewagent", None),
]

# The repository under review, as a git bundle written by hack/fetch-pr-repo.sh.
REPO_BUNDLE = os.path.join(ROOT, ".build", "workshop-repo.bundle")
REPO_BUNDLE_KEY = "workshop-repo.bundle"
HEAD_BRANCH = os.environ.get("WORKSHOP_HEAD_BRANCH", "feat/agent-skill-k8s-review")
BASE_BRANCH = os.environ.get("WORKSHOP_BASE_BRANCH", "main")
# Set to clone from the real remote instead of shipping a bundle.
CLONE_URL = os.environ.get("WORKSHOP_CLONE_URL", "")
PR_NUMBER = os.environ.get("WORKSHOP_PR", "1")

# python:3.12 (not -alpine) because the agent shells out to `git`, and the
# full image already ships it. The built image in agent/Dockerfile installs
# git explicitly and stays small; this path trades size for needing no build.
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "python:3.12")
GIT_IMAGE = os.environ.get("GIT_IMAGE", "alpine/git:2.45.2")

SKIP_DIRS = {"__pycache__", ".git"}
SKIP_SUFFIX = (".pyc",)


def collect(base: str):
    """Return [(configmap_key, relative_path, content)] for a directory tree."""
    entries = []
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if filename.endswith(SKIP_SUFFIX):
                continue
            path = os.path.join(directory, filename)
            relative = os.path.relpath(path, base)
            # ConfigMap keys allow [-._a-zA-Z0-9] only; the real tree is
            # restored through items[].path below.
            key = relative.replace(os.sep, "__").lstrip(".")
            with open(path, "r", encoding="utf-8") as handle:
                entries.append((key, relative, handle.read()))
    return entries


def indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else pad.rstrip() for line in text.splitlines())


def repo_bundle_configmap() -> str:
    if not os.path.exists(REPO_BUNDLE):
        sys.stderr.write(
            "missing %s - run hack/fetch-pr-repo.sh first\n" % REPO_BUNDLE
        )
        raise SystemExit(1)
    with open(REPO_BUNDLE, "rb") as handle:
        payload = handle.read()
    sys.stderr.write("%-18s git bundle, %6d bytes\n" % ("review-agent-repo", len(payload)))
    encoded = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(
        "    " + encoded[index : index + 76] for index in range(0, len(encoded), 76)
    )
    return (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        "  name: review-agent-repo\n  namespace: %s\n"
        "  annotations:\n"
        "    workshop.local/description: >-\n"
        "      git bundle of the repository under review, cloned by an init\n"
        "      container so the agent sees real commits and branches\n"
        "binaryData:\n  %s: |-\n%s\n---" % (NAMESPACE, REPO_BUNDLE_KEY, wrapped)
    )


BUNDLE_CLONE = """set -eu
git clone --quiet /bundle/{bundle_key} /workspace/workshop-repo
cd /workspace/workshop-repo
# Materialise every branch from the bundle, so the agent can be
# pointed at the base branch as well as at the PR head.
for remote in $(git branch -r | grep -v HEAD); do
  git branch --force "${{remote#origin/}}" "$remote" >/dev/null 2>&1 || true
done
git checkout --quiet {head_branch}
git --no-pager log --oneline --graph --all --decorate"""

# The same fetch hack/fetch-pr-repo.sh does, and the same one a CI runner
# does: refs/pull/<n>/head is the PR's head commit whether the branch lives
# in this repository or in a fork.
REMOTE_CLONE = """set -eu
git clone --quiet --branch {base_branch} {clone_url} /workspace/workshop-repo
cd /workspace/workshop-repo
git fetch --quiet origin "refs/pull/{pr_number}/head:refs/heads/{head_branch}"
git checkout --quiet {head_branch}
git --no-pager log --oneline --graph --all --decorate"""

BUNDLE_MOUNT = """            - name: repo-bundle
              mountPath: /bundle
              readOnly: true
"""

BUNDLE_VOLUME = """        - name: repo-bundle
          configMap:
            name: review-agent-repo
"""


def main() -> int:
    out = [] if CLONE_URL else [repo_bundle_configmap()]
    bundles = []
    for name, base, mount, _ in BUNDLES:
        entries = collect(base)
        total = sum(len(content.encode("utf-8")) for _, _, content in entries)
        if total > 900 * 1024:
            sys.stderr.write("%s is %d bytes: too close to the 1MiB ConfigMap limit\n" % (name, total))
            return 1
        sys.stderr.write("%-18s %2d files, %6d bytes\n" % (name, len(entries), total))
        bundles.append((name, mount, entries))

        out.append("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %s\n  namespace: %s\ndata:" % (name, NAMESPACE))
        for key, _, content in entries:
            out.append("  %s: |" % key)
            out.append(indent(content, 4))
        out.append("---")

    volumes = []
    mounts = []
    for name, mount, entries in bundles:
        volumes.append(
            "        - name: %s\n          configMap:\n            name: %s\n            items:" % (name, name)
        )
        for key, relative, _ in entries:
            volumes.append("              - key: %s\n                path: %s" % (key, relative))
        mounts.append("            - name: %s\n              mountPath: %s\n              readOnly: true" % (name, mount))

    if CLONE_URL:
        sys.stderr.write("%-18s clone %s PR #%s\n" % ("repository", CLONE_URL, PR_NUMBER))
        clone_script = REMOTE_CLONE.format(
            clone_url=CLONE_URL,
            pr_number=PR_NUMBER,
            base_branch=BASE_BRANCH,
            head_branch=HEAD_BRANCH,
        )
    else:
        clone_script = BUNDLE_CLONE.format(
            bundle_key=REPO_BUNDLE_KEY, head_branch=HEAD_BRANCH
        )

    out.append(
        DEPLOYMENT.format(
            namespace=NAMESPACE,
            volumes="\n".join(volumes),
            mounts="\n".join(mounts),
            agent_image=AGENT_IMAGE,
            git_image=GIT_IMAGE,
            clone_script=indent(clone_script, 14),
            clone_mounts="" if CLONE_URL else BUNDLE_MOUNT,
            bundle_volume="" if CLONE_URL else BUNDLE_VOLUME,
        )
    )
    print("\n".join(out))
    return 0


DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-pr-review-agent
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: ai-pr-review-agent
    app.kubernetes.io/part-of: platform-tooling
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ai-pr-review-agent
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ai-pr-review-agent
    spec:
      serviceAccountName: review-agent-sa
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        fsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        # Bring the repository under review into the pod exactly as a real
        # deployment would fetch a PR's head ref. The agent then reads a
        # genuine checkout: real commits, real authors, real branches.
        - name: clone-pr
          image: {git_image}
          command: ["/bin/sh", "-c"]
          args:
            - |
{clone_script}
          volumeMounts:
            - name: workspace
              mountPath: /workspace
{clone_mounts}          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
      containers:
        - name: agent
          image: {agent_image}
          command: ["python", "-m", "reviewagent.cli"]
          args: ["serve"]
          workingDir: /app
          env:
            - name: PYTHONPATH
              value: /app
            # /app is a read-only ConfigMap mount: never try to write .pyc.
            - name: PYTHONDONTWRITEBYTECODE
              value: "1"
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: KUBE_BACKEND
              value: cluster
            - name: WORKSHOP_REPO
              value: /workspace/workshop-repo
            - name: WORKSHOP_NAMESPACE
              value: {namespace}
            - name: AUDIT_LOG
              value: /var/log/review-agent/events.jsonl
            - name: SERVICE_ACCOUNT_NAME
              value: review-agent-sa
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet: {{path: /healthz, port: http}}
            initialDelaySeconds: 2
          livenessProbe:
            httpGet: {{path: /healthz, port: http}}
            initialDelaySeconds: 10
          resources:
            requests: {{cpu: 20m, memory: 64Mi}}
            limits: {{cpu: 500m, memory: 256Mi}}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
{mounts}
            - name: workspace
              mountPath: /workspace
            - name: audit
              mountPath: /var/log/review-agent
      volumes:
{volumes}
{bundle_volume}        - name: workspace
          emptyDir: {{}}
        - name: audit
          emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: ai-pr-review-agent
  namespace: {namespace}
spec:
  selector:
    app.kubernetes.io/name: ai-pr-review-agent
  ports:
    - name: http
      port: 80
      targetPort: http
"""


if __name__ == "__main__":
    sys.exit(main())
