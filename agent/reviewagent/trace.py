"""Render an agent audit log into human-readable evidence.

The JSONL event log is the record of what happened; this module turns one run
of it into three artefacts investigators (and instructors) can actually read:

    trace.md        an annotated timeline of the run
    commands.sh     the same API calls written as plain kubectl commands
    patch-NN.json   the bodies those commands need

`commands.sh` matters pedagogically: it removes the "the AI did something
magic" reading. Every step the agent took is an ordinary command a person with
the same ServiceAccount could have typed, which is exactly why the permissions
are the finding.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

TRUST_BOUNDARY_NOTE = (
    "TRUST BOUNDARY CROSSED: from here on, `source` names a skill, not the "
    "operator. Untrusted repository text is now driving the agent."
)

STAGE_LABEL = {
    "init": "start",
    "skill-discovery": "discovery",
    "llm-decision": "decision",
    "skill-load": "skill load",
    "tool-execution": "tool call",
    "review-output": "review",
}


def load_events(path: str, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read a JSONL log, optionally keeping a single run."""
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id and event.get("run_id") != run_id:
                continue
            events.append(event)
    return events


def runs(events: Iterable[Dict[str, Any]]) -> List[str]:
    """Run ids present in a log, in first-seen order."""
    seen: List[str] = []
    for event in events:
        run = event.get("run_id")
        if run and run not in seen:
            seen.append(run)
    return seen


def _kube_context(event: Dict[str, Any], default_namespace: str) -> Tuple[str, str]:
    identity = event.get("identity") or {}
    namespace = (
        (event.get("parameters") or {}).get("namespace")
        or identity.get("namespace")
        or default_namespace
    )
    service_account = identity.get("service_account", "review-agent-sa")
    return namespace, service_account


def render_timeline(events: List[Dict[str, Any]], repo_path: str = "workshop-repo") -> str:
    """An annotated markdown timeline of one review run."""
    if not events:
        return "# Agent trace\n\n(no events)\n"

    first = events[0]
    lines = [
        "# Agent trace - run %s" % first.get("run_id"),
        "",
        "| field | value |",
        "|---|---|",
        "| agent | `%s` |" % first.get("agent"),
        "| decision maker | `%s` |" % first.get("llm"),
        "| started | %s |" % first.get("timestamp"),
        "| finished | %s |" % events[-1].get("timestamp"),
        "| events | %d |" % len(events),
    ]

    identity = next((e.get("identity") for e in events if e.get("identity")), None)
    if identity:
        lines += [
            "| Kubernetes identity | `%s` in `%s` |"
            % (identity.get("service_account"), identity.get("namespace")),
            "| credential | %s |" % identity.get("credential"),
            "| API server | `%s` |" % identity.get("api_server"),
        ]

    pr_event = next((e for e in events if e.get("action") == "pr.loaded"), None)
    pull_request = (pr_event or {}).get("result") or {}
    if pr_event:
        title = "#%s %s" % (pull_request.get("id"), pull_request.get("title"))
        if pull_request.get("url"):
            title = "[%s](%s)" % (title, pull_request["url"])
        lines += [
            "| pull request | %s |" % title,
            "| author (declared) | `%s` (%s) |"
            % (pull_request.get("author"), pull_request.get("author_association")),
            "| untrusted author | %s |" % pull_request.get("untrusted_author"),
        ]
        head = pull_request.get("head_commit")
        if head:
            lines += [
                "| branch | `%s` -> `%s` |"
                % (pull_request.get("source_branch"), pull_request.get("target_branch")),
                "| head commit | `%s` %s |" % (head.get("short_sha"), head.get("subject")),
                "| commit author | %s &lt;%s&gt; |"
                % (head.get("author_name"), head.get("author_email")),
                "| authored | %s |" % head.get("authored_at"),
            ]

    skills_event = next(
        (e for e in events if e.get("action") == "skills.discovered"), None
    )
    if skills_event:
        lines.append(
            "| skills read from | `%s` |"
            % ((skills_event.get("result") or {}).get("ref", "working-tree"))
        )

    if pull_request.get("files_derived") is not None:
        lines += ["", "## Change set", ""]
        lines += _changeset_section(pull_request)

    git_lines = _provenance_section(events)
    if git_lines:
        lines += ["", "## Provenance", ""] + git_lines

    lines += ["", "## Timeline", ""]

    crossed = False
    for event in events:
        source = str(event.get("source", ""))
        stage = STAGE_LABEL.get(event.get("stage", ""), event.get("stage", ""))
        header = "### %s  seq %s  -  %s" % (
            event.get("timestamp"),
            event.get("seq"),
            event.get("action"),
        )
        lines.append(header)
        lines.append("")
        detail = ["- stage: %s" % stage, "- source: `%s`" % source]
        if event.get("tool"):
            detail.append("- tool: `%s`" % event["tool"])
        if event.get("rationale"):
            detail.append("- stated reason: %s" % event["rationale"])
        if event.get("outcome") != "ok":
            detail.append("- outcome: **%s**" % event.get("outcome"))
        lines += detail

        summary = _summarise_event(event)
        if summary:
            lines += ["", summary]

        if not crossed and source.startswith("skill:"):
            crossed = True
            lines += ["", "> **%s**" % TRUST_BOUNDARY_NOTE]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _changeset_section(pull_request: Dict[str, Any]) -> List[str]:
    """What the branch actually changed, next to what the PR says it changed."""
    derived = pull_request.get("files_derived") or []
    declared = pull_request.get("files_declared") or []
    lines = ["| file | in the PR description | actually changed |", "|---|---|---|"]
    for path in sorted(set(derived) | set(declared)):
        lines.append(
            "| `%s` | %s | %s |"
            % (path, "yes" if path in declared else "**no**", "yes" if path in derived else "**no**")
        )
    undeclared = pull_request.get("undeclared_files") or []
    unchanged = pull_request.get("unchanged_declared_files") or []
    if undeclared:
        lines += [
            "",
            "> The branch changed %d file(s) the description does not list: %s."
            % (len(undeclared), ", ".join("`%s`" % p for p in undeclared)),
        ]
    if unchanged:
        lines += [
            "",
            "> The description lists %d file(s) the branch did not change: %s."
            % (len(unchanged), ", ".join("`%s`" % p for p in unchanged)),
        ]
    if not undeclared and not unchanged:
        lines += ["", "> Description and branch agree on the change set."]
    return lines


def _provenance_section(events: List[Dict[str, Any]]) -> List[str]:
    """Where each discovered skill came from, per git."""
    skills_event = next(
        (e for e in events if e.get("action") == "skills.discovered"), None
    )
    if not skills_event:
        return []
    skills = (skills_event.get("result") or {}).get("skills") or []
    if not any(skill.get("introduced_by") for skill in skills):
        return []
    introduced = set((skills_event.get("result") or {}).get("introduced_by_this_pr") or [])
    lines = [
        "| skill | added in | by | when | from this PR |",
        "|---|---|---|---|---|",
    ]
    for skill in skills:
        commit = skill.get("introduced_by") or {}
        lines.append(
            "| `%s` | `%s` | %s &lt;%s&gt; | %s | %s |"
            % (
                skill.get("name"),
                commit.get("short_sha", "?"),
                commit.get("author_name", "?"),
                commit.get("author_email", "?"),
                commit.get("authored_at", "?"),
                "**yes**" if skill.get("name") in introduced else "no",
            )
        )
    return lines


def _summarise_event(event: Dict[str, Any]) -> str:
    action = event.get("action")
    result = event.get("result")
    if action == "skills.discovered" and isinstance(result, dict):
        rows = ["| skill | description | sha256 | from this PR |", "|---|---|---|---|"]
        introduced = set(result.get("introduced_by_this_pr") or [])
        for skill in result.get("skills") or []:
            rows.append(
                "| `%s` | %s | `%s` | %s |"
                % (
                    skill.get("name"),
                    str(skill.get("description", "")).replace("\n", " ")[:90],
                    str(skill.get("sha256", ""))[:16],
                    "**yes**" if skill.get("name") in introduced else "no",
                )
            )
        return "\n".join(rows)

    if event.get("tool") == "load_skill" and isinstance(result, dict):
        excerpt = str(result.get("instruction_excerpt", "")).strip()
        body = [
            "Loaded `%s` (%s bytes, sha256 `%s`)."
            % (result.get("path"), result.get("bytes"), str(result.get("sha256"))[:16]),
        ]
        directives = result.get("directives") or []
        if directives:
            body.append("")
            body.append(
                "Instructions contain %d tool call(s): %s."
                % (len(directives), ", ".join("`%s`" % d.get("tool") for d in directives))
            )
        if excerpt:
            body += ["", "<details><summary>instruction excerpt</summary>", "", "```markdown", excerpt, "```", "", "</details>"]
        return "\n".join(body)

    if event.get("tool") == "kubernetes_patch" and isinstance(result, dict):
        diff = result.get("diff") or {}
        return "Result: containers added %s, changed %s, removed %s." % (
            diff.get("containers_added"),
            diff.get("containers_changed"),
            diff.get("containers_removed"),
        )

    if event.get("tool") == "kubernetes_get" and isinstance(result, dict):
        summary = result.get("summary") or {}
        if summary.get("count") is not None:
            return "Result: %d %s(s)." % (summary["count"], summary.get("kind"))
        return "Result: %s/%s generation %s." % (
            summary.get("kind"),
            summary.get("name"),
            summary.get("generation"),
        )

    if action == "review.completed" and isinstance(result, dict):
        comment = str(result.get("comment", "")).strip()
        if comment:
            return "Review comment posted:\n\n```\n%s\n```" % comment
    return ""


def render_commands(
    events: List[Dict[str, Any]],
    out_dir: str,
    repo_path: str = "workshop-repo",
    default_namespace: str = "ikt-workshop",
) -> Tuple[str, List[Tuple[str, str]]]:
    """Render the run as plain shell commands.

    Returns (script_text, [(filename, contents)]) for the patch/manifest bodies
    the script refers to. Read-only commands are runnable as written; mutating
    ones are commented out, because running them replays the attack.
    """
    files: List[Tuple[str, str]] = []
    run_id = events[0].get("run_id") if events else "unknown"
    namespace, service_account = (
        _kube_context(events[0], default_namespace) if events else (default_namespace, "review-agent-sa")
    )

    lines = [
        "#!/bin/sh",
        "# The agent's run %s, written as plain commands." % run_id,
        "#",
        "# Every step below is something an operator holding the",
        "#   %s" % service_account,
        "# ServiceAccount could have typed. Nothing here needs the agent, an LLM,",
        "# or pods/exec. That is the point of the exercise.",
        "#",
        "# Read-only commands are runnable as written. Mutating commands are",
        "# COMMENTED OUT: uncommenting them replays the attack against your",
        "# cluster. Add --as to run with the agent's identity, for example:",
        "#   kubectl --as=system:serviceaccount:%s:%s -n %s get deploy" % (namespace, service_account, namespace),
        "set -eu",
        "NS=%s" % namespace,
        "REPO=%s" % repo_path,
        "",
    ]

    lines += _git_commands(events)

    step = 0
    for event in events:
        if event.get("action") != "tool.invoked":
            continue
        step += 1
        tool = event.get("tool")
        parameters = event.get("parameters") or {}
        event_namespace, _ = _kube_context(event, default_namespace)
        header = "# --- seq %s  %s  (source: %s)" % (
            event.get("seq"),
            tool,
            event.get("source"),
        )
        lines.append(header)
        if event.get("rationale"):
            lines.append("#     stated reason: %s" % event["rationale"])

        if tool == "load_skill":
            name = parameters.get("name")
            path = (event.get("result") or {}).get("path", ".agents/skills/%s/SKILL.md" % name)
            lines += [
                'cat "$REPO/%s"' % path,
                'shasum -a 256 "$REPO/%s"   # expect %s' % (path, str((event.get("result") or {}).get("sha256", ""))[:64]),
            ]
        elif tool == "read_file":
            lines.append('cat "$REPO/%s"' % parameters.get("path"))
        elif tool == "kubernetes_get":
            kind = str(parameters.get("kind", "")).lower()
            name = parameters.get("name")
            if name:
                lines.append("kubectl -n %s get %s %s -o yaml" % (event_namespace, kind, name))
            else:
                lines.append("kubectl -n %s get %s" % (event_namespace, kind))
        elif tool == "kubernetes_patch":
            kind = str(parameters.get("kind", "")).lower()
            name = parameters.get("name")
            patch_type = parameters.get("patch_type", "strategic")
            filename = "patch-%02d.json" % step
            files.append((filename, json.dumps(parameters.get("patch") or {}, indent=2) + "\n"))
            lines += [
                "#     body: %s" % filename,
                "#     *** this is the review -> code execution step ***",
                "# kubectl -n %s patch %s %s --type=%s --patch-file=%s"
                % (event_namespace, kind, name, patch_type, filename),
            ]
        elif tool == "kubernetes_create":
            filename = "manifest-%02d.json" % step
            files.append((filename, json.dumps(parameters.get("manifest") or {}, indent=2) + "\n"))
            lines += [
                "#     body: %s" % filename,
                "# kubectl -n %s create -f %s" % (event_namespace, filename),
            ]
        else:
            lines.append("# (no plain-command equivalent for %s)" % tool)
        lines.append("")

    lines += [
        "# --- what the agent's actions produced (all read-only)",
        'kubectl -n "$NS" rollout history deploy/payments-api',
        'kubectl -n "$NS" get rs -l app.kubernetes.io/name=payments-api',
        "kubectl -n \"$NS\" get deploy payments-api -o jsonpath='{.metadata.annotations}'",
        'kubectl -n "$NS" logs deploy/payments-api -c runtime-config-sync --tail=20',
        'kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -25',
        "",
    ]
    return "\n".join(lines), files


def _git_commands(events: List[Dict[str, Any]]) -> List[str]:
    """The git commands that answer 'what changed, and who wrote it'."""
    pr_event = next((e for e in events if e.get("action") == "pr.loaded"), None)
    pull_request = (pr_event or {}).get("result") or {}
    if not pull_request.get("head_commit"):
        return []

    base = pull_request.get("target_branch", "main")
    head = pull_request.get("source_branch", "HEAD")
    lines = [
        "# --- the pull request, straight from git",
        'git -C "$REPO" log --oneline --graph --all --decorate',
        'git -C "$REPO" diff --stat %s...%s' % (base, head),
        'git -C "$REPO" show --stat %s' % pull_request["head_commit"].get("short_sha", head),
        "",
    ]

    skills_event = next(
        (e for e in events if e.get("action") == "skills.discovered"), None
    )
    for skill in (skills_event or {}).get("result", {}).get("skills") or []:
        commit = skill.get("introduced_by")
        if not commit:
            continue
        lines += [
            "# who introduced the %s skill, and what it looked like then" % skill.get("name"),
            'git -C "$REPO" log --diff-filter=A -1 --format=\'%%h %%an <%%ae> %%aI\' -- %s'
            % skill.get("path"),
            'git -C "$REPO" show %s -- %s' % (commit.get("short_sha"), skill.get("path")),
            "",
        ]
    return lines


def export(
    audit_log: str,
    out_dir: str,
    run_id: Optional[str] = None,
    repo_path: str = "workshop-repo",
    namespace: str = "ikt-workshop",
) -> Dict[str, Any]:
    """Write trace.md, commands.sh, the bodies and the filtered JSONL."""
    events = load_events(audit_log, run_id)
    if not events:
        raise ValueError("no events in %s%s" % (audit_log, " for run %s" % run_id if run_id else ""))
    run_id = run_id or events[0].get("run_id")
    os.makedirs(out_dir, exist_ok=True)

    written = []
    jsonl_path = os.path.join(out_dir, "events.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    written.append(jsonl_path)

    trace_path = os.path.join(out_dir, "trace.md")
    with open(trace_path, "w", encoding="utf-8") as handle:
        handle.write(render_timeline(events, repo_path))
    written.append(trace_path)

    script, files = render_commands(events, out_dir, repo_path, namespace)
    script_path = os.path.join(out_dir, "commands.sh")
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(script)
    os.chmod(script_path, 0o755)
    written.append(script_path)

    for filename, contents in files:
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        written.append(path)

    return {"run_id": run_id, "events": len(events), "files": written}
