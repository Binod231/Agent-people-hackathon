#!/usr/bin/env python3
"""Run the agent locally, through the same handler the Function URL calls.

    python scripts/local_server.py          # http://127.0.0.1:8000
    python scripts/local_server.py 8080

A development tool, not a server to expose. It binds 127.0.0.1 and nothing
else: `http.server` is explicitly documented as not hardened for production
use, and the handler behind it is a public unauthenticated endpoint whose only
spend controls live in `infra/template.yaml` (an invocation-rate alarm and a
billing alarm, over the account's own concurrency ceiling) rather than in this
process. Do not bind it to 0.0.0.0, do not
put it behind a tunnel, and do not reach for it as a staging environment --
`scripts/deploy.sh` is forty seconds away and produces the real thing.

**Why it exists.** Every request, assets included, is handed to
`agent.handler.handler` in-process with a synthesized Lambda Function URL
event. So the routing, the body guard, the error boundary, and the response
envelope exercised locally are the same code deployed traffic goes through --
not a local reimplementation that can drift. The only thing this file decides
is how an HTTP request becomes an event and how the returned dictionary becomes
an HTTP response.

**What it needs.** `GET /` and the two asset routes work with nothing
configured. `POST /api/chat` reaches GitHub's public endpoints and Amazon
Bedrock for real, so it needs AWS credentials in the environment and a region:

    export TARGET_REGION=us-east-1
    export MODEL_ID=amazon.nova-lite-v1:0

Both are read from `.preflight-ok` by the deploy script and set as function
environment variables in the deployed stack; locally they are yours to export.
Without them a chat request answers with the `INTERNAL` message and logs which
variable was missing.

The asset cache is cleared before each request, so editing `ui/styles.css` and
reloading shows the change. The deployed function keeps the cache, which is the
point of having one.
"""

from __future__ import annotations

import base64
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agent.config import MAX_BODY_BYTES  # noqa: E402
from agent.handler import handler, reset_assets, ui_dir  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# One byte past the guard in `agent.handler`, which is all that needs reading to
# answer a 413. A `Content-Length` of four gigabytes is then a bounded read
# rather than an allocation, and the handler still measures a body that is over
# the limit as over the limit.
MAX_READ_BYTES = MAX_BODY_BYTES + 1


class LocalFunctionUrl(BaseHTTPRequestHandler):
    """Translates HTTP to a payload-format-2.0 event and back."""

    server_version = "LocalFunctionUrl/1.0"
    protocol_version = "HTTP/1.1"  # so Content-Length is honored and reused

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        # A HEAD is routed as the GET it mirrors, so the status and headers are
        # the ones the GET would carry, and only the body is withheld.
        self._dispatch("GET", send_body=False)

    # -- the bridge --------------------------------------------------------

    def _dispatch(self, method: str, *, send_body: bool = True) -> None:
        # Serve the three assets from disk on every request, so a UI edit is
        # visible on a reload rather than after a restart.
        reset_assets()
        try:
            response = handler(self._event(method))
        except Exception:  # pragma: no cover - the handler does not raise
            logging.exception("handler raised, which it is not supposed to do")
            self._send(500, {"Content-Type": "text/plain; charset=utf-8"}, b"500\n")
            return
        status, headers, payload = _unpack(response)
        self._send(status, headers, payload if send_body else b"")

    def _event(self, method: str) -> dict[str, Any]:
        """The Lambda Function URL event this request would have produced.

        `rawPath` excludes the query string and `rawQueryString` carries it,
        which is the format-2.0 split -- `self.path` is the two joined.
        """
        raw_path, _, query = self.path.partition("?")
        body = self._read_body()
        event: dict[str, Any] = {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": raw_path or "/",
            "rawQueryString": query,
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "requestContext": {
                "http": {
                    "method": method,
                    "path": raw_path or "/",
                    "protocol": self.request_version,
                    "sourceIp": self.client_address[0],
                    "userAgent": self.headers.get("User-Agent", ""),
                },
            },
            "isBase64Encoded": False,
            "body": None,
        }
        if body:
            try:
                event["body"] = body.decode("utf-8")
            except UnicodeDecodeError:
                # What a Function URL does with a body it cannot treat as text.
                event["body"] = base64.b64encode(body).decode("ascii")
                event["isBase64Encoded"] = True
        return event

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(min(length, MAX_READ_BYTES))

    def _send(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    # -- logging -----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write(f"{self.address_string()} {format % args}\n")


def _unpack(response: Any) -> tuple[int, dict[str, str], bytes]:
    """A handler response as `(status, headers, bytes)`.

    Tolerant of a shape it should never see, because a local dev tool crashing
    on a malformed response is less useful than one that shows it to you.
    """
    if not isinstance(response, dict):
        return 500, {"Content-Type": "text/plain; charset=utf-8"}, b"bad response\n"
    status = response.get("statusCode")
    headers = response.get("headers")
    body = response.get("body")
    payload = b"" if body is None else str(body).encode("utf-8")
    if response.get("isBase64Encoded") and isinstance(body, str):
        try:
            payload = base64.b64decode(body, validate=True)
        except (ValueError, TypeError):
            pass
    return (
        status if isinstance(status, int) else 500,
        {str(k): str(v) for k, v in (headers or {}).items()},
        payload,
    )


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    except ValueError:
        print(f"usage: {argv[0]} [port]", file=sys.stderr)
        return 2

    missing = [
        name
        for name in ("index.html", "styles.css", "app.js")
        if not (ui_dir() / name).is_file()
    ]
    print(f"assets: {ui_dir()}")
    if missing:
        print(f"missing (those routes will 404): {', '.join(missing)}")
    print(f"serving on http://{HOST}:{port}  -- loopback only, Ctrl-C to stop")
    print("POST /api/chat calls GitHub and Bedrock for real; export")
    print("TARGET_REGION and MODEL_ID, and have AWS credentials available.")

    server = ThreadingHTTPServer((HOST, port), LocalFunctionUrl)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
