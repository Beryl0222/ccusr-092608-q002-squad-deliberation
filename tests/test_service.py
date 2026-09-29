import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squad_deliberation.errors import (
    AlreadyLocked,
    AuthorizationError,
    ConcurrentModification,
    DeliberationBlocked,
    DomainError,
    DuplicateConflict,
)
from squad_deliberation.projections import athlete_fact_chain, competition_decisions
from squad_deliberation.service import DeliberationService
from squad_deliberation.store import EventStore

CUTOFF = "2026-09-26T12:00:00+08:00"
NOW = datetime.fromisoformat("2026-09-28T08:00:00+08:00")
COMP = "cup-2026-final"


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        self.store = EventStore(schema=schema)
        self.service = DeliberationService(
            self.store, clock=lambda: NOW
        )

    def give_eligibility(self, athlete: str, eid: str, *, start="2026-01-01T00:00:00+08:00", end="2026-12-31T00:00:00+08:00"):
        return self.service.record_evidence(
            eid,
            evidence_kind="eligibility",
            subject_ref=athlete,
            observed_at="2026-01-01T00:00:00+08:00",
            source_role="admin",
            content={"eligible_from": start, "eligible_until": end},
        )

    def full_roster(self):
        return [
            {"slot": 1, "athlete_ref": "a1", "status": "starter"},
            {"slot": 2, "athlete_ref": "a2", "status": "starter"},
            {"slot": 3, "athlete_ref": "a3", "status": "substitute"},
        ]

    def agree(self, eid: str, proposal_id: str, reviewer="zhao"):
        return self.service.sign_opinion(
            eid,
            proposal_id=proposal_id,
            reviewer_role="final_reviewer",
            reviewer_ref=reviewer,
            position="agree",
            reason="证据链完整",
            occurred_at="2026-09-26T11:00:00+08:00",
        )


class MedicalRulesTests(ServiceTestBase):
    def test_coach_cannot_impose_or_lift_medical_restriction(self):
        with self.assertRaises(AuthorizationError):
            self.service.record_evidence(
                "ev-med-1",
                evidence_kind="medical_restriction",
                subject_ref="a1",
                observed_at="2026-09-25T09:00:00+08:00",
                source_role="coach",
                content={"note": "教练自称有伤"},
            )
        self.service.record_evidence(
            "ev-med-1",
            evidence_kind="medical_restriction",
            subject_ref="a1",
            observed_at="2026-09-25T09:00:00+08:00",
            source_role="medical",
            source_ref="dr-chen",
            content={"note": "脚踝扭伤，限出场"},
        )
        with self.assertRaises(AuthorizationError):
            self.service.record_evidence(
                "ev-med-2",
                evidence_kind="medical_clearance",
                subject_ref="a1",
                observed_at="2026-09-26T09:00:00+08:00",
                source_role="coach",
                restriction_ref="ev-med-1",
            )

    def test_clearance_must_reference_matching_restriction(self):
        with self.assertRaises(AuthorizationError):
            self.service.record_evidence(
                "ev-clear",
                evidence_kind="medical_clearance",
                subject_ref="a1",
                observed_at="2026-09-26T09:00:00+08:00",
                source_role="medical",
            )
        with self.assertRaises(DomainError):
            self.service.record_evidence(
                "ev-clear",
                evidence_kind="medical_clearance",
                subject_ref="a1",
                observed_at="2026-09-26T09:00:00+08:00",
                source_role="medical",
                restriction_ref="missing-restriction",
            )

    def test_unresolved_restriction_blocks_lock(self):
        for athlete in ("a1", "a2", "a3"):
            self.give_eligibility(athlete, f"elig-{athlete}")
        self.service.record_evidence(
            "ev-med-x",
            evidence_kind="medical_restriction",
            subject_ref="a3",
            observed_at="2026-09-25T09:00:00+08:00",
            source_role="medical",
            content={"note": "未恢复"},
        )
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=self.full_roster(),
            proposer_role="coach", proposer_ref="li",
        )
        self.agree("o1", proposal["event_id"])
        with self.assertRaises(DeliberationBlocked) as ctx:
            self.service.confirm_lineup(
                "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按证据",
            )
        self.assertTrue(any("医疗限制" in r and "ev-med-x" in r for r in ctx.exception.reasons))


class ProposalAndOpinionTests(ServiceTestBase):
    def test_parallel_conflicting_proposals_are_both_kept(self):
        roster_a = [
            {"slot": 1, "athlete_ref": "a1", "status": "starter"},
            {"slot": 2, "athlete_ref": "a2", "status": "substitute"},
        ]
        roster_b = [
            {"slot": 1, "athlete_ref": "a2", "status": "starter"},
            {"slot": 2, "athlete_ref": "a4", "status": "substitute"},
        ]
        self.service.submit_proposal(
            "p-a", competition_ref=COMP, roster=roster_a,
            proposer_role="coach", proposer_ref="li", expected_version=0,
        )
        # 第二位教练基于旧版本并行提交：版本冲突被显式检出。
        with self.assertRaises(ConcurrentModification):
            self.service.submit_proposal(
                "p-b", competition_ref=COMP, roster=roster_b,
                proposer_role="coach", proposer_ref="wang", expected_version=0,
            )
        # 在新版本上重提后，两份互相冲突的方案都保留。
        self.service.submit_proposal(
            "p-b", competition_ref=COMP, roster=roster_b,
            proposer_role="coach", proposer_ref="wang", expected_version=1,
        )
        events = self.store.events_for("lineup_proposal", f"lineup:{COMP}")
        self.assertEqual(
            ["p-a", "p-b"],
            [e["event_id"] for e in events if e["event_type"] == "PROPOSAL_SUBMITTED"],
        )

    def test_roster_shape_is_validated(self):
        with self.assertRaises(DomainError):
            self.service.submit_proposal(
                "bad1", competition_ref=COMP,
                roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"},
                        {"slot": 1, "athlete_ref": "a2", "status": "substitute"}],
                proposer_role="coach", proposer_ref="li",
            )
        with self.assertRaises(DomainError):
            self.service.submit_proposal(
                "bad2", competition_ref=COMP,
                roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"},
                        {"slot": 1, "athlete_ref": "a1", "status": "substitute"}],
                proposer_role="coach", proposer_ref="li",
            )

    def test_proposer_cannot_review_own_plan_and_minority_is_kept(self):
        for athlete in ("a1", "a2"):
            self.give_eligibility(athlete, f"elig-{athlete}")
        roster = [
            {"slot": 1, "athlete_ref": "a1", "status": "starter"},
            {"slot": 2, "athlete_ref": "a2", "status": "substitute"},
        ]
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=roster,
            proposer_role="coach", proposer_ref="li",
        )
        with self.assertRaises(AuthorizationError):
            self.service.sign_opinion(
                "o-self", proposal_id=proposal["event_id"],
                reviewer_role="final_reviewer", reviewer_ref="li", position="agree",
            )
        # 少数反对意见同样允许登记。
        self.service.sign_opinion(
            "o-oppose", proposal_id=proposal["event_id"],
            reviewer_role="coach", reviewer_ref="wang", position="oppose",
            reason="对 a2 近期样本有保留",
        )
        # 教练角色不能终审；缺独立赞成本身也阻断。
        with self.assertRaises(DeliberationBlocked) as ctx:
            self.service.confirm_lineup(
                "lock1", competition_ref=COMP, reviewer_role="coach",
                reviewer_ref="wang", evidence_cutoff=CUTOFF, decision_reason="试",
            )
        reasons = ctx.exception.reasons
        self.assertTrue(any("final_reviewer" in r for r in reasons))
        self.assertTrue(any("赞成意见" in r for r in reasons))


class LockAndReconsiderationTests(ServiceTestBase):
    def _lockable_world(self):
        for athlete in ("a1", "a2", "a3"):
            self.give_eligibility(athlete, f"elig-{athlete}")
        # a3 先有限制，赛前由医疗角色解除。
        self.service.record_evidence(
            "med-a3", evidence_kind="medical_restriction", subject_ref="a3",
            observed_at="2026-09-24T09:00:00+08:00", source_role="medical",
            content={"note": "肌肉紧张"},
        )
        self.service.record_evidence(
            "med-a3-clear", evidence_kind="medical_clearance", subject_ref="a3",
            observed_at="2026-09-26T09:00:00+08:00", source_role="medical",
            restriction_ref="med-a3", content={"note": "复查通过"},
        )
        # a1/a2 之间的回避关系已解决。
        self.service.record_evidence(
            "recusal-1", evidence_kind="matchup_recusal", subject_ref="a1|a2",
            observed_at="2026-09-20T09:00:00+08:00", source_role="admin",
            content={"parties": ["a1", "a2"], "resolved": True},
        )
        # 低置信度证据：不确定性被接受并继续保留。
        self.service.record_evidence(
            "obs-a1", evidence_kind="match_sample", subject_ref="a1",
            observed_at="2026-09-25T20:00:00+08:00", source_role="analyst",
            confidence="low", content={"note": "对左手对手样本仅 2 场"},
        )

    def test_lock_is_one_shot_and_repeat_uses_same_receipt(self):
        self._lockable_world()
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=self.full_roster(),
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree("o1", proposal["event_id"])
        receipt1 = self.service.confirm_lineup(
            "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF,
            decision_reason="证据齐备，a3 已解禁，采纳 wang 的保留意见但维持方案",
        )
        events_after = len(self.store.all_events())
        receipt2 = self.service.confirm_lineup(
            "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="再次确认",
        )
        self.assertEqual(receipt1["receipt"], receipt2["receipt"])
        self.assertEqual(receipt1["roster"], receipt2["roster"])
        self.assertEqual(events_after, len(self.store.all_events()))

    def test_content_drift_forces_reconsideration(self):
        self._lockable_world()
        v1 = self.service.submit_proposal(
            "p1", competition_ref=COMP,
            roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"},
                    {"slot": 2, "athlete_ref": "a2", "status": "substitute"}],
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T09:00:00+08:00",
        )
        self.agree("o1", v1["event_id"])
        # 内容漂移：同一聚合上的修订版本，哈希改变。
        v2 = self.service.submit_proposal(
            "p2", competition_ref=COMP,
            roster=[{"slot": 1, "athlete_ref": "a2", "status": "starter"},
                    {"slot": 2, "athlete_ref": "a1", "status": "substitute"}],
            proposer_role="coach", proposer_ref="li", expected_version=2,
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.assertNotEqual(
            v1["payload"]["proposal_hash"], v2["payload"]["proposal_hash"]
        )
        with self.assertRaises(DeliberationBlocked) as ctx:
            self.service.confirm_lineup(
                "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按新版",
            )
        self.assertTrue(any("复议" in r for r in ctx.exception.reasons))
        # 就新内容重新签署后才能锁定。
        self.agree("o2", v2["event_id"])
        receipt = self.service.confirm_lineup(
            "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="复议通过",
        )
        self.assertEqual("p2", receipt["proposal_id"])

    def test_unresolved_recusal_blocks_lock(self):
        self._lockable_world()
        self.service.record_evidence(
            "recusal-live", evidence_kind="matchup_recusal", subject_ref="a2|a3",
            observed_at="2026-09-26T08:00:00+08:00", source_role="admin",
            content={"parties": ["a2", "a3"], "resolved": False},
        )
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=self.full_roster(),
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree("o1", proposal["event_id"])
        with self.assertRaises(DeliberationBlocked) as ctx:
            self.service.confirm_lineup(
                "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="试",
            )
        self.assertTrue(any("回避关系" in r for r in ctx.exception.reasons))

    def test_post_cutoff_evidence_cannot_be_used(self):
        self._lockable_world()
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=self.full_roster(),
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree("o1", proposal["event_id"])
        self.service.confirm_lineup(
            "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按当日证据",
        )
        # 截止线之后的“神证据”不能解锁任何新判断，也不能改方案。
        self.service.record_evidence(
            "future-1", evidence_kind="match_sample", subject_ref="a1",
            observed_at="2026-09-27T20:00:00+08:00", source_role="analyst",
            confidence="high", content={"note": "赛后才拿到的样本"},
        )
        with self.assertRaises(AlreadyLocked):
            self.service.submit_proposal(
                "p2", competition_ref=COMP, roster=self.full_roster(),
                proposer_role="coach", proposer_ref="wang",
            )
        chain = athlete_fact_chain(self.store.all_events(), "a1", COMP, now=NOW)
        ids = {item["evidence_id"] for item in chain["chain"]}
        self.assertNotIn("future-1", ids)
        self.assertIn("obs-a1", ids)
        new_ids = {e["evidence_id"] for e in self.service.new_evidence_since_lock(COMP)}
        self.assertEqual({"future-1"}, new_ids)

    def test_expired_eligibility_blocks_lock(self):
        self.give_eligibility("a1", "elig-a1", end="2026-09-01T00:00:00+08:00")
        self.give_eligibility("a2", "elig-a2")
        roster = [
            {"slot": 1, "athlete_ref": "a1", "status": "starter"},
            {"slot": 2, "athlete_ref": "a2", "status": "substitute"},
        ]
        proposal = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=roster,
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree("o1", proposal["event_id"])
        with self.assertRaises(DeliberationBlocked) as ctx:
            self.service.confirm_lineup(
                "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="试",
            )
        self.assertTrue(any("资格期" in r and "a1" in r for r in ctx.exception.reasons))


class FactChainTests(ServiceTestBase):
    def test_chain_shows_decision_uncertainty_and_commitments(self):
        for athlete in ("a1", "a2", "a3", "a4"):
            self.give_eligibility(athlete, f"elig-{athlete}")
        self.service.record_evidence(
            "obs-a1-low", evidence_kind="match_sample", subject_ref="a1",
            observed_at="2026-09-25T20:00:00+08:00", source_role="analyst",
            confidence="low", content={"note": "样本只有两场"},
        )
        # a2 的承诺：按期兑现。
        self.service.record_evidence(
            "commit-a2", evidence_kind="training_commitment", subject_ref="a2",
            observed_at="2026-09-10T09:00:00+08:00", source_role="coach",
            content={"description": "补足发接发训练", "due_at": "2026-09-20T09:00:00+08:00"},
        )
        self.service.record_evidence(
            "outcome-a2", evidence_kind="commitment_outcome", subject_ref="a2",
            observed_at="2026-09-19T09:00:00+08:00", source_role="coach",
            restriction_ref="commit-a2", content={"status": "fulfilled"},
        )
        # a3 的承诺：已逾期且无结果。
        self.service.record_evidence(
            "commit-a3", evidence_kind="training_commitment", subject_ref="a3",
            observed_at="2026-09-05T09:00:00+08:00", source_role="coach",
            content={"description": "减重两公斤", "due_at": "2026-09-20T09:00:00+08:00"},
        )
        # a4 的承诺：爽约。
        self.service.record_evidence(
            "commit-a4", evidence_kind="training_commitment", subject_ref="a4",
            observed_at="2026-09-05T09:00:00+08:00", source_role="coach",
            content={"description": "力量复测", "due_at": "2026-09-15T09:00:00+08:00"},
        )
        self.service.record_evidence(
            "outcome-a4", evidence_kind="commitment_outcome", subject_ref="a4",
            observed_at="2026-09-16T09:00:00+08:00", source_role="coach",
            restriction_ref="commit-a4", content={"status": "missed"},
        )

        main = self.service.submit_proposal(
            "p1", competition_ref=COMP, roster=self.full_roster(),
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.service.sign_opinion(
            "o-oppose", proposal_id=main["event_id"],
            reviewer_role="coach", reviewer_ref="wang", position="oppose",
            reason="不认可 a1 首发，经验不足",
            occurred_at="2026-09-26T10:30:00+08:00",
        )
        self.agree("o1", main["event_id"])
        self.service.confirm_lineup(
            "lock1", competition_ref=COMP, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF,
            decision_reason="a1 样本不足的不确定性被接受并记录，继续培养",
        )

        c1 = athlete_fact_chain(self.store.all_events(), "a1", COMP, now=NOW)
        self.assertEqual("starter", c1["decision"])
        self.assertEqual(1, c1["slot"])
        self.assertEqual("locked_snapshot", c1["basis"])
        self.assertEqual(
            ["obs-a1-low"], [u["evidence_id"] for u in c1["accepted_uncertainties"]]
        )
        self.assertEqual(
            [{"opinion_id": "o-oppose", "proposal_id": "p1",
              "reviewer_ref": "wang", "reason": "不认可 a1 首发，经验不足"}],
            c1["minority_opinions"],
        )

        c2 = athlete_fact_chain(self.store.all_events(), "a2", COMP, now=NOW)
        self.assertEqual("starter", c2["decision"])
        self.assertEqual("fulfilled_on_time", c2["commitments"][0]["status"])

        c3 = athlete_fact_chain(self.store.all_events(), "a3", COMP, now=NOW)
        self.assertEqual("substitute", c3["decision"])
        self.assertEqual("overdue", c3["commitments"][0]["status"])

        c4 = athlete_fact_chain(self.store.all_events(), "a4", COMP, now=NOW)
        self.assertEqual("withheld", c4["decision"])
        self.assertEqual("breached", c4["commitments"][0]["status"])

        view = competition_decisions(self.store.all_events(), COMP, now=NOW)
        self.assertTrue(view["locked"])
        self.assertEqual(["a1", "a2", "a3"], [a["athlete_ref"] for a in view["athletes"]])
        self.assertTrue(all("score" not in a for a in view["athletes"]))


class StoreIdempotencyTests(ServiceTestBase):
    def test_same_event_id_same_content_is_idempotent(self):
        kwargs = dict(
            evidence_kind="coach_observation", subject_ref="a1",
            observed_at="2026-09-25T09:00:00+08:00", source_role="coach",
            content={"note": "状态好"},
        )
        first = self.service.record_evidence("ev-x", **kwargs)
        second = self.service.record_evidence("ev-x", **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.store.all_events()))

    def test_same_event_id_different_content_conflicts(self):
        self.service.record_evidence(
            "ev-x", evidence_kind="coach_observation", subject_ref="a1",
            observed_at="2026-09-25T09:00:00+08:00", source_role="coach",
            content={"note": "状态好"},
        )
        with self.assertRaises(DuplicateConflict):
            self.service.record_evidence(
                "ev-x", evidence_kind="coach_observation", subject_ref="a1",
                observed_at="2026-09-25T09:00:00+08:00", source_role="coach",
                content={"note": "状态差，内容被篡改"},
            )


class BatchReviewTests(ServiceTestBase):
    def _lock_minimal(self, comp: str, lock_id: str):
        self.give_eligibility("a1", f"elig-a1-for-{comp}")
        proposal = self.service.submit_proposal(
            f"p-{comp}", competition_ref=comp,
            roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"}],
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree(f"o-{comp}", proposal["event_id"])
        self.service.confirm_lineup(
            lock_id, competition_ref=comp, reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按证据",
        )

    def test_batch_resumes_from_first_unprocessed_competition(self):
        comps = ["m1", "m2", "m3"]
        for comp in comps:
            self._lock_minimal(comp, f"lock-{comp}")
        jobs = [
            {"event_id": f"review-{c}", "competition_ref": c, "outcome": {"result": "loss"}}
            for c in comps
        ]
        # 模拟中断：先只处理第一场。
        first = self.service.batch_review(jobs[:1])
        self.assertEqual({"processed": ["m1"], "skipped": [], "failed": []}, first)
        # 重新跑整批：m1 跳过，从 m2 继续，不重复处理 m1。
        resumed = self.service.batch_review(jobs)
        self.assertEqual({"processed": ["m2", "m3"], "skipped": ["m1"], "failed": []}, resumed)
        # 再来一次：全部跳过，复盘事件不重复。
        again = self.service.batch_review(jobs)
        self.assertEqual({"processed": [], "skipped": ["m1", "m2", "m3"], "failed": []}, again)
        review_events = [e for e in self.store.all_events() if e["event_type"] == "REVIEW_COMPLETED"]
        self.assertEqual(["review-m1", "review-m2", "review-m3"], [e["event_id"] for e in review_events])

    def test_review_requires_lock(self):
        with self.assertRaises(DomainError):
            self.service.complete_review(
                "review-x", competition_ref="unlocked-m", outcome={"result": "win"},
            )


class PersistenceAndFutureRecomputeTests(ServiceTestBase):
    def test_store_rebuilds_from_jsonl_and_batch_resumes_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "events.jsonl"
            store1 = EventStore(db, schema=json.loads(
                (ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8")))
            service1 = DeliberationService(store1, clock=lambda: NOW)
            service1.record_evidence(
                "elig-a1", evidence_kind="eligibility", subject_ref="a1",
                observed_at="2026-01-01T00:00:00+08:00", source_role="admin",
                content={"eligible_from": "2026-01-01T00:00:00+08:00",
                         "eligible_until": "2026-12-31T00:00:00+08:00"},
            )
            proposal = service1.submit_proposal(
                "p-m1", competition_ref="m1",
                roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"}],
                proposer_role="coach", proposer_ref="li",
                occurred_at="2026-09-26T10:00:00+08:00",
            )
            service1.sign_opinion(
                "o-m1", proposal_id=proposal["event_id"],
                reviewer_role="final_reviewer", reviewer_ref="zhao", position="agree",
                occurred_at="2026-09-26T11:00:00+08:00",
            )
            service1.confirm_lineup(
                "lock-m1", competition_ref="m1", reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按证据",
            )
            service1.complete_review(
                "review-m1", competition_ref="m1", outcome={"result": "loss"},
                occurred_at="2026-09-27T12:00:00+08:00",
            )

            # 新进程：从 JSONL 重建，重复确认沿用原回执。
            store2 = EventStore(db, schema=json.loads(
                (ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8")))
            service2 = DeliberationService(store2, clock=lambda: NOW)
            receipt = service2.confirm_lineup(
                "lock-m1", competition_ref="m1", reviewer_role="final_reviewer",
                reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="重放",
            )
            result = service2.batch_review([
                {"event_id": "review-m1", "competition_ref": "m1", "outcome": {"result": "loss"}},
            ])
            self.assertEqual({"processed": [], "skipped": ["m1"], "failed": []}, result)
            self.assertIn("roster", receipt)

    def test_post_match_data_can_drive_future_competition_only(self):
        for athlete in ("a1",):
            self.give_eligibility(athlete, "elig-a1")
        proposal = self.service.submit_proposal(
            "p-m1", competition_ref="m1",
            roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"}],
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-26T10:00:00+08:00",
        )
        self.agree("o-m1", proposal["event_id"])
        self.service.confirm_lineup(
            "lock-m1", competition_ref="m1", reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=CUTOFF, decision_reason="按当日证据",
        )
        self.service.complete_review(
            "review-m1", competition_ref="m1", outcome={"result": "loss"},
            occurred_at="2026-09-27T12:00:00+08:00",
        )
        # 赛后反馈：不回写 m1 快照，但对下一场 m2 是可见证据。
        self.service.record_evidence(
            "feedback-1", evidence_kind="post_match_feedback", subject_ref="a1",
            observed_at="2026-09-27T18:00:00+08:00", source_role="analyst",
            competition_ref="m1", content={"note": "关键分出手犹豫"},
        )
        chain_m1 = athlete_fact_chain(self.store.all_events(), "a1", "m1", now=NOW)
        self.assertNotIn("feedback-1", {c["evidence_id"] for c in chain_m1["chain"]})

        # m2 的截止线在反馈之后，反馈进入 m2 的事实链。
        m2_cutoff = "2026-09-29T12:00:00+08:00"
        proposal2 = self.service.submit_proposal(
            "p-m2", competition_ref="m2",
            roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"}],
            proposer_role="coach", proposer_ref="li",
            occurred_at="2026-09-29T09:00:00+08:00",
        )
        self.service.sign_opinion(
            "o-m2", proposal_id=proposal2["event_id"],
            reviewer_role="final_reviewer", reviewer_ref="zhao", position="agree",
            occurred_at="2026-09-29T10:00:00+08:00",
        )
        self.service.confirm_lineup(
            "lock-m2", competition_ref="m2", reviewer_role="final_reviewer",
            reviewer_ref="zhao", evidence_cutoff=m2_cutoff, decision_reason="参考上场反馈",
            occurred_at="2026-09-29T11:30:00+08:00",
        )
        chain_m2 = athlete_fact_chain(self.store.all_events(), "a1", "m2", now=NOW)
        self.assertIn("feedback-1", {c["evidence_id"] for c in chain_m2["chain"]})


if __name__ == "__main__":
    unittest.main()
