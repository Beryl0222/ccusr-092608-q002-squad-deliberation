"""只读查询模型：呈现事实链，而非单一评分。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .eventstore import EventStore, StoredEvent
from .service import parse_dt


@dataclass(frozen=True)
class Fact:
    """事实链上的一环：何时、何种事实、来自哪个事件、说明什么。"""

    occurred_at: str
    fact_type: str
    event_ref: str
    detail: Mapping[str, Any]


def _evidence_fact(event: StoredEvent) -> Fact:
    body = event.payload
    return Fact(
        occurred_at=event.occurred_at,
        fact_type=f"evidence:{body.get('evidence_kind')}",
        event_ref=event.event_id,
        detail={k: v for k, v in body.items() if k in ("subject_id", "summary", "observed_at", "status")},
    )


def athlete_decision_report(
    store: EventStore,
    competition_ref: str,
    athlete_id: str,
    as_of: str | None = None,
) -> Mapping[str, Any]:
    """某队员在某场比赛入选/替补/暂缓的完整事实链。

    - 只读取锁定回执所属方案，赛后重算另开的方案不影响本报告。
    - 并行签署的反对/弃权意见以少数意见形式保留。
    - 培养承诺给出按期、逾期未兑现或迟兑状态。
    """

    now = parse_dt(as_of) if as_of else datetime.now().astimezone()
    all_events = store.all_events()
    lock = _lock_for(all_events, competition_ref)
    if lock is None:
        return {"competition_ref": competition_ref, "athlete_id": athlete_id, "disposition": "not_decided", "fact_chain": []}

    body = lock.payload
    entry = next((item for item in body.get("roster", []) if item.get("athlete_id") == athlete_id), None)
    proposal_events = store.events_for_aggregate(body["proposal_ref"])
    submitted = next(e for e in proposal_events if e.event_type == "PROPOSAL_SUBMITTED")

    events_by_id = {e.event_id: e for e in all_events}
    facts: list[Fact] = []
    for ref in submitted.payload.get("evidence_refs", []):
        event = events_by_id.get(ref)
        if event is not None:
            facts.append(_evidence_fact(event))
    # 医疗解除在锁定前生效的，也是事实链的一环
    for event in store.events_for_aggregate(f"athlete-{athlete_id}"):
        if event.event_type in ("MEDICAL_CLEARANCE_GRANTED", "MEDICAL_CLEARANCE_REVOKED") and parse_dt(
            event.occurred_at
        ) <= parse_dt(body["evidence_cutoff"]):
            facts.append(
                Fact(
                    event.occurred_at,
                    event.event_type.lower(),
                    event.event_id,
                    {"restriction_ref": event.payload.get("restriction_ref"), "by": event.payload.get("granted_by")},
                )
            )
    facts.append(
        Fact(
            submitted.occurred_at,
            "proposal_submitted",
            submitted.event_id,
            {"proposed_by": submitted.payload["proposed_by"], "proposal_ref": body["proposal_ref"]},
        )
    )
    opinions = [e for e in proposal_events if e.event_type == "OPINION_SIGNED"]
    for event in opinions:
        facts.append(
            Fact(
                event.occurred_at,
                "opinion_signed",
                event.event_id,
                {
                    "signed_by": event.payload["signed_by"],
                    "reviewer_role": event.payload["reviewer_role"],
                    "position": event.payload["position"],
                    "rationale": event.payload.get("rationale", ""),
                },
            )
        )
    facts.append(
        Fact(
            lock.occurred_at,
            "lineup_locked",
            lock.event_id,
            {
                "receipt_id": body["receipt_id"],
                "locked_by": body["locked_by"],
                "evidence_cutoff": body["evidence_cutoff"],
                "decision_reason": body["decision_reason"],
            },
        )
    )
    facts.sort(key=lambda fact: parse_dt(fact.occurred_at))

    minority = [
        {
            "signed_by": e.payload["signed_by"],
            "reviewer_role": e.payload["reviewer_role"],
            "position": e.payload["position"],
            "rationale": e.payload.get("rationale", ""),
        }
        for e in opinions
        if e.payload["position"] != "support"
    ]
    recalculations = [
        {
            "event_ref": e.event_id,
            "target_competition_ref": e.payload["target_competition_ref"],
            "trigger_event_refs": list(e.payload["trigger_event_refs"]),
        }
        for e in all_events
        if e.event_type == "RECALCULATION_TRIGGERED" and e.payload.get("source_snapshot_ref") == body["receipt_id"]
    ]
    commitments = _commitment_status(all_events, athlete_id, body["receipt_id"], now)

    return {
        "competition_ref": competition_ref,
        "athlete_id": athlete_id,
        "disposition": (entry or {}).get("slot", "not_in_roster"),
        "order": (entry or {}).get("order"),
        "doubles_pair": (entry or {}).get("doubles_pair"),
        "receipt_id": body["receipt_id"],
        "snapshot": {"evidence_cutoff": body["evidence_cutoff"], "locked_at": lock.occurred_at, "immutable": True},
        "fact_chain": [fact.__dict__ for fact in facts],
        "accepted_uncertainties": list(submitted.payload.get("accepted_uncertainties", [])),
        "minority_opinions": minority,
        "commitments": commitments,
        "future_recalculations": recalculations,
    }


def _commitment_status(
    events: Sequence[StoredEvent], athlete_id: str, receipt_id: str, now: datetime
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for event in events:
        if event.event_type != "COMMITMENT_RECORDED" or event.payload.get("subject_id") != athlete_id:
            continue
        if event.payload.get("source_receipt_id") not in (None, receipt_id):
            continue
        fulfilled = next(
            (e for e in events if e.event_type == "COMMITMENT_FULFILLED" and e.aggregate_id == event.aggregate_id),
            None,
        )
        due_at = parse_dt(event.payload["due_at"])
        if fulfilled is None:
            status = "overdue" if now > due_at else "pending"
            result.append(
                {
                    "commitment_ref": event.aggregate_id,
                    "description": event.payload["description"],
                    "due_at": event.payload["due_at"],
                    "status": status,
                }
            )
        else:
            result.append(
                {
                    "commitment_ref": event.aggregate_id,
                    "description": event.payload["description"],
                    "due_at": event.payload["due_at"],
                    "status": "fulfilled",
                    "outcome": fulfilled.payload["outcome"],
                    "fulfilled_at": fulfilled.payload["fulfilled_at"],
                }
            )
    return result


def batch_review_progress(
    store: EventStore, batch_id: str, competition_refs: Sequence[str]
) -> Mapping[str, Any]:
    done = {
        e.payload["competition_ref"]
        for e in store.all_events()
        if e.event_type == "REVIEW_COMPLETED" and e.payload.get("batch_id") == batch_id
    }
    ordered = list(competition_refs)
    pending = [ref for ref in ordered if ref not in done]
    return {
        "batch_id": batch_id,
        "completed": [ref for ref in ordered if ref in done],
        "next_unprocessed": pending[0] if pending else None,
        "pending": pending,
    }


def _lock_for(events: Sequence[StoredEvent], competition_ref: str) -> StoredEvent | None:
    locks = [
        e
        for e in events
        if e.event_type == "LINEUP_LOCKED" and e.payload.get("competition_ref") == competition_ref
    ]
    return locks[-1] if locks else None
