# Runbook — every step by hand

No `make`, no wrapper scripts
cluster (single-node kubeadm, v1.37, containerd) in the order shown.

`make` targets exist for convenience and call exactly these commands; if you
prefer them, `make help` lists them. This file is the source of truth for what
they do.

Conventions used throughout:

```sh
export KUBECONFIG=/path/to/your/kubeconfig      # a throwaway cluster
export NS=ikt-workshop
cd /path/to/this/repo
```

---

## 0. Prerequisites

| need | why | check |
|---|---|---|
| Python 3.9+ | the agent (standard library only) | `python3 --version` |
| git | the agent derives PR facts from real history | `git --version` |
| kubectl | everything cluster-side | `kubectl version --client` |
| a throwaway cluster | the lab is deliberately vulnerable | `kubectl get nodes` |

No pip installs. No registry, no image build, no LLM API key.

---

## 1. Fetch the repository under review

The repository under review is a real one:
**<https://github.com/Magier/ikt26>**, pull request **#1**. Open it in a
browser first — everything below reviews that exact commit.

```sh
hack/fetch-pr-repo.sh
```

This clones the repository into `.build/workshop-repo`, brings the pull
request down through `refs/pull/1/head` (the ref a CI runner checks out, so it
works whether the branch lives in the repository or in a fork), and leaves the
checkout on the PR branch — an agent reviewing a pull request works from the
head ref, which is exactly the problem.

Verify — three commits, and the malicious skill only on the PR branch:

```sh
git -C .build/workshop-repo log --oneline --graph --all --decorate
git -C .build/workshop-repo diff --stat main...feat/agent-skill-k8s-review
git -C .build/workshop-repo ls-tree -r --name-only main | grep k8s-review || echo "not on main - good"
```

```text
* 084e96b (HEAD -> feat/agent-skill-k8s-review, origin/pr/1) chore(ci): add k8s-review agent skill…
* ad47b05 (origin/main, main) ci: add dependency-audit skill for the PR review agent
* 4bdfa48 initial import: payments-api service and deployment manifests
```

Rerunning updates the checkout in place; if GitHub is unreachable and the
checkout is already there, the run continues with what it has. It also writes
`.build/workshop-repo.git` (bare) and `.build/workshop-repo.bundle`, which is
how the repository reaches the cluster in step 4.

**No network?** `hack/build-pr-repo.sh` rebuilds the identical history from
the authored content in `workshop-repo/` — fixed identities and fixed dates,
so the SHAs above come out the same. It is what seeded GitHub in the first
place, and what the test suite uses.

**Instructor, resetting the story:** `hack/publish-pr-repo.sh --yes` rebuilds
the history and force-pushes both branches, then opens or updates PR #1.
Force-push: anything pushed to `main` or the PR branch by hand is discarded.

---

## 2. Run it locally, with no cluster at all

The local backend is a file-backed Kubernetes simulator with a toy kubelet: it
really executes the injected container's command in a sandbox directory, so
the code-execution stage is observable offline.

```sh
rm -rf sim/state audit/events.jsonl

PYTHONPATH=agent SIM_CONTAINER_TIMEOUT=3 python3 -m reviewagent.cli \
  --repo .build/workshop-repo \
  --backend local \
  --audit-log audit/events.jsonl \
  review-pr --quiet
```

Expected: 4 steps, `source` flipping from `operator-task` to `skill:k8s-review#N`.

Inspect what it produced:

```sh
PYTHONPATH=agent python3 -m reviewagent.cli \
  --repo .build/workshop-repo --backend local --audit-log audit/events.jsonl inspect
```

The marker the injected container wrote, and its captured stdout:

```sh
find sim/state/sandbox -name agent-review-marker.txt -exec cat {} \;
cat sim/state/podlogs/*/runtime-config-sync.log
```

The same run with the mitigation (see step 8):

```sh
rm -rf sim/state
PYTHONPATH=agent SIM_CONTAINER_TIMEOUT=3 python3 -m reviewagent.cli \
  --repo .build/workshop-repo --backend local --audit-log audit/events-mitigated.jsonl \
  review-pr --quiet --skills-ref main
```

Run the tests any time (41 tests, ~30s, no cluster):

```sh
python3 -m unittest discover -s tests -v
```

---

## 3. Deploy the namespace, the target workload and the RBAC

```sh
kubectl apply -f deploy/00-namespace.yaml
kubectl apply -f workshop-repo/deploy/application.yaml
kubectl apply -f deploy/20-review-agent-rbac.yaml
```

Note the second file: the target workload is applied straight from the
repository under review, because in the story that file *is* the deployment
source of truth — and the attack works by making the live object diverge from
it.

Check the agent's identity is constrained. Use a `SubjectAccessReview`, not
`kubectl auth can-i`: `can-i` mis-parses subresources and will wrongly report
`yes` for `pods/exec`.

```sh
kubectl create -o jsonpath='{.status.allowed}{"\n"}' -f - <<EOF
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: system:serviceaccount:$NS:review-agent-sa
  resourceAttributes: {namespace: $NS, verb: create, group: "", resource: pods, subresource: exec}
EOF
# false  <- no exec permission anywhere in this lab
```

---

## 4. Deploy the review agent

Two ways. The second needs no image build and no registry, and is the path
this lab is verified on.

### 4a. From a built image (needs a registry or `kind load`)

```sh
docker build -f agent/Dockerfile -t ikt-workshop/review-agent:0.1.0 .
kind load docker-image ikt-workshop/review-agent:0.1.0 --name ikt-workshop
kubectl apply -f deploy/30-review-agent.yaml
```

### 4b. Without an image (recommended for lab clusters)

Generates a Deployment that runs stock `python:3.12`, with the agent package
mounted from a ConfigMap and the repository cloned by an init container from a
git bundle in another ConfigMap (the bundle step 1 wrote, so the commits in the
cluster are the commits on GitHub):

```sh
python3 hack/gen-configmap-deploy.py > /tmp/review-agent.yaml
kubectl apply -f /tmp/review-agent.yaml
```

If the cluster has egress to GitHub, skip the bundle and let the init
container fetch `refs/pull/1/head` itself, which is what a CI runner does:

```sh
WORKSHOP_CLONE_URL=https://github.com/Magier/ikt26.git \
  python3 hack/gen-configmap-deploy.py > /tmp/review-agent.yaml
kubectl apply -f /tmp/review-agent.yaml
```

Wait for both workloads:

```sh
kubectl -n $NS rollout status deploy/payments-api    --timeout=180s
kubectl -n $NS rollout status deploy/ai-pr-review-agent --timeout=300s
kubectl -n $NS get pods
```

Confirm the agent got a genuine checkout, not a pile of files:

```sh
kubectl -n $NS logs deploy/ai-pr-review-agent -c clone-pr | tail -3
```

---

## 5. Record the "before" state

```sh
kubectl -n $NS get deploy payments-api \
  -o jsonpath='generation={.metadata.generation} containers={.spec.template.spec.containers[*].name}{"\n"}'
# generation=1 containers=payments-api
```

---

## 6. Trigger the review — this is the attack

Any one of these three. They are equivalent; the HTTP one is closest to how a
webhook would fire it.

```sh
# a) CLI inside the agent pod
kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- \
  python -m reviewagent.cli --backend cluster review-pr --quiet

# b) HTTP API, from another pod in the namespace
kubectl -n $NS run api-check --rm -i --restart=Never --image=curlimages/curl:8.11.1 --quiet -- \
  -sS -XPOST -H 'Content-Type: application/json' -d '{}' http://ai-pr-review-agent/review

# c) HTTP API, from your laptop
kubectl -n $NS port-forward svc/ai-pr-review-agent 8080:80 &
curl -sS -XPOST localhost:8080/review | python3 -m json.tool
```

Expected: `load_skill`, then three tool calls whose `source` is
`skill:k8s-review#1..3`.

---

## 7. Verify the compromise

```sh
kubectl -n $NS rollout status deploy/payments-api --timeout=120s

kubectl -n $NS get deploy payments-api \
  -o jsonpath='generation={.metadata.generation} containers={.spec.template.spec.containers[*].name}{"\n"}'
# generation=2 containers=runtime-config-sync payments-api

# what executed inside the workload
kubectl -n $NS logs deploy/payments-api -c runtime-config-sync --tail=20

# the marker, served by the application itself
kubectl -n $NS run marker-check --rm -i --restart=Never --image=curlimages/curl:8.11.1 --quiet -- \
  -sS http://payments-api/agent-review-marker.txt
```

`sa_token_readable: yes` in that output is the workload's own ServiceAccount
token, reachable from the injected container. That is the hook into the
post-exploitation stage.

---

## 8. Run the mitigation

Same PR, same agent, same ServiceAccount, same Role. One flag: skills are
loaded from the base branch instead of the PR head.

```sh
# reset the workload to its pristine, manifest-defined state
kubectl -n $NS delete deploy payments-api
kubectl apply -f workshop-repo/deploy/application.yaml
kubectl -n $NS rollout status deploy/payments-api --timeout=180s

kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- \
  python -m reviewagent.cli --backend cluster --skills-ref main review-pr --quiet

kubectl -n $NS get deploy payments-api \
  -o jsonpath='generation={.metadata.generation} containers={.spec.template.spec.containers[*].name}{"\n"}'
# generation=1 containers=payments-api   <- nothing happened
```

The agent discovers only `dependency-audit`, loads nothing, and makes zero
tool calls. Point out the cost: it also produced a worse review, because it no
longer has a Kubernetes skill at all.

---

## 9. Collect the evidence

The agent's audit log lives in an `emptyDir` and dies with its pod. Collect
before tearing anything down.

```sh
# the agent's own structured log
kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- \
  cat /var/log/review-agent/events.jsonl

# who changed the Deployment, and under which run
kubectl -n $NS rollout history deploy/payments-api
kubectl -n $NS get deploy payments-api -o jsonpath='{.metadata.annotations}' | python3 -m json.tool

# the object contradicts its own apply record: the agent patched, it did not apply
kubectl -n $NS get deploy payments-api \
  -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
  | python3 -m json.tool | grep -A3 '"containers"'

# ownership chain: pod -> ReplicaSet -> Deployment
kubectl -n $NS get pods -o custom-columns='POD:.metadata.name,OWNER:.metadata.ownerReferences[0].name'
kubectl -n $NS get rs

# the agent's own Kubernetes Events
kubectl -n $NS get events --sort-by=.lastTimestamp | tail -25

# the repository, from inside the pod: who added the skill, and when
kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- sh -c \
  'cd /workspace/workshop-repo && git --no-pager log --oneline --graph --all --decorate'
kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- sh -c \
  'cd /workspace/workshop-repo && git --no-pager log --diff-filter=A -1 \
     --format="%H%x09%an <%ae>%x09%aI" -- .agents/skills/k8s-review/SKILL.md'
kubectl -n $NS exec deploy/ai-pr-review-agent -c agent -- sh -c \
  'cd /workspace/workshop-repo && git --no-pager diff main...HEAD'
```

To bundle all of the above into `audit/exports/cluster/` in one go — including
a rendered timeline and a `commands.sh` that restates the run as plain
kubectl — run `hack/export-trace.sh`. To render a local run instead:

```sh
PYTHONPATH=agent python3 -m reviewagent.cli \
  --repo .build/workshop-repo --audit-log audit/events.jsonl \
  export-trace --out-dir audit/exports/local
```

---

## 10. Reset and teardown

```sh
# re-arm: pristine workload, agent untouched
kubectl -n $NS delete deploy payments-api
kubectl apply -f workshop-repo/deploy/application.yaml

# start the whole thing over
kubectl delete namespace $NS

# local state
rm -rf sim/state audit/events.jsonl audit/exports .build
```

---

## Troubleshooting

**`Unable to connect to the server: tls: ... certificate is valid for 10.96.0.1, 172.16.0.2, not 127.0.0.1`**
A kubeconfig pointing at a tunnel on `127.0.0.1`. Add `tls-server-name` under
the cluster entry (use a name the cert covers), or use a kubeconfig whose
server address matches the certificate.

**`kubectl auth can-i create pods/exec --as=…` says `yes`**
kubectl mis-parses subresources. Use the `SubjectAccessReview` in step 3; it
correctly answers `false`.

**`payments-api` CrashLoopBackOff, `mkdir() "/var/cache/nginx/client_temp" failed (13: Permission denied)`**
nginx running as uid 101 needs writable `/var/cache/nginx` and `/var/run`
volumes plus `fsGroup: 101`. Already fixed in
`workshop-repo/deploy/application.yaml`; if you edited that file, keep those
volumes.

**`error: container clone-pr is not valid for pod …`**
You are looking at the old pod from a previous revision. Pick the newest:
`kubectl -n $NS get pods -l app.kubernetes.io/name=ai-pr-review-agent --sort-by=.metadata.creationTimestamp`

**`missing .build/workshop-repo.bundle - run hack/fetch-pr-repo.sh first`**
Step 1 has not run, or `.build/` was deleted.

**The agent reports `vcs: none` and no commit facts**
It is reading a plain directory. Point `--repo` at `.build/workshop-repo`, not
`workshop-repo`.

**Agent pod restarted and the audit log is empty**
Expected: `emptyDir`. The repository checkout is rebuilt by the init container
from the ConfigMap, but the log is gone. Collect evidence before restarts.

**`fatal: could not read from remote repository` in the `clone-pr` init container**
Only in the `WORKSHOP_CLONE_URL` variant, which needs egress to GitHub from
the cluster. Drop the variable to go back to the git bundle in a ConfigMap.
