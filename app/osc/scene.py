"""Console-side scene save: a safety net that outlives the PC.

Every snapshot the app takes lives in the app's own JSON files -- if the
PC dies mid-gig, restoring them needs the PC back. `/save` (doc-confirmed,
X32_OSC.pdf "Manipulation of datasets") writes a real scene into the
console's own scene list, so the pre-app state can be recalled from the
desk's Scenes page with the PC in a ditch:

    /save ,siss scene <index 0-99> <name> <note>
    reply: /save ,si scene <0|1>     (1 = success)

The scene slot is user-chosen (AppConfig.safety_scene_slot, default None
= feature off): scene slots hold real show data, and silently overwriting
one would be exactly the kind of destruction this app exists to avoid --
the user picks a slot they know is free.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

SAVE_ADDRESS = "/save"
SCENE_SLOT_MIN, SCENE_SLOT_MAX = 0, 99
# Scene writes hit the console's internal storage -- give them longer than
# an ordinary parameter query.
SAVE_TIMEOUT_SEC = 10.0

DEFAULT_SCENE_NAME = "X32VOCAL SAFETY"


class SceneSaveError(Exception):
    pass


def save_console_scene(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    slot: int,
    name: str = DEFAULT_SCENE_NAME,
    note: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Save the console's current state as a scene in the given slot.
    Raises SceneSaveError on a failure status or no reply. Per the doc,
    a success status can arrive before the save fully completes on the
    console's storage -- treat it as fire-and-mostly-forget."""
    if not SCENE_SLOT_MIN <= slot <= SCENE_SLOT_MAX:
        raise ValueError(f"scene slot must be {SCENE_SLOT_MIN}-{SCENE_SLOT_MAX}, got {slot}")
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    if note is None:
        note = datetime.now(timezone.utc).strftime("pre-app state %Y-%m-%d %H:%M UTC")

    try:
        reply = osc.query(
            SAVE_ADDRESS, args=("scene", slot, name, note),
            timeout=SAVE_TIMEOUT_SEC, correlation_id=correlation_id,
        )
    except TimeoutError as exc:
        error = SceneSaveError(f"console did not acknowledge the scene save within {SAVE_TIMEOUT_SEC}s")
        diagnostics.log_error(error, context="save_console_scene", correlation_id=correlation_id)
        raise error from exc

    # Real console replies ("scene", 1) on success, ("scene", 0) on failure.
    status = reply[-1] if reply else None
    if status != 1:
        error = SceneSaveError(f"console reported scene save failure (reply {reply!r})")
        diagnostics.log_error(error, context="save_console_scene", correlation_id=correlation_id)
        raise error

    diagnostics.log_state_change(
        "console_scene_saved",
        after={"slot": slot, "name": name, "note": note},
        correlation_id=correlation_id,
    )
