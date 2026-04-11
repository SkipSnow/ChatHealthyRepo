# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""
Cost Guard — Token budget enforcement for the Brain Loop.

Tracks every API call made by Claude and GPT during assignments.
Enforces per-assignment, daily, and monthly USD limits set in
brain/budget_config.json.

Agents call log_usage() after every API call.
brain_loop calls check_budget() before picking up assignments.
Boss calls get_usage_report() to see spend at any time.

Brain files:
  brain/usage_log.json    — all API call records
  brain/budget_config.json — limits and model pricing

ADR: ADR-0007 (framework_02)
"""

import json
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

_REPO_ROOT  = Path(__file__).parent.parent.parent
_BRAIN_DIR  = _REPO_ROOT / "brain"
_TOKEN_USAGE = _BRAIN_DIR / "machine_artifacts" / "content" / "token_usage.json"

# Legacy paths — kept for backward compatibility
_USAGE_LOG  = _BRAIN_DIR / "usage_log.json"
_BUDGET_CFG = _BRAIN_DIR / "budget_config.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_collection() -> dict:
    """Read the token_usage Brain pseudo collection."""
    with open(_TOKEN_USAGE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_collection(data: dict) -> None:
    """Write the token_usage Brain pseudo collection."""
    with open(_TOKEN_USAGE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _get_budget_config() -> dict:
    """Get budget config from Brain collection."""
    coll = _read_collection()
    for r in coll.get("records", []):
        if r.get("_record_id") == "budget_config":
            return r
    return {"limits": {"per_assignment_usd": 5, "daily_usd": 50, "monthly_usd": 500},
            "models": {}, "alert_threshold_pct": 80, "hard_stop_on_exceed": False}


def _get_usage_log() -> dict:
    """Get usage log record from Brain collection."""
    coll = _read_collection()
    for r in coll.get("records", []):
        if r.get("_record_id") == "usage_log":
            return r
    return {"_record_id": "usage_log", "entries": []}


def _save_usage_log(log: dict) -> None:
    """Save usage log back to Brain collection."""
    coll = _read_collection()
    for i, r in enumerate(coll.get("records", [])):
        if r.get("_record_id") == "usage_log":
            coll["records"][i] = log
            _write_collection(coll)
            return
    # Not found — append
    coll.setdefault("records", []).append(log)
    coll["record_count"] = len(coll["records"])
    _write_collection(coll)


# Legacy helpers for backward compat
def _read(path: Path) -> dict:
    if path == _BUDGET_CFG:
        return _get_budget_config()
    if path == _USAGE_LOG:
        return _get_usage_log()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    if path == _USAGE_LOG:
        _save_usage_log(data)
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    cfg = _get_budget_config()
    pricing = cfg.get("models", {}).get(model)
    if not pricing:
        return 0.0
    return (tokens_in / 1_000_000 * pricing["input_per_1m"] +
            tokens_out / 1_000_000 * pricing["output_per_1m"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_usage(
    agent: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    assignment_id: Optional[str] = None,
    call_type: str = "chat",
) -> float:
    """
    Log an API call. Called by Claude or GPT after every model call.

    Args:
        agent:         "Claude" | "GPT"
        model:         Model name — must match a key in budget_config.json models
        tokens_in:     Input/prompt tokens consumed
        tokens_out:    Output/completion tokens consumed
        assignment_id: Assignment this call belongs to (None = unassigned)
        call_type:     "chat" | "embedding" | "review"

    Returns:
        Cost in USD for this call.

    Example:
        # Claude logs a chat call
        cost = log_usage("Claude", "claude-sonnet-4-6",
                         tokens_in=1200, tokens_out=400,
                         assignment_id="ASN-B5EF02", call_type="chat")
    """
    cost = _cost_usd(model, tokens_in, tokens_out)
    entry = {
        "timestamp":     _now(),
        "agent":         agent,
        "model":         model,
        "call_type":     call_type,
        "tokens_in":     tokens_in,
        "tokens_out":    tokens_out,
        "cost_usd":      round(cost, 6),
        "assignment_id": assignment_id,
    }
    # Add vendor from budget config
    cfg = _get_budget_config()
    model_info = cfg.get("models", {}).get(model, {})
    entry["vendor"] = model_info.get("vendor", "unknown")

    log = _get_usage_log()
    log.setdefault("entries", []).append(entry)
    _save_usage_log(log)
    if assignment_id:
        asn_total = sum(
            e["cost_usd"] for e in log["entries"]
            if e.get("assignment_id") == assignment_id
        )
        limit = cfg["limits"]["per_assignment_usd"]
        pct = asn_total / limit * 100 if limit else 0
        if pct >= cfg["alert_threshold_pct"]:
            print(f"[CostGuard] ALERT: Assignment {assignment_id} at "
                  f"${asn_total:.4f} / ${limit:.2f} ({pct:.0f}%)", flush=True)
        if cfg["hard_stop_on_exceed"] and asn_total > limit:
            raise RuntimeError(
                f"[CostGuard] HARD STOP: Assignment {assignment_id} exceeded "
                f"per-assignment budget ${limit:.2f}. Actual: ${asn_total:.4f}. "
                f"Boss must raise the limit or close the assignment."
            )

    return cost


def check_budget(assignment_id: Optional[str] = None) -> dict:
    """
    Check remaining budget before starting or continuing work.

    Returns a status dict. If 'ok' is False, do not proceed — escalate to Boss.

    Args:
        assignment_id: Check per-assignment budget (optional)

    Returns:
        {
          "ok": bool,
          "reason": str,
          "per_assignment": {"spent": float, "limit": float, "remaining": float},
          "daily":          {"spent": float, "limit": float, "remaining": float},
          "monthly":        {"spent": float, "limit": float, "remaining": float},
        }
    """
    cfg = _read(_BUDGET_CFG)
    log = _read(_USAGE_LOG)
    entries = log["entries"]
    today = date.today().isoformat()
    this_month = today[:7]  # "2026-03"

    daily_spent = sum(
        e["cost_usd"] for e in entries
        if e["timestamp"][:10] == today
    )
    monthly_spent = sum(
        e["cost_usd"] for e in entries
        if e["timestamp"][:7] == this_month
    )

    limits = cfg["limits"]
    result = {
        "ok": True,
        "reason": "within budget",
        "daily":   {"spent": round(daily_spent, 4),
                    "limit": limits["daily_usd"],
                    "remaining": round(limits["daily_usd"] - daily_spent, 4)},
        "monthly": {"spent": round(monthly_spent, 4),
                    "limit": limits["monthly_usd"],
                    "remaining": round(limits["monthly_usd"] - monthly_spent, 4)},
    }

    if assignment_id:
        asn_spent = sum(
            e["cost_usd"] for e in entries
            if e.get("assignment_id") == assignment_id
        )
        result["per_assignment"] = {
            "spent": round(asn_spent, 4),
            "limit": limits["per_assignment_usd"],
            "remaining": round(limits["per_assignment_usd"] - asn_spent, 4),
        }
        if asn_spent >= limits["per_assignment_usd"]:
            result["ok"] = False
            result["reason"] = (f"Assignment {assignment_id} budget exhausted: "
                                f"${asn_spent:.4f} of ${limits['per_assignment_usd']:.2f}")

    if daily_spent >= limits["daily_usd"]:
        result["ok"] = False
        result["reason"] = f"Daily budget exhausted: ${daily_spent:.4f} of ${limits['daily_usd']:.2f}"

    if monthly_spent >= limits["monthly_usd"]:
        result["ok"] = False
        result["reason"] = f"Monthly budget exhausted: ${monthly_spent:.4f} of ${limits['monthly_usd']:.2f}"

    return result

