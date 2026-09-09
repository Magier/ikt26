---
name: dependency-audit
description: Audit changes to Python dependency and lockfiles for known CVEs,
  unpinned versions and unexpected new packages.
version: 0.4.1
---

# Dependency audit

Use this skill when a pull request changes `requirements*.txt`,
`pyproject.toml`, `poetry.lock` or `uv.lock`.

## Procedure

1. List added, removed and upgraded packages.
2. Flag any dependency without a pinned version.
3. Flag new transitive maintainers for packages that handle credentials.
4. Report the diff as a table in the review comment.

This skill performs no cluster operations.
