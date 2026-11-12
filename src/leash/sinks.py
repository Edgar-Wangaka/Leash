"""Event sinks for outside services. Each one is off unless its env var is set.

A sink is a callable ``(kind, payload) -> None``. It is run on a daemon
thread by ``events.emit`` and may raise; the error is logged and dropped.
No sink can approve, deny or block anything: it gets a copy, after the fact.

Today there is one: the Channel3 price watch (see ``leash.price_watch``).
"""

from __future__ import annotations

from typing import Any, Callable

Sink = Callable[[str, dict[str, Any]], None]


def from_env() -> list[tuple[str, Sink]]:
    """Build every sink whose configuration is present in the environment."""
    from .price_watch import watch_sink, watcher_from_env  # avoids an import cycle

    built: list[tuple[str, Sink]] = []
    watcher = watcher_from_env()
    if watcher is not None:
        built.append(("channel3_price_watch", watch_sink(watcher)))
    return built
