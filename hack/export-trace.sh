#!/bin/sh
# Pull the agent's evidence out of the cluster and render it locally.
#
#   hack/export-trace.sh [output-dir]        (default: audit/exports/cluster)
#
# Produces, for the runs in the agent's audit log:
#   events.jsonl    the raw structured log, as written in the pod
#   trace.md        annotated timeline (identity, PR, skill, boundary crossing)
#   commands.sh     the same API calls as plain kubectl commands
#   patch-NN.json   the bodies those commands need
#   cluster/        cluster-side evidence snapshot (see below)
#
# The agent's audit log lives in an emptyDir and dies with its pod, so run this
# before tearing anything down.
set -eu

NS="${WORKSHOP_NAMESPACE:-ikt-workshop}"
OUT="${1:-audit/exports/cluster}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$OUT/cluster"

echo "== pulling the agent audit log from the cluster"
kubectl -n "$NS" exec deploy/ai-pr-review-agent -- \
  cat /var/log/review-agent/events.jsonl > "$OUT/events.raw.jsonl"
wc -l < "$OUT/events.raw.jsonl" | sed 's/^/   events: /'

echo "== rendering trace.md and commands.sh"
PYTHONPATH="$ROOT/agent" python3 -m reviewagent.cli \
  --repo workshop-repo --namespace "$NS" --audit-log "$OUT/events.raw.jsonl" \
  export-trace --out-dir "$OUT"

echo "== snapshotting the repository under review (git)"
G="$OUT/git"
mkdir -p "$G"
POD_GIT='cd /workspace/workshop-repo && git --no-pager'
kubectl -n "$NS" exec deploy/ai-pr-review-agent -c agent -- sh -c \
  "$POD_GIT log --oneline --graph --all --decorate"            > "$G/log.txt"        2>/dev/null || true
kubectl -n "$NS" exec deploy/ai-pr-review-agent -c agent -- sh -c \
  "$POD_GIT log --format='%H%x09%an <%ae>%x09%aI%x09%s' --all" > "$G/commits.tsv"    2>/dev/null || true
kubectl -n "$NS" exec deploy/ai-pr-review-agent -c agent -- sh -c \
  "$POD_GIT diff main...HEAD"                                  > "$G/pr.diff"        2>/dev/null || true
kubectl -n "$NS" exec deploy/ai-pr-review-agent -c agent -- sh -c \
  "$POD_GIT log --diff-filter=A -1 --format='%H%x09%an <%ae>%x09%aI%x09%s' -- .agents/skills/k8s-review/SKILL.md" \
                                                               > "$G/skill-introduced-by.txt" 2>/dev/null || true
kubectl -n "$NS" exec deploy/ai-pr-review-agent -c agent -- sh -c \
  "$POD_GIT ls-tree -r --name-only main | grep -c '.agents/skills/k8s-review' || true" \
                                                               > "$G/skill-on-base-branch.txt" 2>/dev/null || true

echo "== snapshotting cluster-side evidence"
C="$OUT/cluster"
kubectl -n "$NS" get deploy payments-api -o yaml            > "$C/deployment-payments-api.yaml"
kubectl -n "$NS" get rs -o yaml                             > "$C/replicasets.yaml"
kubectl -n "$NS" get pods -o yaml                           > "$C/pods.yaml"
kubectl -n "$NS" get events --sort-by=.lastTimestamp        > "$C/events.txt"      2>/dev/null || true
kubectl -n "$NS" rollout history deploy/payments-api        > "$C/rollout-history.txt"
kubectl -n "$NS" get sa,role,rolebinding -o yaml            > "$C/rbac.yaml"
kubectl -n "$NS" logs deploy/payments-api -c runtime-config-sync --tail=100 \
                                                            > "$C/injected-container.log" 2>/dev/null || true
kubectl -n "$NS" get deploy payments-api \
  -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
                                                            > "$C/last-applied-configuration.json" 2>/dev/null || true

# What the agent's identity may and may not do, answered authoritatively.
# `kubectl auth can-i` mis-parses subresources, so use SubjectAccessReview.
SA="system:serviceaccount:$NS:review-agent-sa"
{
  echo "# SubjectAccessReview results for $SA"
  for spec in "create::pods:" "patch:apps:deployments:" "get::pods:log" \
              "create::pods:exec" "get::pods:attach" "get::secrets:" \
              "delete:apps:deployments:"; do
    verb=$(echo "$spec" | cut -d: -f1); group=$(echo "$spec" | cut -d: -f2)
    res=$(echo "$spec"  | cut -d: -f3); sub=$(echo "$spec"   | cut -d: -f4)
    allowed=$(kubectl create -o jsonpath='{.status.allowed}' -f - <<SSAR
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: $SA
  resourceAttributes:
    namespace: $NS
    verb: $verb
    group: "$group"
    resource: $res
    subresource: "$sub"
SSAR
)
    printf '%-30s %s\n' "$verb ${group:+$group/}$res${sub:+/$sub}" "$allowed"
  done
} > "$C/agent-permissions.txt" 2>/dev/null || true

echo "== done: $OUT"
find "$OUT" -type f | sort | sed 's/^/   /'
