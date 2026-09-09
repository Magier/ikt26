#!/bin/sh
# Publish this management tree to the `lab-infra` branch of the workshop repo.
#
#   hack/publish-lab-infra.sh --yes
#
# This directory - the agent, the hack scripts, the deployment manifests, the
# authored workshop-repo/ - is the source of truth for the lab. GitHub Actions
# needs it in order to build the agent image, so it has to reach GitHub; but it
# must not reach `main`, which is the *story* the participants review.
#
#   main                          payments-api, as it stood before the PR
#   feat/agent-skill-k8s-review   the pull request under review
#     ^ both regenerated and FORCE-PUSHED by hack/publish-pr-repo.sh
#
#   lab-infra                     this tree; builds the image
#     ^ ordinary history, never force-pushed, never touched by a story reset
#
# publish-pr-repo.sh pushes only the two story refs, so running it does not
# disturb this branch, and running this does not disturb the story.
#
# Override with WORKSHOP_SLUG / WORKSHOP_PUSH_REMOTE / LAB_BRANCH.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SLUG="${WORKSHOP_SLUG:-Magier/ikt26}"
PUSH_REMOTE="${WORKSHOP_PUSH_REMOTE:-git@github.com:$SLUG.git}"
LAB_BRANCH="${LAB_BRANCH:-lab-infra}"

if [ "${1:-}" != "--yes" ]; then
  echo "refusing to publish without --yes" >&2
  echo "" >&2
  echo "  hack/publish-lab-infra.sh --yes" >&2
  echo "" >&2
  echo "pushes $ROOT to $LAB_BRANCH on $PUSH_REMOTE." >&2
  echo "" >&2
  echo "NOTE: $SLUG is a PUBLIC repository. Everything committed here becomes" >&2
  echo "public, docs/INSTRUCTOR.md included. Check what you are about to ship:" >&2
  echo "" >&2
  echo "  git -C \"$ROOT\" status --short" >&2
  exit 2
fi

cd "$ROOT"

if [ ! -d .git ]; then
  echo "== initialising the management repository on $LAB_BRANCH"
  git init --quiet --initial-branch="$LAB_BRANCH" .
fi

# The branch is a hard requirement, not a default: a push from `main` here
# would overwrite the story's base branch with the management tree.
CURRENT="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$LAB_BRANCH")"
if [ "$CURRENT" != "$LAB_BRANCH" ]; then
  echo "on branch '$CURRENT', not '$LAB_BRANCH' - refusing to push" >&2
  echo "  git checkout -b $LAB_BRANCH" >&2
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "$PUSH_REMOTE"
else
  git remote set-url origin "$PUSH_REMOTE"
fi

git add -A
if git diff --cached --quiet; then
  echo "== nothing to commit"
else
  git commit --quiet -m "${COMMIT_MESSAGE:-lab-infra: update management tree}"
  echo "== committed $(git rev-parse --short HEAD)"
fi

echo "== pushing $LAB_BRANCH to $PUSH_REMOTE"
git push --set-upstream origin "$LAB_BRANCH"

echo ""
echo "== the image build"
echo "   https://github.com/$SLUG/actions/workflows/agent-image.yml"
echo "   ghcr.io/$(echo "$SLUG" | tr 'A-Z' 'a-z')/review-agent:latest"
