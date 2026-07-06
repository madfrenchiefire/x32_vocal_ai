from __future__ import annotations

import json
import zipfile

from app.config import AppConfig
from app.diagnostics.export import build_debug_bundle
from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState


def test_build_debug_bundle_contains_expected_members(tmp_path):
    state = AppState()
    diagnostics = DiagnosticsLogger(log_dir=tmp_path / "logs", state_provider=state.summary)
    diagnostics.log_user_action("test_action")
    config = AppConfig(console_ip="192.168.1.10")

    output_path = tmp_path / "bundle.zip"
    build_debug_bundle(output_path, diagnostics, state, config, connection_info={"model": "X32"})

    with zipfile.ZipFile(output_path) as zf:
        names = set(zf.namelist())
        assert names == {
            "events.jsonl",
            "manifest.json",
            "routing_state.json",
            "config.json",
            "connection_info.json",
            "versions.json",
            "summary.txt",
        }

        events = zf.read("events.jsonl").decode().strip().splitlines()
        assert len(events) == 1

        config_data = json.loads(zf.read("config.json"))
        assert config_data["console_ip"] == "192.168.1.10"

        connection_info = json.loads(zf.read("connection_info.json"))
        assert connection_info == {"model": "X32"}

        versions = json.loads(zf.read("versions.json"))
        assert "python" in versions
        assert "libraries" in versions

        summary_text = zf.read("summary.txt").decode()
        assert "test_action" in summary_text

    diagnostics.close()
