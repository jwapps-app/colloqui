"""Fire-and-forget work off the request path (pushes, webhook deliveries, link
preview fetches). One place holds the strong references: the event loop keeps
tasks only weakly, so a bare create_task() can be garbage-collected mid-flight
and the work silently never happens."""
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("colloqui.background")

_tasks: set[asyncio.Task] = set()


async def _guard(coro: Coroutine[Any, Any, Any], what: str, quiet: bool) -> None:
    try:
        await coro
    except Exception:
        if not quiet:
            log.exception("background %s failed", what)


def spawn(
    coro: Coroutine[Any, Any, Any], what: str = "task", *, quiet: bool = False
) -> asyncio.Task | None:
    """Run `coro` in the background. Never raises into the caller and never
    blocks it; a failure is logged (pass quiet=True where failing is normal,
    like closing a socket the peer already dropped). Returns None (and does
    nothing) when there is no running event loop, e.g. in a sync script."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(_guard(coro, what, quiet))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
