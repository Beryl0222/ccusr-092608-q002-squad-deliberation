"""梯队出场议事服务。

在事件存储之上落实业务规则：

- 证据按种类独立登记；医疗限制只有 ``medical`` 角色能登记与解除。
- 同一场比赛的方案修订共用一个阵容聚合，靠版本号检测并行提交；
  互相冲突的方案全部保留在事件流中，不覆盖少数方案。
- 意见绑定具体方案修订的内容哈希；方案内容漂移后旧哈希失效，必须重新签署（复议）。
- 名额与出场次序在终审时一次性锁定；终审人不得是方案提出者。
- 重复确认沿用原回执，不产生新事件；锁定后拒绝新方案与新意见。
- 锁定快照不可变；赛后证据只能触发未来比赛重算。
- 批量复盘以事件流为游标，中断后从未处理比赛继续。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .clock import parse_time
from .errors import (
    AlreadyLocked,
    AuthorizationError,
    ConcurrentModification,
    DeliberationBlocked,
    DomainError,
)
from .projections import (
    CompetitionReader,
    active_medical_restrictions,
    active_recusals,
    load_evidence,
    visible,
)
from .store import EventStore

MEDICAL_ROLE = "medical"
FINAL_REVIEWER_ROLE = "final_reviewer"
_VALID_STATUSES = {"starter", "substitute"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def proposal_content_hash(roster: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical(roster).encode("utf-8")).hexdigest()


def lineup_aggregate_id(competition_ref: str) -> str:
    return f"lineup:{competition_ref}"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class DeliberationService:
    def __init__(
        self,
        store: EventStore,
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------------ 证据

    def record_evidence(
        self,
        event_id: str,
        *,
        evidence_kind: str,
        subject_ref: str,
        observed_at: str,
        source_role: str,
        source_ref: str | None = None,
        confidence: str | None = None,
        content: Mapping[str, Any] | None = None,
        competition_ref: str | None = None,
        restriction_ref: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        if evidence_kind in {"medical_restriction", "medical_clearance"} and source_role != MEDICAL_ROLE:
            raise AuthorizationError("医疗限制的登记与解除只能由 medical 角色完成")
        if evidence_kind == "medical_clearance":
            self._require_restriction(subject_ref, restriction_ref)
        payload: dict[str, Any] = {
            "evidence_kind": evidence_kind,
            "subject_ref": subject_ref,
            "observed_at": observed_at,
            "source_role": source_role,
            "content": dict(content or {}),
        }
        for key, value in (
            ("source_ref", source_ref),
            ("confidence", confidence),
            ("competition_ref", competition_ref),
            ("restriction_ref", restriction_ref),
        ):
            if value is not None:
                payload[key] = value
        return self._append(
            event_id=event_id,
            event_type="EVIDENCE_RECORDED",
            aggregate_type="selection_evidence",
            aggregate_id=event_id,
            payload=payload,
            occurred_at=occurred_at,
        )

    def _require_restriction(self, subject_ref: str, restriction_ref: str | None) -> None:
        if not restriction_ref:
            raise AuthorizationError("医疗解禁必须指向被解除的限制证据")
        for event in self.store.all_events():
            if event["event_id"] != restriction_ref:
                continue
            body = event["payload"]
            if (
                event["event_type"] == "EVIDENCE_RECORDED"
                and body.get("evidence_kind") == "medical_restriction"
                and body.get("subject_ref") == subject_ref
            ):
                return
        raise DomainError(f"限制证据 {restriction_ref} 不存在或不属于该运动员")

    # ------------------------------------------------------------------ 方案

    def submit_proposal(
        self,
        event_id: str,
        *,
        competition_ref: str,
        roster: list[Mapping[str, Any]],
        proposer_role: str,
        proposer_ref: str,
        note: str = "",
        expected_version: int | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        reader = CompetitionReader(self.store.all_events(), competition_ref)
        if reader.lock is not None:
            raise AlreadyLocked("比赛阵容已锁定；调整请针对未来比赛提交方案")
        self._validate_roster_shape(roster)
        aggregate_id = lineup_aggregate_id(competition_ref)
        current = self.store.version_of("lineup_proposal", aggregate_id)
        if expected_version is not None and expected_version != current:
            raise ConcurrentModification(
                f"阵容聚合当前版本 {current} 与提交方预期 {expected_version} 不一致；"
                "已有并行修订，请在其基础上重新提交（冲突意见会被保留）"
            )
        payload = {
            "competition_ref": competition_ref,
            "roster": [dict(entry) for entry in roster],
            "proposer_role": proposer_role,
            "proposer_ref": proposer_ref,
            "proposal_hash": proposal_content_hash(roster),
            "note": note,
        }
        return self._append(
            event_id=event_id,
            event_type="PROPOSAL_SUBMITTED",
            aggregate_type="lineup_proposal",
            aggregate_id=aggregate_id,
            payload=payload,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _validate_roster_shape(roster: list[Mapping[str, Any]]) -> None:
        if not roster:
            raise DomainError("出场名单不能为空")
        slots: list[Any] = []
        athletes: set[str] = set()
        has_starter = False
        for entry in roster:
            for key in ("slot", "athlete_ref", "status"):
                if key not in entry:
                    raise DomainError(f"名单条目缺少 {key}")
            if entry["status"] not in _VALID_STATUSES:
                raise DomainError(f"status 只能是 {sorted(_VALID_STATUSES)}")
            has_starter = has_starter or entry["status"] == "starter"
            slots.append(entry["slot"])
            athlete = entry["athlete_ref"]
            if athlete in athletes:
                raise DomainError(f"运动员 {athlete} 在名单中重复出现")
            athletes.add(athlete)
        if not has_starter:
            raise DomainError("出场名单至少包含一名首发")
        if sorted(slots) != list(range(1, len(roster) + 1)):
            raise DomainError("出场次序 slot 必须是从 1 开始的连续且不重复的序号")

    # ------------------------------------------------------------------ 意见

    def sign_opinion(
        self,
        event_id: str,
        *,
        proposal_id: str,
        reviewer_role: str,
        reviewer_ref: str,
        position: str,
        reason: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        proposal = self._require_proposal(proposal_id)
        body = proposal["payload"]
        if reviewer_ref == body["proposer_ref"]:
            raise AuthorizationError("提出阵容的教练不能对本人方案进行终审签署")
        if position not in {"agree", "oppose", "abstain"}:
            raise DomainError("意见立场只能是 agree / oppose / abstain")
        reader = CompetitionReader(self.store.all_events(), body["competition_ref"])
        if reader.lock is not None:
            raise AlreadyLocked("比赛阵容已锁定，不能再补充意见")
        payload = {
            "proposal_id": proposal_id,
            "proposal_hash": body["proposal_hash"],
            "competition_ref": body["competition_ref"],
            "reviewer_role": reviewer_role,
            "reviewer_ref": reviewer_ref,
            "position": position,
            "reason": reason,
        }
        return self._append(
            event_id=event_id,
            event_type="OPINION_SIGNED",
            aggregate_type="lineup_proposal",
            aggregate_id=lineup_aggregate_id(body["competition_ref"]),
            payload=payload,
            occurred_at=occurred_at,
        )

    def _require_proposal(self, proposal_id: str) -> dict[str, Any]:
        for event in self.store.all_events():
            if (
                event["event_type"] == "PROPOSAL_SUBMITTED"
                and event["event_id"] == proposal_id
            ):
                return event
        raise DomainError(f"方案修订 {proposal_id} 不存在")

    # ------------------------------------------------------------------ 锁定

    def confirm_lineup(
        self,
        event_id: str,
        *,
        competition_ref: str,
        reviewer_role: str,
        reviewer_ref: str,
        evidence_cutoff: str,
        decision_reason: str,
        proposal_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        reader = CompetitionReader(self.store.all_events(), competition_ref)

        # 重复确认：沿用原回执，不产生新事件。
        if reader.lock is not None:
            return self._receipt(reader.lock)

        cutoff = parse_time(evidence_cutoff)
        proposal = self._select_proposal(reader, proposal_id)
        proposer_ref = proposal["payload"]["proposer_ref"]

        block_reasons = self._review_blockers(
            reader, proposal, reviewer_role, reviewer_ref, proposer_ref, cutoff
        )
        if block_reasons:
            raise DeliberationBlocked(block_reasons)

        snapshot = self._build_snapshot(reader, proposal, cutoff)
        lock = self._append(
            event_id=event_id,
            event_type="LINEUP_LOCKED",
            aggregate_type="lineup_proposal",
            aggregate_id=lineup_aggregate_id(competition_ref),
            payload={
                "competition_ref": competition_ref,
                "proposal_id": proposal["event_id"],
                "proposal_hash": proposal["payload"]["proposal_hash"],
                "reviewer_role": reviewer_role,
                "reviewer_ref": reviewer_ref,
                "evidence_cutoff": evidence_cutoff,
                "decision_reason": decision_reason,
                "snapshot": snapshot,
            },
            occurred_at=occurred_at,
        )
        return self._receipt(lock)

    @staticmethod
    def _select_proposal(
        reader: CompetitionReader, proposal_id: str | None
    ) -> dict[str, Any]:
        if not reader.proposals:
            raise DeliberationBlocked(["该比赛尚无任何出场方案"])
        if proposal_id is None:
            return reader.latest_proposal()
        for proposal in reader.proposals:
            if proposal["event_id"] == proposal_id:
                return proposal
        raise DeliberationBlocked([f"方案修订 {proposal_id} 不属于该比赛或不存在"])

    def _review_blockers(
        self,
        reader: CompetitionReader,
        proposal: dict[str, Any],
        reviewer_role: str,
        reviewer_ref: str,
        proposer_ref: str,
        cutoff,
    ) -> list[str]:
        reasons: list[str] = []
        if reviewer_role != FINAL_REVIEWER_ROLE:
            reasons.append("终审必须由 final_reviewer 角色完成")
        if reviewer_ref == proposer_ref:
            reasons.append("提出阵容的教练不能单独完成终审")
        if parse_time(proposal["occurred_at"]) > cutoff:
            reasons.append("所选方案修订晚于证据截止线，不属于比赛当日可获得的信息")

        target_hash = proposal["payload"]["proposal_hash"]
        independent = [
            o
            for o in reader.opinions.get(proposal["event_id"], [])
            if o["payload"].get("reviewer_ref") != proposer_ref
            and o["payload"].get("proposal_hash") == target_hash
            and parse_time(o["occurred_at"]) <= cutoff
        ]
        if not any(o["payload"]["position"] == "agree" for o in independent):
            reasons.append(
                "当前方案修订缺少独立复核角色的赞成意见；"
                "若方案较已签署版本发生内容漂移，须重新签署后进入复议"
            )

        all_records = load_evidence(self.store.all_events())
        cutoff_records = visible(all_records, cutoff)
        athletes = {entry["athlete_ref"] for entry in proposal["payload"]["roster"]}
        for athlete in sorted(athletes):
            if not self._eligible_at(cutoff_records, athlete, cutoff):
                reasons.append(f"运动员 {athlete} 在证据截止线时点不在资格期内或缺少资格证据")
            restrictions = active_medical_restrictions(all_records, athlete, cutoff)
            if restrictions:
                reasons.append(
                    f"运动员 {athlete} 存在未解除的医疗限制: "
                    + ", ".join(e.evidence_id for e in restrictions)
                )
        for recusal in active_recusals(all_records, athletes, cutoff):
            reasons.append(
                f"出场名单内部存在未解决的回避关系 {recusal.evidence_id}: "
                + "/".join(recusal.content.get("parties", []))
            )
        return reasons

    @staticmethod
    def _eligible_at(records, athlete: str, cutoff) -> bool:
        windows = [
            e
            for e in records
            if e.kind == "eligibility"
            and e.subject_ref == athlete
            and not e.content.get("revoked")
        ]
        for window in windows:
            start = window.content.get("eligible_from")
            end = window.content.get("eligible_until")
            if start and cutoff < parse_time(start):
                continue
            if end and cutoff >= parse_time(end):
                continue
            return True
        return False

    def _build_snapshot(
        self, reader: CompetitionReader, proposal: dict[str, Any], cutoff
    ) -> dict[str, Any]:
        all_records = load_evidence(self.store.all_events())
        records = visible(all_records, cutoff)
        roster_athletes = {entry["athlete_ref"] for entry in proposal["payload"]["roster"]}
        opinions = [
            {
                "opinion_id": o["event_id"],
                "reviewer_ref": o["payload"].get("reviewer_ref"),
                "position": o["payload"]["position"],
                "proposal_hash": o["payload"].get("proposal_hash"),
                "reason": o["payload"].get("reason", ""),
            }
            for o in reader.opinions.get(proposal["event_id"], [])
            if parse_time(o["occurred_at"]) <= cutoff
        ]
        medical_state = {
            athlete: [
                e.evidence_id
                for e in active_medical_restrictions(all_records, athlete, cutoff)
            ]
            for athlete in sorted(roster_athletes)
        }
        return {
            "roster": proposal["payload"]["roster"],
            "proposal_hash": proposal["payload"]["proposal_hash"],
            "evidence_ids": sorted(
                e.evidence_id
                for e in records
                if e.subject_ref in roster_athletes
                or e.kind in {"opponent_analysis", "matchup_recusal"}
            ),
            "opinions": opinions,
            "medical_state": medical_state,
        }

    @staticmethod
    def _receipt(lock_event: dict[str, Any]) -> dict[str, Any]:
        body = lock_event["payload"]
        basis = f'{body["competition_ref"]}|{body["proposal_id"]}|{_canonical(body["snapshot"]["roster"])}'
        receipt = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
        return {
            "receipt": receipt,
            "competition_ref": body["competition_ref"],
            "proposal_id": body["proposal_id"],
            "locked_at": lock_event["occurred_at"],
            "roster": body["snapshot"]["roster"],
        }

    # ------------------------------------------------------------------ 复盘

    def complete_review(
        self,
        event_id: str,
        *,
        competition_ref: str,
        outcome: Mapping[str, Any],
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        reader = CompetitionReader(self.store.all_events(), competition_ref)
        if reader.lock is None:
            raise DomainError("比赛尚未锁定阵容，不能完成赛后复盘")
        if reader.review is not None:
            return reader.review
        return self._append(
            event_id=event_id,
            event_type="REVIEW_COMPLETED",
            aggregate_type="lineup_proposal",
            aggregate_id=lineup_aggregate_id(competition_ref),
            payload={"competition_ref": competition_ref, "outcome": dict(outcome)},
            occurred_at=occurred_at,
        )

    def new_evidence_since_lock(self, competition_ref: str) -> list[dict[str, str]]:
        """赛后新增证据：只能作为未来比赛重算的输入，不回写锁定快照。"""
        reader = CompetitionReader(self.store.all_events(), competition_ref)
        if reader.lock is None:
            return []
        cutoff = parse_time(reader.lock["payload"]["evidence_cutoff"])
        result = []
        for event in self.store.all_events():
            if event["event_type"] != "EVIDENCE_RECORDED":
                continue
            observed = parse_time(event["payload"]["observed_at"])
            if observed > cutoff:
                result.append(
                    {
                        "evidence_id": event["event_id"],
                        "evidence_kind": event["payload"]["evidence_kind"],
                        "subject_ref": event["payload"]["subject_ref"],
                        "observed_at": event["payload"]["observed_at"],
                    }
                )
        return result

    def batch_review(
        self,
        jobs: list[Mapping[str, Any]],
    ) -> dict[str, list[str]]:
        """按顺序复盘一批比赛，已完成的跳过。

        每个 job 含 ``competition_ref``、``outcome``、``event_id``。
        进度完全以事件流为游标：处理到一半中断后重新调用同一列表，
        已完成比赛被跳过，自动从第一个未处理比赛继续，不会重复处理。
        """
        processed, skipped, failed = [], [], []
        for index, job in enumerate(jobs):
            competition_ref = job["competition_ref"]
            reader = CompetitionReader(self.store.all_events(), competition_ref)
            if reader.review is not None:
                skipped.append(competition_ref)
                continue
            try:
                self.complete_review(
                    job["event_id"],
                    competition_ref=competition_ref,
                    outcome=job.get("outcome", {}),
                    occurred_at=job.get("occurred_at"),
                )
                processed.append(competition_ref)
            except DomainError as exc:
                failed.append(competition_ref)
                raise type(exc)(f"第 {index} 项 {competition_ref} 复盘失败: {exc}") from exc
        return {"processed": processed, "skipped": skipped, "failed": failed}

    # ------------------------------------------------------------------ 内部

    def _append(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        occurred_at: str | None,
    ) -> dict[str, Any]:
        version = self.store.version_of(aggregate_type, aggregate_id) + 1
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or self.clock().isoformat(),
            "version": version,
            "payload": dict(payload),
        }
        return self.store.append(event)
