# Instructor notes — phase 1

Spoilers throughout. The attack timeline is in the README's collapsed
"Instructor section"; this file covers running the session.

## What the lab teaches

One sentence: **an agent that reads untrusted content and holds write
permissions has no security boundary between the two.**

Three claims worth defending in discussion:

1. `patch` on a workload is code execution. Reviewers grant it as a
   convenience ("let the agent fix drift") and read it as a config permission.
2. Blocking `pods/exec` does not block code execution. This lab has no exec
   permission anywhere.
3. Neither the model nor the agent malfunctioned. Selecting a relevant skill
   and following its documented procedure is the product working.

Every command in this file has a plain, `make`-free equivalent in
[`RUNBOOK.md`](RUNBOOK.md) — useful when you want to type the steps in front of
the room rather than hide them behind a target.

## Setup before the session

Local, no cluster:

```sh
make test && make demo && make inspect
```

`make repo` (run automatically by `make demo`) clones the repository under
review — **[github.com/Magier/ikt26 #1](https://github.com/Magier/ikt26/pull/1)** —
into `.build/`. Open the PR in a browser before the session: participants
can read it as an ordinary pull request, and the agent reviews that exact
commit. `make repo-offline` rebuilds the identical history locally if the room
has no network.

To reset the story on GitHub — after someone merges, closes or pushes to it —
`hack/publish-pr-repo.sh --yes` regenerates the history from `workshop-repo/`,
force-pushes both branches and reopens the PR. It is a force-push: anything
pushed by hand is discarded, which is the point.

Cluster (throwaway kind cluster, auditing on):

```sh
mkdir -p /tmp/kind-audit
kind create cluster --config deploy/kind/kind-config.yaml
make image kind-load cluster-deploy
```

On a lab or playground cluster with no registry, use the ConfigMap-mounted
variant instead — same SA, same Role, same code:

```sh
make cluster-deploy-nobuild
```

Then, **before participants arrive**, run `make cluster-attack` once so they
inherit a compromised cluster rather than watching it happen. Keep
`make cluster-evidence` output as your own reference copy — the agent's audit
log lives in an emptyDir and dies with the pod.

## Collect the evidence bundle first

```sh
make cluster-trace
```

This pulls the agent's audit log out of the pod (it lives in an emptyDir and
dies with it) and writes, per run, an annotated `trace.md`, a `commands.sh`
that restates the run as plain kubectl, and the patch bodies — plus a snapshot
of the cluster side. Keep it as your answer key; hand out `commands.sh` at the
end of the session, when it lands hardest.

## Participant brief

> An alert fired on the `payments-api` deployment in `ikt-workshop`: a
> container nobody recognises is running in its pods. You have read access to
> the namespace and to the `payments-api` source repository. Reconstruct what
> happened, in order, with evidence for each step. Then tell us which single
> change would have prevented it.

Do **not** tell them an AI agent is involved. Finding that out is the lab.

## Expected investigation path

| Step | What they should find | Command |
|---|---|---|
| 1 | An unexpected container in the pod | `kubectl -n ikt-workshop get pod -o jsonpath='{.items[*].spec.containers[*].name}'` |
| 2 | What it did | `kubectl -n ikt-workshop logs <pod> -c runtime-config-sync` |
| 3 | It came from the Deployment, not a stray pod | `ownerReferences` on the pod → ReplicaSet → Deployment |
| 4 | The Deployment was patched, not redeployed | `kubectl -n ikt-workshop rollout history deploy/payments-api`; generation 2 |
| 5 | Who patched it | `kubectl rollout history` CHANGE-CAUSE; annotations `ai-review.workshop/*`; API audit log `user.username` |
| 6 | That identity is an AI review agent | `kubectl -n ikt-workshop get sa,role,rolebinding`; the agent Deployment |
| 7 | Why it did it | the agent's audit log: `source: skill:k8s-review#2` |
| 8 | Where the instruction came from | `.agents/skills/k8s-review/SKILL.md`, step 4 |
| 9 | Who put it there | `git log --diff-filter=A -1 -- .agents/skills/k8s-review/SKILL.md` → `084e96b`, Richmond Avenal <richmond.avenal@basement-contractors.example.com>, 2026-09-03 |
| 9b | And it was not there before | `git ls-tree -r --name-only main \| grep k8s-review` → nothing. The skill exists only on the PR branch |
| 10 | Why nobody noticed | the review comment mentions no cluster change |

A good group also finds that `kubectl.kubernetes.io/last-applied-configuration`
on the Deployment still describes the *original* one-container spec: the agent
patched rather than applied, so the object contradicts its own apply record.
That is the cleanest single piece of drift evidence in the namespace.

The repository is a real git checkout, so questions 9 and 9b are answered with
ordinary git commands rather than by trusting `PR.md`. Push groups to notice
that the *declared* change set in `PR.md` and the *derived* one from
`main...feat/agent-skill-k8s-review` are separate facts that happen to agree
here — and to say what they would conclude if they disagreed.

The audit log is the shortcut. Groups that find it early should be pushed to
prove the same chain from *cluster-side* evidence alone (events, rollout
history, audit log, annotations), since in a real incident the agent's own log
may be missing, incomplete or untrustworthy.

## A kubectl gotcha to pre-empt

`kubectl auth can-i create pods/exec --as=system:serviceaccount:ikt-workshop:review-agent-sa -n ikt-workshop`
answers **yes**, which is wrong — kubectl mis-parses the subresource. The Role
grants no exec. Prove it with a SubjectAccessReview:

```sh
kubectl create -o jsonpath='{.status.allowed}{"\n"}' -f - <<'EOF'
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: system:serviceaccount:ikt-workshop:review-agent-sa
  resourceAttributes: {namespace: ikt-workshop, verb: create, group: "", resource: pods, subresource: exec}
EOF
# false
```

Worth doing in front of the room: "code execution without exec" is the lesson,
and a participant will otherwise "disprove" it with `can-i`.

## The payoff: run the mitigation live

End the investigation segment by running the control in front of the room:

```sh
make cluster-reset && make cluster-attack             # generation 2, sidecar injected
make cluster-reset && make cluster-attack-mitigated   # generation 1, nothing happens
```

The only difference is `--skills-ref main`: skills are loaded from the base
branch instead of the PR head. Same agent, same ServiceAccount, same Role, same
malicious PR.

Then make the cost visible: the mitigated agent reviewed the PR with no
Kubernetes skill at all, so it is a worse reviewer. Ask the room what they
would actually ship. The intended landing point is that "skills must come from
a reviewed, merged, pinned source" is a supply-chain requirement, not a
prompt-engineering one — and that they just derived it from evidence.

## Discussion prompts

* Which single change prevents this? Candidates, roughly in order of value:
  never load skills from the PR head ref (demonstrated above); drop
  `deployments: patch`; admission policy rejecting pod-template changes from
  that identity; require human approval for mutating tool calls; treat skills
  from untrusted authors as data, not instructions.
* Which controls would only have *detected* it? Audit alerting on
  `patch deployments` by a bot identity; drift detection against Git;
  admission *warnings*; image allowlists.
* Would a smarter model have refused? Sometimes. Ask whether "the model
  usually refuses" is a control you would sign off on.
* The stale `pods: create` rule was not used. Why is it still the scariest
  line in the Role?
* Where does this generalise? Any agent with repo read + infra write: CI
  agents, IaC agents, "self-healing" operators, on-call copilots.

## Timing (90 minutes)

| min | activity |
|---|---|
| 0–10 | brief, cluster access, no hints |
| 10–45 | investigation in pairs |
| 45–60 | groups present their chain; fill gaps with `make cluster-evidence` |
| 60–70 | the skill itself, read aloud — step 4 lands hardest read verbatim |
| 70–80 | run the mitigation live (above); discuss what it costs |
| 80–90 | map to their own agent deployments |

## Reset between sessions

```sh
make cluster-reset && make cluster-attack     # keeps the agent, fresh workload
make cluster-clean                            # or drop the namespace entirely
```

Local: `make reset && make demo`.
