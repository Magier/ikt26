"""Stand-in for the workshop application.

The deployed workload is nginx serving a static directory; this file exists so
the repository looks like a normal service repo and so PRs have app code to
"review". Nothing in the scenario executes it.
"""

import http.server
import os

PORT = int(os.environ.get("PORT", "8080"))
ROOT = os.environ.get("WEB_ROOT", "/usr/share/nginx/html")


def main() -> None:
    os.chdir(ROOT)
    handler = http.server.SimpleHTTPRequestHandler
    with http.server.ThreadingHTTPServer(("", PORT), handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
