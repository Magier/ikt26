"""Web UI for the coding-agent box.

One page: check a repo out into /workspace, point an agent at it, watch the
output stream back. Everything it does, it does by running the same `checkout`
and `agent-run` scripts that are on PATH in the shell - so driving this from a
browser and driving it from `podman exec -it` are the same thing, and neither
can do something the other cannot.

Output is streamed rather than buffered: an agent run is minutes of work, and a
page that shows nothing until it finishes is useless for watching an agent
think.

This executes whatever it is given, as root, with no authentication - the same
deal as the netshoot console next door, and for the same reason. No sandbox is
the current, deliberate state. Reach it over a port-forward on a cluster you
own.

No dependencies - Python's standard library only.
"""

import html
import json
import os
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/skills")
# Agent runs are long. 30 minutes is a guard against a wedged process holding a
# thread forever, not a serious limit on how long a review may take.
TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "1800"))

AGENTS = ["claude", "codex", "agy", "pi", "hermes"]

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent box</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 2rem 1rem; background: #14161a; color: #e6e6e6;
         font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  main { max-width: 64rem; margin: 0 auto; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0 0 .25rem; }
  h2 { font-size: .8rem; font-weight: 600; text-transform: uppercase;
       letter-spacing: .08em; color: #8b93a1; margin: 1.75rem 0 .6rem; }
  .host { color: #8b93a1; margin: 0 0 1.5rem; }
  .warn { border-left: 3px solid #d98026; background: #241d13; color: #e8b27a;
          padding: .6rem .9rem; margin: 0 0 .5rem; }
  .row { display: flex; gap: .5rem; flex-wrap: wrap; }
  .row > * { font: inherit; }
  input, select, textarea {
    min-width: 0; padding: .55rem .7rem; background: #0e1013; color: #e6e6e6;
    border: 1px solid #2c313a; border-radius: 4px; font: inherit; }
  input.grow { flex: 1; }
  textarea { width: 100%; box-sizing: border-box; resize: vertical; }
  button { padding: .55rem 1rem; background: #2b3442; color: #e6e6e6;
           border: 1px solid #3a4553; border-radius: 4px; cursor: pointer; }
  button:hover { background: #35414f; }
  button[disabled] { opacity: .5; cursor: default; }
  pre { background: #0e1013; border: 1px solid #2c313a; border-radius: 4px;
        padding: .9rem; margin: .6rem 0 0; max-height: 34rem; overflow: auto;
        white-space: pre-wrap; word-break: break-word; }
  .muted { color: #8b93a1; }
</style>
<main>
  <h1>agent box</h1>
  <p class="host">__HOST__ &middot; workspace __WORKSPACE__</p>
  <p class="warn">Runs arbitrary commands as root, unauthenticated, no sandbox.
     Reach it through a port-forward, not an Ingress.</p>

  <h2>1. check out a repo</h2>
  <div class="row">
    <input id="repo" class="grow" placeholder="https://github.com/owner/repo"
           autocomplete="off">
    <input id="ref" placeholder="ref or pr/123" size="14" autocomplete="off">
    <button id="go-checkout">Checkout</button>
  </div>

  <h2>2. point an agent at it</h2>
  <div class="row" style="margin-bottom:.5rem">
    <select id="dir"></select>
    <select id="agent">__AGENT_OPTIONS__</select>
    <button id="go-agent">Run agent</button>
    <button id="go-list" title="print installed agents and versions">Versions</button>
  </div>
  <textarea id="prompt" rows="4"
    placeholder="What the agent should do. Leave empty and it starts interactively - which a browser cannot drive, so use a shell for that."></textarea>

  <h2>3. or just run a command</h2>
  <div class="row">
    <input id="cmd" class="grow" placeholder="git log --oneline -5"
           autocomplete="off">
    <button id="go-cmd">Run</button>
  </div>

  <h2>output</h2>
  <pre id="out" class="muted">nothing yet</pre>
</main>
<script>
const $ = (id) => document.getElementById(id);
const out = $("out");
let busy = false;

function setBusy(b) {
  busy = b;
  for (const id of ["go-checkout", "go-agent", "go-cmd", "go-list"])
    $(id).disabled = b;
}

async function stream(cmd, cwd) {
  if (busy) return;
  setBusy(true);
  out.classList.remove("muted");
  out.textContent = "$ " + cmd + "\\n";
  try {
    const res = await fetch("/api/stream", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({cmd, cwd}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      out.textContent += dec.decode(value, {stream: true});
      out.scrollTop = out.scrollHeight;
    }
  } catch (e) {
    out.textContent += "\\n[stream failed: " + e + "]";
  }
  setBusy(false);
  refreshDirs();
}

async function refreshDirs() {
  const res = await fetch("/api/state");
  const {repos} = await res.json();
  const sel = $("dir"), prev = sel.value;
  sel.innerHTML = repos.length
    ? repos.map((r) => `<option>${r}</option>`).join("")
    : `<option value="">(no repo checked out)</option>`;
  if (repos.includes(prev)) sel.value = prev;
}

const q = (s) => "'" + s.replace(/'/g, "'\\\\''") + "'";

$("go-checkout").onclick = () => {
  const repo = $("repo").value.trim();
  if (!repo) return;
  const ref = $("ref").value.trim();
  stream("checkout " + q(repo) + (ref ? " " + q(ref) : ""), null);
};

$("go-agent").onclick = () => {
  const dir = $("dir").value;
  if (!dir) { alert("check a repo out first"); return; }
  const prompt = $("prompt").value.trim();
  if (!prompt) { alert("a browser cannot drive an interactive TUI - give a prompt, or use a shell"); return; }
  stream("agent-run " + $("agent").value + " " + q(prompt), dir);
};

$("go-list").onclick = () => stream("agent-run list", null);
$("go-cmd").onclick = () => stream($("cmd").value.trim(), $("dir").value || null);
$("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("go-cmd").click(); });

refreshDirs();
</script>
"""


def repos():
    """Directories in the workspace that are actually git checkouts."""
    try:
        names = sorted(os.listdir(WORKSPACE))
    except OSError:
        return []
    return [n for n in names if os.path.isdir(os.path.join(WORKSPACE, n, ".git"))]


class Handler(BaseHTTPRequestHandler):
    # Chunked streaming needs 1.1; the default 1.0 would force us to buffer the
    # whole run before sending a byte, which is the thing to avoid.
    protocol_version = "HTTP/1.1"
    server_version = "agentbox"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    # -- helpers -------------------------------------------------------------

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routes --------------------------------------------------------------

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok")
        if self.path == "/api/state":
            return self._send(
                200,
                json.dumps({"repos": repos(), "agents": AGENTS, "skills": SKILLS_DIR}),
                "application/json",
            )
        if self.path in ("/", "/index.html"):
            # Plain substitution, not str.format: the page is full of CSS
            # braces and JS template literals, which format() would choke on.
            page = (PAGE
                    .replace("__HOST__", html.escape(os.uname().nodename))
                    .replace("__WORKSPACE__", html.escape(WORKSPACE))
                    .replace("__AGENT_OPTIONS__",
                             "".join("<option>%s</option>" % a for a in AGENTS)))
            return self._send(200, page, "text/html; charset=utf-8")
        self._send(404, "not found\n")

    def do_POST(self):
        if self.path != "/api/stream":
            return self._send(404, "not found\n")
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._send(400, "body must be JSON\n")

        cmd = (body.get("cmd") or "").strip()
        if not cmd:
            return self._send(400, 'need a "cmd"\n')

        cwd = WORKSPACE
        if body.get("cwd"):
            # Confine to the workspace: the shell below can escape it trivially
            # anyway, but a typo in the UI should not land the agent in /.
            candidate = os.path.realpath(os.path.join(WORKSPACE, body["cwd"]))
            if candidate.startswith(os.path.realpath(WORKSPACE)):
                cwd = candidate

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._run(cmd, cwd)

    def _run(self, cmd, cwd):
        """Run cmd, streaming merged stdout+stderr to the client as it comes."""
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Own process group, so a timeout or a disconnect takes the agent's
            # children with it rather than orphaning a model call.
            start_new_session=True,
        )

        timer = threading.Timer(
            TIMEOUT, lambda: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        )
        timer.start()
        try:
            for line in proc.stdout:
                self._chunk(line)
            proc.wait()
            self._chunk(b"\n[exit %d]\n" % proc.returncode)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Browser went away mid-run. Do not leave an agent burning tokens.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            timer.cancel()


def main():
    print("agentbox on :%d, workspace %s, skills %s" % (PORT, WORKSPACE, SKILLS_DIR), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
