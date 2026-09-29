"""只读投影：把事件流还原为比赛状态与队员事实链。

查询不产出单一评分，而是给出：
- 某队员在某场比赛中入选（starter）/替补（substitute）/暂缓（withheld）的事实链；
- 锁定时仍被接受的不确定性；
- 后续训练承诺是否按期兑现。

所有历史视图都以锁定快照中的 ``evidence_cutoff`` 为准，赛后证据无法渗入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clock import parse_time


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    subject_ref: str
    observed_at: str
    source_role: str
    source_ref: str | None
    confidence: str | None
    content: dict[str, Any]
    competition_ref: str | None
    restriction_ref: str | None

    @property
    def observed(self):
        return parse_time(self.observed_at)

    def summary(self) -> str:
        note = self.content.get("note")
        if isinstance(note, str) and note.strip():
            return note
        return self.kind


def load_evidence(events: list[dict[str, Any]]) -> list[Evidence]:
    records: list[Evidence] = []
    for event in events:
        if event["event_type"] != "EVIDENCE_RECORDED":
            continue
        body = event["payload"]
        records.append(
            Evidence(
                evidence_id=event["event_id"],
                kind=body["evidence_kind"],
                subject_ref=body["subject_ref"],
                observed_at=body["observed_at"],
                source_role=body["source_role"],
                source_ref=body.get("source_ref"),
                confidence=body.get("confidence"),
                content=dict(body.get("content") or {}),
                competition_ref=body.get("competition_ref"),
                restriction_ref=body.get("restriction_ref"),
            )
        )
    return records


def visible(records: list[Evidence], cutoff) -> list[Evidence]:
    return [e for e in records if e.observed <= cutoff]


class CompetitionReader:
    """从事件流构造单场比赛的议事状态。"""

    def __init__(self, events: list[dict[str, Any]], competition_ref: str) -> None:
        self.competition_ref = competition_ref
        self.proposals: list[dict[str, Any]] = []
        self.opinions: dict[str, list[dict[str, Any]]] = {}
        self.lock: dict[str, Any] | None = None
        self.review: dict[str, Any] | None = None
        for event in events:
            body = event.get("payload", {})
            etype = event["event_type"]
            if etype == "PROPOSAL_SUBMITTED" and body.get("competition_ref") == competition_ref:
                self.proposals.append(event)
            elif etype == "OPINION_SIGNED":
                self.opinions.setdefault(body.get("proposal_id"), []).append(event)
            elif etype == "LINEUP_LOCKED" and body.get("competition_ref") == competition_ref:
                self.lock = event
            elif etype == "REVIEW_COMPLETED" and body.get("competition_ref") == competition_ref:
                self.review = event

    @property
    def locked(self) -> bool:
        return self.lock is not None

    def latest_proposal(self) -> dict[str, Any] | None:
        if not self.proposals:
            return None
        return max(self.proposals, key=lambda e: parse_time(e["occurred_at"]))

    def roster_athletes(self, proposal: dict[str, Any]) -> list[str]:
        return [entry["athlete_ref"] for entry in proposal["payload"]["roster"]]


def active_medical_restrictions(records: list[Evidence], athlete: str, cutoff) -> list[Evidence]:
    """cutoff 时点仍未被对应解禁证据解除的医疗限制。"""
    restrictions = [
        e
        for e in records
        if e.kind == "medical_restriction" and e.subject_ref == athlete and e.observed <= cutoff
    ]
    clearances = [
        e
        for e in records
        if e.kind == "medical_clearance" and e.subject_ref == athlete and e.observed <= cutoff
    ]
    cleared = {c.restriction_ref for c in clearances}
    return [r for r in restrictions if r.evidence_id not in cleared]


def active_recusals(records: list[Evidence], parties: set[str], cutoff) -> list[Evidence]:
    """cutoff 时点在出场名单内部仍然生效的回避关系。"""
    found: list[Evidence] = []
    for e in records:
        if e.kind != "matchup_recusal" or e.observed > cutoff:
            continue
        pair = set(e.content.get("parties") or [])
        if len(pair) == 2 and pair <= parties and not e.content.get("resolved"):
            found.append(e)
    return found


def _is_accepted_uncertainty(e: Evidence) -> bool:
    return (e.confidence or "").lower() == "low" or bool(e.content.get("accepted_uncertainty"))


def commitment_status(records: list[Evidence], athlete: str, now) -> list[dict[str, Any]]:
    """汇总某队员全部训练承诺及其兑现情况。"""
    outcomes = [
        e
        for e in records
        if e.kind == "commitment_outcome" and e.subject_ref == athlete
    ]
    by_ref = {e.restriction_ref: e for e in outcomes if e.restriction_ref}
    result: list[dict[str, Any]] = []
    for e in records:
        if e.kind != "training_commitment" or e.subject_ref != athlete:
            continue
        due = e.content.get("due_at")
        outcome = by_ref.get(e.evidence_id)
        if outcome is None:
            if not due:
                status = "pending"
            elif now <= parse_time(due):
                status = "pending"
            else:
                status = "overdue"
            result.append(
                {
                    "commitment_id": e.evidence_id,
                    "description": e.content.get("description", ""),
                    "due_at": due,
                    "status": status,
                    "outcome_at": None,
                }
            )
            continue
        fulfilled = str(outcome.content.get("status", "")).lower() == "fulfilled"
        if not fulfilled:
            status = "breached"
        elif due and outcome.observed <= parse_time(due):
            status = "fulfilled_on_time"
        else:
            status = "fulfilled_late"
        result.append(
            {
                "commitment_id": e.evidence_id,
                "description": e.content.get("description", ""),
                "due_at": due,
                "status": status,
                "outcome_at": outcome.observed_at,
            }
        )
    return result


def athlete_fact_chain(
    events: list[dict[str, Any]],
    athlete: str,
    competition_ref: str,
    *,
    now,
) -> dict[str, Any]:
    """构造队员在某场比赛的完整事实链。"""
    records = load_evidence(events)
    reader = CompetitionReader(events, competition_ref)

    proposed_here: list[dict[str, Any]] = []
    for proposal in reader.proposals:
        if athlete in reader.roster_athletes(proposal):
            proposed_here.append(proposal)

    if reader.lock is not None:
        lock_body = reader.lock["payload"]
        cutoff = parse_time(lock_body["evidence_cutoff"])
        basis = "locked_snapshot"
        locked_entries = {
            entry["athlete_ref"]: entry for entry in lock_body.get("snapshot", {}).get("roster", [])
        }
        if athlete in locked_entries:
            entry = locked_entries[athlete]
            decision = entry.get("status", "starter")
            slot = entry.get("slot")
        else:
            decision, slot = "withheld", None
    else:
        latest = reader.latest_proposal()
        cutoff = now
        basis = "working"
        if latest is not None and athlete in reader.roster_athletes(latest):
            entry = next(
                x for x in latest["payload"]["roster"] if x["athlete_ref"] == athlete
            )
            decision, slot = entry["status"], entry["slot"]
        elif proposed_here:
            decision, slot = "withheld", None
        else:
            decision, slot = "not_considered", None

    athlete_records = visible(
        [
            e
            for e in records
            if e.subject_ref == athlete
            or (
                e.kind == "matchup_recusal"
                and athlete in set(e.content.get("parties") or [])
            )
        ],
        cutoff,
    )
    chain = [
        {
            "evidence_id": e.evidence_id,
            "kind": e.kind,
            "observed_at": e.observed_at,
            "source_role": e.source_role,
            "source_ref": e.source_ref,
            "confidence": e.confidence,
            "summary": e.summary(),
        }
        for e in sorted(athlete_records, key=lambda x: (x.observed, x.evidence_id))
    ]
    uncertainties = [
        {
            "evidence_id": e.evidence_id,
            "kind": e.kind,
            "confidence": e.confidence,
            "note": e.summary(),
        }
        for e in athlete_records
        if _is_accepted_uncertainty(e)
    ]
    restrictions = active_medical_restrictions(records, athlete, cutoff)

    minority: list[dict[str, Any]] = []
    for proposal in proposed_here:
        for opinion in reader.opinions.get(proposal["event_id"], []):
            if opinion["payload"].get("position") != "oppose":
                continue
            if parse_time(opinion["occurred_at"]) > cutoff:
                # 截止线之后补签的意见不得渗入已锁定决策的事实链。
                continue
            minority.append(
                {
                    "opinion_id": opinion["event_id"],
                    "proposal_id": proposal["event_id"],
                    "reviewer_ref": opinion["payload"].get("reviewer_ref"),
                    "reason": opinion["payload"].get("reason", ""),
                }
            )

    return {
        "athlete_ref": athlete,
        "competition_ref": competition_ref,
        "decision": decision,
        "slot": slot,
        "basis": basis,
        "evidence_cutoff": cutoff.isoformat(),
        "chain": chain,
        "accepted_uncertainties": uncertainties,
        "active_medical_restrictions": [e.evidence_id for e in restrictions],
        "minority_opinions": minority,
        "commitments": commitment_status(records, athlete, now),
    }


def competition_decisions(
    events: list[dict[str, Any]],
    competition_ref: str,
    *,
    now,
) -> dict[str, Any]:
    """比赛级视图：列出每名被讨论过的队员的事实链，含暂缓者。

    不输出任何综合评分，只返回事实链集合；调用方可逐条展示
    入选/替补/暂缓的依据、被接受的不确定性与承诺兑现情况。
    """
    reader = CompetitionReader(events, competition_ref)
    considered: set[str] = set()
    for proposal in reader.proposals:
        considered.update(reader.roster_athletes(proposal))
    if reader.lock is not None:
        snapshot_roster = reader.lock["payload"].get("snapshot", {}).get("roster", [])
        ordered = [entry["athlete_ref"] for entry in snapshot_roster]
        considered.update(ordered)
        athletes = ordered + sorted(a for a in considered if a not in ordered)
    else:
        latest = reader.latest_proposal()
        ordered = reader.roster_athletes(latest) if latest else []
        athletes = ordered + sorted(a for a in considered if a not in ordered)
    return {
        "competition_ref": competition_ref,
        "locked": reader.locked,
        "athletes": [
            athlete_fact_chain(events, athlete, competition_ref, now=now)
            for athlete in athletes
        ],
    }
