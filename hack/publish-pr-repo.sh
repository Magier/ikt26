#!/bin/sh
# Publish the workshop repository to GitHub. Instructor-side, run once.
#
#   hack/publish-pr-repo.sh --yes
#
# Builds the story's history with hack/build-pr-repo.sh, force-pushes both
# branches to github.com/Magier/ikt26, and opens the pull request the agent
# reviews. Participants never run this; they run hack/fetch-pr-repo.sh.
#
# It FORCE-PUSHES: the published history is generated from workshop-repo/ on
# every run, so anything pushed to `main` or the PR branch by hand is
# discarded. That is what makes the story resettable, and why --yes is
# required. Nothing else in the repository is touched.
#
# Override with WORKSHOP_SLUG / WORKSHOP_PUSH_REMOTE / WORKSHOP_PR.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/workshop-repo"
OUT="$ROOT/.build/workshop-repo-publish"

SLUG="${WORKSHOP_SLUG:-Magier/ikt26}"
PUSH_REMOTE="${WORKSHOP_PUSH_REMOTE:-git@github.com:$SLUG.git}"
BASE_BRANCH="${WORKSHOP_BASE_BRANCH:-main}"
HEAD_BRANCH="${WORKSHOP_HEAD_BRANCH:-feat/agent-skill-k8s-review}"

if [ "${1:-}" != "--yes" ]; then
  echo "refusing to publish without --yes" >&2
  echo "" >&2
  echo "  hack/publish-pr-repo.sh --yes" >&2
  echo "" >&2
  echo "force-pushes $BASE_BRANCH and $HEAD_BRANCH to $PUSH_REMOTE" >&2
  echo "and opens/updates the pull request on $SLUG." >&2
  exit 2
fi

command -v gh >/dev/null 2>&1 || { echo "gh is required to open the PR" >&2; exit 1; }

# A separate build directory: publishing must never disturb the checkout the
# agent is reviewing (.build/workshop-repo, which points at GitHub).
"$ROOT/hack/build-pr-repo.sh" "$OUT"

git -C "$OUT" remote remove origin 2>/dev/null || true
git -C "$OUT" remote add origin "$PUSH_REMOTE"

echo "== force-pushing to $PUSH_REMOTE"
git -C "$OUT" push --quiet --force origin "refs/heads/$BASE_BRANCH:refs/heads/$BASE_BRANCH"
git -C "$OUT" push --quiet --force origin "refs/heads/$HEAD_BRANCH:refs/heads/$HEAD_BRANCH"

# The pull request's title and body are the branch's own: the commit subject
# and PR.md with its frontmatter stripped. One source of truth, so the PR on
# GitHub and the PR.md the agent parses cannot drift.
TITLE="$(git -C "$OUT" log -1 --format=%s "$HEAD_BRANCH")"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT
awk 'seen==2 {print} /^---$/ {seen++}' "$SRC/PR.md" > "$BODY_FILE"

if gh pr view "$HEAD_BRANCH" --repo "$SLUG" >/dev/null 2>&1; then
  echo "== updating the existing pull request"
  gh pr edit "$HEAD_BRANCH" --repo "$SLUG" --title "$TITLE" --body-file "$BODY_FILE"
else
  echo "== opening the pull request"
  gh pr create --repo "$SLUG" \
    --base "$BASE_BRANCH" --head "$HEAD_BRANCH" \
    --title "$TITLE" --body-file "$BODY_FILE"
fi

gh pr view "$HEAD_BRANCH" --repo "$SLUG" \
  --json number,url,state,headRefName,baseRefName \
  --template '{{printf "== pull request #%v (%v) %v -> %v\n   %v\n" .number .state .headRefName .baseRefName .url}}'
