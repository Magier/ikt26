"""Command line entry point.

    review-pr    run one review of the pull request in a repository
    inspect      print the evidence a run produced (agent + simulated cluster)
    tools        print the agent's capability inventory

The same wiring is reused by `server.py`, so the HTTP API and the CLI cannot
drift apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from .agent import ReviewAgent
from .evidence import EventLog
from .llm import FakeLLM
from .pr import Repository
from .skills import SkillRegistry
from .tools.registry import build_default_registry

DEFAULT_REPO = os.environ.get("WORKSHOP_REPO", "/workspace/workshop-repo")
# None = the working tree (for a PR checkout, the head ref). Set to the base
# branch to demonstrate the "do not load skills from the PR" mitigation.
DEFAULT_SKILLS_REF = os.environ.get("SKILLS_REF") or None
DEFAULT_AUDIT = os.environ.get("AUDIT_LOG", "/var/log/review-agent/events.jsonl")
DEFAULT_NAMESPACE = os.environ.get("WORKSHOP_NAMESPACE", "ikt-workshop")
DEFAULT_BACKEND = os.environ.get("KUBE_BACKEND", "auto")
DEFAULT_SIM_STATE = os.environ.get("SIM_STATE_DIR", "sim/state")
DEFAULT_SIM_SEED = os.environ.get("SIM_SEED_DIR", "sim/seed")


def build_kube_client(backend: str, namespace: str, **kwargs):
    """Select a Kubernetes backend.

    auto      in-cluster when a ServiceAccount token is mounted, else local
    cluster   in-cluster REST client (real API server, real audit log)
    local     file-backed simulator with a toy kubelet
    none      no Kubernetes access at all
    """
    if backend == "none":
        return None
    in_cluster = os.path.exists(
        "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    if backend == "auto":
        backend = "cluster" if in_cluster else "local"
    if backend == "cluster":
        from .kube.rest import RestKubeClient

        return RestKubeClient(namespace=namespace)
    if backend == "local":
        from .kube.local import LocalKubeClient

        return LocalKubeClient(
            state_dir=kwargs.get("state_dir") or DEFAULT_SIM_STATE,
            seed_dir=kwargs.get("seed_dir") or DEFAULT_SIM_SEED,
            namespace=namespace,
            service_account=os.environ.get("SERVICE_ACCOUNT_NAME", "review-agent-sa"),
            run_containers=kwargs.get("run_containers", True),
        )
    raise SystemExit("unknown kube backend: %s" % backend)


def build_agent(
    repo_path: str,
    backend: str = DEFAULT_BACKEND,
    namespace: str = DEFAULT_NAMESPACE,
    audit_log: Optional[str] = DEFAULT_AUDIT,
    echo_events: bool = True,
    max_steps: int = 12,
    llm=None,
    skills_ref: Optional[str] = DEFAULT_SKILLS_REF,
    **kube_kwargs,
):
    repository = Repository(repo_path)
    skills = SkillRegistry(repository, ref=skills_ref)
    kube = build_kube_client(backend, namespace, **kube_kwargs)
    log = EventLog(path=audit_log, echo=echo_events)
    tools = build_default_registry(repository, skills, kube, log)
    agent = ReviewAgent(
        repository=repository,
        llm=llm or FakeLLM(),
        event_log=log,
        kube_client=kube,
        tools=tools,
        skill_registry=skills,
        max_steps=max_steps,
    )
    return agent, log, kube


# ---------------------------------------------------------------- subcommands


def cmd_review(args: argparse.Namespace) -> int:
    agent, log, kube = build_agent(
        repo_path=args.repo,
        backend=args.backend,
        namespace=args.namespace,
        audit_log=args.audit_log,
        echo_events=not args.quiet,
        max_steps=args.max_steps,
        skills_ref=args.skills_ref,
        state_dir=args.state_dir,
        seed_dir=args.seed_dir,
    )
    result = agent.review(pr_file=args.pr_file, task=args.task)
    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_summary(summary, log, kube)
    return 0


def _print_summary(summary: Dict[str, Any], log: EventLog, kube) -> None:
    print("")
    print("run_id:            %s" % summary["run_id"])
    print("pull request:      #%s %s" % (summary["pull_request"]["id"], summary["pull_request"]["title"]))
    print("author:            %s (%s)" % (summary["pull_request"]["author"], summary["pull_request"]["author_association"]))
    print("skills discovered: %s" % ", ".join(s["name"] for s in summary["skills_discovered"]))
    print("skills loaded:     %s" % ", ".join(summary["skills_loaded"]))
    print("")
    print("steps:")
    for step in summary["steps"]:
        print(
            "  %d. %-16s %-18s source=%s outcome=%s"
            % (step["step"], step["action"], step.get("tool", "-"), step["source"], step["outcome"])
        )
    print("")
    print("review comment:")
    for line in summary["review_comment"].splitlines():
        print("  | %s" % line)
    print("")
    print("evidence: %d events%s" % (summary["event_count"], " -> %s" % log.path if log.path else ""))
    if kube is not None and getattr(kube, "backend", "") == "local-simulator":
        print("simulated cluster state: %s" % kube.objects_path)


def cmd_inspect(args: argparse.Namespace) -> int:
    kube = build_kube_client(
        "local" if args.backend == "auto" else args.backend,
        args.namespace,
        state_dir=args.state_dir,
        seed_dir=args.seed_dir,
        run_containers=False,
    )
    print("== agent audit log: %s" % args.audit_log)
    if os.path.exists(args.audit_log):
        with open(args.audit_log, "r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                print(
                    "%s seq=%-3s %-16s %-18s source=%-20s outcome=%s"
                    % (
                        event["timestamp"],
                        event["seq"],
                        event["action"],
                        event.get("tool") or "-",
                        event.get("source"),
                        event["outcome"],
                    )
                )
    else:
        print("(no audit log yet)")

    if kube is None:
        return 0
    print("")
    print("== workloads")
    for kind in ("deployment", "replicaset", "pod"):
        try:
            listing = kube.get(kind, namespace=args.namespace)
        except Exception as exc:  # simulator not seeded yet
            print("%s: %s" % (kind, exc))
            continue
        for item in listing.get("items", []):
            metadata = item.get("metadata", {})
            spec = item.get("spec", {})
            template_spec = (spec.get("template") or {}).get("spec") or spec
            containers = [c.get("name") for c in template_spec.get("containers") or []]
            print(
                "%-11s %-40s gen=%-3s containers=%s"
                % (kind, metadata.get("name"), metadata.get("generation", "-"), containers)
            )
    print("")
    print("== kubernetes events")
    try:
        events = kube.get("event", namespace=args.namespace).get("items", [])
    except Exception:
        events = []
    for event in events[:25]:
        print(
            "%-16s %-18s %s"
            % (event.get("reason"), event.get("type"), event.get("message", "")[:90])
        )
    if not events:
        print("(none)")

    if hasattr(kube, "pod_logs"):
        print("")
        print("== container logs (simulated)")
        for item in kube.get("pod", namespace=args.namespace).get("items", []):
            name = item["metadata"]["name"]
            for container, text in kube.pod_logs(name).items():
                if not text.strip():
                    continue
                print("--- %s/%s" % (name, container))
                for line in text.strip().splitlines()[:20]:
                    print("    %s" % line)
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    agent, _, _ = build_agent(
        repo_path=args.repo,
        backend=args.backend,
        namespace=args.namespace,
        audit_log=None,
        echo_events=False,
        skills_ref=args.skills_ref,
        state_dir=args.state_dir,
        seed_dir=args.seed_dir,
        run_containers=False,
    )
    print(json.dumps(agent.tools.inventory(), indent=2))
    return 0


def cmd_export_trace(args: argparse.Namespace) -> int:
    from . import trace

    if args.list_runs:
        events = trace.load_events(args.audit_log)
        for run in trace.runs(events):
            count = len([e for e in events if e.get("run_id") == run])
            first = next(e for e in events if e.get("run_id") == run)
            print("%s  %s  %d events" % (run, first["timestamp"], count))
        return 0

    # A log can hold several runs (the agent pod outlives one review). Each run
    # is its own narrative, so each gets its own directory.
    all_runs = trace.runs(trace.load_events(args.audit_log))
    selected = [args.run_id] if args.run_id else all_runs
    if args.run_id and args.run_id not in all_runs:
        raise SystemExit(
            "run %s is not in %s (found: %s)"
            % (args.run_id, args.audit_log, ", ".join(all_runs) or "none")
        )

    for run in selected:
        result = trace.export(
            audit_log=args.audit_log,
            out_dir=os.path.join(args.out_dir, run),
            run_id=run,
            repo_path=args.repo,
            namespace=args.namespace,
        )
        print("exported run %s (%d events)" % (result["run_id"], result["events"]))
        for path in result["files"]:
            print("  %s" % path)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port, args=args)
    return 0


def add_global_options(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Options that apply to every subcommand.

    They are added twice: once on the top-level parser and once on each
    subparser, so `review-agent --backend local review-pr` and
    `review-agent review-pr --backend local` both work. The subparser copies
    default to SUPPRESS, so an option not given after the subcommand leaves the
    top-level value alone instead of overwriting it.
    """

    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--repo", default=default(DEFAULT_REPO),
                        help="repository checkout to review")
    parser.add_argument("--namespace", default=default(DEFAULT_NAMESPACE))
    parser.add_argument(
        "--backend",
        default=default(DEFAULT_BACKEND),
        choices=["auto", "cluster", "local", "none"],
        help="Kubernetes backend (default: auto)",
    )
    parser.add_argument("--audit-log", default=default(DEFAULT_AUDIT))
    parser.add_argument(
        "--skills-ref",
        default=default(DEFAULT_SKILLS_REF),
        help=(
            "git ref to load skills from (default: the working tree, i.e. the "
            "PR head). Set to the base branch to load only skills that were "
            "already reviewed and merged."
        ),
    )
    parser.add_argument("--state-dir", default=default(DEFAULT_SIM_STATE))
    parser.add_argument("--seed-dir", default=default(DEFAULT_SIM_SEED))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="review-agent", description=__doc__)
    add_global_options(parser)

    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, suppress=True)

    sub = parser.add_subparsers(dest="command")

    review = sub.add_parser(
        "review-pr", parents=[common], help="review the PR in the repository"
    )
    review.add_argument("--pr-file", default="PR.md")
    review.add_argument("--task", default=None, help="override the review request")
    review.add_argument("--max-steps", type=int, default=12)
    review.add_argument("--json", action="store_true")
    review.add_argument("--quiet", action="store_true", help="do not echo events")
    review.set_defaults(func=cmd_review)

    inspect = sub.add_parser(
        "inspect", parents=[common], help="show the evidence a run produced"
    )
    inspect.set_defaults(func=cmd_inspect)

    tools = sub.add_parser(
        "tools", parents=[common], help="show the agent capability inventory"
    )
    tools.set_defaults(func=cmd_tools)

    export = sub.add_parser(
        "export-trace",
        parents=[common],
        help="render an audit log into trace.md + plain kubectl commands",
    )
    export.add_argument("--out-dir", default="audit/exports")
    export.add_argument(
        "--run-id",
        default=None,
        help="export a single run (default: every run in the log)",
    )
    export.add_argument("--list-runs", action="store_true", help="list runs and exit")
    export.set_defaults(func=cmd_export_trace)

    serve_cmd = sub.add_parser("serve", parents=[common], help="run the review HTTP API")
    serve_cmd.add_argument("--host", default="0.0.0.0")
    serve_cmd.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    serve_cmd.add_argument("--pr-file", default="PR.md")
    serve_cmd.add_argument("--max-steps", type=int, default=12)
    serve_cmd.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
