"""Contract test: long-running tools stream progress via Context (issue #223).

run_tests / run_and_capture / export_project are multi-second operations. They must
accept a FastMCP ``Context`` and emit progress updates so the client/agent has
observability during the slow call. The human-readable status text rides on the
progress notification's ``message`` field: the MCP logging capability
(``notifications/message``) is deprecated as of the 2026-07-28 protocol (SEP-2577),
so tools must not send log messages.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import Client, FastMCP
from mcp.types import LoggingMessageNotificationParams

from mcp_server.bridge import Bridge
from mcp_server.config import ServerConfig
from mcp_server.runtime import RunOutput
from mcp_server.server import create_server
from tests.fakes import FakeAddonConnection, connector_for

_GUT_PASS = """\
Totals
Passing tests      3
Failing tests      0
---- 3 of 3 tests passed (3 ran). ----
"""


class _FakeRunner:
    binary: str | None = "fake-godot"

    def __init__(self, output: RunOutput) -> None:
        self._output = output

    async def run(self, project_dir: str, scene: str | None, timeout: float) -> RunOutput:
        return self._output

    async def check_script(self, project_dir: str, script_path: str, timeout: float) -> RunOutput:
        return RunOutput(command=["fake"])

    async def check_shader(self, project_dir: str, shader_path: str, timeout: float) -> RunOutput:
        return RunOutput(command=["fake"])

    async def export(
        self, project_dir: str, preset: str, output_path: str, debug: bool, timeout: float
    ) -> RunOutput:
        return self._output

    async def run_tests(self, project_dir: str, test_dir: str, timeout: float) -> RunOutput:
        return self._output


def _build(project_dir: str, output: RunOutput) -> FastMCP:
    config = ServerConfig(godot_project_dir=project_dir)
    bridge = Bridge(config.bridge, connector=connector_for(FakeAddonConnection()))
    return create_server(config, bridge=bridge, runner=_FakeRunner(output))


def _with_gut(project_dir: Path) -> None:
    gut = project_dir / "addons" / "gut"
    gut.mkdir(parents=True, exist_ok=True)
    (gut / "gut_cmdln.gd").write_text("# gut runner stub\n")


async def test_run_tests_streams_progress_and_logs(tmp_path: Path) -> None:
    _with_gut(tmp_path)
    server = _build(str(tmp_path), RunOutput(command=["gut"], stdout=_GUT_PASS, exit_code=0))

    progress: list[tuple[float, float | None, str | None]] = []
    logs: list[str] = []

    async def on_progress(p: float, total: float | None, message: str | None) -> None:
        progress.append((p, total, message))

    async def on_log(params: LoggingMessageNotificationParams) -> None:
        logs.append(str(params.data))

    async with Client(server, progress_handler=on_progress, log_handler=on_log) as client:
        await client.call_tool("godot_enable_toolset", {"category": "testing"})
        await client.call_tool("godot_testing_run_tests", {"test_dir": "res://test"})

    assert progress, "expected at least one progress update from run_tests"
    messages = [m for _, _, m in progress if m]
    assert any("Running GUT suite" in m for m in messages), messages
    assert any("3 passed" in m for m in messages), messages
    assert not logs, f"deprecated logging capability used (SEP-2577): {logs}"


def test_no_tool_uses_the_deprecated_logging_capability() -> None:
    """Server code must not call Context log methods (SEP-2577 deprecation)."""
    import re

    root = Path(__file__).resolve().parents[2] / "mcp_server"
    pattern = re.compile(r"\bctx\.(log|debug|info|warning|error)\(")
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, offenders


async def test_safe_progress_noops_on_session_unavailable() -> None:
    """The guarded progress helpers swallow RuntimeError (detached task session).

    Simulates the ``task=True`` detached-session failure by passing a Context
    whose ``report_progress`` raises ``RuntimeError: session is not
    available``. The tools must complete successfully — the progress calls
    no-op instead of propagating the error.
    """
    from mcp_server.tools._progress import safe_progress

    class _BrokenCtx:
        async def report_progress(
            self, current: float, total: float, *args: object, **kwargs: object
        ) -> None:
            raise RuntimeError("session is not available")

    ctx = _BrokenCtx()
    await safe_progress(ctx, 0, 1, "should not raise")
    await safe_progress(ctx, 1, 1)
