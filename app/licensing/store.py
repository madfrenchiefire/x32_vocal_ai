"""Where the activated license token and trial state live on disk.

Per-user app-data directory (survives app restarts and re-installs, unlike
anything in the program folder). The token file is just the pasteable
string; trial state is a small JSON record.

Trial tamper-resistance is deliberately modest -- deleting the state file
resets the clock, which is the same fundamental limit every offline trial
has. What's here catches the casual cases (file survives, and an obvious
system-clock rollback is detected), not a determined bypass.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def default_data_dir() -> Path:
    r"""Per-user data directory: %LOCALAPPDATA%\X32VocalAI on Windows,
    $XDG_DATA_HOME/x32_vocal_ai (or ~/.local/share/...) elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return Path(base) / "X32VocalAI"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "x32_vocal_ai"


class LicenseStore:
    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else default_data_dir()
        self.token_path = self.base_dir / "license.key"
        self.trial_path = self.base_dir / "trial.json"

    def _ensure_dir(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # -- activated token -----------------------------------------------------

    def load_token(self) -> str | None:
        try:
            text = self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def save_token(self, token: str) -> None:
        self._ensure_dir()
        self.token_path.write_text(token.strip(), encoding="utf-8")

    def clear_token(self) -> None:
        try:
            self.token_path.unlink()
        except OSError:
            pass

    # -- trial state ---------------------------------------------------------

    def load_trial(self) -> dict | None:
        try:
            return json.loads(self.trial_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_trial(self, record: dict) -> None:
        self._ensure_dir()
        self.trial_path.write_text(json.dumps(record), encoding="utf-8")

    def start_trial(self, machine: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        record = {"first_run": now.isoformat(), "machine": machine, "last_seen": now.isoformat()}
        self.save_trial(record)
        return record
