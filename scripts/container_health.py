"""Probe the local HTTP server without treating HTTPS redirects as success."""

import json
import os
import sys
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in {"health", "ready"}:
        return 1
    port = os.environ.get("PORT", "10000")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        return 1
    hostname = os.environ.get("PENNY_ALLOWED_HOSTS", "").split(",")[0].strip()
    if not hostname:
        return 1
    # This header is sent only over loopback. Production must explicitly trust its ingress.
    request = Request(
        f"http://127.0.0.1:{port}/api/v1/{mode}/",
        headers={"Host": hostname, "X-Forwarded-Proto": "https"},
    )
    try:
        with build_opener(NoRedirects()).open(request, timeout=8) as response:
            return int(response.status != 200 or json.load(response) != {"status": "ok"})
    except URLError, OSError, ValueError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
