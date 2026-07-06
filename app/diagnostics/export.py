"""Debug bundle export.

Produces a single zip with everything needed to diagnose a problem with
zero access to the running system -- the intended workflow is "attach this
zip to a Claude Code session and ask what went wrong."
"""
from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app import __version__ as app_version

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.diagnostics.logger import DiagnosticsLogger
    from app.state import AppState

# Libraries this app depends on -- versions are recorded best-effort; a
# library not yet installed (e.g. audio/MIDI deps before Phase 1 plumbing
# is wired up) is simply reported as "not installed", not an error.
_TRACKED_PACKAGES = [
    "Flask",
    "Flask-SocketIO",
    "python-osc",
    "mido",
    "python-rtmidi",
    "sounddevice",
    "numpy",
    "scipy",
    "onnxruntime",
]


def _library_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def build_debug_bundle(
    output_path: str | Path,
    diagnostics: "DiagnosticsLogger",
    state: "AppState",
    config: "AppConfig",
    connection_info: dict[str, Any] | None = None,
) -> Path:
    """Write a debug bundle zip to output_path and return its path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "app_version": app_version,
        "session_name": diagnostics.session_name,
    }

    routing_state = {
        "current_snapshot": _serialize(state.current_snapshot),
        "state_summary": state.summary(),
    }

    versions = {
        "python": sys.version,
        "platform": platform.platform(),
        "libraries": _library_versions(),
    }

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Full on-disk event history (not just the in-memory ring buffer window).
        if diagnostics.session_file.exists():
            zf.write(diagnostics.session_file, arcname="events.jsonl")
        else:
            zf.writestr("events.jsonl", "")

        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        zf.writestr("routing_state.json", json.dumps(routing_state, indent=2, default=str))
        zf.writestr("config.json", json.dumps(config.to_dict(), indent=2, default=str))
        zf.writestr(
            "connection_info.json",
            json.dumps(connection_info or {}, indent=2, default=str),
        )
        zf.writestr("versions.json", json.dumps(versions, indent=2, default=str))
        zf.writestr("summary.txt", diagnostics.summary_text(50))

    return output_path
