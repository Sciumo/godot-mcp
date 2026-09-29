"""Contract tests: the server tells the editor its version and toolset state.

The addon's dock and status bar show which toolsets the agent can reach. The
server sends them on ``cmd_server_hello`` (issue #521's version push, extended
with ``toolsets``) as soon as an editor connects, and again after every
``enable_toolset`` / ``disable_toolset`` — not only on the first lazy
``cmd_get_addon_info`` handshake, which a re-enabled plugin might never see.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client

import mcp_server
from mcp_server.bridge import Bridge
from mcp_server.config import BridgeConfig
from mcp_server.toolsets import TOOLSETS
from tests.fakes import FakeAddonConnection, connector_for


def _hellos(conn: FakeAddonConnection) -> list[dict[str, Any]]:
    out = []
    for raw in conn.sent:
        msg = json.loads(raw)
        if msg["command"] == "cmd_server_hello":
            out.append(msg["params"])
    return out


async def _settle(conn: FakeAddonConnection, count: int) -> None:
    for _ in range(100):
        if len(_hellos(conn)) >= count:
            return
        await asyncio.sleep(0.01)


def _enabled(hello: dict[str, Any]) -> set[str]:
    return {t["name"] for t in hello["toolsets"] if t["enabled"]}


@pytest.mark.asyncio
async def test_bridge_without_provider_sends_no_eager_hello() -> None:
    conn = FakeAddonConnection()
    bridge = Bridge(BridgeConfig(), connector=connector_for(conn))
    await bridge.connect()
    await asyncio.sleep(0.05)
    assert _hellos(conn) == []


@pytest.mark.asyncio
async def test_push_state_without_peer_is_a_no_op() -> None:
    bridge = Bridge(BridgeConfig())
    bridge.set_state_provider(lambda: {"toolsets": []})
    await bridge.push_state()


@pytest.mark.asyncio
async def test_new_peer_gets_version_and_every_toolset() -> None:
    conn = FakeAddonConnection()
    bridge = Bridge(BridgeConfig(), connector=connector_for(conn))
    from mcp_server.server import create_server

    server = create_server(bridge=bridge)
    async with Client(server, mode="legacy"):
        await _settle(conn, 1)
    hello = _hellos(conn)[0]
    assert hello["version"] == mcp_server.__version__
    names = [t["name"] for t in hello["toolsets"]]
    assert names == ["core", *TOOLSETS]
    assert {"core", "inspection"} <= _enabled(hello)


@pytest.mark.asyncio
async def test_toolset_toggle_pushes_the_new_state() -> None:
    conn = FakeAddonConnection()
    bridge = Bridge(BridgeConfig(), connector=connector_for(conn))
    from mcp_server.server import create_server

    server = create_server(bridge=bridge)
    async with Client(server, mode="legacy") as client:
        await _settle(conn, 1)
        await client.call_tool("godot_enable_toolset", {"category": "editor"})
        await _settle(conn, 2)
        assert "editor" in _enabled(_hellos(conn)[-1])
        await client.call_tool("godot_disable_toolset", {"category": "editor"})
        await _settle(conn, 3)
        assert "editor" not in _enabled(_hellos(conn)[-1])
