# Core modules for AI and automation
import sys

# Suppress known Windows Python asyncio Proactor bug (WinError 10054 on closed pipe/transport)
if sys.platform == "win32":
    try:
        import asyncio.proactor_events
        _orig_call_conn_lost = asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost

        def _safe_call_connection_lost(self, exc):
            try:
                _orig_call_conn_lost(self, exc)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass
            except OSError as e:
                if getattr(e, "winerror", None) in (10054, 10053, 10038):
                    pass
                else:
                    raise

        asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost = _safe_call_connection_lost
    except Exception:
        pass

try:
    from .session_capturer import (
        save_auth_target_url,
        clear_auth_session,
        get_auth_session_summary,
        capture_interactive_session,
    )
except Exception:
    pass
