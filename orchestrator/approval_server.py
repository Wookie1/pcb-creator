"""Ephemeral HTTP server for post-routing approval gate.

Serves the board visualizer and provides API endpoints for:
- Continue (approve routing, resume pipeline)
- Import KiCad (upload .kicad_pcb, re-import routing)
- Status check (client detects server availability)

Uses only Python stdlib — no external dependencies.
"""

from __future__ import annotations

import io
import json
import secrets
import socket
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Hostnames a legitimate same-machine request presents. Anything else (e.g. an
# attacker domain used for DNS-rebinding) is rejected — the localhost socket
# bind alone does not stop rebinding because the Host header is attacker-chosen.
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class _ApprovalHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the approval gate server."""

    def log_message(self, format, *args):
        # Suppress default access log noise
        pass

    def _host_ok(self) -> bool:
        """Reject cross-origin / DNS-rebinding requests by Host header."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        return host in _ALLOWED_HOSTS

    def _api_ok(self) -> bool:
        """Host is local AND the request carries the per-session token.

        The token lives in the URL path (``/api/<token>/...``), so a web page on
        another origin — which cannot read this page or guess the ephemeral port
        + secret — cannot forge a state-changing request (CSRF defense).
        """
        return self._host_ok() and secrets.compare_digest(
            self._api_token(), self.server.token
        )

    def _api_token(self) -> str:
        """Extract the token segment from ``/api/<token>/<action>``."""
        parts = self.path.strip("/").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "api" else ""

    def _action(self) -> str:
        """Extract the trailing action from ``/api/<token>/<action>``."""
        parts = self.path.strip("/").split("/")
        return parts[2] if len(parts) >= 3 and parts[0] == "api" else ""

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            if not self._host_ok():
                self.send_error(403)
                return
            self._send_html(self.server.viewer_html)
        elif self._action() == "status":
            if not self._api_ok():
                self.send_error(403)
                return
            self._send_json({"ready": True, "project": self.server.project_name})
        else:
            self.send_error(404)

    def do_POST(self):
        action = self._action()
        if action in ("continue", "import") and not self._api_ok():
            self.send_error(403)
            return

        if action == "continue":
            self._send_json({"status": "ok", "message": "Continuing pipeline..."})
            self.server.result = "continue"
            self.server.approval_event.set()

        elif action == "import":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            tmp_path = None
            try:
                payload = json.loads(body)
                kicad_content = payload.get("content", "")
                filename = payload.get("filename", "imported.kicad_pcb")

                # Write to temp file and run import
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".kicad_pcb", delete=False
                ) as f:
                    f.write(kicad_content)
                    tmp_path = f.name

                from exporters.kicad_importer import import_kicad_pcb

                updated = import_kicad_pcb(
                    tmp_path,
                    self.server.routed_data,
                    self.server.netlist_data,
                )

                # Update server state
                self.server.routed_data = updated

                # Save to project directory
                routed_path = self.server.project_dir / (
                    f"{self.server.project_name}_routed.json"
                )
                routed_path.write_text(json.dumps(updated, indent=2))

                # Regenerate viewer HTML
                from visualizers.placement_viewer import generate_html

                self.server.viewer_html = generate_html(
                    updated,
                    self.server.netlist_data,
                    self.server.bom_data,
                    routed=updated,
                    api_url=self.server.api_url,
                )

                from optimizers.routed_board import routing_stats
                stats = routing_stats(updated)
                self._send_json({
                    "status": "ok",
                    "message": f"Imported {filename}",
                    "stats": {
                        "routed_nets": stats.get("routed_nets", 0),
                        "total_nets": stats.get("total_nets", 0),
                        "completion_pct": stats.get("completion_pct", 0),
                    },
                    "reload": True,
                })
                print(f"  Imported {filename}: {stats.get('routed_nets', 0)}/{stats.get('total_nets', 0)} nets")

            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, status=500)
            finally:
                # Always remove the uploaded temp file, even on parse/import error,
                # so attacker-supplied content never lingers on disk.
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)

        else:
            self.send_error(404)


class _ApprovalServer(HTTPServer):
    """HTTPServer with approval gate state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.viewer_html: str = ""
        self.project_name: str = ""
        self.project_dir: Path = Path(".")
        self.routed_data: dict = {}
        self.netlist_data: dict = {}
        self.bom_data: dict | None = None
        self.api_url: str = ""
        self.token: str = ""
        self.result: str | None = None
        self.approval_event = threading.Event()


def _find_free_port() -> int:
    """Find an available port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def serve_approval_gate(
    project_name: str,
    routed: dict,
    netlist: dict,
    bom: dict | None,
    project_dir: Path,
    port: int = 0,
    drc_report: dict | None = None,
) -> str:
    """Serve the visualizer and wait for user approval.

    Opens the board viewer in the browser with Export, Import, and Continue
    buttons. Blocks until the user clicks Continue.

    Args:
        project_name: Project name for display.
        routed: Routed dict (traces, vias, fills).
        netlist: Netlist dict.
        bom: Optional BOM dict for component details.
        project_dir: Project directory for saving imported files.
        port: Port to bind (0 = auto-select).
        drc_report: Optional DRC report dict to display in viewer.

    Returns:
        "continue" when user approves.
    """
    if port == 0:
        port = _find_free_port()

    # Per-session secret embedded in the API path. The viewer (served from the
    # same origin) concatenates api_url for its fetches, so it carries the token
    # automatically; a cross-origin page cannot read it → CSRF-safe.
    token = secrets.token_urlsafe(24)
    api_url = f"http://localhost:{port}/api/{token}"

    # Generate viewer HTML with embedded API URL
    from visualizers.placement_viewer import generate_html

    viewer_html = generate_html(
        routed, netlist, bom, routed=routed, api_url=api_url,
        drc_report=drc_report,
    )

    # Create and configure server
    server = _ApprovalServer(("localhost", port), _ApprovalHandler)
    server.viewer_html = viewer_html
    server.project_name = project_name
    server.project_dir = project_dir
    server.routed_data = routed
    server.netlist_data = netlist
    server.bom_data = bom
    server.api_url = api_url
    server.token = token

    # Start server in background thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://localhost:{port}/"
    print(f"\n  Board viewer: {url}")
    print(f"  Review the routed board, then click 'Continue to DRC & Export'")
    print(f"  (or Ctrl+C to abort)\n")

    # Open in browser
    webbrowser.open(url)

    # Wait for approval
    try:
        server.approval_event.wait()
    except KeyboardInterrupt:
        print("\n  Approval gate cancelled by user")
        server.shutdown()
        raise SystemExit(1)

    result = server.result or "continue"
    server.shutdown()

    return result
