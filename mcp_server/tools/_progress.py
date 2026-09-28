"""Guarded Context progress helper (issue #331).

``ctx.report_progress`` raises ``RuntimeError: session is not available`` in
the detached task input loop (``task=True``), because the session back-channel
is gone. This helper no-ops on that failure so long-running tools can be
task-enabled without losing progress for the non-task path.

Human-readable status text rides on the progress notification's ``message``:
the MCP logging capability (``ctx.info`` → ``notifications/message``) is
deprecated as of the 2026-07-28 protocol (SEP-2577), so tools must not use it.
"""

from __future__ import annotations

from typing import Any


async def safe_progress(ctx: Any, current: float, total: float, message: str | None = None) -> None:
    try:
        await ctx.report_progress(current, total, message)
    except Exception:
        pass
