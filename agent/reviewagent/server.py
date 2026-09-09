"""Minimal review API.

    GET  /healthz            liveness
    GET  /skills             skills currently discoverable in the checkout
    GET  /tools              the agent's capability inventory
    POST /review             run one review; returns the structured result
    GET  /events             the agent audit log for this container (JSONL)

`POST /review` is the "one command" milestone of phase 1. It exists so the
review can be triggered the way a webhook would trigger it later, without the
trigger mechanism being part of the interesting code.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

_LOCK = threading.Lock()


def _handler_factory(args):
    from .cli import build_agent
    from .pr import Repository
    from .skills import SkillRegistry

    class Handler(BaseHTTPRequestHandler):
        server_version = "ai-pr-review-agent/0.1"

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            body = (
                json.dumps(payload, indent=2).encode("utf-8")
                if content_type == "application/json"
                else str(payload).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *fmt_args: Any) -> None:
            # Access logs are noise next to the structured event log.
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/healthz"):
                return self._send(200, {"status": "ok", "repo": args.repo})
            if self.path.startswith("/skills"):
                repository = Repository(args.repo)
                registry = SkillRegistry(
                    repository, ref=getattr(args, "skills_ref", None)
                )
                return self._send(
                    200,
                    {"skills": [meta.to_dict() for meta in registry.discover()]},
                )
            if self.path.startswith("/tools"):
                agent, _, _ = build_agent(
                    repo_path=args.repo,
                    backend=args.backend,
                    namespace=args.namespace,
                    audit_log=None,
                    echo_events=False,
                )
                return self._send(200, {"tools": agent.tools.inventory()})
            if self.path.startswith("/events"):
                path = args.audit_log
                if not path or not os.path.exists(path):
                    return self._send(200, "", content_type="text/plain")
                with open(path, "r", encoding="utf-8") as handle:
                    return self._send(200, handle.read(), content_type="text/plain")
            return self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/review"):
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                request: Dict[str, Any] = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return self._send(400, {"error": "body must be JSON"})

            # One review at a time: the audit log is an ordered narrative.
            with _LOCK:
                agent, log, _ = build_agent(
                    repo_path=request.get("repo") or args.repo,
                    backend=args.backend,
                    namespace=args.namespace,
                    audit_log=args.audit_log,
                    echo_events=True,
                    max_steps=int(request.get("max_steps") or args.max_steps),
                    skills_ref=request.get("skills_ref", getattr(args, "skills_ref", None)),
                )
                result = agent.review(
                    pr_file=request.get("pr_file") or args.pr_file,
                    task=request.get("task"),
                )
            return self._send(200, result.to_dict())

    return Handler


def serve(host: str, port: int, args) -> None:
    handler = _handler_factory(args)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(
        json.dumps(
            {
                "action": "server.listening",
                "host": host,
                "port": port,
                "repo": args.repo,
                "backend": args.backend,
                "namespace": args.namespace,
                "audit_log": args.audit_log,
            }
        ),
        flush=True,
    )
    httpd.serve_forever()
