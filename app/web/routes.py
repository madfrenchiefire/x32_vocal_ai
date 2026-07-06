"""HTTP routes.

Only the diagnostics export endpoint is implemented this phase (per
CLAUDE.md's diagnostics section + the task scope for this session). The
routing grid, snapshot apply/restore, per-channel controls, MIDI slot
config, and spectrum display described in CLAUDE.md's "Web UI" section are
later-phase work and intentionally have no routes here yet.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template, send_file

from app.diagnostics.export import build_debug_bundle

bp = Blueprint("main", __name__)


@bp.route("/")
def index() -> str:
    return render_template("index.html")


@bp.route("/api/diagnostics/export", methods=["POST"])
def export_debug_bundle():
    diagnostics = current_app.extensions["diagnostics"]
    state = current_app.extensions["app_state"]
    config = current_app.extensions["app_config"]
    osc = current_app.extensions.get("osc_connection")

    correlation_id = diagnostics.log_user_action("export_debug_bundle")

    bundles_dir = config.resolved_log_dir() / "bundles"
    filename = f"debug_bundle_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    output_path = bundles_dir / filename

    connection_info = dict(osc.xinfo) if osc is not None else {}
    try:
        build_debug_bundle(output_path, diagnostics, state, config, connection_info=connection_info)
    except Exception as exc:
        diagnostics.log_error(exc, context="export_debug_bundle failed", correlation_id=correlation_id)
        raise

    diagnostics.log_state_change(
        "debug_bundle_exported", after={"path": str(output_path)}, correlation_id=correlation_id
    )
    return send_file(output_path, as_attachment=True, download_name=filename)
