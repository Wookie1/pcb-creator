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
import socket
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


class _ApprovalHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the approval gate server."""

    def log_message(self, format, *args):
        # Suppress default access log noise
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            self._send_html(self.server.viewer_html)
        elif self.path == "/api/status":
            self._send_json({"ready": True, "project": self.server.project_name})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/continue":
            self._send_json({"status": "ok", "message": "Continuing pipeline..."})
            self.server.result = "continue"
            self.server.approval_event.set()

        elif self.path == "/api/import":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

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

                # Clean up temp file
                Path(tmp_path).unlink(missing_ok=True)

                stats = updated.get("routing", {}).get("statistics", {})
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

    api_url = f"http://localhost:{port}/api"

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
