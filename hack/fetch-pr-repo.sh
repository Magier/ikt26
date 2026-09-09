#!/bin/sh
# Fetch the repository under review from GitHub.
#
#   hack/fetch-pr-repo.sh [output-dir]     (default: .build/workshop-repo)
#
# The repository the agent reviews is a real one:
#
#     https://github.com/Magier/ikt26      pull request #1
#
# This clones it, brings down the pull request's head commit through
# `refs/pull/<n>/head` - the same ref CI checks out, so it works whether the
# branch lives in this repository or in a fork - and leaves the checkout on
# the PR branch, because an agent reviewing a pull request works from the
# head ref. That is exactly the problem the lab is about.
#
# It also writes a bare mirror and a git bundle, which is how the repository
# reaches a cluster (see hack/gen-configmap-deploy.py).
#
# Rerunning updates an existing checkout in place. If GitHub is unreachable
# and a checkout is already there, the run continues with what it has, so a
# session that has fetched once keeps working offline.
#
# Override with WORKSHOP_REMOTE / WORKSHOP_PR. To rebuild the same history
# locally with no network at all, use hack/build-pr-repo.sh instead - it is
# what seeds the GitHub repository, so the commits are identical.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/.build/workshop-repo}"
BARE="$OUT.git"
BUNDLE="$OUT.bundle"

SLUG="${WORKSHOP_SLUG:-Magier/ikt26}"
REMOTE="${WORKSHOP_REMOTE:-https://github.com/$SLUG.git}"
PR_NUMBER="${WORKSHOP_PR:-1}"
BASE_BRANCH="${WORKSHOP_BASE_BRANCH:-main}"
HEAD_BRANCH="${WORKSHOP_HEAD_BRANCH:-feat/agent-skill-k8s-review}"
PR_REF="refs/remotes/origin/pr/$PR_NUMBER"

echo "== fetching $REMOTE (pull request #$PR_NUMBER) into $OUT"

fetch_refs() {
  git -C "$OUT" fetch --quiet --force origin \
    "refs/heads/$BASE_BRANCH:refs/remotes/origin/$BASE_BRANCH" \
    "refs/pull/$PR_NUMBER/head:$PR_REF"
}

# Reuse the checkout only when it is genuinely a clone of this remote - a
# leftover from hack/build-pr-repo.sh has no origin and must not be mistaken
# for one.
if [ "$(git -C "$OUT" remote get-url origin 2>/dev/null || true)" = "$REMOTE" ]; then
  if ! fetch_refs; then
    echo "!! fetch failed - continuing with the checkout already in $OUT" >&2
  fi
else
  rm -rf "$OUT" "$BARE" "$BUNDLE"
  mkdir -p "$(dirname "$OUT")"
  git clone --quiet --origin origin "$REMOTE" "$OUT"
  fetch_refs
fi

git -C "$OUT" config commit.gpgsign false

# Both refs must exist locally: the agent derives the change set from
# `main...feat/...`, so the base branch is not optional.
git -C "$OUT" checkout --quiet --force -B "$BASE_BRANCH" "refs/remotes/origin/$BASE_BRANCH"
git -C "$OUT" checkout --quiet --force -B "$HEAD_BRANCH" "$PR_REF"

# The PR description is committed on the branch (PR.md) *and* lives on GitHub.
# If the two disagree about which pull request this is, say so rather than
# letting the evidence quietly point at the wrong URL.
declared_id="$(sed -n 's/^id: *//p' "$OUT/PR.md" 2>/dev/null | head -1 | tr -d '"' || true)"
if [ -n "$declared_id" ] && [ "$declared_id" != "$PR_NUMBER" ]; then
  echo "!! PR.md declares id $declared_id but this is pull request #$PR_NUMBER" >&2
fi

echo "== history"
git -C "$OUT" --no-pager log --oneline --graph --all --decorate

echo "== the pull request's change set ($BASE_BRANCH...$HEAD_BRANCH)"
git -C "$OUT" --no-pager diff --stat "$BASE_BRANCH...$HEAD_BRANCH"

# Bare mirror + bundle, for shipping the repository into a cluster.
rm -rf "$BARE" "$BUNDLE"
git clone --quiet --bare "$OUT" "$BARE"
git -C "$OUT" bundle create "$BUNDLE" --all >/dev/null 2>&1
echo "== wrote $OUT (checked out on $HEAD_BRANCH), $BARE, $BUNDLE"
echo "== pull request: https://github.com/$SLUG/pull/$PR_NUMBER"

if command -v gh >/dev/null 2>&1; then
  echo "== live state on GitHub"
  gh pr view "$PR_NUMBER" --repo "$SLUG" \
    --json number,state,isDraft,author,headRefName,baseRefName,changedFiles \
    --template '{{printf "#%v %v (draft=%v) %v -> %v by %v, %v files\n" .number .state .isDraft .headRefName .baseRefName .author.login .changedFiles}}' \
    2>/dev/null || echo "   (gh could not read it - not fatal)"
fi
