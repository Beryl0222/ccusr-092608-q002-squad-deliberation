"""梯队出场议事服务。

规则要点：
- 一切结论只增事件；锁定后的方案流即封存，任何赛后信息只能另开未来方案。
- 证据按证据截止时点截取，禁止用赛后信息重写当日判断。
- 提议教练不能终审；医疗限制只能由医疗角色解除。
- 并行意见全部保留，冲突的少数意见不被覆盖。
- 确认一次性锁定名额与次序；重复确认沿用原回执，内容漂移进入复议（新修订流）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import validate_event
from .eventstore import DuplicateEvent, EventStore, StoredEvent

MEDICAL_ROLES = {"team_physician", "medical_committee"}
COACH_ROLES = {"head_coach", "assistant_coach"}
LOCK_ROLES = {"selectors_panel", "head_coach"}
REVIEWER_ROLES = COACH_ROLES | MEDICAL_ROLES | {"selectors_panel"}

STARTER = "starter"
SUBSTITUTE = "substitute"
HOLD = "hold"


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DomainError(Exception):
    """业务规则冲突。"""


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    proposal_ref: str
    competition_ref: str
    lock_event: StoredEvent


class SquadDeliberationService:
    def __init__(self, store: EventStore, schedule: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.schedule = dict(schedule or {})
        schema_path = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
        self._schema: Mapping[str, Any] | None = None
        if schema_path.exists():
            self._schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 内部工具

    def _append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        occurred_at: str,
    ) -> StoredEvent:
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": 1,  # 占位，存储按聚合实际版本落盘
            "payload": dict(payload),
        }
        if self._schema is not None:
            issues = validate_event(envelope, self._schema)
            if issues:
                raise DomainError("; ".join(f"{i.field}:{i.code}" for i in issues))
        return self.store.append(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at,
            payload=dict(payload),
        )

    def _stream(self, aggregate_id: str) -> list[StoredEvent]:
        return self.store.events_for_aggregate(aggregate_id)

    def _all(self) -> list[StoredEvent]:
        return self.store.all_events()

    def _proposal_state(self, proposal_id: str) -> dict[str, Any]:
        events = self._stream(proposal_id)
        submitted = next((e for e in events if e.event_type == "PROPOSAL_SUBMITTED"), None)
        locked = next((e for e in events if e.event_type == "LINEUP_LOCKED"), None)
        return {"events": events, "submitted": submitted, "locked": locked}

    def _find_receipt(self, receipt_id: str) -> StoredEvent:
        for event in self._all():
            if event.event_type == "LINEUP_LOCKED" and event.payload.get("receipt_id") == receipt_id:
                return event
        raise DomainError(f"回执不存在: {receipt_id}")

    def _recused_reviewers(self, competition_ref: str) -> set[str]:
        return {
            e.payload["reviewer"]
            for e in self._all()
            if e.event_type == "RECUSAL_DECLARED" and e.payload.get("competition_ref") == competition_ref
        }

    # ------------------------------------------------------------------ 证据与医疗

    def record_evidence(
        self,
        event_id: str,
        subject_id: str,
        evidence_kind: str,
        observed_at: str,
        occurred_at: str,
        summary: str = "",
        **extra: Any,
    ) -> StoredEvent:
        payload: dict[str, Any] = {
            "subject_id": subject_id,
            "evidence_kind": evidence_kind,
            "observed_at": observed_at,
            "summary": summary,
        }
        payload.update(extra)
        return self._append(
            event_id, "EVIDENCE_RECORDED", "selection_evidence", f"evidence-{event_id}", payload, occurred_at
        )

    def grant_medical_clearance(
        self,
        event_id: str,
        subject_id: str,
        restriction_ref: str,
        granted_by: str,
        granted_by_role: str,
        occurred_at: str,
    ) -> StoredEvent:
        if granted_by_role not in MEDICAL_ROLES:
            raise DomainError("医疗限制只能由队医或医务委员会解除")
        payload = {
            "subject_id": subject_id,
            "restriction_ref": restriction_ref,
            "granted_by": granted_by,
            "granted_by_role": granted_by_role,
        }
        return self._append(
            event_id, "MEDICAL_CLEARANCE_GRANTED", "athlete_profile", f"athlete-{subject_id}", payload, occurred_at
        )

    def revoke_medical_clearance(
        self,
        event_id: str,
        subject_id: str,
        restriction_ref: str,
        revoked_by: str,
        occurred_at: str,
    ) -> StoredEvent:
        payload = {"subject_id": subject_id, "restriction_ref": restriction_ref, "revoked_by": revoked_by}
        return self._append(
            event_id, "MEDICAL_CLEARANCE_REVOKED", "athlete_profile", f"athlete-{subject_id}", payload, occurred_at
        )

    def declare_recusal(
        self, event_id: str, competition_ref: str, reviewer: str, reason: str, occurred_at: str
    ) -> StoredEvent:
        payload = {"competition_ref": competition_ref, "reviewer": reviewer, "reason": reason}
        return self._append(
            event_id,
            "RECUSAL_DECLARED",
            "lineup_proposal",
            f"recusal:{competition_ref}:{reviewer}",
            payload,
            occurred_at,
        )

    # ------------------------------------------------------------------ 方案与意见

    def submit_proposal(
        self,
        event_id: str,
        competition_ref: str,
        proposed_by: str,
        roster: Sequence[Mapping[str, Any]],
        evidence_refs: Sequence[str],
        accepted_uncertainties: Sequence[Mapping[str, Any]] = (),
        occurred_at: str | None = None,
    ) -> str:
        occurred_at = occurred_at or self._now()
        self._validate_roster_shape(roster)
        if competition_ref not in self.schedule:
            raise DomainError(f"比赛未登记: {competition_ref}")
        payload = {
            "competition_ref": competition_ref,
            "proposed_by": proposed_by,
            "roster": [dict(entry) for entry in roster],
            "evidence_refs": list(evidence_refs),
            "accepted_uncertainties": [dict(item) for item in accepted_uncertainties],
        }
        self._append(event_id, "PROPOSAL_SUBMITTED", "lineup_proposal", f"proposal-{event_id}", payload, occurred_at)
        return f"proposal-{event_id}"

    @staticmethod
    def _validate_roster_shape(roster: Sequence[Mapping[str, Any]]) -> None:
        if not roster:
            raise DomainError("阵容不能为空")
        athletes = [entry.get("athlete_id") for entry in roster]
        if any(not isinstance(a, str) or not a for a in athletes) or len(set(athletes)) != len(athletes):
            raise DomainError("同一名队员在一份阵容中只能出现一次")
        slots = [entry.get("slot") for entry in roster]
        if any(slot not in (STARTER, SUBSTITUTE, HOLD) for slot in slots):
            raise DomainError("名额类型必须是 starter/substitute/hold")
        orders = [entry["order"] for entry in roster if entry.get("slot") == STARTER]
        if any(not isinstance(o, int) or isinstance(o, bool) for o in orders):
            raise DomainError("首发必须给出整数出场次序")
        if len(set(orders)) != len(orders):
            raise DomainError("出场次序不得重复")

    def sign_opinion(
        self,
        event_id: str,
        proposal_id: str,
        signed_by: str,
        reviewer_role: str,
        position: str,
        occurred_at: str,
        rationale: str = "",
    ) -> StoredEvent:
        if reviewer_role not in REVIEWER_ROLES:
            raise DomainError("角色无权签署评议意见")
        if position not in ("support", "object", "abstain"):
            raise DomainError("立场必须是 support/object/abstain")
        state = self._proposal_state(proposal_id)
        submitted = state["submitted"]
        if submitted is None:
            raise DomainError("方案不存在或尚未提交")
        if state["locked"] is not None:
            raise DomainError("方案已锁定，意见只能签在复议产生的新修订流上")
        competition_ref = submitted.payload["competition_ref"]
        if signed_by in self._recused_reviewers(competition_ref):
            raise DomainError("已声明回避的裁判/教练不得参与本场评议")
        payload = {
            "proposal_ref": proposal_id,
            "signed_by": signed_by,
            "reviewer_role": reviewer_role,
            "position": position,
            "rationale": rationale,
        }
        return self._append(event_id, "OPINION_SIGNED", "lineup_proposal", proposal_id, payload, occurred_at)

    # ------------------------------------------------------------------ 确认/复议

    def confirm_lineup(
        self,
        event_id: str,
        proposal_id: str,
        locked_by: str,
        locked_by_role: str,
        roster: Sequence[Mapping[str, Any]],
        evidence_cutoff: str,
        decision_reason: str,
        occurred_at: str | None = None,
    ) -> Receipt:
        occurred_at = occurred_at or self._now()
        if locked_by_role not in LOCK_ROLES:
            raise DomainError("终审只能由主教练或选拔委员会执行")
        state = self._proposal_state(proposal_id)
        submitted = state["submitted"]
        if submitted is None:
            raise DomainError("方案不存在或尚未提交")
        already = state["locked"]
        if already is not None:
            return self._handle_repeat_confirmation(
                already, submitted, roster, evidence_cutoff, decision_reason, occurred_at
            )

        proposed_by = submitted.payload["proposed_by"]
        if locked_by == proposed_by:
            raise DomainError("提出阵容的教练不能单独完成终审")
        competition_ref = submitted.payload["competition_ref"]
        if locked_by in self._recused_reviewers(competition_ref):
            raise DomainError("已声明回避者不能终审锁定")

        self._validate_roster_shape(roster)
        cutoff = parse_dt(evidence_cutoff)
        opinions = [
            e
            for e in state["events"]
            if e.event_type == "OPINION_SIGNED"
            and e.payload["signed_by"] not in self._recused_reviewers(competition_ref)
        ]
        independent_support = [
            e
            for e in opinions
            if e.payload["position"] == "support"
            and e.payload["signed_by"] != proposed_by
            and e.payload["reviewer_role"] in COACH_ROLES
        ]
        if not independent_support:
            raise DomainError("终审前至少需要一名提议人之外的教练支持")

        evidence = self._evidence_as_of(cutoff)
        evidence_by_id = {e.event_id: e for e in evidence}
        missing = [ref for ref in submitted.payload["evidence_refs"] if ref not in evidence_by_id]
        if missing:
            raise DomainError(f"以下证据在截止时点尚不可获得: {missing}")
        for entry in roster:
            athlete_id = entry["athlete_id"]
            self._check_eligibility(athlete_id, competition_ref, evidence_by_id.values())
            self._check_medical(athlete_id, evidence_by_id.values(), cutoff)
        self._check_avoidance(roster, evidence_by_id.values())

        receipt_id = f"receipt-{competition_ref}-{proposal_id.removeprefix('proposal-')}"
        payload = {
            "proposal_ref": proposal_id,
            "competition_ref": competition_ref,
            "evidence_cutoff": evidence_cutoff,
            "decision_reason": decision_reason,
            "roster": [dict(entry) for entry in roster],
            "receipt_id": receipt_id,
            "locked_by": locked_by,
            "locked_by_role": locked_by_role,
            "evidence_refs": list(submitted.payload["evidence_refs"]),
            "opinions_digest": [
                {
                    "signed_by": e.payload["signed_by"],
                    "reviewer_role": e.payload["reviewer_role"],
                    "position": e.payload["position"],
                }
                for e in opinions
            ],
        }
        self._append(event_id, "LINEUP_LOCKED", "lineup_proposal", proposal_id, payload, occurred_at)
        return Receipt(receipt_id, proposal_id, competition_ref, self._stream(proposal_id)[-1])

    def _handle_repeat_confirmation(
        self,
        locked: StoredEvent,
        submitted: StoredEvent,
        roster: Sequence[Mapping[str, Any]],
        evidence_cutoff: str,
        decision_reason: str,
        occurred_at: str,
    ) -> Receipt:
        original = locked.payload
        drift_fields = self._content_drift(
            original,
            {"roster": [dict(e) for e in roster], "evidence_cutoff": evidence_cutoff, "decision_reason": decision_reason},
        )
        if not drift_fields:
            return Receipt(original["receipt_id"], original["proposal_ref"], original["competition_ref"], locked)
        revision_id = f"proposal-revision-{locked.event_id}-{len(self._all())}"
        payload = {
            "receipt_id": original["receipt_id"],
            "drift_fields": drift_fields,
            "reason": "重复确认内容与锁定快照不一致，进入复议",
            "revises_proposal_ref": original["proposal_ref"],
            "competition_ref": original["competition_ref"],
            "proposed_roster": [dict(entry) for entry in roster],
            "evidence_cutoff": evidence_cutoff,
            "decision_reason": decision_reason,
        }
        self._append(
            f"reconsider-{locked.event_id}-{len(self._all())}",
            "RECONSIDERATION_OPENED",
            "lineup_proposal",
            revision_id,
            payload,
            occurred_at,
        )
        raise DomainError(f"内容漂移 {drift_fields}，已开启复议流 {revision_id}（原回执 {original['receipt_id']} 不变）")

    @staticmethod
    def _content_drift(original: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
        drift: list[str] = []
        if json.dumps(original.get("roster"), ensure_ascii=False, sort_keys=True) != json.dumps(
            candidate["roster"], ensure_ascii=False, sort_keys=True
        ):
            drift.append("roster")
        if original.get("evidence_cutoff") != candidate["evidence_cutoff"]:
            drift.append("evidence_cutoff")
        if original.get("decision_reason") != candidate["decision_reason"]:
            drift.append("decision_reason")
        return drift

    # ------------------------------------------------------------------ 赛前资格核查

    def _evidence_as_of(self, cutoff: datetime) -> list[StoredEvent]:
        return [
            event
            for event in self._all()
            if event.event_type == "EVIDENCE_RECORDED" and parse_dt(event.occurred_at) <= cutoff
        ]

    def _check_eligibility(
        self, athlete_id: str, competition_ref: str, evidence: Sequence[StoredEvent]
    ) -> None:
        entries = [
            e
            for e in evidence
            if e.payload.get("evidence_kind") == "eligibility" and e.payload.get("subject_id") == athlete_id
        ]
        if not entries:
            raise DomainError(f"队员 {athlete_id} 缺少资格期证据")
        latest = max(entries, key=lambda e: parse_dt(e.occurred_at))
        body = latest.payload
        if body.get("status") != "eligible":
            raise DomainError(f"队员 {athlete_id} 资格期状态不可出场")
        match_time = parse_dt(self.schedule[competition_ref])
        valid_from = parse_dt(body["valid_from"]) if body.get("valid_from") else None
        valid_to = parse_dt(body["valid_to"]) if body.get("valid_to") else None
        if (valid_from and match_time < valid_from) or (valid_to and match_time > valid_to):
            raise DomainError(f"队员 {athlete_id} 在比赛日不在资格期内")

    def _check_medical(
        self, athlete_id: str, evidence: Sequence[StoredEvent], cutoff: datetime
    ) -> None:
        restrictions = [
            e
            for e in evidence
            if e.payload.get("evidence_kind") == "medical_restriction"
            and e.payload.get("subject_id") == athlete_id
        ]
        for restriction in restrictions:
            ref = restriction.payload.get("restriction_ref")
            if not ref:
                raise DomainError("医疗限制证据必须携带 restriction_ref")
            resolutions = [
                e
                for e in self._stream(f"athlete-{athlete_id}")
                if e.event_type in ("MEDICAL_CLEARANCE_GRANTED", "MEDICAL_CLEARANCE_REVOKED")
                and e.payload.get("restriction_ref") == ref
                and parse_dt(e.occurred_at) <= cutoff
            ]
            latest = resolutions[-1] if resolutions else None
            if latest is None or latest.event_type == "MEDICAL_CLEARANCE_REVOKED":
                raise DomainError(f"队员 {athlete_id} 存在未解除的医疗限制 {ref}")

    def _check_avoidance(
        self, roster: Sequence[Mapping[str, Any]], evidence: Sequence[StoredEvent]
    ) -> None:
        pairs: set[frozenset[str]] = set()
        for event in evidence:
            if event.payload.get("evidence_kind") != "avoidance":
                continue
            other = event.payload.get("other_athlete_id")
            subject = event.payload.get("subject_id")
            if subject and other:
                pairs.add(frozenset((subject, other)))
        entries = [dict(e) for e in roster if e.get("slot") == STARTER]
        for i, left in enumerate(entries):
            for right in entries[i + 1 :]:
                if frozenset((left["athlete_id"], right["athlete_id"])) in pairs:
                    same_pair = left.get("doubles_pair") and left.get("doubles_pair") == right.get("doubles_pair")
                    if same_pair:
                        raise DomainError(
                            f"回避关系的队员 {left['athlete_id']} 与 {right['athlete_id']} 不能配对双打"
                        )

    # ------------------------------------------------------------------ 赛后触发重算

    def trigger_recalculation(
        self,
        event_id: str,
        source_receipt_id: str,
        trigger_evidence_refs: Sequence[str],
        target_competition_ref: str,
        occurred_at: str | None = None,
    ) -> str:
        occurred_at = occurred_at or self._now()
        locked = self._find_receipt(source_receipt_id)
        source_competition = locked.payload["competition_ref"]
        if target_competition_ref not in self.schedule:
            raise DomainError(f"未来比赛未登记: {target_competition_ref}")
        if parse_dt(self.schedule[target_competition_ref]) <= parse_dt(self.schedule[source_competition]):
            raise DomainError("重算只能面向未来比赛，已锁定的比赛快照不变")
        cutoff = parse_dt(locked.payload["evidence_cutoff"])
        for ref in trigger_evidence_refs:
            event = next((e for e in self._all() if e.event_id == ref), None)
            if event is None or event.event_type != "EVIDENCE_RECORDED":
                raise DomainError(f"触发证据不存在: {ref}")
            if parse_dt(event.occurred_at) <= cutoff:
                raise DomainError("只有赛后新增证据才能触发重算")
        new_proposal_id = f"proposal-recalc-{event_id}"
        payload = {
            "trigger_event_refs": list(trigger_evidence_refs),
            "target_competition_ref": target_competition_ref,
            "source_snapshot_ref": source_receipt_id,
        }
        self._append(
            event_id,
            "RECALCULATION_TRIGGERED",
            "lineup_proposal",
            new_proposal_id,
            payload,
            occurred_at,
        )
        return new_proposal_id

    # ------------------------------------------------------------------ 培养承诺

    def record_commitment(
        self,
        event_id: str,
        subject_id: str,
        description: str,
        due_at: str,
        occurred_at: str,
        source_receipt_id: str | None = None,
    ) -> StoredEvent:
        payload = {
            "subject_id": subject_id,
            "description": description,
            "due_at": due_at,
        }
        if source_receipt_id:
            payload["source_receipt_id"] = source_receipt_id
        return self._append(
            event_id,
            "COMMITMENT_RECORDED",
            "development_commitment",
            f"commitment-{event_id}",
            payload,
            occurred_at,
        )

    def fulfill_commitment(self, event_id: str, commitment_ref: str, fulfilled_at: str) -> StoredEvent:
        recorded = next(
            (e for e in self._stream(commitment_ref) if e.event_type == "COMMITMENT_RECORDED"), None
        )
        if recorded is None:
            raise DomainError(f"承诺不存在: {commitment_ref}")
        if any(e.event_type == "COMMITMENT_FULFILLED" for e in self._stream(commitment_ref)):
            raise DomainError("承诺已兑现，结果不可改写")
        outcome = "on_time" if parse_dt(fulfilled_at) <= parse_dt(recorded.payload["due_at"]) else "late"
        payload = {"commitment_ref": commitment_ref, "fulfilled_at": fulfilled_at, "outcome": outcome}
        return self._append(
            event_id, "COMMITMENT_FULFILLED", "development_commitment", commitment_ref, payload, fulfilled_at
        )

    # ------------------------------------------------------------------ 批量复盘

    def run_batch_review(
        self,
        batch_id: str,
        competition_refs: Sequence[str],
        review_one: Callable[[str, StoredEvent], None] | None = None,
        reviewed_at: str | None = None,
    ) -> list[str]:
        """按序复盘；已完成的比赛跳过，中断后再次调用从首个未处理比赛继续。"""
        reviewed_at = reviewed_at or self._now()
        done = {
            e.payload["competition_ref"]
            for e in self._all()
            if e.event_type == "REVIEW_COMPLETED" and e.payload.get("batch_id") == batch_id
        }
        processed: list[str] = []
        for index, competition_ref in enumerate(competition_refs):
            if competition_ref in done:
                continue
            lock = self._locked_for_competition(competition_ref)
            if review_one is not None:
                review_one(competition_ref, lock)
            payload = {
                "competition_ref": competition_ref,
                "batch_id": batch_id,
                "reviewed_at": reviewed_at,
                "receipt_id": lock.payload["receipt_id"],
                "sequence": index,
            }
            self._append(
                f"review-{batch_id}-{competition_ref}",
                "REVIEW_COMPLETED",
                "lineup_proposal",
                f"review:{batch_id}:{competition_ref}",
                payload,
                reviewed_at,
            )
            processed.append(competition_ref)
        return processed

    def _locked_for_competition(self, competition_ref: str) -> StoredEvent:
        locks = [
            e
            for e in self._all()
            if e.event_type == "LINEUP_LOCKED" and e.payload.get("competition_ref") == competition_ref
        ]
        if not locks:
            raise DomainError(f"比赛尚无锁定阵容，无法复盘: {competition_ref}")
        return locks[-1]

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
