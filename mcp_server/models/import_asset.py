"""Typed results for asset import tools (issue #108)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportAssetResult(BaseModel):
    imported: bool = False
    target_path: str
    detected_type: str | None = None
    dry_run: bool = False
    scan_complete: bool = False
    # #418: True when the imported .wav's loop config (import_settings.loop_mode /
    # loop_begin / loop_end) was patched into the .import sidecar and reimported.
    loop_applied: bool = False


class CreateMaterialResult(BaseModel):
    material_path: str
    created: bool = False
    channels_set: list[str] = Field(default_factory=list)
    dry_run: bool = False


class ImportStatusResult(BaseModel):
    imported: bool = False
    last_modified: str | None = None
    type: str | None = None
    # #459/#453: true while the editor's filesystem scan is in flight — a status
    # read then is provisional (an import just triggered the scan).
    scanning: bool = False
    # Stable reason token while pending: ``rescan_in_flight`` when not yet
    # imported and the scan is running.
    reason: str | None = None


class ImportBatchItem(BaseModel):
    """One entry of ``godot_asset_import_batch`` — same fields as a single import."""

    source: str
    target_path: str
    options: dict[str, Any] | None = None


class ImportBatchItemResult(BaseModel):
    index: int
    target_path: str
    ok: bool
    detected_type: str | None = None
    scan_complete: bool = False
    loop_applied: bool = False
    error: str | None = None


class ImportBatchResult(BaseModel):
    results: list[ImportBatchItemResult] = Field(default_factory=list)
    ok_all: bool = False
    imported_count: int = 0
    failed_count: int = 0
    dry_run: bool = False
    # #461-style honesty: a stop_on_error halt is never silent.
    aborted_at: int | None = None
    skipped_count: int | None = None
    hint: str | None = None
