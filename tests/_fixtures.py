"""议事服务测试夹具：构造一场可锁定的完整议事流。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squad_deliberation.eventstore import InMemoryEventStore
from squad_deliberation.service import SquadDeliberationService

MATCH_A = "match-2026-team-A"
MATCH_B = "match-2026-team-B"
MATCH_C = "match-2026-team-C"

CUTOFF = "2026-09-29T12:00:00+08:00"

SCHEDULE = {
    MATCH_A: "2026-10-01T19:00:00+08:00",
    MATCH_B: "2026-10-08T19:00:00+08:00",
    MATCH_C: "2026-10-15T19:00:00+08:00",
}

ROSTER = [
    {"athlete_id": "athlete-001", "slot": "starter", "order": 1},
    {"athlete_id": "athlete-002", "slot": "starter", "order": 2, "doubles_pair": "pair-x"},
    {"athlete_id": "athlete-003", "slot": "starter", "order": 3, "doubles_pair": "pair-x"},
    {"athlete_id": "athlete-004", "slot": "substitute"},
    {"athlete_id": "athlete-005", "slot": "hold", "hold_reason": "培养观察期，暂缓出场"},
]


def build_service(store: InMemoryEventStore | None = None, schedule: dict[str, str] | None = None) -> SquadDeliberationService:
    return SquadDeliberationService(store or InMemoryEventStore(), schedule or SCHEDULE)


def seed_evidence(service: SquadDeliberationService, prefix: str = "ev") -> list[str]:
    """记录截止时点之前可获得的各类证据，返回证据标识。"""
    refs: list[str] = []

    def add(event_id: str, athlete: str, kind: str, at: str = "2026-09-20T10:00:00+08:00", **extra: object) -> None:
        service.record_evidence(
            f"{prefix}-{event_id}",
            subject_id=athlete,
            evidence_kind=kind,
            observed_at=at,
            occurred_at="2026-09-20T10:05:00+08:00",
            **extra,
        )
        refs.append(f"{prefix}-{event_id}")

    for athlete in ("athlete-001", "athlete-002", "athlete-003", "athlete-004", "athlete-005"):
        add(f"elig-{athlete}", athlete, "eligibility",
            status="eligible", valid_from="2026-01-01T00:00:00+08:00", valid_to="2026-12-31T23:59:59+08:00")
    add("load-1", "athlete-001", "training_load", summary="近四周负荷平稳")
    add("sample-1", "athlete-002", "match_sample", summary="近八场胜负与关键分")
    add("opponent-1", "athlete-003", "opponent_analysis", summary="对手左手打法分析")
    add("observation-1", "athlete-004", "coach_observation", summary="训练赛当日状态良好")
    add("goal-5", "athlete-005", "development_goal", summary="本周期以抗压培养为目标")
    add("medical-2", "athlete-002", "medical_restriction",
        restriction_ref="restr-2026-017", summary="肩伤恢复观察")
    service.grant_medical_clearance(
        f"{prefix}-clear-2",
        subject_id="athlete-002",
        restriction_ref="restr-2026-017",
        granted_by="doctor-chen",
        granted_by_role="team_physician",
        occurred_at="2026-09-25T09:00:00+08:00",
    )
    return refs


def full_lock(
    service: SquadDeliberationService,
    competition_ref: str = MATCH_A,
    prefix: str = "t1",
    roster: list[dict] | None = None,
    minority: bool = True,
) -> tuple[str, str]:
    """提交、会签并锁定，返回 (方案标识, 回执标识)。"""
    refs = seed_evidence(service, prefix=prefix)
    uncertainties = [
        {"athlete_id": "athlete-002", "point": "肩伤复发概率样本仅三场", "accepted_by": "coach-li"}
    ]
    proposal_id = service.submit_proposal(
        f"{prefix}-proposal",
        competition_ref=competition_ref,
        proposed_by="coach-li",
        roster=roster or ROSTER,
        evidence_refs=refs,
        accepted_uncertainties=uncertainties,
        occurred_at="2026-09-28T09:00:00+08:00",
    )
    service.sign_opinion(
        f"{prefix}-opinion-head",
        proposal_id=proposal_id,
        signed_by="coach-wang",
        reviewer_role="head_coach",
        position="support",
        rationale="次序与双打适配认可",
        occurred_at="2026-09-28T15:00:00+08:00",
    )
    if minority:
        service.sign_opinion(
            f"{prefix}-opinion-doc",
            proposal_id=proposal_id,
            signed_by="doctor-chen",
            reviewer_role="team_physician",
            position="object",
            rationale="002 肩伤仍建议替补",
            occurred_at="2026-09-28T16:00:00+08:00",
        )
    receipt = service.confirm_lineup(
        f"{prefix}-lock",
        proposal_id=proposal_id,
        locked_by="panel-2026",
        locked_by_role="selectors_panel",
        roster=roster or ROSTER,
        evidence_cutoff=CUTOFF,
        decision_reason="按截止时点证据锁定，保留医务反对意见",
        occurred_at="2026-09-29T18:00:00+08:00",
    )
    return proposal_id, receipt.receipt_id
