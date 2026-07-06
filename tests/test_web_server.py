from __future__ import annotations

import zipfile
from io import BytesIO

from app.config import AppConfig
from app.web.server import create_app


def test_index_page_serves_export_button(tmp_path, app_state, diagnostics):
    config = AppConfig(log_dir=str(tmp_path / "logs"), snapshot_dir=str(tmp_path / "snapshots"))
    app, _socketio = create_app(config, app_state, diagnostics)
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200
    assert b"Export Debug Bundle" in response.data


def test_export_endpoint_returns_zip_bundle(tmp_path, app_state, diagnostics):
    config = AppConfig(log_dir=str(tmp_path / "logs"), snapshot_dir=str(tmp_path / "snapshots"))
    app, _socketio = create_app(config, app_state, diagnostics)
    client = app.test_client()

    response = client.post("/api/diagnostics/export")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"

    with zipfile.ZipFile(BytesIO(response.data)) as zf:
        assert "events.jsonl" in zf.namelist()
        assert "summary.txt" in zf.namelist()
