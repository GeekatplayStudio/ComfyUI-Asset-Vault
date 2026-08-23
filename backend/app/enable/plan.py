"""Short-lived plan tokens: the plan the user saw is the plan that runs (R9).

The dependency report is produced first and hands back an opaque ``plan_token``
bound to the exact item set it described.  ``fetch`` will only accept item ids
that were in that plan, and only while that plan is still the current one for
its workflow.  A stale UI, a replayed request or a mistaken agent call therefore
cannot start a download the user never looked at - the same guarantee the C8
updater gets from echoing ``confirm_path``.

Plans live in process memory on purpose.  They are consent, not data: a restart
should invalidate them, and there is nothing here worth persisting.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ValidationError

#: Long enough that a plan survives a user reading it; short enough that a token
#: left in a log or a scrollback is useless by the time anyone finds it.
TTL_S = 15 * 60
MAX_PLANS = 32
MAX_ITEMS = 200


@dataclass
class PlanItem:
    item_id: str
    kind: str                      # 'model' | 'node_package'
    ref_name: str
    payload: dict = field(default_factory=dict)


@dataclass
class Plan:
    token: str
    workflow_id: int
    created_at: float
    expires_at: float
    items: dict[str, PlanItem]
    fingerprint: str

    def ttl_ms(self) -> int:
        return max(0, int((self.expires_at - time.time()) * 1000))


_lock = threading.Lock()
_plans: dict[str, Plan] = {}
_current: dict[int, str] = {}


def item_id(kind: str, ref_name: str) -> str:
    """A stable id for one dependency, so a re-issued plan keeps the same ids."""
    digest = hashlib.sha256(f"{kind}\x00{ref_name}".encode()).hexdigest()
    return f"{kind[:4]}_{digest[:16]}"


def _fingerprint(workflow_id: int, items: list[PlanItem]) -> str:
    canonical = json.dumps(
        {"workflow_id": int(workflow_id),
         "items": sorted(
             [{"id": i.item_id, "kind": i.kind, "ref": i.ref_name,
               "url": i.payload.get("source_url"),
               "target": i.payload.get("target_abs_path"),
               "size": i.payload.get("expected_size"),
               "sha256": i.payload.get("expected_sha256")}
              for i in items], key=lambda d: d["id"])},
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sweep(now: float) -> None:
    dead = [tok for tok, plan in _plans.items() if plan.expires_at <= now]
    for tok in dead:
        plan = _plans.pop(tok, None)
        if plan is not None and _current.get(plan.workflow_id) == tok:
            _current.pop(plan.workflow_id, None)
    while len(_plans) > MAX_PLANS:
        oldest = min(_plans.values(), key=lambda p: p.created_at)
        _plans.pop(oldest.token, None)
        if _current.get(oldest.workflow_id) == oldest.token:
            _current.pop(oldest.workflow_id, None)


def issue(workflow_id: int, items: list[PlanItem]) -> Plan:
    """Register a plan and supersede any earlier one for the same workflow."""
    if len(items) > MAX_ITEMS:
        raise ValidationError(
            f"A plan may describe at most {MAX_ITEMS} items; {len(items)} were "
            "resolved. Narrow the selection.",
            details={"items": len(items), "max": MAX_ITEMS})
    now = time.time()
    token = secrets.token_urlsafe(24)
    plan = Plan(token=token, workflow_id=int(workflow_id), created_at=now,
                expires_at=now + TTL_S,
                items={i.item_id: i for i in items},
                fingerprint=_fingerprint(workflow_id, items))
    with _lock:
        _sweep(now)
        previous = _current.get(int(workflow_id))
        if previous:
            _plans.pop(previous, None)
        _plans[token] = plan
        _current[int(workflow_id)] = token
    return plan


def redeem(token: str | None, workflow_id: int,
           item_ids: list[str] | None) -> list[PlanItem]:
    """Return the exact items this plan promised, or refuse with a 422.

    Refusals are deliberately specific about *which* rule failed: "expired",
    "superseded" and "not in this plan" are three different mistakes and the UI
    recovers from each differently.
    """
    now = time.time()
    with _lock:
        _sweep(now)
        plan = _plans.get(str(token or ""))
        current = _current.get(int(workflow_id))
    if plan is None:
        raise ValidationError(
            "That plan is no longer valid. Re-read the dependency report and "
            "confirm the new plan.",
            details={"reason": "unknown_or_expired", "workflow_id": int(workflow_id)})
    if plan.workflow_id != int(workflow_id):
        raise ValidationError(
            "That plan was issued for a different workflow.",
            details={"reason": "workflow_mismatch",
                     "plan_workflow_id": plan.workflow_id,
                     "workflow_id": int(workflow_id)})
    if current != plan.token:
        raise ValidationError(
            "That plan has been superseded by a newer dependency report.",
            details={"reason": "superseded", "workflow_id": int(workflow_id)})
    wanted = [str(i) for i in (item_ids or [])]
    if not wanted:
        raise ValidationError(
            "Select at least one item to fetch. Nothing downloads implicitly.",
            details={"reason": "empty_selection"})
    if len(wanted) > MAX_ITEMS:
        raise ValidationError(
            f"At most {MAX_ITEMS} items may be fetched in one call.",
            details={"reason": "too_many", "requested": len(wanted)})
    unknown = [i for i in wanted if i not in plan.items]
    if unknown:
        raise ValidationError(
            "The selection does not match the plan that was confirmed.",
            details={"reason": "item_not_in_plan", "unknown": unknown[:20],
                     "plan_items": len(plan.items)})
    seen: set[str] = set()
    out: list[PlanItem] = []
    for i in wanted:
        if i in seen:
            continue
        seen.add(i)
        out.append(plan.items[i])
    return out


def current_token(workflow_id: int) -> str | None:
    with _lock:
        return _current.get(int(workflow_id))


def peek(token: str | None) -> Plan | None:
    with _lock:
        _sweep(time.time())
        return _plans.get(str(token or ""))


def invalidate(workflow_id: int) -> None:
    with _lock:
        token = _current.pop(int(workflow_id), None)
        if token:
            _plans.pop(token, None)


def reset() -> None:
    """Test hook: forget every outstanding plan."""
    with _lock:
        _plans.clear()
        _current.clear()


def stats() -> dict[str, Any]:
    with _lock:
        return {"plans": len(_plans), "ttl_s": TTL_S}
