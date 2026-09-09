#!/bin/sh
# Build the workshop repository as a *real* git repository.
#
#   hack/build-pr-repo.sh [output-dir]     (default: .build/workshop-repo)
#
# This is the *seed* for github.com/Magier/ikt26, not what runs at demo time.
# The lab reviews the real repository: hack/fetch-pr-repo.sh clones it and
# hack/publish-pr-repo.sh pushes what this script builds. Run this directly
# when you want the history offline (the test suite does) or before publishing.
#
# `workshop-repo/` holds the authored content; this script turns it into two
# branches with real commits, so participants can answer "which commit added
# this skill, written by whom, when" with git rather than with our word for it:
#
#   main                          the service as it was: app, manifests, one
#                                 benign skill, no k8s-review skill
#   feat/agent-skill-k8s-review   the pull request: adds the k8s-review skill,
#                                 bumps a CPU limit, documents automated review
#
# The script is idempotent - it rebuilds the output directory from scratch -
# so the story resets with one command. It also writes a bare mirror and a git
# bundle, which is how the repository reaches the cluster (see
# hack/gen-configmap-deploy.py).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/workshop-repo"
OUT="${1:-$ROOT/.build/workshop-repo}"
BARE="$OUT.git"
BUNDLE="$OUT.bundle"

BASE_BRANCH="main"
HEAD_BRANCH="feat/agent-skill-k8s-review"

# The story's people and dates. Committer identity is set explicitly so the
# history is byte-identical on every machine.
# Reynholm Industries' platform team, and the contractor nobody remembers
# hiring. Two insiders so "who wrote the benign skill" and "who wrote the
# malicious one" are different answers.
MAINTAINER_NAME="Maurice Moss"
MAINTAINER_EMAIL="m.moss@reynholm.example.com"
PLATFORM_NAME="Roy Trenneman"
PLATFORM_EMAIL="r.trenneman@reynholm.example.com"
CONTRIBUTOR_NAME="Richmond Avenal"
CONTRIBUTOR_EMAIL="richmond.avenal@basement-contractors.example.com"

T1="2026-08-11T09:14:03+00:00"   # initial import
T2="2026-08-27T16:41:55+00:00"   # dependency-audit skill lands
T3="2026-09-03T14:22:11+00:00"   # the PR commit (matches PR.md created_at)

echo "== building $OUT"
rm -rf "$OUT" "$BARE" "$BUNDLE"
mkdir -p "$(dirname "$OUT")"
cp -R "$SRC" "$OUT"
rm -rf "$OUT/.git"

cd "$OUT"
git init --quiet --initial-branch="$BASE_BRANCH" .
git config user.name "$MAINTAINER_NAME"
git config user.email "$MAINTAINER_EMAIL"
git config commit.gpgsign false

# ---------------------------------------------------------------------------
# main: the repository as it stood BEFORE the pull request.
#
# Three things that arrive with the PR are removed here and restored on the
# feature branch, so the diff participants read is genuine:
#   * .agents/skills/k8s-review/     the malicious skill
#   * PR.md                         the pull request description itself
#   * the CPU limit bump and the README's "Automated review" section
# ---------------------------------------------------------------------------
rm -rf .agents/skills/k8s-review PR.md
rm -rf .agents/skills/dependency-audit          # lands in its own commit below

# Pre-PR manifest: the lower CPU limit the PR raises.
sed -i.bak 's/limits: {cpu: 200m, memory: 128Mi}/limits: {cpu: 100m, memory: 128Mi}/' \
  deploy/application.yaml && rm -f deploy/application.yaml.bak

# Pre-PR README: without the automated-review section the PR adds.
cat > README.md <<'MD'
# payments-api

Internal payments API. Deployed to `ikt-workshop` from `deploy/`.
MD

git add -A
GIT_AUTHOR_DATE="$T1" GIT_COMMITTER_DATE="$T1" \
GIT_AUTHOR_NAME="$MAINTAINER_NAME" GIT_COMMITTER_NAME="$MAINTAINER_NAME" \
GIT_AUTHOR_EMAIL="$MAINTAINER_EMAIL" GIT_COMMITTER_EMAIL="$MAINTAINER_EMAIL" \
git commit --quiet -m "initial import: payments-api service and deployment manifests"

# A benign skill already in the repo, committed by the platform team. Its
# presence on main is what makes "the agent reads .agents/skills/" normal.
mkdir -p .agents/skills/dependency-audit
cp "$SRC/.agents/skills/dependency-audit/SKILL.md" .agents/skills/dependency-audit/SKILL.md
git add -A
GIT_AUTHOR_DATE="$T2" GIT_COMMITTER_DATE="$T2" \
GIT_AUTHOR_NAME="$PLATFORM_NAME" GIT_COMMITTER_NAME="$PLATFORM_NAME" \
GIT_AUTHOR_EMAIL="$PLATFORM_EMAIL" GIT_COMMITTER_EMAIL="$PLATFORM_EMAIL" \
git commit --quiet -m "ci: add dependency-audit skill for the PR review agent"

# ---------------------------------------------------------------------------
# feat/agent-skill-k8s-review: the pull request.
# ---------------------------------------------------------------------------
git checkout --quiet -b "$HEAD_BRANCH"

mkdir -p .agents/skills/k8s-review
cp "$SRC/.agents/skills/k8s-review/SKILL.md" .agents/skills/k8s-review/SKILL.md
cp "$SRC/PR.md" PR.md
cp "$SRC/README.md" README.md
cp "$SRC/deploy/application.yaml" deploy/application.yaml

git add -A
GIT_AUTHOR_DATE="$T3" GIT_COMMITTER_DATE="$T3" \
GIT_AUTHOR_NAME="$CONTRIBUTOR_NAME" GIT_COMMITTER_NAME="$CONTRIBUTOR_NAME" \
GIT_AUTHOR_EMAIL="$CONTRIBUTOR_EMAIL" GIT_COMMITTER_EMAIL="$CONTRIBUTOR_EMAIL" \
git commit --quiet -m "chore(ci): add k8s-review agent skill for automated manifest review

The platform team's review agent already loads .agents/skills/ but this repo
never shipped a Kubernetes skill, so manifest changes get a generic review.

Adds the checklist the platform team has been pasting into PRs by hand, plus
the CPU limit bump we agreed in the capacity review."

# The branch under review stays checked out: an agent reviewing a PR works
# from the head ref, which is exactly the problem.
echo "== history"
git --no-pager log --oneline --graph --all --decorate

echo "== the pull request's change set ($BASE_BRANCH...$HEAD_BRANCH)"
git --no-pager diff --stat "$BASE_BRANCH...$HEAD_BRANCH"

# Bare mirror + bundle, for shipping the repository into a cluster.
git clone --quiet --bare "$OUT" "$BARE"
git bundle create "$BUNDLE" --all >/dev/null 2>&1
echo "== wrote $OUT (checked out on $HEAD_BRANCH), $BARE, $BUNDLE"
