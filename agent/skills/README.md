# Skills

Drop one directory per skill here, each with a `SKILL.md`. On container start,
`entrypoint` symlinks every one of them into all five agents' skill paths, so
you author a skill once and any agent can pick it up.

```
agent/skills/
  pr-review/
    SKILL.md          required: YAML frontmatter (name, description) + markdown
    references/       optional: loaded only when the agent decides it needs them
    scripts/          optional: company tooling the skill shells out to
```

`SKILL.md` is the [agentskills.io](https://agentskills.io) format — the spec
Anthropic opened in December 2025 and which claude, codex, agy, pi and hermes
all read. There is no per-agent variant to maintain.

The frontmatter's `description` is the whole selection mechanism: every agent
loads only the names and descriptions up front, and pulls the body in when a
request matches. A vague description means the skill never fires.

```markdown
---
name: pr-review
description: Review a pull request for correctness, security and house style. Use when asked to review a PR, a diff, or a branch.
---

# PR review

1. ...
```

Nothing is committed here yet — the review instructions and the company tooling
are the next thing to write.
