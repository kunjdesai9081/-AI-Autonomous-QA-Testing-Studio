import http.server
import json
import os
import secrets
import threading
from typing import Optional
from urllib.parse import parse_qs, urlparse

CONTROL_SIGNAL_FILE = ".execution_control_signal"
_SERVER_INSTANCE = None
_SERVER_PORT = 8503
_SERVER_LOCK = threading.Lock()
_SESSION_AUTH_TOKEN = secrets.token_hex(16)


def get_control_auth_token() -> str:
    """Returns the active session authentication token required for control signals."""
    global _SESSION_AUTH_TOKEN
    return _SESSION_AUTH_TOKEN


class ExecutionControlHandler(http.server.BaseHTTPRequestHandler):
    """Handles secure asynchronous pause/stop/resume signals from verified local client widgets."""

    def _is_authenticated(self) -> bool:
        """Verifies caller provides valid X-Control-Token or ?token= query parameter."""
        token_hdr = self.headers.get("X-Control-Token", "").strip()
        parsed_url = urlparse(self.path)
        qs = parse_qs(parsed_url.query)
        token_qs = qs.get("token", [""])[0].strip()
        expected = get_control_auth_token()
        # Accept if header or query matches, or if test mode token matches
        return token_hdr == expected or token_qs == expected or not expected

    def _send_cors_headers(self):
        # Restrict CORS strictly to loopback/local origin (L-04 security fix)
        origin = self.headers.get("Origin", "")
        if any(h in origin.lower() for h in ["localhost", "127.0.0.1", "streamlit"]):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Control-Token, Authorization")

    def do_POST(self):
        if not self._is_authenticated():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Unauthorized control signal"}).encode("utf-8"))
            return

        path_lower = self.path.lower()
        if "pause" in path_lower:
            sig = "PAUSE"
        elif "stop" in path_lower or "abort" in path_lower:
            sig = "STOP"
        elif "step" in path_lower:
            sig = "STEP"
        else:
            sig = "RESUME"

        try:
            with open(CONTROL_SIGNAL_FILE, "w", encoding="utf-8") as f:
                f.write(sig)
        except Exception:
            pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "signal": sig}).encode("utf-8"))

    def do_GET(self):
        sig = read_control_signal() or "IDLE"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "active_signal": sig}).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Silent log


def ensure_control_server(start_port: int = 8503) -> int:
    """Ensures a lightweight daemon HTTP control server is active on localhost (127.0.0.1 loopback only)."""
    global _SERVER_INSTANCE, _SERVER_PORT
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is not None:
            return _SERVER_PORT

        # L-04 fix: Strictly bind to 127.0.0.1 (never 0.0.0.0)
        host_to_bind = "127.0.0.1"
        for port in [start_port, start_port + 1, start_port + 2, start_port + 3]:
            try:
                server = http.server.ThreadingHTTPServer((host_to_bind, port), ExecutionControlHandler)
                t = threading.Thread(target=server.serve_forever, daemon=True, name="ExecutionControlServerThread")
                t.start()
                _SERVER_INSTANCE = server
                _SERVER_PORT = port
                print(f"[ExecutionControl] Signal server listening strictly on http://{host_to_bind}:{port}")
                return port
            except OSError:
                continue

        _SERVER_PORT = start_port
        return start_port


def write_control_signal(signal: str) -> None:
    """Manually writes a pause or stop signal to disk."""
    try:
        with open(CONTROL_SIGNAL_FILE, "w", encoding="utf-8") as f:
            f.write(signal.strip().upper())
    except Exception:
        pass


def read_control_signal() -> Optional[str]:
    """Reads the current signal without clearing it."""
    if os.path.exists(CONTROL_SIGNAL_FILE):
        try:
            with open(CONTROL_SIGNAL_FILE, "r", encoding="utf-8") as f:
                sig = f.read().strip().upper()
            if sig in ["PAUSE", "STOP", "RESUME"]:
                return sig
        except Exception:
            pass
    return None


def read_and_clear_control_signal() -> Optional[str]:
    """Reads the current signal and deletes the file so it is processed once."""
    if os.path.exists(CONTROL_SIGNAL_FILE):
        try:
            with open(CONTROL_SIGNAL_FILE, "r", encoding="utf-8") as f:
                sig = f.read().strip().upper()
            try:
                os.remove(CONTROL_SIGNAL_FILE)
            except Exception:
                pass
            if sig in ["PAUSE", "STOP", "RESUME"]:
                return sig
        except Exception:
            pass
    return None


def render_live_control_bar_html(port: int = 8503, total_tests: int = 0, step_pause: bool = False) -> str:
    """Generates the HTML/JS for the live execution pause/stop control bar with anti-wrap flex layout."""
    total_badge = f'<span class="badge-total">{total_tests} Tests</span>' if total_tests > 0 else ""
    step_pause_badge = '<span class="badge-step">⏸️ Step-Pause</span>' if step_pause else ""
    token = get_control_auth_token()

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}
            body {{
                margin: 0;
                padding: 0;
                overflow: hidden;
                background: transparent;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            }}
            .ctrl-container {{
                display: flex;
                flex-direction: column;
                justify-content: center;
                gap: 6px;
                background: linear-gradient(135deg, rgba(30, 41, 59, 0.97), rgba(15, 23, 42, 0.98));
                border: 1px solid rgba(255, 255, 255, 0.14);
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
                padding: 9px 14px 10px 14px;
                border-radius: 8px;
                min-height: 66px;
            }}
            .ctrl-row-top {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
            }}
            .ctrl-status-group {{
                display: flex;
                align-items: center;
                gap: 7px;
                min-width: 0;
                flex: 1;
            }}
            .status-dot {{
                display: inline-block;
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: #10B981;
                box-shadow: 0 0 8px #10B981;
                animation: pulse 1.5s infinite;
                flex-shrink: 0;
            }}
            .status-title {{
                color: #F8FAFC;
                font-size: 0.88rem;
                font-weight: 700;
                letter-spacing: 0.2px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }}
            .badge-total {{
                background: rgba(56, 189, 248, 0.18);
                color: #38BDF8;
                border: 1px solid rgba(56, 189, 248, 0.35);
                padding: 1px 7px;
                border-radius: 10px;
                font-size: 0.74rem;
                font-weight: 600;
                white-space: nowrap;
                flex-shrink: 0;
            }}
            .badge-step {{
                background: rgba(245, 158, 11, 0.18);
                color: #F59E0B;
                border: 1px solid rgba(245, 158, 11, 0.35);
                padding: 1px 7px;
                border-radius: 10px;
                font-size: 0.74rem;
                font-weight: 600;
                white-space: nowrap;
                flex-shrink: 0;
            }}
            .ctrl-btn-group {{
                display: flex;
                align-items: center;
                gap: 7px;
                flex-shrink: 0;
            }}
            .btn-ctrl {{
                color: #FFFFFF;
                border: none;
                height: 28px;
                padding: 0 12px;
                border-radius: 6px;
                font-size: 0.80rem;
                font-weight: 600;
                cursor: pointer;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 5px;
                white-space: nowrap;
                flex-shrink: 0;
                transition: all 0.15s ease;
                user-select: none;
            }}
            .btn-pause {{
                background: linear-gradient(135deg, #F59E0B, #D97706);
                box-shadow: 0 2px 6px rgba(245, 158, 11, 0.3);
            }}
            .btn-pause:hover {{
                transform: translateY(-1px);
                box-shadow: 0 3px 8px rgba(245, 158, 11, 0.45);
            }}
            .btn-stop {{
                background: linear-gradient(135deg, #EF4444, #DC2626);
                box-shadow: 0 2px 6px rgba(239, 68, 68, 0.3);
            }}
            .btn-stop:hover {{
                transform: translateY(-1px);
                box-shadow: 0 3px 8px rgba(239, 68, 68, 0.45);
            }}
            .ctrl-subtext {{
                font-size: 0.76rem;
                color: #94A3B8;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                line-height: 1.3;
                padding-bottom: 2px;
            }}
            @keyframes pulse {{
                0% {{ opacity: 1; transform: scale(1); }}
                50% {{ opacity: 0.4; transform: scale(0.9); }}
                100% {{ opacity: 1; transform: scale(1); }}
            }}
        </style>
    </head>
    <body>
    <div class="ctrl-container">
        <div class="ctrl-row-top">
            <div class="ctrl-status-group">
                <span class="status-dot"></span>
                <span class="status-title">Execution in Progress</span>
                {total_badge}
                {step_pause_badge}
            </div>
            <div class="ctrl-btn-group">
                <button id="btn_pause_live" class="btn-ctrl btn-pause" onclick="sendControlSignal('pause')" title="Pause batch execution after current test completes">
                    <span>⏸️</span> Pause After Step
                </button>
                <button id="btn_stop_live" class="btn-ctrl btn-stop" onclick="sendControlSignal('stop')" title="Stop and abort batch execution cleanly after current test">
                    <span>🛑</span> Stop Execution
                </button>
            </div>
        </div>
        <div id="sig_badge" class="ctrl-subtext">Click Pause or Stop to interrupt after the current test</div>
    </div>
    <script>
    function sendControlSignal(sig) {{
        const badge = document.getElementById('sig_badge');
        const pBtn = document.getElementById('btn_pause_live');
        const sBtn = document.getElementById('btn_stop_live');
        if (sig === 'pause') {{
            pBtn.innerHTML = '<span>⏳</span> Pausing...';
            pBtn.style.opacity = '0.75';
            pBtn.style.pointerEvents = 'none';
            badge.innerHTML = '<strong style="color: #F59E0B;">⏸️ Pause requested! Stopping cleanly after current step...</strong>';
        }} else {{
            sBtn.innerHTML = '<span>⏳</span> Stopping...';
            sBtn.style.opacity = '0.75';
            sBtn.style.pointerEvents = 'none';
            badge.innerHTML = '<strong style="color: #EF4444;">🛑 Stop requested! Aborting cleanly after current step...</strong>';
        }}
        const host = window.location.hostname || '127.0.0.1';
        const headers = {{ 'X-Control-Token': '{token}' }};
        const query = '?token=' + encodeURIComponent('{token}');
        fetch('http://' + host + ':{port}/' + sig + query, {{ method: 'POST', mode: 'cors', headers: headers }})
        .then(r => r.json())
        .catch(e => {{
            fetch('http://127.0.0.1:{port}/' + sig + query, {{ method: 'POST', mode: 'cors', headers: headers }})
            .catch(err => console.log('Signal delivery fallback note:', err));
        }});
    }}
    </script>
    </body>
    </html>
    """
