# agentbox

A container with five coding agents in it. Check a repo out into `/workspace`,
point an agent at it, watch what it does. Driven from a shell or from a web UI
on `:8080` — both run the same two scripts, so neither can do anything the
other cannot.

This is the workbench, not the reviewer. The review instructions live in a
skill (`skills/`), which is still to be written.

| agent | command | source |
|---|---|---|
| Claude Code | `claude` | `@anthropic-ai/claude-code` |
| OpenAI Codex | `codex` | `@openai/codex` |
| Google Antigravity | `agy` | `antigravity.google/cli/install.sh` |
| pi | `pi` | `@mariozechner/pi-coding-agent` |
| Nous Hermes | `hermes` | `hermes-agent.nousresearch.com/install.sh` |

## Run it

The image is built by GitHub Actions and served from GHCR — there is no local
build step.

```sh
docker run -d --name agentbox -p 8080:8080 \
  -v "$PWD/agent/skills:/skills:ro" \
  -v agentbox-workspace:/workspace \
  --env-file agent.env \
  ghcr.io/magier/ikt26/agentbox:latest
```

Then either:

```sh
docker exec -it agentbox bash          # shell: checkout, then run an agent's TUI
open http://localhost:8080             # web UI: same thing, streamed into a page
```

A new GHCR package is **private** even when the repository is public — flip it
under *Packages → agentbox → Package settings*, or the pull fails.

## Use it

Two commands, on `PATH` in the shell and behind the buttons in the UI:

```sh
checkout https://github.com/Magier/ikt26          # clone or update into /workspace
checkout https://github.com/Magier/ikt26 pr/1     # ...at a pull request
checkout https://github.com/Magier/ikt26 my-branch

agent-run list                                    # what is installed, and which version
agent-run claude                                  # interactive TUI, shell only
agent-run claude "review the diff against main"   # one-shot, streams and exits
```

`agent-run` exists so the same prompt can be thrown at all five without
remembering five sets of flags — which is the point of having five.

The web UI cannot drive an interactive TUI; give it a prompt, or use the shell.

## Auth

Either works, and they can be mixed:

**API keys** — an `agent.env` passed with `--env-file`:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
GH_TOKEN=...
```

**Existing logins** — mount the credential directories instead, and each agent
uses the subscription you already logged into:

```sh
-v "$HOME/.claude:/root/.claude" \
-v "$HOME/.codex:/root/.codex"
```

`GH_TOKEN` is what `checkout` needs for private repos and what an agent needs to
post a review. Without it, public clones still work.

## Skills

One `SKILL.md` standard, five different directories to put it in. `entrypoint`
resolves that by symlinking everything under `/skills` into all of them at
start, preserving each agent's own bundled skills. See [`skills/`](skills/).

## Installing things at runtime

Deliberately unrestricted — the container runs as root, `apt`, `npm -g`, `pip`
and `uv` all work, and Debian's `EXTERNALLY-MANAGED` marker is removed so `pip
install` does not need a flag. Company tooling can be installed straight into a
running container while you work out what it needs; fold it into the Dockerfile
once it settles.

Anything installed at runtime lives and dies with the container. `/workspace` is
a named volume and survives.

## Not hardened

No sandbox, no auth on the web UI, agents run with their approval prompts
bypassed, everything as root. That is the current, deliberate state — hardening
comes after the thing works. Reach it over a port-forward on a host you own; do
not put an Ingress in front of it.

The single place that encodes "no sandbox" is the `case` block at the bottom of
[`bin/agent-run`](bin/agent-run).

## Not yet verified

The image has never been built — it goes to CI first. Two things to expect on
the first green build:

- **Headless flags** in `bin/agent-run` come from each vendor's docs, not from a
  running binary. `agent-run list` is the quick check; expect to correct a line.
- **pi's skills directory** is a guess (`~/.pi/skills`). The other four are
  confirmed. Check with `pi --help` in the container and fix
  `AGENT_SKILL_DIRS` in `entrypoint.sh`.

The CI smoke test fails the build if any agent is missing, so a broken vendor
installer shows up as a red workflow rather than a surprise in a shell.

## Layout

```
Dockerfile          node:22-bookworm + the five agents + runtime-install tooling
entrypoint.sh       symlinks /skills into every agent's skills path
server.py           the web UI - Python standard library only
bin/checkout        clone or update a repo into /workspace
bin/agent-run       one way to start any of the five
skills/             SKILL.md directories, shared by all agents
```
