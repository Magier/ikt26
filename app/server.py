"""A web shell for the workshop pod.

Serves one page: a box you type a command into, and the output of running it
inside this container. The image is built on netshoot, so `curl`, `dig`, `nmap`,
`tcpdump` and friends are already on PATH - which is the point. It is how you
answer "what can this pod reach" from a browser instead of from `kubectl exec`.

This executes whatever it is given, as whoever the container runs as. That is
the feature, not an oversight. It belongs on a cluster you own, reached through
`kubectl port-forward`. Do not put an Ingress or a LoadBalancer in front of it.

There is a JSON endpoint too, for driving it from a script:

    curl -sS localhost:8080/api/run -d '{"cmd": "id"}'

No dependencies - Python's standard library only.
"""

import html
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = int(os.environ.get("PORT", "8080"))
TIMEOUT = float(os.environ.get("COMMAND_TIMEOUT", "30"))

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>netshoot console</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 2rem 1rem; background: #14161a; color: #e6e6e6;
         font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  main {{ max-width: 60rem; margin: 0 auto; }}
  h1 {{ font-size: 1rem; font-weight: 600; margin: 0 0 .25rem; }}
  .host {{ color: #8b93a1; margin: 0 0 1.5rem; }}
  .warn {{ border-left: 3px solid #d98026; background: #241d13; color: #e8b27a;
           padding: .6rem .9rem; margin: 0 0 1.5rem; }}
  form {{ display: flex; gap: .5rem; margin: 0 0 1.5rem; }}
  input {{ flex: 1; min-width: 0; padding: .6rem .75rem; background: #0e1013;
           color: #e6e6e6; border: 1px solid #2c313a; border-radius: 4px;
           font: inherit; }}
  input:focus {{ outline: none; border-color: #5b7fc7; }}
  button {{ padding: .6rem 1.2rem; background: #2f6feb; color: #fff; border: 0;
            border-radius: 4px; font: inherit; cursor: pointer; }}
  pre {{ background: #0e1013; border: 1px solid #2c313a; border-radius: 4px;
         padding: .9rem; overflow-x: auto; white-space: pre-wrap;
         word-break: break-word; margin: 0; }}
  .cmd {{ color: #7fb069; }}
  .meta {{ color: #8b93a1; margin: .5rem 0 0; }}
  .fail {{ color: #e06c75; }}
</style>
<main>
  <h1>netshoot console</h1>
  <p class="host">{host}</p>
  <p class="warn">Runs anything you type, in this container. Reach it with
    <code>kubectl port-forward</code> - never an Ingress.</p>
  <form method="post" action="/">
    <input name="cmd" value="{value}" placeholder="curl -s http://example/"
           autofocus autocomplete="off" spellcheck="false">
    <button type="submit">Run</button>
  </form>
  {result}
</main>
</html>
"""


def run(command):
    """Run one command through a shell. Never raises - a failure is a result."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=TIMEOUT
        )
        output = done.stdout + done.stderr
        code = done.returncode
    except subprocess.TimeoutExpired:
        output, code = "timed out after %gs\n" % TIMEOUT, 124
    except Exception as exc:  # a broken shell is still an answer worth showing
        output, code = "%s\n" % exc, 1
    return output, code, time.monotonic() - started


def render(command="", output=None, code=0, seconds=0.0):
    if output is None:
        result = ""
    else:
        body = html.escape(output) or "<em>(no output)</em>"
        cls = "meta" if code == 0 else "meta fail"
        result = (
            '<pre><span class="cmd">$ %s</span>\n%s</pre>'
            '<p class="%s">exit %d &middot; %.2fs</p>'
            % (html.escape(command), body, cls, code, seconds)
        )
    return PAGE.format(
        host=html.escape(os.environ.get("HOSTNAME", "unknown")),
        value=html.escape(command, quote=True),
        result=result,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "netshoot-console/0.1"

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, indent=2) + "\n", "application/json")

    def do_GET(self):
        # A probe endpoint that does not shell out, so a failing probe means
        # the process is wedged rather than that a command misbehaved.
        if self.path.startswith("/healthz"):
            return self._send(200, "ok\n", "text/plain; charset=utf-8")
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, render())
        return self._send(404, "not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path == "/api/run":
            return self._api_run()
        if self.path != "/":
            return self._send(404, "not found\n", "text/plain; charset=utf-8")
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
        command = (form.get("cmd") or [""])[0].strip()
        if not command:
            return self._send(200, render())
        print("run: %s" % command, flush=True)
        output, code, seconds = run(command)
        self._send(200, render(command, output, code, seconds))

    def _api_run(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "body must be JSON"})
        command = str(body.get("cmd") or "").strip()
        if not command:
            return self._send_json(400, {"error": 'missing "cmd"'})
        print("run: %s" % command, flush=True)
        output, code, seconds = run(command)
        self._send_json(
            200,
            {
                "cmd": command,
                "output": output,
                "exit": code,
                "seconds": round(seconds, 3),
            },
        )

    def log_message(self, fmt, *args):
        pass  # the `run:` lines above are the log worth having


if __name__ == "__main__":
    print("listening on 0.0.0.0:%d" % PORT, flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
