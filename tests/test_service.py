"""议事库业务规则测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from squad_deliberation.eventstore import DuplicateEvent, InMemoryEventStore, JsonlEventStore
from squad_deliberation.queries import athlete_decision_report, batch_review_progress
from squad_deliberation.service import DomainError

from _fixtures import (
    CUTOFF,
    MATCH_A,
    MATCH_B,
    MATCH_C,
    ROSTER,
    build_service,
    full_lock,
    seed_evidence,
)


class EventStoreTests(unittest.TestCase):
    def test_event_id_is_idempotent(self) -> None:
        store = InMemoryEventStore()
        store.append("e1", "EVIDENCE_RECORDED", "selection_evidence", "x", "2026-09-20T10:00:00+08:00", {"a": 1})
        with self.assertRaises(DuplicateEvent):
            store.append("e1", "EVIDENCE_RECORDED", "selection_evidence", "x", "2026-09-20T10:00:00+08:00", {"a": 2})
        self.assertEqual(store.all_events()[0].payload, {"a": 1})

    def test_version_increments_per_aggregate(self) -> None:
        store = InMemoryEventStore()
        first = store.append("e1", "EVIDENCE_RECORDED", "selection_evidence", "agg-1", "2026-09-20T10:00:00+08:00", {})
        second = store.append("e2", "EVIDENCE_RECORDED", "selection_evidence", "agg-1", "2026-09-20T11:00:00+08:00", {})
        other = store.append("e3", "EVIDENCE_RECORDED", "selection_evidence", "agg-2", "2026-09-20T12:00:00+08:00", {})
        self.assertEqual((first.version, second.version, other.version), (1, 2, 1))

    def test_jsonl_store_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = JsonlEventStore(path)
            store.append("e1", "EVIDENCE_RECORDED", "selection_evidence", "agg-1", "2026-09-20T10:00:00+08:00", {})
            reloaded = JsonlEventStore(path)
            self.assertEqual([e.event_id for e in reloaded.all_events()], ["e1"])
            self.assertEqual(reloaded.all_events()[0].version, 1)


class ConfirmationRulesTests(unittest.TestCase):
    def test_proposer_cannot_be_final_reviewer(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "o1", proposal_id, "coach-li", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T10:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock1", proposal_id, "coach-li", "head_coach", ROSTER, CUTOFF, "自我终审",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("不能单独完成终审", str(ctx.exception))

    def test_needs_independent_coach_support(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock1", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "无人会签",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("提议人之外的教练支持", str(ctx.exception))

    def test_medical_restriction_can_only_be_cleared_by_medical_role(self) -> None:
        service = build_service()
        with self.assertRaises(DomainError) as ctx:
            service.grant_medical_clearance(
                "bad-clear", "athlete-002", "restr-2026-017",
                granted_by="coach-li", granted_by_role="head_coach",
                occurred_at="2026-09-25T09:00:00+08:00",
            )
        self.assertIn("只能由队医或医务委员会解除", str(ctx.exception))

    def test_unresolved_medical_restriction_blocks_lock(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        service.record_evidence(
            "ev-medical-1", subject_id="athlete-001", evidence_kind="medical_restriction",
            observed_at="2026-09-26T10:00:00+08:00", occurred_at="2026-09-26T10:05:00+08:00",
            restriction_ref="restr-2026-018", summary="踝伤未过体检",
        )
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs + ["ev-medical-1"],
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "o1", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock1", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "带伤上阵",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("restr-2026-018", str(ctx.exception))

    def test_revoked_clearance_blocks_lock(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        service.revoke_medical_clearance(
            "revoke-2", subject_id="athlete-002", restriction_ref="restr-2026-017",
            revoked_by="doctor-chen", occurred_at="2026-09-28T08:00:00+08:00",
        )
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "o1", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        with self.assertRaises(DomainError):
            service.confirm_lineup(
                "lock1", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "解除被撤回",
                occurred_at="2026-09-29T18:00:00+08:00",
            )

    def test_recused_reviewer_cannot_sign_or_lock(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        service.declare_recusal(
            "rec-1", MATCH_A, reviewer="coach-wang", reason="与对方教练有亲属关系",
            occurred_at="2026-09-27T09:00:00+08:00",
        )
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        with self.assertRaises(DomainError):
            service.sign_opinion(
                "o1", proposal_id, "coach-wang", "head_coach", "support",
                rationale="", occurred_at="2026-09-28T15:00:00+08:00",
            )


class AvoidanceTests(unittest.TestCase):
    def test_avoided_pair_cannot_be_doubles_partners(self) -> None:
        service = build_service()
        refs = seed_evidence(service, prefix="av")
        service.record_evidence(
            "av-avoid", subject_id="athlete-002", evidence_kind="avoidance",
            observed_at="2026-09-26T10:00:00+08:00", occurred_at="2026-09-26T10:05:00+08:00",
            other_athlete_id="athlete-003", summary="两人沟通冲突，不宜配对双打",
        )
        proposal_id = service.submit_proposal(
            "av-p", MATCH_A, proposed_by="coach-li", roster=ROSTER,
            evidence_refs=refs + ["av-avoid"], occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "av-o", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "av-lock", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "强行配对",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("回避", str(ctx.exception))

    def test_avoided_pair_can_stay_as_separate_singles(self) -> None:
        service = build_service()
        refs = seed_evidence(service, prefix="av2")
        service.record_evidence(
            "av2-avoid", subject_id="athlete-002", evidence_kind="avoidance",
            observed_at="2026-09-26T10:00:00+08:00", occurred_at="2026-09-26T10:05:00+08:00",
            other_athlete_id="athlete-003", summary="不配对即可同场",
        )
        singles_roster = [dict(entry) for entry in ROSTER]
        for entry in singles_roster:
            entry.pop("doubles_pair", None)
        proposal_id = service.submit_proposal(
            "av2-p", MATCH_A, proposed_by="coach-li", roster=singles_roster,
            evidence_refs=refs + ["av2-avoid"], occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "av2-o", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        receipt = service.confirm_lineup(
            "av2-lock", proposal_id, "panel-2026", "selectors_panel", singles_roster, CUTOFF,
            "分开出场", occurred_at="2026-09-29T18:00:00+08:00",
        )
        self.assertTrue(receipt.receipt_id.startswith("receipt-"))


class EvidenceCutoffTests(unittest.TestCase):
    def test_post_cutoff_evidence_cannot_support_lock(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        late_id = "ev-late-feedback"
        service.record_evidence(
            late_id, subject_id="athlete-001", evidence_kind="post_match_feedback",
            observed_at="2026-09-29T20:00:00+08:00", occurred_at="2026-09-29T20:05:00+08:00",
            summary="赛后反馈，不能用于当日决策",
        )
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs + [late_id],
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "o1", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock1", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "引用未来信息",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("截止时点尚不可获得", str(ctx.exception))

    def test_eligibility_window_must_cover_match_day(self) -> None:
        service = build_service()
        refs = seed_evidence(service)
        service.record_evidence(
            "ev-elig-expired", subject_id="athlete-001", evidence_kind="eligibility",
            observed_at="2026-09-27T10:00:00+08:00", occurred_at="2026-09-27T10:05:00+08:00",
            status="eligible", valid_from="2026-01-01T00:00:00+08:00",
            valid_to="2026-09-30T23:59:59+08:00",
        )
        refs.append("ev-elig-expired")
        proposal_id = service.submit_proposal(
            "p1", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        service.sign_opinion(
            "o1", proposal_id, "coach-wang", "head_coach", "support",
            rationale="", occurred_at="2026-09-28T15:00:00+08:00",
        )
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock1", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF, "资格过期",
                occurred_at="2026-09-29T18:00:00+08:00",
            )
        self.assertIn("资格期", str(ctx.exception))


class ParallelOpinionsTests(unittest.TestCase):
    def test_conflicting_opinions_are_all_retained(self) -> None:
        service = build_service()
        _, receipt_id = full_lock(service)
        report = athlete_decision_report(service.store, MATCH_A, "athlete-002", as_of="2026-10-02T10:00:00+08:00")
        self.assertEqual(report["receipt_id"], receipt_id)
        positions = {fact["detail"]["signed_by"]: fact["detail"]["position"] for fact in report["fact_chain"]
                     if fact["fact_type"] == "opinion_signed"}
        self.assertEqual(positions["coach-wang"], "support")
        self.assertEqual(positions["doctor-chen"], "object")
        minority = report["minority_opinions"]
        self.assertEqual(len(minority), 1)
        self.assertEqual(minority[0]["signed_by"], "doctor-chen")

    def test_two_coaches_can_submit_competing_proposals(self) -> None:
        service = build_service()
        refs = seed_evidence(service, prefix="par")
        roster_b = [dict(entry) for entry in ROSTER if entry["athlete_id"] != "athlete-005"]
        roster_b.append({"athlete_id": "athlete-006", "slot": "hold", "hold_reason": "另一教练主张暂缓"})
        service.record_evidence(
            "par-elig-6", subject_id="athlete-006", evidence_kind="eligibility",
            observed_at="2026-09-20T10:00:00+08:00", occurred_at="2026-09-20T10:05:00+08:00",
            status="eligible", valid_from="2026-01-01T00:00:00+08:00",
            valid_to="2026-12-31T23:59:59+08:00",
        )
        proposal_a = service.submit_proposal(
            "par-pa", MATCH_A, proposed_by="coach-li", roster=ROSTER, evidence_refs=refs,
            occurred_at="2026-09-28T09:00:00+08:00",
        )
        proposal_b = service.submit_proposal(
            "par-pb", MATCH_A, proposed_by="coach-zhao", roster=roster_b,
            evidence_refs=refs + ["par-elig-6"], occurred_at="2026-09-28T09:30:00+08:00",
        )
        service.sign_opinion("par-oa", proposal_a, "coach-zhao", "assistant_coach", "object",
                             rationale="005 应给机会", occurred_at="2026-09-28T11:00:00+08:00")
        service.sign_opinion("par-ob", proposal_b, "coach-li", "assistant_coach", "object",
                             rationale="005 更稳", occurred_at="2026-09-28T11:30:00+08:00")
        # 两条互相冲突的方案流都保留，没有哪条覆盖另一条
        self.assertNotEqual(proposal_a, proposal_b)
        self.assertEqual(
            [e.payload["position"] for e in service.store.events_for_aggregate(proposal_a)
             if e.event_type == "OPINION_SIGNED"],
            ["object"],
        )
        self.assertEqual(
            [e.payload["position"] for e in service.store.events_for_aggregate(proposal_b)
             if e.event_type == "OPINION_SIGNED"],
            ["object"],
        )


class LockAndReceiptTests(unittest.TestCase):
    def test_roster_slots_and_order_lock_atomically(self) -> None:
        service = build_service()
        proposal_id, receipt_id = full_lock(service)
        lock = next(e for e in service.store.events_for_aggregate(proposal_id) if e.event_type == "LINEUP_LOCKED")
        self.assertEqual(lock.payload["receipt_id"], receipt_id)
        starters = [(e["athlete_id"], e["order"]) for e in lock.payload["roster"] if e["slot"] == "starter"]
        self.assertEqual(starters, [("athlete-001", 1), ("athlete-002", 2), ("athlete-003", 3)])

    def test_duplicate_order_is_rejected(self) -> None:
        service = build_service()
        bad_roster = [dict(e) for e in ROSTER]
        bad_roster[1]["order"] = 1
        with self.assertRaises(DomainError) as ctx:
            service.submit_proposal(
                "p-bad", MATCH_A, proposed_by="coach-li", roster=bad_roster, evidence_refs=[],
                occurred_at="2026-09-28T09:00:00+08:00",
            )
        self.assertIn("出场次序", str(ctx.exception))

    def test_repeat_confirmation_reuses_receipt(self) -> None:
        service = build_service()
        proposal_id, receipt_id = full_lock(service)
        second = service.confirm_lineup(
            "lock-repeat", proposal_id, "panel-2026", "selectors_panel", ROSTER, CUTOFF,
            "按截止时点证据锁定，保留医务反对意见", occurred_at="2026-09-30T08:00:00+08:00",
        )
        self.assertEqual(second.receipt_id, receipt_id)
        locks = [e for e in service.store.all_events() if e.event_type == "LINEUP_LOCKED"]
        self.assertEqual(len(locks), 1)

    def test_content_drift_opens_reconsideration(self) -> None:
        service = build_service()
        proposal_id, receipt_id = full_lock(service)
        drifted = [dict(entry) for entry in ROSTER]
        drifted[0], drifted[1] = drifted[1], drifted[0]
        with self.assertRaises(DomainError) as ctx:
            service.confirm_lineup(
                "lock-drift", proposal_id, "panel-2026", "selectors_panel", drifted, CUTOFF,
                "调整前两号次序", occurred_at="2026-09-30T08:00:00+08:00",
            )
        self.assertIn("复议", str(ctx.exception))
        reconsiderations = [e for e in service.store.all_events() if e.event_type == "RECONSIDERATION_OPENED"]
        self.assertEqual(len(reconsiderations), 1)
        self.assertEqual(reconsiderations[0].payload["receipt_id"], receipt_id)
        self.assertIn("roster", reconsiderations[0].payload["drift_fields"])
        # 原回执快照没有被改写
        lock = next(e for e in service.store.events_for_aggregate(proposal_id) if e.event_type == "LINEUP_LOCKED")
        self.assertEqual(lock.payload["roster"][0]["athlete_id"], "athlete-001")

    def test_locked_proposal_rejects_new_opinions(self) -> None:
        service = build_service()
        proposal_id, _ = full_lock(service)
        with self.assertRaises(DomainError):
            service.sign_opinion(
                "late-opinion", proposal_id, "coach-zhao", "assistant_coach", "object",
                rationale="赛后才想反对", occurred_at="2026-10-02T09:00:00+08:00",
            )


class RecalculationTests(unittest.TestCase):
    def test_post_match_data_triggers_future_recalculation_only(self) -> None:
        service = build_service()
        _, receipt_id = full_lock(service)
        feedback_id = "fb-001"
        service.record_evidence(
            feedback_id, subject_id="athlete-002", evidence_kind="post_match_feedback",
            observed_at="2026-10-02T20:00:00+08:00", occurred_at="2026-10-02T20:30:00+08:00",
            summary="团体赛后002关键分处理数据",
        )
        new_stream = service.trigger_recalculation(
            "rec-1", source_receipt_id=receipt_id, trigger_evidence_refs=[feedback_id],
            target_competition_ref=MATCH_B, occurred_at="2026-10-03T09:00:00+08:00",
        )
        self.assertTrue(new_stream.startswith("proposal-recalc-"))
        # 原快照内容保持不变
        report = athlete_decision_report(service.store, MATCH_A, "athlete-002", as_of="2026-10-03T10:00:00+08:00")
        self.assertTrue(report["snapshot"]["immutable"])
        self.assertEqual(report["disposition"], "starter")
        self.assertEqual(len(report["future_recalculations"]), 1)

    def test_pre_cutoff_evidence_cannot_trigger(self) -> None:
        service = build_service()
        _, receipt_id = full_lock(service)
        with self.assertRaises(DomainError):
            service.trigger_recalculation(
                "rec-bad", source_receipt_id=receipt_id, trigger_evidence_refs=["t1-sample-1"],
                target_competition_ref=MATCH_B, occurred_at="2026-10-03T09:00:00+08:00",
            )

    def test_recalculation_cannot_target_past_match(self) -> None:
        service = build_service()
        refs = seed_evidence(service, prefix="x")
        # 给 B 场也锁一场
        full_lock(service, competition_ref=MATCH_B, prefix="mb")
        _, receipt_a = full_lock(service, competition_ref=MATCH_A, prefix="ma")
        feedback_id = "fb-late"
        service.record_evidence(
            feedback_id, subject_id="athlete-001", evidence_kind="post_match_feedback",
            observed_at="2026-10-09T20:00:00+08:00", occurred_at="2026-10-09T20:30:00+08:00",
            summary="B 场后反馈",
        )
        with self.assertRaises(DomainError):
            service.trigger_recalculation(
                "rec-past", source_receipt_id=receipt_a, trigger_evidence_refs=[feedback_id],
                target_competition_ref=MATCH_A, occurred_at="2026-10-10T09:00:00+08:00",
            )


class CommitmentTests(unittest.TestCase):
    def test_commitment_fulfilment_tracking(self) -> None:
        service = build_service()
        _, receipt_id = full_lock(service)
        service.record_commitment(
            "c1", subject_id="athlete-005", description="四周内完成抗压模拟训练并复评",
            due_at="2026-10-27T18:00:00+08:00", occurred_at="2026-09-30T09:00:00+08:00",
            source_receipt_id=receipt_id,
        )
        pending = athlete_decision_report(service.store, MATCH_A, "athlete-005", as_of="2026-10-10T10:00:00+08:00")
        self.assertEqual(pending["commitments"][0]["status"], "pending")
        service.fulfill_commitment("f1", "commitment-c1", fulfilled_at="2026-10-25T10:00:00+08:00")
        done = athlete_decision_report(service.store, MATCH_A, "athlete-005", as_of="2026-10-26T10:00:00+08:00")
        self.assertEqual(done["commitments"][0]["status"], "fulfilled")
        self.assertEqual(done["commitments"][0]["outcome"], "on_time")

    def test_overdue_and_late(self) -> None:
        service = build_service()
        _, receipt_id = full_lock(service)
        service.record_commitment(
            "c2", subject_id="athlete-005", description="补强发球落点",
            due_at="2026-10-20T18:00:00+08:00", occurred_at="2026-09-30T09:00:00+08:00",
            source_receipt_id=receipt_id,
        )
        overdue = athlete_decision_report(service.store, MATCH_A, "athlete-005", as_of="2026-10-22T10:00:00+08:00")
        self.assertEqual(overdue["commitments"][0]["status"], "overdue")
        service.fulfill_commitment("f2", "commitment-c2", fulfilled_at="2026-10-23T10:00:00+08:00")
        late = athlete_decision_report(service.store, MATCH_A, "athlete-005", as_of="2026-10-24T10:00:00+08:00")
        self.assertEqual(late["commitments"][0]["outcome"], "late")
        with self.assertRaises(DomainError):
            service.fulfill_commitment("f3", "commitment-c2", fulfilled_at="2026-10-24T10:00:00+08:00")


class FactChainQueryTests(unittest.TestCase):
    def test_report_shows_dispositions_and_uncertainties(self) -> None:
        service = build_service()
        full_lock(service)
        starter = athlete_decision_report(service.store, MATCH_A, "athlete-001", as_of="2026-10-02T10:00:00+08:00")
        substitute = athlete_decision_report(service.store, MATCH_A, "athlete-004", as_of="2026-10-02T10:00:00+08:00")
        hold = athlete_decision_report(service.store, MATCH_A, "athlete-005", as_of="2026-10-02T10:00:00+08:00")
        self.assertEqual((starter["disposition"], substitute["disposition"], hold["disposition"]),
                         ("starter", "substitute", "hold"))
        fact_types = [fact["fact_type"] for fact in starter["fact_chain"]]
        self.assertIn("evidence:eligibility", fact_types)
        self.assertIn("lineup_locked", fact_types)
        self.assertTrue(starter["fact_chain"] == sorted(starter["fact_chain"], key=lambda f: f["occurred_at"]))
        self.assertEqual(starter["accepted_uncertainties"][0]["athlete_id"], "athlete-002")

    def test_unknown_athlete_reports_not_in_roster(self) -> None:
        service = build_service()
        full_lock(service)
        report = athlete_decision_report(service.store, MATCH_A, "athlete-999", as_of="2026-10-02T10:00:00+08:00")
        self.assertEqual(report["disposition"], "not_in_roster")


class BatchReviewTests(unittest.TestCase):
    def _three_matches(self, store: InMemoryEventStore | None = None) -> tuple:
        service = build_service(store)
        full_lock(service, competition_ref=MATCH_A, prefix="ma")
        full_lock(service, competition_ref=MATCH_B, prefix="mb")
        full_lock(service, competition_ref=MATCH_C, prefix="mc")
        return service, [MATCH_A, MATCH_B, MATCH_C]

    def test_interrupted_batch_continues_on_restart(self) -> None:
        service, matches = self._three_matches()

        def flaky(ref: str, lock: object) -> None:
            if ref == MATCH_B:
                raise RuntimeError("批量复盘中断")

        with self.assertRaises(RuntimeError):
            service.run_batch_review("batch-9", matches, review_one=flaky,
                                     reviewed_at="2026-10-20T10:00:00+08:00")
        progress = batch_review_progress(service.store, "batch-9", matches)
        self.assertEqual(progress["completed"], [MATCH_A])
        self.assertEqual(progress["next_unprocessed"], MATCH_B)

        resumed = service.run_batch_review("batch-9", matches, reviewed_at="2026-10-21T10:00:00+08:00")
        self.assertEqual(resumed, [MATCH_B, MATCH_C])
        progress = batch_review_progress(service.store, "batch-9", matches)
        self.assertIsNone(progress["next_unprocessed"])

    def test_batch_checkpoint_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            service, matches = self._three_matches(JsonlEventStore(path))

            def flaky(ref: str, lock: object) -> None:
                if ref == MATCH_C:
                    raise RuntimeError("中断在最后一场")

            with self.assertRaises(RuntimeError):
                service.run_batch_review("batch-x", matches, review_one=flaky,
                                         reviewed_at="2026-10-20T10:00:00+08:00")
            restarted = build_service(JsonlEventStore(path))
            progress = batch_review_progress(restarted.store, "batch-x", matches)
            self.assertEqual(progress["next_unprocessed"], MATCH_C)
            resumed = restarted.run_batch_review("batch-x", matches, reviewed_at="2026-10-21T10:00:00+08:00")
            self.assertEqual(resumed, [MATCH_C])


if __name__ == "__main__":
    unittest.main()
