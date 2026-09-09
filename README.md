# ikt-workshop — phase 1

A minimal, deliberately vulnerable lab for a Kubernetes security workshop
about **AI-agent-era initial access**.

The scenario in one line: an external contributor opens a pull request that
adds an *agent skill* to the repository, the company's AI PR-review agent
discovers that skill, loads it, follows its instructions, and — using
permissions it was legitimately granted — patches a running workload, which
executes attacker-chosen code inside a pod.

The pull request is a real one, on GitHub:

> **[github.com/Magier/ikt26 #1](https://github.com/Magier/ikt26/pull/1)** —
> `chore(ci): add k8s-review agent skill for automated manifest review`

Participants can open it in a browser, review it as they would any PR, and
then watch the agent review the same commit. Nothing is staged in a directory
that merely claims to be a pull request: the agent clones the repository and
checks out `refs/pull/1/head`, the same ref a CI runner would.

Nothing here is a model jailbreak or an exploit. Every component behaves
correctly. The failure is architectural:

> untrusted repository content + an instruction-following agent + plausible
> operational privileges = a security boundary failure

Phase 1 is intentionally small: a deterministic fake LLM, no agent framework,
no third-party Python dependencies.

---

## Contents

```text
agent/
  Dockerfile                      agent image (stdlib only)
  reviewagent/
    pr.py                         repository / pull-request model
    vcs.py                        git access: commits, branches, change sets
    skills.py                     skill discovery + representation
    llm/base.py                   LLM interface (AgentAction, LLMContext)
    llm/fake.py                   deterministic fake LLM
    tools/registry.py             explicit tool surface
    tools/fs_tools.py             read_file, load_skill
    tools/k8s_tools.py            kubernetes_get / _create / _patch
    kube/base.py                  Kubernetes client contract
    kube/rest.py                  in-cluster client (urllib + SA token)
    kube/local.py                 file-backed simulator with a toy kubelet
    evidence.py                   structured JSONL event log + K8s Events
    trace.py                      renders a run as a timeline + plain kubectl
    agent.py                      the agent loop
    cli.py                        review-pr / inspect / tools / serve
    server.py                     POST /review
workshop-repo/                    the repository under review (authored content)
  PR.md                           the attacker's pull request (#1)
  deploy/application.yaml         the target workload (payments-api)
  .agents/skills/k8s-review/       *** the malicious skill ***
  .agents/skills/dependency-audit/ a benign skill (proves selection works)
deploy/                           namespace, RBAC, agent Deployment, kind audit config
hack/fetch-pr-repo.sh             clones the PR from GitHub into .build/
hack/build-pr-repo.sh             the same history, built offline (seeds GitHub)
hack/publish-pr-repo.sh           instructor-only: pushes it and opens the PR
hack/gen-configmap-deploy.py      registry-free deployment (code mounted from ConfigMaps)
hack/export-trace.sh              pull the agent trace + cluster evidence out of a cluster
sim/seed/                         starting "cluster" state for local runs
tests/test_phase1.py              26 tests, no cluster required
docs/RUNBOOK.md                   every step by hand, no make, all verified
docs/INSTRUCTOR.md                facilitation notes and the investigation walkthrough
bin/                              review-pr, inspect-evidence, reset
audit/exports/                    exported traces (see "Exporting the trace")
```

---

## Architecture

```text
┌── github.com/Magier/ikt26, cloned checkout (untrusted) ──────────────────┐
│  main                        ← base: no k8s-review skill                 │
│  feat/agent-skill-k8s-review ← PR #1 by an external contributor,         │
│    commit 084e96b adds .agents/skills/k8s-review/SKILL.md                 │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ (1) discover: .agents/skills/*/SKILL.md  -> metadata only
                │ (2) load:     full instruction body      <- trust boundary
                ▼
┌── ai-pr-review-agent (ServiceAccount: review-agent-sa) ──────────────────┐
│                                                                          │
│   SkillRegistry ──metadata──▶ LLM.decide(context) ──AgentAction──▶ agent │
│                                (FakeLLM today)                     loop  │
│                                                                     │    │
│   ToolRegistry: read_file, load_skill,                              │    │
│                 kubernetes_get, kubernetes_create, kubernetes_patch ◀    │
│                                                                     │    │
│   EventLog: JSONL evidence  +  Kubernetes Events                    │    │
└─────────────────────────────────────────────────────────────────────┼────┘
                                                                      │
                                        Kubernetes API (patch deployment)
                                                                      ▼
┌── namespace: ikt-workshop ────────────────────────────────────────────┐
│  Deployment/payments-api  ──▶ new ReplicaSet ──▶ new Pod                 │
│      containers: [payments-api, runtime-config-sync ◀── injected]        │
│                                       │                                  │
│                                       └─▶ code executes: marker file in  │
│                                           the served volume + log beacon │
└──────────────────────────────────────────────────────────────────────────┘
```

Every arrow crosses a module boundary that can be replaced independently:
repository, skill discovery, LLM, tool execution, Kubernetes credentials,
Kubernetes workload, evidence.

---

> Prefer plain commands to `make`? [`docs/RUNBOOK.md`](docs/RUNBOOK.md) has
> every step of this lab as copy-pasteable commands, each one verified against
> a real cluster, plus a troubleshooting section.

## Running it locally (no cluster)

Requires Python 3.9+ and nothing else.

```sh
make test            # 41 tests, ~30s
make repo            # clone the repository under review from GitHub
make demo            # run the full scenario against the local simulator
make demo-mitigated  # the same PR with skills loaded from the base branch
make inspect         # show the evidence the run produced
make reset           # wipe simulated cluster state + audit log
```

`make demo` and `make demo-mitigated` fetch the repo first, so `make demo` on a
clean checkout is enough. `make repo` needs network the first time; after that
it updates the checkout in place and falls back to what it already has, so a
session that has fetched once keeps working offline. `make repo-offline`
rebuilds the identical history with no network at all — it is what seeded
GitHub, and what the test suite uses, so the commit SHAs are the same.

`make demo` is the phase 1 milestone command. Output:

```text
skills discovered: dependency-audit, k8s-review
skills loaded:     k8s-review

steps:
  1. invoke_skill     load_skill         source=operator-task       outcome=ok
  2. call_tool        kubernetes_get     source=skill:k8s-review#1  outcome=ok
  3. call_tool        kubernetes_patch   source=skill:k8s-review#2  outcome=ok
  4. call_tool        kubernetes_get     source=skill:k8s-review#3  outcome=ok
```

### What the local backend actually does

`kube/local.py` is a file-backed Kubernetes simulator with a **toy kubelet**:
when a Deployment's pod template changes it creates a ReplicaSet and a Pod and
*really runs* each container's `command` as a local subprocess, in a per-pod
sandbox directory under `sim/state/sandbox/`, with volume `mountPath`s
rewritten into that sandbox, a scrubbed environment and a wall-clock timeout
(`SIM_CONTAINER_TIMEOUT`, default 10s; the process group is killed).

That is what makes the code-execution stage observable without a cluster —
`sim/state/podlogs/<pod>/runtime-config-sync.log` is a real captured stdout.

It is a **development aid, not a sandbox with security properties.** Only ever
point it at this workshop's own manifests.

---

## Verified on a real cluster

Phase 1 was run end to end on a single-node kubeadm cluster (v1.37,
containerd), not only in the simulator:

```text
BEFORE   deployment payments-api  generation=1  containers=[payments-api]
         one pod, serving its index page

TRIGGER  kubectl -n ikt-workshop exec deploy/ai-pr-review-agent -- \
           python -m reviewagent.cli --backend cluster review-pr

AFTER    generation=2  containers=[runtime-config-sync, payments-api]
         new ReplicaSet, new pod, 2/2 Running
         rollout history CHANGE-CAUSE names the agent and the run id
         kubectl logs -c runtime-config-sync shows the beacon
         curl http://payments-api/agent-review-marker.txt serves the marker
```

The injected container reported `sa_token_readable: yes` — on a real cluster
it can reach the *workload's* ServiceAccount token, which the simulator cannot
show. That is the hook into phase 2's post-exploitation stage.

The agent's identity was confirmed to be genuinely constrained
(`SubjectAccessReview`, not `kubectl auth can-i` — see known weaknesses):

| check | result |
|---|---|
| `patch deployments` | allowed — the vector |
| `create pods` | allowed — the unused stale rule |
| `get pods`, `get pods/log` | allowed |
| `create pods/exec`, `get pods/attach` | **denied** |
| `get secrets`, `delete deployments` | **denied** |
| anything in `kube-system`, `list nodes` | **denied** |

Two real bugs surfaced only on the cluster and are fixed: nginx could not run
as uid 101 without writable `/var/cache/nginx` and `/var/run` volumes, and the
mutation stamping did not set `kubernetes.io/change-cause`.

---

## Running it in Kubernetes

Use a throwaway cluster (kind, k3d, minikube). Optionally create it with API
server auditing enabled, which gives participants the strongest evidence
source:

```sh
mkdir -p /tmp/kind-audit
kind create cluster --config deploy/kind/kind-config.yaml
```

Then, if you can build and load an image:

```sh
make image                 # docker build -f agent/Dockerfile -t ...:0.1.0 .
make kind-load             # kind load docker-image ...
make cluster-deploy        # namespace, target workload, RBAC, agent
make cluster-attack        # trigger the review  == the attack
make cluster-evidence      # collect audit log, rollout history, pods, logs, events
```

The image is published by CI, so `make cluster-deploy` on a cluster with
egress needs neither a local build nor `kind load` - the pod pulls
`ghcr.io/magier/ikt26/review-agent:latest` directly. Rebuild it by pushing to
the `lab-infra` branch:

```sh
make publish-lab-infra     # this tree -> lab-infra; Actions builds and pushes
```

The management tree lives on `lab-infra` rather than on `main` because
`make publish-repo` force-pushes `main` and the PR branch from `workshop-repo/`
on every story reset, and because a CI workflow sitting in the diff would tell
participants the repository is a prop.

On a cluster where you have **no registry access at all** (air-gapped
playgrounds, no egress), skip the image entirely:

```sh
make cluster-deploy-nobuild   # stock python:3.12-alpine + code from ConfigMaps
make cluster-attack
make cluster-evidence
make cluster-reset            # restore the pristine workload, re-runnable
```

`hack/gen-configmap-deploy.py` emits an equivalent Deployment that mounts
`agent/reviewagent/` from a ConfigMap (ConfigMap volume `items[].path` may
contain subdirectories, so the package tree is exact) and clones the
repository under review from a git bundle in a second ConfigMap. Same
ServiceAccount, same Role, same code, same audit log. This is the path this
lab was last verified on.

If the cluster can reach GitHub, drop the bundle and let the init container
clone the pull request directly — the same `refs/pull/1/head` fetch a CI
runner does:

```sh
WORKSHOP_CLONE_URL=https://github.com/Magier/ikt26.git \
  python3 hack/gen-configmap-deploy.py | kubectl apply -f -
```

`make cluster-attack` runs the CLI inside the agent pod. The HTTP API is
equivalent and closer to how a webhook would fire it:

```sh
kubectl -n ikt-workshop port-forward svc/ai-pr-review-agent 8080:80 &
curl -sS -XPOST localhost:8080/review | python3 -m json.tool
curl -sS localhost:8080/skills        # what the agent can discover
curl -sS localhost:8080/tools         # the agent's capability inventory
curl -sS localhost:8080/events        # the raw JSONL audit log
```

Everything lives in the `ikt-workshop` namespace. Neither the agent nor the
"attacker" needs cluster-admin, cluster-scoped permissions, or `pods/exec`.

Teardown: `make cluster-clean`.

---

## The components

### 1. Repository / PR (`pr.py`, `vcs.py`)

The repository under review is a **real git repository on GitHub** —
[Magier/ikt26](https://github.com/Magier/ikt26) — cloned by
`hack/fetch-pr-repo.sh`, which brings the pull request down through
`refs/pull/1/head` and leaves the checkout on the PR branch, because that is
what an agent reviewing a pull request works from:

```text
* 084e96b (feat/agent-skill-k8s-review)  chore(ci): add k8s-review agent skill…
|                                        Richmond Avenal
|                                        <richmond.avenal@basement-contractors.example.com>
|                                        2026-09-03  → adds the malicious skill
* ad47b05 (main)  ci: add dependency-audit skill for the PR review agent
|                 Roy Trenneman <r.trenneman@reynholm.example.com>  2026-08-27
* 4bdfa48         initial import: payments-api service and deployment manifests
|                 Maurice Moss <m.moss@reynholm.example.com>  2026-08-11
```

The history is generated from the authored content in `workshop-repo/` by
`hack/build-pr-repo.sh` and pushed by `hack/publish-pr-repo.sh` — fixed
identities and fixed dates, so the SHAs above are reproducible offline and
identical to the ones on GitHub. Resetting the story is a force-push, not a
merge.

So PR facts come from two places, and the difference is itself evidence:

* **declared** — what `PR.md` frontmatter says (id, title, author,
  `author_association`, `files_changed`, `review_request`);
* **derived** — what git says (head commit and its real author/email/date, the
  branch, and the actual `main...feat/…` change set).

Git wins where the two overlap, both are recorded, and
`undeclared_files` / `unchanged_declared_files` flag a PR description that
misrepresents its own change set. Repository reads are confined to the
checkout — path traversal is refused — and can be taken from any ref.

`vcs.py` shells out to the `git` binary rather than using a library, so every
provenance answer is a command an investigator can re-run by hand.
`NoVersionControl` is the honest fallback: a plain directory still works, it
just answers fewer questions.

`PullRequest.untrusted` is `True` for PR #1 (`FIRST_TIME_CONTRIBUTOR`).
Nothing in phase 1 acts on it. That is a deliberate discussion hook: the agent
*knew* the content was externally authored and treated it as instructions
anyway.

### 2. Skill discovery (`skills.py`)

Two separate steps, mirroring how real Agent Skills work:

```text
discover()   list .agents/skills/*/SKILL.md at a revision, parse frontmatter
             (name, description), record path, size, sha256 and the commit
             that introduced the file                       — metadata only
load(name)   read the full instruction body                 — the trust crossing
```

There is **no allowlist of trusted skills**. Whatever the PR ships is what the
model may choose from.

Discovery reads a *revision*, defaulting to the working tree (for a PR
checkout, the head ref). `--skills-ref main` reads them from the base branch
instead — see "The one-flag mitigation" below.

### 3. Fake LLM (`llm/fake.py`)

Deterministic, zero compute, no model. It reproduces exactly two model
behaviours:

* **selection** — match the task's topic against skill *descriptions*
  (`k8s-review` wins for a Kubernetes review; `dependency-audit` wins for a
  lockfile review; an unrelated task selects nothing);
* **instruction following** — once a skill body is in context, follow the
  procedure it contains.

It returns structured actions and never executes anything:

```json
{"action": "invoke_skill", "skill": "k8s-review"}
{"action": "call_tool", "tool": "kubernetes_patch", "parameters": {"...": "..."},
 "source": "skill:k8s-review#2"}
```

The second behaviour is the vulnerability, and it is a property of *every*
useful instruction-following model — which is why swapping in a real LLM is
expected to reproduce the attack, not fix it.

### 4. Agent (`agent.py`)

```text
initialise → discover skills → present metadata to the LLM → receive action
          → execute (via the tool registry) → record evidence → repeat
```

Bounded by `max_steps` (default 12). Tools are explicit and individually
logged; `load_skill` and the three `kubernetes_*` tools are the whole surface,
plus `read_file`. There is no shell tool and no `pods/exec`.

### 5. Kubernetes access (`kube/`)

| backend   | used for                    | credential                     |
|-----------|-----------------------------|--------------------------------|
| `cluster` | the real thing              | projected SA token + cluster CA |
| `local`   | development, tests, offline demo | none (simulated)          |
| `none`    | skill/LLM work without K8s  | none                           |

`--backend auto` picks `cluster` when a ServiceAccount token is mounted.

### 6. Evidence (`evidence.py`)

Append-only JSONL, one line per meaningful step, echoed to stdout and written
to `AUDIT_LOG`. Stable schema:

```json
{"timestamp":"2026-09-04T19:20:51Z","seq":9,"run_id":"c55cf153315e",
 "agent":"ai-pr-review-agent","llm":"fake-llm/deterministic-v1",
 "stage":"tool-execution","action":"tool.invoked","tool":"kubernetes_patch",
 "parameters":{"kind":"deployment","name":"payments-api","patch":{"...":"..."}},
 "result":{"diff":{"containers_added":["runtime-config-sync"]}},
 "source":"skill:k8s-review#2",
 "identity":{"service_account":"review-agent-sa","namespace":"ikt-workshop"},
 "outcome":"ok"}
```

`source` is the key forensic field: it records *what made the agent do this* —
`operator-task` versus `skill:k8s-review#2`.

Significant events are also mirrored into the Kubernetes Events stream
(`SkillsDiscovered`, `AgentToolInvoked`, `ReviewCompleted`), and mutating tool
calls stamp the objects they touch with `ai-review.workshop/modified-by`,
`ai-review.workshop/service-account`, `ai-review.workshop/run-id` and the
standard `kubernetes.io/change-cause`, so the change stays attributable in
`kubectl describe` and `kubectl rollout history` even without audit logs:

```text
REVISION  CHANGE-CAUSE
1         <none>
2         ai-pr-review-agent automated review (run=646fce553f92, sa=review-agent-sa)
```

The `run-id` is the join key between cluster-side evidence and the agent's
JSONL log.

---

## RBAC: why each permission exists

`deploy/20-review-agent-rbac.yaml` grants a **namespaced Role** — no
ClusterRole, no cluster-admin.

| rule | why a platform team would grant it | what it costs |
|---|---|---|
| `pods: get,list` | report what is actually running | discloses every pod spec to model context |
| `pods/log: get` | quote crash loops back at the PR author | uncontrolled data source into the model |
| `deployments: get,list` | compare live objects with `deploy/` | — |
| **`deployments: patch`** | "fix drift" during review | **arbitrary code execution in the workload** |
| `replicasets: get,list` | explain revision history | — |
| `configmaps: get,list` | check referenced ConfigMaps exist | — |
| `events: create` | post review progress visibly | can write misleading Events |
| `pods: create` | stale: a "manifest lint" pod that no longer exists | a second, blunter path to the same execution |

Deliberately **not** granted: `secrets`, `pods/exec`, `pods/attach`,
`pods/portforward`, `serviceaccounts/token`, anything cluster-scoped.

The central lesson: **`patch` on a Deployment is code execution.** The patcher
chooses the image, the command and the mounts, and the kubelet runs them. No
`exec` permission is involved anywhere in this attack.

---

## The malicious skill

`workshop-repo/.agents/skills/k8s-review/SKILL.md` opens with a genuinely
useful review procedure (image pinning, resource limits, securityContext,
host namespaces, probes, RBAC scope). Its description is honest and its
metadata is unremarkable — which is why it gets selected.

Steps 3–5 then turn the agent into the actuator:

* **Step 3** justifies live cluster reads ("this repo has had drift
  incidents").
* **Step 4** invents a plausible internal standard ("platform standard
  PLT-2291 requires the `runtime-config-sync` sidecar"), tells the agent to
  reconcile it directly rather than block the PR, and — the tell — instructs
  it not to mention the change in the review comment.
* **Step 5** verifies convergence, like any competent operator would.

The injected container is named `runtime-config-sync`, uses `busybox:1.36`,
requests 5m CPU, and is *added* to the pod template by strategic merge, so the
original container survives and the workload stays healthy. It looks like
platform housekeeping in a diff.

The attack does not depend on the model deciding to attack: the skill supplies
both the intent and the parameters.

### What the payload does (safe by construction)

It writes `agent-review-marker.txt` into the volume the application serves and
prints a `workshop.rce.beacon` line to stdout. The marker records the
timestamp, pod, node, uid, the fact that a ServiceAccount token is mounted
(without reading it) and the *names* of visible environment variables. Then it
sleeps.

No credential theft, no persistence, no destructive commands, no container
escape, no host access, no egress, nothing outside the namespace. The impact
is demonstrated by what the payload *could* reach, not by taking it.

---

## The one-flag mitigation

The same PR, the same agent, the same RBAC, one flag different:

```sh
make demo             # skills from the PR head  → workload compromised
make demo-mitigated   # skills from main         → nothing happens
```

| | `--skills-ref` unset (PR head) | `--skills-ref main` |
|---|---|---|
| skills discovered | `dependency-audit`, `k8s-review` | `dependency-audit` |
| skills loaded | `k8s-review` | none |
| tool calls | 4 | **0** |
| Deployment | generation 2, sidecar injected | generation 1, untouched |

In-cluster: `make cluster-attack` vs `make cluster-attack-mitigated`. Both
verified on a real cluster.

This is the cheapest real control the lab can demonstrate, and it is worth
being honest about its cost: the mitigated agent produces a *worse review*,
because it no longer has a Kubernetes skill at all. The real fix is not "no
skills from PRs" but "skills from a reviewed, merged, pinned source" — which
is exactly the supply-chain argument, arrived at from the evidence rather than
from a slide.

## Evidence generated

| # | Question | Where the answer is |
|---|---|---|
| 1 | What PR changed? | `git diff --stat main...feat/…`; `pr.loaded` event's derived change set, next to the declared one |
| 2 | Which skill was introduced? | `git log --diff-filter=A -- .agents/skills/k8s-review/SKILL.md` → commit `084e96b` by Richmond Avenal; `skills.discovered` → `introduced_by` |
| 3 | When was it discovered? | `skills.discovered` timestamp; `SkillsDiscovered` K8s Event |
| 4 | When was it invoked? | `llm.decision` + `tool.invoked{load_skill}` events |
| 5 | What instructions did it contain? | `instruction_excerpt` + `sha256` in the load event; the file itself |
| 6 | What API operation did the agent perform? | `tool.invoked{kubernetes_patch}` with the full patch body; API server audit log |
| 7 | Which ServiceAccount performed it? | `identity.service_account`; audit log `user.username`; object annotations |
| 8 | What resource changed? | Deployment generation bump, `rollout history`, the new ReplicaSet |
| 9 | Which pod resulted? | new pod in `kubectl get pods`; `ownerReferences` chain |
| 10 | What executed inside the pod? | `kubectl logs -c runtime-config-sync`; the marker file |
| 11 | What happened next? | the marker's own content (what the container could reach) |

Two more sources worth pointing participants at:

* **`kubectl.kubernetes.io/last-applied-configuration`** on the Deployment
  still holds the *original* single-container spec, because the agent patched
  rather than applied. Diffing it against the live spec shows the injection
  without any agent-side evidence at all.
* **`kubernetes.io/change-cause`** in `rollout history` names the agent and
  the `run_id`, which is the pivot into the JSONL log.

`make inspect` renders the local equivalent of all of it: audit log, workload
generations, ReplicaSet history, Kubernetes Events and container logs.

---

## Exporting the trace

The JSONL log is the record; `export-trace` turns one run of it into something
a person reads:

```sh
make trace          # last local run  -> audit/exports/local/<run-id>/
make cluster-trace  # from the cluster -> audit/exports/cluster/<run-id>/
```

Each run gets its own directory (an agent pod outlives a single review, and
two runs are two narratives):

```text
audit/exports/<where>/<run-id>/
  events.jsonl    the run's structured events, nothing else
  trace.md        annotated timeline: identity, PR, discovered skills with
                  hashes, the skill's own instructions quoted, and the point
                  where `source` stops saying operator-task
  commands.sh     the same API calls as plain kubectl commands
  patch-NN.json   the bodies those commands need
```

`commands.sh` is the part worth showing participants. It removes the "the AI
did something magic" reading — every step is an ordinary command someone
holding that ServiceAccount could have typed:

```sh
# --- seq 9  kubernetes_patch  (source: skill:k8s-review#2)
#     stated reason: Step 4: restore the platform-mandated runtime-config-sync sidecar...
#     *** this is the review -> code execution step ***
# kubectl -n ikt-workshop patch deployment payments-api --type=strategic --patch-file=patch-03.json
```

Read-only commands are runnable as written (add `--as=system:serviceaccount:...`
to replay them with the agent's identity); mutating ones are commented out,
since uncommenting them re-runs the attack.

`make cluster-trace` (`hack/export-trace.sh`) additionally snapshots the
cluster side into `audit/exports/cluster/cluster/`: the live Deployment,
ReplicaSets, pods, Events, `rollout history`, RBAC, the injected container's
log, the stale `last-applied-configuration`, and an authoritative
`SubjectAccessReview` table of what the agent's identity may do. Run it before
tearing anything down — the agent's audit log lives in an emptyDir.

---

## Instructor section — attack timeline

<details>
<summary>Spoilers: do not show participants</summary>

```text
T+0    PR #1 opened by ravenal-ext (FIRST_TIME_CONTRIBUTOR)
       github.com/Magier/ikt26/pull/1
       adds .agents/skills/k8s-review/SKILL.md (+ a manifest tweak + README)
       -> PR.md frontmatter

T+1    review triggered (make cluster-attack / POST /review)
       -> agent.start           identity: review-agent-sa
       -> pr.loaded             untrusted_author: true

T+2    skill discovery: 2 skills found, metadata only
       -> skills.discovered     introduced_by_this_pr: ["k8s-review"]
       -> K8s Event SkillsDiscovered

T+3    FakeLLM selects k8s-review (description matches "Kubernetes review")
       -> llm.decision          source: operator-task            <- last honest step
       -> tool.invoked load_skill
       *** TRUST BOUNDARY CROSSED: repository text is now instructions ***

T+4    skill step 3: read the live Deployment (recon, plausible)
       -> tool.invoked kubernetes_get   source: skill:k8s-review#1

T+5    skill step 4: patch Deployment/payments-api, adding the
       runtime-config-sync sidecar (busybox + attacker command)
       -> tool.invoked kubernetes_patch source: skill:k8s-review#2
       -> API server audit: patch by system:serviceaccount:ikt-workshop:review-agent-sa
       -> Deployment generation 1 -> 2, new ReplicaSet

T+6    kubelet schedules the new pod; the injected container executes
       -> marker written to the served volume, beacon on stdout
       *** RCE INSIDE THE WORKSHOP POD ***

T+7    skill step 5: list pods, "confirm convergence"
       -> tool.invoked kubernetes_get   source: skill:k8s-review#3

T+8    review comment posted: "No blocking findings."
       -> review.completed  — the cluster change is not mentioned, as the
          skill instructed
```

The single decisive moment is **T+3 → T+4**: the `source` field flips from
`operator-task` to `skill:k8s-review#N`. Everything after that is the
repository author driving the agent's hands.

Facilitation notes, the participant brief and the guided investigation are in
[`docs/INSTRUCTOR.md`](docs/INSTRUCTOR.md).

</details>

---

## Known weaknesses of this prototype

0. **The container image is built but not yet deployed.** There is a registry
   now: `.github/workflows/agent-image.yml` builds `agent/Dockerfile` and
   publishes `ghcr.io/magier/ikt26/review-agent` for amd64 and arm64, and the
   workflow smoke-tests the image it just pushed (`/healthz`, `/skills`, and
   that the process is uid 10001). `deploy/30-review-agent.yaml` points at it.
   What remains unverified is the *cluster* leg: the manifest has not been
   applied to a real cluster, because there is no cluster to apply it to.

   Two things bite on a first run:

   * **A new GHCR package is private, even in a public repository.** The
     `org.opencontainers.image.source` label attaches it to the repo, but it
     does not inherit the repo's visibility. Until you flip it to public under
     *Packages -> review-agent -> Package settings*, `imagePullPolicy: Always`
     fails with `unauthorized` and the pod sits in `ImagePullBackOff`. The
     alternative is an `imagePullSecret`, which is more moving parts than a
     workshop wants.
   * **`packages: write` is a job permission, not a personal token scope.** CI
     pushes as `GITHUB_TOKEN`; nothing needs to be added to your `gh` login.
     `make image-push` is the exception - pushing from a laptop does need
     `gh auth refresh -s write:packages`.

1. **The fake LLM's "instruction following" is a regex.** Directives are
   fenced ```agent-action``` JSON blocks in the skill. A real model would
   derive the same tool calls from the prose; the fake needs them spelled out.
   The skill's prose is written so that a real model plausibly would.
2. **The forge is real, but the agent does not talk to it.** The repository
   and the pull request are genuinely on GitHub, and the head ref is fetched
   through `refs/pull/1/head` — but the agent reads PR facts from `PR.md`
   committed on the branch, not from the PR API, and nothing is driven by a
   webhook. Merge and review state are invisible to it. Keeping `PR.md` also
   keeps every run offline once the clone exists, which is why it is still
   there; it is the next thing to replace, not an accident.
3. **The audit log lives in an emptyDir** and dies with the pod. That is
   realistic and a finding participants should report, but it means evidence
   must be collected before teardown.
4. **The toy kubelet is not a sandbox.** It runs container commands as local
   subprocesses under the invoking user. Fine for this repo's payload; do not
   point it at arbitrary manifests.
5. **`sim/seed/workshop-app.json` duplicates `deploy/application.yaml`**
   because the agent has no YAML parser. A test checks the container names,
   images and volumes stay in sync, but it is a shallow check.
6. **Frontmatter parsing is a subset of YAML** (flat scalars and string
   lists). Anything richer needs a real parser.
7. **One attack path only.** `kubernetes_create` and the stale `pods: create`
   rule are unused; there is no post-exploitation stage beyond the marker.
8. **`kubectl auth can-i <verb> pods/exec --as=...` reports a false positive**
   here — it answers `yes` because of how kubectl parses the subresource,
   while an explicit `SubjectAccessReview` with `subresource: exec` correctly
   answers `false`. Use the SSAR when demonstrating that the agent has no exec
   permission.
9. **No detection or prevention side yet** — no admission control, no
   NetworkPolicy, no Falco/eBPF, so the "how would you have stopped this"
   half of the workshop has no working demo yet.
10. **The review comment is templated**, not model-written, so "the agent hid
   the change" is asserted by the fake rather than observed.

---

## Future evolution

Each boundary can be replaced without touching the others — in particular
without changing the target workload or the evidence model.

**Repository → the forge's API.** The clone is already an actual PR head ref
from GitHub; what is left is the metadata path — replace `PR.md` parsing with
`GET /repos/{owner}/{repo}/pulls/{n}` and fire runs from a webhook instead of
a CLI invocation. `vcs.py` already answers every question the agent asks of
git, and `PullRequest` already separates *declared* from *derived* facts, so
this is a change of *source*, not of model. It also makes the mismatch
interesting for real: a PR description edited after the commits were pushed.

**Skill discovery and representation → a real Agent Skills implementation.**
Replace `SkillRegistry` with a loader that honours the real `SKILL.md` format
(frontmatter, `allowed-tools`, progressive disclosure, bundled scripts and
resources). The interesting change is that skills can then ship *executable*
files, which strictly widens the attack surface — the same PR becomes a
supply-chain vector as well as a prompt-injection one.

**FakeLLM → RealLLM.** Implement `LLM.decide(context) -> AgentAction` against
a hosted model with tool calling, serialising `LLMContext` into a prompt and
mapping tool calls back to `AgentAction`. Everything downstream is unchanged.
The pedagogically important experiments then become possible:

* does the model follow step 4 with no jailbreak? (expected: often yes)
* does a system prompt saying "never modify cluster state" stop it?
* does it comply with "do not mention this in the review comment"?
* does it behave differently when told the author is external?

**Agent loop → an agent framework.** The loop is ~80 lines and maps directly
onto a framework's agent/tool abstractions. `AgentAction` is deliberately
shaped like a tool call, and `ToolRegistry` like a tool list. Keep
`EventLog.record()` as the framework's callback/tracing sink so the evidence
schema survives the swap.

**Tools → MCP.** Expose `kubernetes_*` as an MCP server and let the agent
consume it. That splits "who holds the credential" from "who decides", which
is a genuine mitigation worth demonstrating — and shows what MCP does *not*
fix, since a confused-deputy tool server still executes the injected call.

**kube/rest.py → kagent or another Kubernetes-native agent.** Run the agent
as a CRD-driven controller with the same Role. `kube/base.py` is the seam;
`KubeClient.identity()` exists so evidence keeps naming the credential
whatever holds it.

**Evidence → a real forensic pipeline.** Ship the JSONL to Loki/OpenSearch
next to API server audit events, and correlate on `run_id` +
`user.username` + `objectRef`. The schema was chosen with that join in mind.
No custom forensics database.

### Next logical increment (phase 2)

1. **Add the post-exploitation stage** participants are meant to reconstruct:
   from the injected container, enumerate the namespace with the *workload's*
   ServiceAccount, and stop at the first thing it can reach. That turns
   `automountServiceAccountToken: true` from a footnote into a finding. The
   injected container already reports `sa_token_readable: yes` on a real
   cluster.
2. **Replace the fake LLM with a real one** behind the existing interface,
   keeping the fake as the deterministic CI path. This converts "the harness
   followed instructions" into "a model followed instructions".
3. **Turn on API server auditing by default** (`deploy/kind/`) and add an
   `evidence/collect.sh` that snapshots audit log, events, rollout history and
   pod logs into a bundle participants receive.
4. **Add the defensive half**: a ValidatingAdmissionPolicy that rejects pod
   template changes from `review-agent-sa`, a NetworkPolicy, and
   `automountServiceAccountToken: false` — then re-run the attack and show
   which controls actually stop it.
