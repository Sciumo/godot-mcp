"""External asset import tools (issue #108).

Generic drop-in for external files (local paths, HTTP URLs, or inline
``data:`` URIs) and PBR material assembly. This is the open-source foundation
for external AI content pipelines (e.g. Meshy, Tripo) without hard-coding
provider-specific logic.

Gated ``asset_import`` toolset.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from mcp_server.bridge import Bridge
from mcp_server.categories import ASSET_IMPORT_TAG
from mcp_server.constraints import TimeoutMs
from mcp_server.models.import_asset import (
    CreateMaterialResult,
    ImportAssetResult,
    ImportBatchItem,
    ImportBatchItemResult,
    ImportBatchResult,
    ImportStatusResult,
)
from mcp_server.safety import MUTATING, READ_ONLY
from mcp_server.tools._route import route

ASSET_IMPORT = {ASSET_IMPORT_TAG}

# Extension → Godot type mapping (auto-detect on import).
_EXT_TO_TYPE: dict[str, str] = {
    ".png": "Texture2D",
    ".jpg": "Texture2D",
    ".jpeg": "Texture2D",
    ".webp": "Texture2D",
    ".svg": "Texture2D",
    ".glb": "PackedScene",
    ".gltf": "PackedScene",
    ".obj": "Mesh",
    ".fbx": "PackedScene",
    ".wav": "AudioStreamWAV",
    ".ogg": "AudioStreamOggVorbis",
    ".mp3": "AudioStreamMP3",
    ".tres": "Resource",
    ".res": "Resource",
    ".tscn": "PackedScene",
    ".scn": "PackedScene",
    ".gd": "GDScript",
    ".cs": "CSharpScript",
    ".gdshader": "Shader",
}


def _detect_type(path: str) -> str | None:
    """Auto-detect Godot resource type from file extension."""
    ext = Path(path).suffix.lower()
    return _EXT_TO_TYPE.get(ext)


def _require_res_path(path: str, field: str = "target_path") -> None:
    if not path.startswith("res://"):
        raise ToolError(f"VALIDATION_ERROR: '{field}' must start with 'res://' (got '{path}').")


# #419: metallic/roughness/emission_enabled are scalars on StandardMaterial3D, not
# textures — a numeric string is a scalar value; anything else for these channels
# is still accepted as a texture path (texture-driven roughness is legitimate).
_SCALAR_CHANNELS = ("metallic", "roughness", "emission_enabled")


def _is_scalar(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


async def _validate_material_params(bridge: Bridge, params: dict[str, Any]) -> None:
    """Validate the material channels BEFORE the (dry-run or real) bridge call (#419).

    Every texture-typed channel that carries a res:// path is probed for existence via
    ``cmd_search_files`` (a read-only recursive search) so a missing texture fails
    the call — a dry-run gets the same validation as the real run instead of a
    success-flavored empty preview. The search globs the *basename* (the addon's glob
    matches file names, not relative paths — verified on Godot 4.7) and passes only
    when a match's path ends with the requested ``res://`` path, so a different file
    sharing the basename can't satisfy the probe.
    """
    for channel in ("albedo", "normal", "roughness", "metallic", "ao", "emission"):
        value = str(params.get(channel, ""))
        if not value or _is_scalar(value):
            continue  # empty or scalar — nothing filesystem-backed to validate
        _require_res_path(value, field=channel)
        rel = value.removeprefix("res://")
        probe = await route(
            bridge, "cmd_search_files",
            {"directory": "res://", "name_glob": rel.rsplit("/", 1)[-1]},
        )
        matches = probe.get("matches") or []
        if not any(str(m).endswith(value) for m in matches):
            raise ToolError(
                f"RESOURCE_NOT_FOUND: Texture not found for '{channel}': '{value}'."
            )


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


async def _download_url(url: str) -> str:
    """Download ``url`` to a temporary file and return its absolute path.

    Streams the response to disk in 64 KiB chunks to avoid buffering the
    entire payload in memory. Raises ``ToolError`` on network or HTTP failure.
    """
    try:
        import httpx2 as httpx
    except ImportError as exc:
        raise ToolError(
            "INTERNAL_ERROR: httpx2 is not installed. Install it to enable URL download support."
        ) from exc

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                suffix = Path(url.split("?")[0]).suffix or ".tmp"
                fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                try:
                    with os.fdopen(fd, "wb") as tmp:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            tmp.write(chunk)
                    return tmp_path
                except Exception:
                    os.close(fd)
                    raise
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"DOWNLOAD_FAILED: HTTP {exc.response.status_code} for '{url}'.") from exc
    except httpx.RequestError as exc:
        raise ToolError(f"DOWNLOAD_FAILED: Network error downloading '{url}': {exc}.") from exc


# ---------------------------------------------------------------------------
# Inline data sources — AI image/audio/model generators return base64 blobs,
# so a ``data:<mime>;base64,<payload>`` source is decoded to a temp file and
# then follows the same path as a download.
# ---------------------------------------------------------------------------

_MAX_INLINE_BYTES = 32 * 1024 * 1024

# Leading-byte signatures, used both to verify a payload matches its target
# extension and to name what it actually is when it does not.
_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PNG", (".png",)),
    ("JPEG", (".jpg", ".jpeg")),
    ("WebP", (".webp",)),
    ("WAV", (".wav",)),
    ("Ogg", (".ogg",)),
    ("MP3", (".mp3",)),
    ("GLB", (".glb",)),
    ("SVG", (".svg",)),
)


def _sniff(data: bytes) -> str | None:
    """Name the payload's format from its leading bytes, or ``None`` if unknown."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "WAV"
    if data.startswith(b"OggS"):
        return "Ogg"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "MP3"
    if data.startswith(b"glTF"):
        return "GLB"
    if b"<svg" in data[:1024]:
        return "SVG"
    return None


def _is_data_uri(source: str) -> bool:
    return source.startswith("data:")


def _decode_data_uri(source: str, target_path: str) -> bytes:
    """Decode and validate a ``data:`` URI against ``target_path``'s extension.

    Pure (no filesystem), so dry-runs get the same validation as real runs.
    """
    header, sep, payload = source.partition(",")
    if not sep or not header.endswith(";base64"):
        raise ToolError(
            "VALIDATION_ERROR: inline sources must be base64 data URIs "
            "('data:<mime>;base64,<payload>')."
        )
    # 4 base64 chars encode 3 bytes — reject before decoding a huge payload.
    if len(payload) * 3 // 4 > _MAX_INLINE_BYTES:
        raise ToolError(
            f"VALIDATION_ERROR: inline payload too large (limit {_MAX_INLINE_BYTES} bytes); "
            "write it to disk or serve it over HTTP and pass that as 'source' instead."
        )
    try:
        data = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError(f"VALIDATION_ERROR: data URI payload is not valid base64: {exc}.") from exc
    if len(data) > _MAX_INLINE_BYTES:
        raise ToolError(
            f"VALIDATION_ERROR: inline payload too large ({len(data)} bytes, limit "
            f"{_MAX_INLINE_BYTES})."
        )
    ext = Path(target_path).suffix.lower()
    expected = next((name for name, exts in _SIGNATURES if ext in exts), None)
    actual = _sniff(data)
    if expected is not None and actual != expected:
        raise ToolError(
            f"VALIDATION_ERROR: payload for '{target_path}' is not {expected} "
            f"(looks like {actual or 'unknown data'}); fix the extension or the source."
        )
    return data


def _write_temp(data: bytes, target_path: str) -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=Path(target_path).suffix or ".tmp")
    with os.fdopen(fd, "wb") as tmp:
        tmp.write(data)
    return tmp_path


def _validate_source(source: str, target_path: str) -> None:
    """Checks shared by the real run and the dry-run (no network, no writes)."""
    _require_res_path(target_path)
    if _is_data_uri(source):
        _decode_data_uri(source, target_path)


async def _import_one(
    bridge: Bridge, source: str, target_path: str, options: dict[str, Any]
) -> dict[str, Any]:
    """Materialize ``source`` locally, hand it to ``cmd_import_asset``, clean up."""
    _validate_source(source, target_path)
    local_source = source
    temp_path: str | None = None
    if _is_data_uri(source):
        temp_path = _write_temp(_decode_data_uri(source, target_path), target_path)
        local_source = temp_path
    elif _is_url(source):
        temp_path = await _download_url(source)
        local_source = temp_path

    params: dict[str, Any] = {
        "source": local_source,
        "target_path": target_path,
        "overwrite": options.get("overwrite", False),
        "import_settings": options.get("import_settings", {}),
    }
    try:
        return await route(bridge, "cmd_import_asset", params)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


# ---------------------------------------------------------------------------
# Public tool registration
# ---------------------------------------------------------------------------

# ``cmd_import_asset`` only kicks EditorFileSystem.scan() (async) on the addon
# side; the .import sidecar lands a few frames later. Poll from the server —
# a busy-wait in the handler would block the very main thread the scan needs.
_SCAN_POLL_INTERVAL_S = 0.25
_SCAN_TIMEOUT_MS_DEFAULT = 5000


async def _poll_import_ready(
    bridge: Bridge, target_path: str, timeout_ms: int
) -> dict[str, Any] | None:
    """Poll ``cmd_get_import_status`` until the sidecar exists or time is up.

    Returns the ready status result, or ``None`` if the budget expired first.
    Transient command failures are treated as not-ready (retried until deadline).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while True:
        response = await bridge.send("cmd_get_import_status", {"target_path": target_path})
        if response.ok and (response.result or {}).get("imported"):
            return dict(response.result or {})
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_SCAN_POLL_INTERVAL_S)


def register_import_asset(mcp: FastMCP, bridge: Bridge) -> None:
    """Register the asset import tools."""

    @mcp.tool(meta=MUTATING, tags=ASSET_IMPORT)
    async def import_asset(
        source: str,
        target_path: str,
        options: dict[str, Any] | None = None,
        dry_run: bool = False,
        wait_for_scan: bool = False,
        timeout_ms: TimeoutMs = _SCAN_TIMEOUT_MS_DEFAULT,
    ) -> ImportAssetResult:
        """Bring an external file into the Godot project.

        ``source`` may be a local filesystem path, an HTTP(S) URL, or an inline
        ``data:<mime>;base64,<payload>`` URI (e.g. an image returned by an
        AI image generator; max 32 MiB decoded, and the bytes must match the
        ``target_path`` extension — checked on dry-runs too). The file is
        copied into ``target_path`` (``res://…``) and the editor filesystem is
        updated so Godot imports it on the next scan.

        ``options`` (optional dict):
        - ``overwrite`` (bool, default ``False``)
        - ``import_settings`` (dict, type-specific — e.g.
          ``{"type": "Texture2D", "compress": "lossy"}``). For a ``.wav``
          (AudioStreamWAV): ``{"loop_mode": 1, "loop_begin": 0, "loop_end": N}``
          (#418) — looping music/ambience through the MCP surface; the loop
          config lands in the ``.import`` sidecar and the artifact is reimported
          (``loop_applied`` in the result reports whether it was applied).

        ``wait_for_scan`` (default ``False``): after copying, poll the import
        status until the editor's async scan has produced the ``.import``
        sidecar (up to ``timeout_ms``); ``scan_complete`` in the result reports
        whether the import artifacts are ready (vs just the copy landing).
        """
        _validate_source(source, target_path)
        options = options or {}
        detected_type = _detect_type(target_path)

        preview = ImportAssetResult(
            imported=False,
            target_path=target_path,
            detected_type=detected_type,
            dry_run=True,
        )
        if dry_run:
            return preview

        result = await _import_one(bridge, source, target_path, options)
        scan_complete = False
        if wait_for_scan and result.get("imported", True):
            scan_complete = await _poll_import_ready(bridge, target_path, timeout_ms) is not None
        return ImportAssetResult(
            imported=result.get("imported", True),
            target_path=result.get("target_path", target_path),
            detected_type=result.get("detected_type") or detected_type,
            dry_run=False,
            scan_complete=scan_complete,
            # #418: the .wav loop config landed in the .import sidecar + reimport.
            loop_applied=result.get("loop_applied", False),
        )

    @mcp.tool(meta=MUTATING, tags=ASSET_IMPORT)
    async def import_batch(
        items: Annotated[list[ImportBatchItem], Field(min_length=1, max_length=100)],
        stop_on_error: bool = False,
        dry_run: bool = False,
        wait_for_scan: bool = False,
        timeout_ms: TimeoutMs = _SCAN_TIMEOUT_MS_DEFAULT,
    ) -> ImportBatchResult:
        """Import many files in one call — e.g. a set of AI-generated sprites.

        Each item takes the same ``source`` / ``target_path`` / ``options`` as
        ``godot_asset_import_asset`` (path, URL, or ``data:`` URI). Items run in
        order; each gets its own ``ok``/``error`` in ``results``. With
        ``stop_on_error=True`` the first failure halts the batch and the result
        carries ``aborted_at``, ``skipped_count`` and a ``hint``. ``dry_run``
        validates every item (path, payload decode and type check) without
        importing. ``wait_for_scan`` polls each imported item's ``.import``
        sidecar, sharing one ``timeout_ms`` budget across the batch.
        """
        results: list[ImportBatchItemResult] = []
        aborted_at: int | None = None
        for index, item in enumerate(items):
            detected_type = _detect_type(item.target_path)
            try:
                if dry_run:
                    _validate_source(item.source, item.target_path)
                    out: dict[str, Any] = {}
                else:
                    out = await _import_one(
                        bridge, item.source, item.target_path, item.options or {}
                    )
            except ToolError as exc:
                results.append(
                    ImportBatchItemResult(
                        index=index,
                        target_path=item.target_path,
                        ok=False,
                        detected_type=detected_type,
                        error=str(exc),
                    )
                )
                if stop_on_error:
                    aborted_at = index
                    break
                continue
            results.append(
                ImportBatchItemResult(
                    index=index,
                    target_path=out.get("target_path", item.target_path),
                    ok=True,
                    detected_type=out.get("detected_type") or detected_type,
                    loop_applied=out.get("loop_applied", False),
                )
            )

        if wait_for_scan and not dry_run:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000.0
            for r in results:
                if r.ok:
                    remaining_ms = max(0, int((deadline - loop.time()) * 1000))
                    ready = await _poll_import_ready(bridge, r.target_path, remaining_ms)
                    r.scan_complete = ready is not None

        failed = sum(1 for r in results if not r.ok)
        skipped = len(items) - len(results) if aborted_at is not None else None
        return ImportBatchResult(
            results=results,
            ok_all=failed == 0 and aborted_at is None,
            imported_count=0 if dry_run else len(results) - failed,
            failed_count=failed,
            dry_run=dry_run,
            aborted_at=aborted_at,
            skipped_count=skipped,
            hint=(
                f"Stopped at item {aborted_at}; {skipped} item(s) were not attempted. "
                "Fix that item and resubmit the remainder."
                if aborted_at is not None
                else None
            ),
        )

    @mcp.tool(meta=MUTATING, tags=ASSET_IMPORT)
    async def create_material_from_textures(
        albedo: str = "",
        normal: str = "",
        roughness: str = "",
        metallic: str = "",
        ao: str = "",
        emission: str = "",
        emission_enabled: str = "",
        path: str = "",
        dry_run: bool = False,
    ) -> CreateMaterialResult:
        """Assemble a PBR material from texture paths and save it as a ``.tres``.

        Creates a ``StandardMaterial3D`` and assigns whichever texture channels
        were provided.  ``path`` defaults to ``res://materials/generated_{rand}.tres``
        when omitted. ``metallic``/``roughness``/``emission_enabled`` accept either a
        texture path or a scalar string (e.g. ``"0.7"``); a scalar sets the float
        property directly, and an ``emission`` texture auto-enables emission
        (``emission_enabled=true``, white color) so the material actually glows.
        Validation (including texture existence) runs before the call, on both the
        dry-run and the real run.
        """
        params: dict[str, Any] = {
            "albedo": albedo,
            "normal": normal,
            "roughness": roughness,
            "metallic": metallic,
            "ao": ao,
            "emission": emission,
            "emission_enabled": emission_enabled,
            "path": path,
        }
        # #419: validate BEFORE the bridge call in both modes — a dry-run must
        # fail on a missing texture exactly like the real run, not return a
        # success-flavored empty preview.
        await _validate_material_params(bridge, params)
        if dry_run:
            channels_preview = [
                c for c, v in (
                    ("albedo", albedo), ("normal", normal), ("roughness", roughness),
                    ("metallic", metallic), ("ao", ao), ("emission", emission),
                    ("emission_enabled", emission_enabled),
                ) if v
            ]
            return CreateMaterialResult(
                material_path=path or "res://materials/generated_{rand}.tres",
                created=False,
                channels_set=channels_preview,
                dry_run=True,
            )
        return CreateMaterialResult(
            **await route(bridge, "cmd_create_material_from_textures", params)
        )

    @mcp.tool(meta=READ_ONLY, tags=ASSET_IMPORT)
    async def get_import_status(
        target_path: str, wait_ms: Annotated[int, Field(ge=0, le=600_000)] = 0
    ) -> ImportStatusResult:
        """Check whether ``target_path`` has been imported by the Godot editor.

        Looks in ``.godot/imported/`` for the corresponding import file and
        returns the detected type and last-modified time.

        ``wait_ms`` (default 0): if not yet imported, keep polling for that
        many milliseconds first — useful right after ``import_asset``, whose
        editor-side scan is asynchronous.
        """
        _require_res_path(target_path)
        result = await route(bridge, "cmd_get_import_status", {"target_path": target_path})
        if not result.get("imported", False) and wait_ms > 0:
            polled = await _poll_import_ready(bridge, target_path, wait_ms)
            if polled is not None:
                result = polled
        return ImportStatusResult(
            imported=result.get("imported", False),
            last_modified=result.get("last_modified"),
            type=result.get("type"),
            # #459/#453: the scan-in-flight state rides the status read.
            scanning=result.get("scanning", False),
            reason=result.get("reason"),
        )
