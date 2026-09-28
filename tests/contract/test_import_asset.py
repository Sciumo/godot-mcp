"""Contract tests for external asset import tools (issue #108).

All three tools live in a new ``asset_import`` toolset (gated off by default).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_server.bridge import Bridge
from mcp_server.config import ServerConfig
from mcp_server.models.envelope import CommandEnvelope, ResponseEnvelope
from mcp_server.server import create_server
from tests.fakes import FakeAddonConnection, connector_for

pytestmark = pytest.mark.asyncio


def _is_scalar(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# The fixture project's existing textures (paths the material tests reference).
# The probe models the real addon search: the glob matches file *names* only,
# so these must have distinct basenames for the tool's suffix check to resolve.
_FIXTURE_TEXTURES = [
    "res://tex/albedo.png",
    "res://tex/normal.png",
    "res://tex/a.png",
    "res://textures/metal.png",
    "res://tex/crystal.png",
    "res://tex/glow.png",
    "res://tex/rough.png",
    "res://tex/metallic.png",
]


def _responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
    p = cmd.params
    match cmd.command:
        case "cmd_import_asset":
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "imported": True,
                    "target_path": p["target_path"],
                    "detected_type": "Texture2D",
                },
            )
        case "cmd_search_files":
            # Texture-existence probe: model the real addon semantics (Qodo #452
            # review) — the search recurses, but the glob matches file *names*
            # only (verified on Godot 4.7), so the tool globs the basename and
            # requires a match ending with the full requested res:// path. The
            # fixture project "has" every texture in _FIXTURE_TEXTURES (paths the
            # tests reference) plus anything whose basename contains "missing"
            # is absent — driving the NOT_FOUND path.
            glob = p.get("name_glob", "")
            if "missing" in glob:
                return ResponseEnvelope.success(cmd.id, {"matches": [], "truncated": False})
            return ResponseEnvelope.success(
                cmd.id, {"matches": list(_FIXTURE_TEXTURES), "truncated": False}
            )
        case "cmd_create_material_from_textures":
            # Mirror the addon's channel handling (#419/#428): scalars are noted
            # as "<channel>:scalar", emission auto-enables emission_enabled.
            channels = ["albedo", "normal", "roughness", "metallic", "ao", "emission"]
            set_channels: list[str] = []
            for c in channels:
                v = str(p.get(c, ""))
                if not v:
                    continue
                set_channels.append(c if not _is_scalar(v) else f"{c}:scalar")
            if str(p.get("emission", "")):
                set_channels.append("emission_enabled:scalar")
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "material_path": p.get("path", "res://materials/generated_mat.tres"),
                    "created": True,
                    "channels_set": set_channels,
                },
            )
        case "cmd_get_import_status":
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "imported": True,
                    "last_modified": "2026-06-09T12:00:00Z",
                    "type": "Texture2D",
                },
            )
    return ResponseEnvelope.failure(cmd.id, "VALIDATION_ERROR", "unexpected")


def _build() -> tuple[FastMCP, FakeAddonConnection]:
    conn = FakeAddonConnection(responder=_responder)
    bridge = Bridge(ServerConfig().bridge, connector=connector_for(conn))
    return create_server(ServerConfig(), bridge=bridge), conn


def _commands(conn: FakeAddonConnection) -> list[str]:
    return [CommandEnvelope.model_validate_json(s).command for s in conn.sent]


async def test_gated_in_asset_import_toolset() -> None:
    server, _ = _build()
    async with Client(server, mode="legacy") as client:
        assert "godot_asset_import_asset" not in {t.name for t in await client.list_tools()}
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        names = {t.name for t in await client.list_tools()}
    assert {
        "godot_asset_import_asset",
        "godot_asset_import_create_material_from_textures",
        "godot_asset_import_get_status",
    } <= names


async def test_import_asset_success() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {"source": "res://temp/download.png", "target_path": "res://assets/download.png"},
        )
    assert result.structured_content["imported"] is True
    assert result.structured_content["target_path"] == "res://assets/download.png"
    assert result.structured_content["detected_type"] == "Texture2D"
    assert "cmd_import_asset" in _commands(conn)


async def test_import_asset_with_url_download_cleanup() -> None:
    """When source is a URL, the downloaded temporary file is cleaned up
    after the addon call.
    """
    server, conn = _build()
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    assert os.path.exists(tmp)

    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        with patch("mcp_server.tools.import_asset._download_url", return_value=tmp):
            result = await client.call_tool(
                "godot_asset_import_asset",
                {
                    "source": "https://example.com/img.png",
                    "target_path": "res://assets/img.png",
                },
            )
    assert result.structured_content["imported"] is True
    # The addon was called with the temp path; it should have been cleaned up.
    assert not os.path.exists(tmp)
    last = CommandEnvelope.model_validate_json(conn.sent[-1])
    assert last.params.get("source") == tmp


async def test_import_asset_dry_run() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        dry = await client.call_tool(
            "godot_asset_import_asset",
            {
                "source": "https://example.com/img.png",
                "target_path": "res://assets/img.png",
                "dry_run": True,
            },
        )
    assert dry.structured_content["dry_run"] is True
    assert dry.structured_content["imported"] is False
    assert "cmd_import_asset" not in _commands(conn)


async def test_create_material_from_textures() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_create_material_from_textures",
            {
                "albedo": "res://tex/albedo.png",
                "normal": "res://tex/normal.png",
                "path": "res://materials/hero.tres",
            },
        )
    assert result.structured_content["created"] is True
    assert result.structured_content["material_path"] == "res://materials/hero.tres"
    assert result.structured_content["channels_set"] == ["albedo", "normal"]
    assert "cmd_create_material_from_textures" in _commands(conn)


async def test_create_material_dry_run() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        dry = await client.call_tool(
            "godot_asset_import_create_material_from_textures",
            {"albedo": "res://tex/a.png", "dry_run": True},
        )
    assert dry.structured_content["dry_run"] is True
    assert dry.structured_content["created"] is False
    assert "cmd_create_material_from_textures" not in _commands(conn)


async def test_create_material_accepts_scalar_metallic_roughness() -> None:
    """#419: metallic/roughness are scalar floats on StandardMaterial3D — a numeric
    string must be accepted as a scalar (channel noted as `<channel>:scalar`), not
    treated as a texture path that aborts the whole material."""
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_create_material_from_textures",
            {
                "albedo": "res://textures/metal.png",
                "metallic": "0.7",
                "roughness": "0.45",
                "path": "res://materials/metal.tres",
            },
        )
    assert result.structured_content["created"] is True
    assert result.structured_content["channels_set"] == [
        "albedo",
        "roughness:scalar",
        "metallic:scalar",
    ]
    sent = CommandEnvelope.model_validate_json(conn.sent[-1])
    assert sent.params["metallic"] == "0.7"
    assert sent.params["roughness"] == "0.45"


async def test_create_material_dry_run_validates_params() -> None:
    """#419: a dry_run must run the same validation as the real run — a missing
    texture fails the preview instead of giving false confidence."""
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        with pytest.raises(ToolError, match="Texture not found for 'albedo'"):
            await client.call_tool(
                "godot_asset_import_create_material_from_textures",
                {"albedo": "res://missing/does_not_exist.png", "dry_run": True},
            )
    assert "cmd_create_material_from_textures" not in _commands(conn)


async def test_create_material_missing_texture_validates_in_dry_run() -> None:
    """#419: the same bad texture also fails the real run — validation parity."""
    server, _ = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        with pytest.raises(ToolError, match="RESOURCE_NOT_FOUND"):
            await client.call_tool(
                "godot_asset_import_create_material_from_textures",
                {"albedo": "res://missing/does_not_exist.png"},
            )


async def test_create_material_emission_enables_emission() -> None:
    """#428: providing an emission texture must leave the material emissive —
    emission_enabled defaults on with a white color; a half-set (texture wired,
    emission_enabled=false) renders black no matter the texture."""

    def responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_search_files":
            return ResponseEnvelope.success(
                cmd.id, {"matches": list(_FIXTURE_TEXTURES), "truncated": False}
            )
        if cmd.command == "cmd_create_material_from_textures":
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "material_path": cmd.params.get("path", "res://materials/generated_mat.tres"),
                    "created": True,
                    "channels_set": ["albedo", "emission", "emission_enabled:scalar"],
                },
            )
        return ResponseEnvelope.failure(cmd.id, "VALIDATION_ERROR", "unexpected")

    conn = FakeAddonConnection(responder=responder)
    bridge = Bridge(ServerConfig().bridge, connector=connector_for(conn))
    server = create_server(ServerConfig(), bridge=bridge)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_create_material_from_textures",
            {"albedo": "res://tex/crystal.png", "emission": "res://tex/glow.png"},
        )
    sc = result.structured_content
    assert sc["created"] is True
    assert "emission" in sc["channels_set"]
    # The pin for the half-set state: emission_enabled travels with the texture.
    assert "emission_enabled:scalar" in sc["channels_set"]


async def test_get_import_status() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_get_status", {"target_path": "res://assets/download.png"}
        )
    assert result.structured_content["imported"] is True
    assert result.structured_content["type"] == "Texture2D"
    assert "cmd_get_import_status" in _commands(conn)


async def test_import_asset_safety_class() -> None:
    server, _ = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        tool = next(t for t in await client.list_tools() if t.name == "godot_asset_import_asset")
    assert tool.meta is not None and tool.meta.get("safety_class") == "mutating"


async def test_get_import_status_is_read_only() -> None:
    server, _ = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        tool = next(
            t for t in await client.list_tools() if t.name == "godot_asset_import_get_status"
        )
    assert tool.meta is not None and tool.meta.get("safety_class") == "read_only"


# --- wait-for-scan support (issue #400) --------------------------------------


def _build_with(
    responder: Callable[[CommandEnvelope], ResponseEnvelope | None],
) -> tuple[FastMCP, FakeAddonConnection]:
    conn = FakeAddonConnection(responder=responder)
    bridge = Bridge(ServerConfig().bridge, connector=connector_for(conn))
    return create_server(ServerConfig(), bridge=bridge), conn


def _status_response(cmd: CommandEnvelope, ready: bool) -> ResponseEnvelope:
    return ResponseEnvelope.success(
        cmd.id,
        {
            "imported": ready,
            "last_modified": "2026-06-09T12:00:00Z" if ready else None,
            "type": "Texture2D" if ready else None,
        },
    )


async def test_import_asset_wait_for_scan_polls_until_ready() -> None:
    status_calls = {"n": 0}

    def responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_import_asset":
            return _responder(cmd)
        if cmd.command == "cmd_get_import_status":
            status_calls["n"] += 1
            return _status_response(cmd, ready=status_calls["n"] >= 3)
        return ResponseEnvelope.failure(cmd.id, "VALIDATION_ERROR", "unexpected")

    server, _ = _build_with(responder)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {
                "source": "res://temp/x.png",
                "target_path": "res://assets/x.png",
                "wait_for_scan": True,
            },
        )
    assert result.structured_content["imported"] is True
    assert result.structured_content["scan_complete"] is True
    assert status_calls["n"] == 3


async def test_import_asset_wait_for_scan_timeout_reports_incomplete() -> None:
    status_calls = {"n": 0}

    def responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_import_asset":
            return _responder(cmd)
        if cmd.command == "cmd_get_import_status":
            status_calls["n"] += 1
            return _status_response(cmd, ready=False)
        return ResponseEnvelope.failure(cmd.id, "VALIDATION_ERROR", "unexpected")

    server, _ = _build_with(responder)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {
                "source": "res://temp/x.png",
                "target_path": "res://assets/x.png",
                "wait_for_scan": True,
                "timeout_ms": 400,
            },
        )
    assert result.structured_content["imported"] is True
    assert result.structured_content["scan_complete"] is False
    assert status_calls["n"] >= 1


async def test_import_asset_without_wait_does_not_poll() -> None:
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {"source": "res://temp/x.png", "target_path": "res://assets/x.png"},
        )
    assert result.structured_content["scan_complete"] is False
    assert "cmd_get_import_status" not in _commands(conn)


async def test_dry_run_with_wait_for_scan_never_polls() -> None:
    """dry_run returns the preview before the bridge (and any polling) is touched."""
    server, conn = _build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {
                "source": "res://temp/x.png",
                "target_path": "res://assets/x.png",
                "dry_run": True,
                "wait_for_scan": True,
            },
        )
    assert result.structured_content["dry_run"] is True
    assert result.structured_content["scan_complete"] is False
    assert _commands(conn) == []  # nothing sent: no import, no status polling


async def test_get_import_status_wait_ms_polls_until_ready() -> None:
    status_calls = {"n": 0}

    def responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_get_import_status":
            status_calls["n"] += 1
            return _status_response(cmd, ready=status_calls["n"] >= 2)
        return _responder(cmd)

    server, _ = _build_with(responder)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_get_status",
            {"target_path": "res://assets/x.png", "wait_ms": 1500},
        )
    assert result.structured_content["imported"] is True
    assert status_calls["n"] == 2


async def test_import_status_reports_rescan_in_flight() -> None:
    """#459/#453: a status read while the editor's filesystem scan is running
    carries the stable ``rescan_in_flight`` token — the read is provisional."""
    server, _ = _build()

    def scanning_responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_get_import_status":
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "imported": False,
                    "last_modified": None,
                    "type": None,
                    "scanning": True,
                    "reason": "rescan_in_flight",
                },
            )
        return _responder(cmd)

    conn = FakeAddonConnection(responder=scanning_responder)
    bridge = Bridge(ServerConfig().bridge, connector=connector_for(conn))
    server = create_server(ServerConfig(), bridge=bridge)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_get_status", {"target_path": "res://a.png"}
        )
    data = result.structured_content
    assert data["scanning"] is True
    assert data["reason"] == "rescan_in_flight"
    assert data["imported"] is False


async def test_import_wav_loop_mode_applied() -> None:
    """#418: a .wav import with loop settings patches the .import sidecar and
    reimports — the result reports loop_applied."""
    def wav_responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_import_asset":
            settings = cmd.params.get("import_settings", {})
            applied = cmd.params["target_path"].endswith(".wav") and "loop_mode" in settings
            return ResponseEnvelope.success(
                cmd.id,
                {
                    "imported": True,
                    "target_path": cmd.params["target_path"],
                    "detected_type": "AudioStreamWAV",
                    "loop_applied": applied,
                },
            )
        return _responder(cmd)

    server, conn = _build_with(wav_responder)
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {
                "source": "res://music.wav",
                "target_path": "res://audio/music.wav",
                "options": {"import_settings": {"loop_mode": 1, "loop_begin": 0}},
            },
        )
    data = result.structured_content
    assert data["loop_applied"] is True
    sent = CommandEnvelope.model_validate_json(conn.sent[-1])
    settings = sent.params["import_settings"]
    assert settings["loop_mode"] == 1
    assert settings["loop_begin"] == 0


# ---------------------------------------------------------------------------
# Inline data sources (data: URIs) — the sink for AI-generated assets.
# Image-gen MCP servers hand back base64 blobs, not files or URLs.
# ---------------------------------------------------------------------------

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def _data_uri(payload: bytes, mime: str = "image/png") -> str:
    import base64

    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _capturing_build() -> tuple[FastMCP, FakeAddonConnection, list[tuple[str, bytes]]]:
    """Like ``_build`` but records (source path, bytes at that path) per import."""
    seen: list[tuple[str, bytes]] = []

    def responder(cmd: CommandEnvelope) -> ResponseEnvelope | None:
        if cmd.command == "cmd_import_asset":
            src = cmd.params["source"]
            with open(src, "rb") as fh:
                seen.append((src, fh.read()))
            if "fail" in cmd.params["target_path"]:
                return ResponseEnvelope.failure(cmd.id, "ALREADY_EXISTS", "target exists")
        return _responder(cmd)

    conn = FakeAddonConnection(responder=responder)
    bridge = Bridge(ServerConfig().bridge, connector=connector_for(conn))
    return create_server(ServerConfig(), bridge=bridge), conn, seen


async def test_import_asset_accepts_data_uri_and_cleans_up() -> None:
    server, _, seen = _capturing_build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_asset",
            {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/gen.png"},
        )
    assert result.structured_content["imported"] is True
    [(src, payload)] = seen
    assert payload == _PNG_BYTES
    assert src.endswith(".png")
    assert not os.path.exists(src), "decoded temp file must be removed after the call"


async def test_import_asset_data_uri_content_mismatch_is_refused_even_on_dry_run() -> None:
    server, conn, _ = _capturing_build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        for dry_run in (False, True):
            with pytest.raises(ToolError, match="VALIDATION_ERROR.*JPEG"):
                await client.call_tool(
                    "godot_asset_import_asset",
                    {
                        "source": _data_uri(_JPEG_BYTES, "image/jpeg"),
                        "target_path": "res://art/gen.png",
                        "dry_run": dry_run,
                    },
                )
    assert "cmd_import_asset" not in _commands(conn)


async def test_import_asset_malformed_data_uri_is_refused() -> None:
    server, conn, _ = _capturing_build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        for bad in ("data:image/png;base64,@@not-base64@@", "data:image/png,rawtext"):
            with pytest.raises(ToolError, match="VALIDATION_ERROR"):
                await client.call_tool(
                    "godot_asset_import_asset",
                    {"source": bad, "target_path": "res://art/gen.png"},
                )
    assert "cmd_import_asset" not in _commands(conn)


async def test_import_asset_data_uri_size_cap() -> None:
    server, conn, _ = _capturing_build()
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        with patch("mcp_server.tools.import_asset._MAX_INLINE_BYTES", 16):
            with pytest.raises(ToolError, match="VALIDATION_ERROR.*too large"):
                await client.call_tool(
                    "godot_asset_import_asset",
                    {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/gen.png"},
                )
    assert "cmd_import_asset" not in _commands(conn)


# ---------------------------------------------------------------------------
# Batch import — many generated assets in one call, honest per-item results.
# ---------------------------------------------------------------------------


async def test_import_batch_is_gated_and_mutating() -> None:
    server, _ = _build()
    async with Client(server, mode="legacy") as client:
        assert "godot_asset_import_batch" not in {t.name for t in await client.list_tools()}
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        tools = {t.name: t for t in await client.list_tools()}
    tool = tools["godot_asset_import_batch"]
    assert (tool.meta or {}).get("safety_class") == "mutating"


async def test_import_batch_reports_per_item_results() -> None:
    server, _, seen = _capturing_build()
    items = [
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/a.png"},
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/fail.png"},
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/c.png"},
    ]
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool("godot_asset_import_batch", {"items": items})
    out = result.structured_content
    assert out["ok_all"] is False
    assert out["imported_count"] == 2
    assert out["failed_count"] == 1
    assert [r["ok"] for r in out["results"]] == [True, False, True]
    assert "ALREADY_EXISTS" in out["results"][1]["error"]
    assert out.get("aborted_at") is None
    assert len(seen) == 3
    assert not any(os.path.exists(src) for src, _ in seen)


async def test_import_batch_stop_on_error_is_never_silent() -> None:
    server, _, seen = _capturing_build()
    items = [
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/fail.png"},
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/b.png"},
    ]
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_batch", {"items": items, "stop_on_error": True}
        )
    out = result.structured_content
    assert out["aborted_at"] == 0
    assert out["skipped_count"] == 1
    assert out["hint"]
    assert len(out["results"]) == 1
    assert len(seen) == 1


async def test_import_batch_dry_run_validates_every_item_and_sends_nothing() -> None:
    server, conn, _ = _capturing_build()
    items = [
        {"source": _data_uri(_PNG_BYTES), "target_path": "res://art/a.png"},
        {"source": _data_uri(_JPEG_BYTES, "image/jpeg"), "target_path": "res://art/b.png"},
        {"source": "x.png", "target_path": "art/c.png"},
    ]
    async with Client(server) as client:
        await client.call_tool("godot_enable_toolset", {"category": "asset_import"})
        result = await client.call_tool(
            "godot_asset_import_batch", {"items": items, "dry_run": True}
        )
    out = result.structured_content
    assert out["dry_run"] is True
    assert [r["ok"] for r in out["results"]] == [True, False, False]
    assert "JPEG" in out["results"][1]["error"]
    assert "res://" in out["results"][2]["error"]
    assert out["imported_count"] == 0
    assert "cmd_import_asset" not in _commands(conn)
