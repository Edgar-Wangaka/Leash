"""Decision events: tell outside services what the gate decided.

The gate calls ``emit()`` after a decision row is committed. Each registered
sink gets the event on its own daemon thread, so a slow or broken sink can
never change or delay a decision. Sink failures are logged and dropped.

Sinks are for hosting, watching and notifying. None of them can approve or
block a payment: they get a copy of the record, after the fact.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

Sink = Callable[[str, dict[str, Any]], None]

# How long one sink call may take. HTTP sinks pass this as their own timeout;
# drain() waits at most this long per pending call.
SINK_TIMEOUT_SECONDS = 3.0

EVENT_KINDS = {
    "decision.created",          # allowed by policy, about to be paid
    "decision.held",             # waiting for a human
    "decision.blocked",          # refused by a hard rule
    "decision.approved",         # a human claimed a hold
    "decision.denied",           # a human denied a hold
    "decision.paid",
    "decision.submitted",        # PayPal accepted it, not final yet
    "decision.failed",
    "decision.unclaimed",
    "decision.expired",
    "decision.needs_reconcile",
    "decision.rejected_on_recheck",
}

# Status → event kind for status changes after the first verdict.
STATUS_EVENTS = {
    "paid": "decision.paid",
    "submitted": "decision.submitted",
    "failed": "decision.failed",
    "unclaimed": "decision.unclaimed",
    "expired": "decision.expired",
    "needs_reconcile": "decision.needs_reconcile",
    "denied": "decision.denied",
    "rejected_on_recheck": "decision.rejected_on_recheck",
}

_sinks: list[Sink] = []
_pending: list[threading.Thread] = []
_lock = threading.Lock()


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """The one shape a decision takes outside the gate (dashboard and sinks)."""
    payout = record.get("payout") or {}
    request = record.get("request") or {}
    return {
        "request_id": record["request_id"],
        "agent_id": record["agent_id"],
        "merchant_email": record["merchant_email"],
        "amount_usd": f"{(record.get('amount_cents') or 0) / 100:.2f}",
        "reason": request.get("reason", ""),
        "product_id": request.get("product_id"),
        "verdict": record["verdict"],
        "reasons": record["reasons"],
        "status": record["status"],
        "explanation": record["explanation"],
        "catalog": request.get("catalog"),
        "risk_signal": record["risk_signal"],
        "approved_by": record["approved_by"],
        "paypal_batch_id": payout.get("paypal_batch_id"),
        "paypal_transaction_id": payout.get("transaction_id"),
        "paypal_status": payout.get("transaction_status") or payout.get("batch_status"),
        "paypal_time_processed": payout.get("time_processed"),
        "error_name": record["error_name"],
        "created_at": record["created_at"],
    }


def initial_kind(record: dict[str, Any]) -> str:
    """Event kind for a decision's first verdict."""
    if record["verdict"] == "block":
        return "decision.blocked"
    if record["verdict"] == "hold_for_human":
        return "decision.held"
    return "decision.created"


def register(sink: Sink) -> None:
    with _lock:
        _sinks.append(sink)


def unregister(sink: Sink) -> None:
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)


def clear() -> None:
    """Remove every sink (tests use this)."""
    with _lock:
        _sinks.clear()


def sinks() -> list[Sink]:
    with _lock:
        return list(_sinks)


def _run(sink: Sink, kind: str, payload: dict[str, Any]) -> None:
    try:
        sink(kind, payload)
    except Exception as error:  # a sink must never reach the gate
        logger.warning(
            "Event sink %s failed on %s: %s",
            getattr(sink, "__name__", sink.__class__.__name__),
            kind,
            error.__class__.__name__,
        )


def emit(kind: str, record: dict[str, Any]) -> None:
    """Send one event to every sink without waiting for any of them."""
    if kind not in EVENT_KINDS:
        logger.warning("Unknown event kind %s dropped", kind)
        return
    current = sinks()
    if not current:
        return
    try:
        payload = public_record(record)
    except Exception as error:
        logger.warning("Could not build event payload: %s", error.__class__.__name__)
        return
    payload["event"] = kind
    with _lock:
        _pending[:] = [thread for thread in _pending if thread.is_alive()]
        for sink in current:
            # Each sink gets its own copy, so one cannot change what another sees.
            thread = threading.Thread(
                target=_run, args=(sink, kind, dict(payload)), daemon=True
            )
            thread.start()
            _pending.append(thread)


def drain(timeout: float = SINK_TIMEOUT_SECONDS) -> None:
    """Wait for in-flight sink calls. Tests and clean shutdown only."""
    with _lock:
        threads = list(_pending)
    for thread in threads:
        thread.join(timeout)


_installed = False


def install_from_env() -> list[str]:
    """Register the sinks whose env vars are set. Safe to call more than once.

    Returns the names of the sinks it installed on this call.
    """
    global _installed
    if _installed:
        return []
    _installed = True
    from . import sinks as sink_module  # local import: optional dependencies

    installed = []
    for name, sink in sink_module.from_env():
        register(sink)
        installed.append(name)
    return installed


def reset_install_flag() -> None:
    """Tests only: allow install_from_env() to run again."""
    global _installed
    _installed = False
