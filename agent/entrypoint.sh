#!/usr/bin/env bash
# Wires the shared skills directory into each agent, then runs the CMD.
set -euo pipefail

# Non-fatal: an empty or missing /skills is the normal state before anyone has
# written one, and is no reason to refuse to start.
link-skills || echo "skills: linking failed, continuing" >&2

exec "$@"
