#!/usr/bin/env bash
# Wires the shared skills directory into each agent, then runs the CMD.
set -euo pipefail

SKILLS_DIR="${SKILLS_DIR:-/skills}"

# SKILL.md is one standard (agentskills.io), but every agent looks somewhere
# different. Rather than pick a winner, link each skill into all of them - you
# author once under agent/skills/ and whichever agent you try can see it.
#
# Per-skill symlinks, not one symlinked directory: hermes and friends ship
# bundled skills in these paths, and replacing the directory would hide them.
AGENT_SKILL_DIRS=(
  "$HOME/.claude/skills"                  # claude code
  "$HOME/.codex/skills"                   # openai codex
  "$HOME/.hermes/skills"                  # hermes
  "$HOME/.pi/agent/skills"                # pi - note the /agent/, .pi/skills is project-local
  "$HOME/.agents/skills"                  # vendor-neutral path, read by pi and agy
  # Published guides disagree on which of these two agy uses. Linking both
  # costs two symlinks and saves an afternoon of wondering why it sees nothing.
  "$HOME/.gemini/antigravity/skills"
  "$HOME/.gemini/antigravity-cli/skills"
)

link_skills() {
  shopt -s nullglob
  local skills=("$SKILLS_DIR"/*/)
  [ ${#skills[@]} -eq 0 ] && return 0

  for dir in "${AGENT_SKILL_DIRS[@]}"; do
    mkdir -p "$dir"
    for skill in "${skills[@]}"; do
      ln -sfn "${skill%/}" "$dir/$(basename "$skill")"
    done
  done
  echo "skills: linked ${#skills[@]} into ${#AGENT_SKILL_DIRS[@]} agent dirs" >&2
}

link_skills

exec "$@"
