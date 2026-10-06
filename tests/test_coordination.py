from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autism_care_coordination.service import (
    AccessDenied,
    CareCoordinationService,
    PlanNotEffective,
    ProviderNotAuthorized,
    ReviewerNotIndependent,
    ServiceClaimConflict,
)
from autism_care_coordination.store import EventStore, parse_time

CASE = "case-001"


def make_service() -> CareCoordinationService:
    svc = CareCoordinationService(":memory:")
    svc.grant_staff_access("sup-1", CASE, "supervisor")
    svc.grant_staff_access("therapist-a", CASE, "provider", provider_id="org-A")
    svc.grant_staff_access("therapist-b", CASE, "provider", provider_id="org-B")
    return svc


def seed_plan(svc: CareCoordinationService, *, at: str = "2026-09-01T09:00:00+08:00") -> str:
    svc.accept_assessment(
        CASE, "asmt-1", "2026-08-30T10:00:00+08:00",
        instrument="PEP-3", instrument_version="C/2024",
        scores={"communication": 42, "daily_living": 35},
    )
    svc.approve_plan(
        CASE, "plan-1", at,
        assessment_event_id="asmt-1",
        goals=[
            {"goal_id": "g1", "wording": "主动提出需求 5 次/日", "target": "5 次/日"},
            {"goal_id": "g2", "wording": "共同注意 3 分钟", "target": "3 分钟"},
        ],
        methods=[
            {"name": "PRT", "evidence": "NCAEP 2020", "version": "manual-v2"},
            {"name": "视觉支持", "evidence": "NPDC 2024", "version": "v1"},
        ],
        frequency={"org-A": "每周 3 次", "home": "每日 2 个回合"},
        guardian_consent=True,
        family_priorities=["先解决就餐时的情绪崩溃"],
        family_private={"income_note": "敏感家庭资料"},
    )
    return "plan-1"


class TimelineAndPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_events_ordered_by_occurred_time_not_arrival(self) -> None:
        seed_plan(self.svc)
        # 晚发生的先补录，早发生的后补录，时间线仍按发生时间排列。
        self.svc.record_observation(
            CASE, "obs-late", "2026-09-10T09:00:00+08:00",
            goal_id="g1", observer_type="family", observer_id="parent", progress_value=3,
        )
        self.svc.record_observation(
            CASE, "obs-early", "2026-09-03T09:00:00+08:00",
            goal_id="g1", observer_type="family", observer_id="parent", progress_value=1,
        )
        ids = [e["event_id"] for e in self.svc.timeline(CASE)]
        self.assertLess(ids.index("obs-early"), ids.index("obs-late"))

    def test_service_must_point_to_plan_effective_at_occurrence(self) -> None:
        seed_plan(self.svc)
        self.svc.claim_service(
            CASE, "claim-A", "2026-09-02T08:00:00+08:00",
            slot_key="aba-mon-wed-fri", provider_id="org-A", responsibility="个训",
        )
        self.svc.record_service(
            CASE, "svc-1", "2026-09-02T10:00:00+08:00",
            slot_key="aba-mon-wed-fri", provider_id="org-A", plan_version=1,
            summary="PRT 回合 12 个", goal_ids=["g1"],
        )
        with self.assertRaises(PlanNotEffective):
            self.svc.record_service(
                CASE, "svc-bad", "2026-09-02T11:00:00+08:00",
                slot_key="aba-mon-wed-fri", provider_id="org-A", plan_version=9,
                summary="指向了不存在的版本",
            )

    def test_service_before_any_plan_rejected(self) -> None:
        self.svc.accept_assessment(
            CASE, "asmt-x", "2026-08-30T10:00:00+08:00",
            instrument="PEP-3", instrument_version="C/2024", scores={},
        )
        self.svc.claim_service(
            CASE, "claim-x", "2026-08-31T08:00:00+08:00",
            slot_key="s1", provider_id="org-A", responsibility="评估陪同",
        )
        with self.assertRaises(PlanNotEffective):
            self.svc.record_service(
                CASE, "svc-x", "2026-08-31T09:00:00+08:00",
                slot_key="s1", provider_id="org-A", plan_version=1, summary="无计划",
            )


class IdempotencyAndDisputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def _observation(self, note: str):
        return {
            "case_id": CASE, "event_id": "obs-dup",
            "occurred_at": "2026-09-04T09:00:00+08:00",
            "goal_id": "g1", "observer_type": "family",
            "observer_id": "parent", "progress_value": 2, "note": note,
        }

    def test_duplicate_upload_returns_original_receipt(self) -> None:
        kwargs = self._observation("孩子配合")
        r1 = self.svc.record_observation(**kwargs)
        r2 = self.svc.record_observation(**kwargs)
        self.assertEqual(r1.status, "accepted")
        self.assertEqual(r2.status, "accepted_duplicate")
        self.assertTrue(r2.duplicate)
        self.assertEqual(r1.seq, r2.seq)
        obs_events = [e for e in self.svc.timeline(CASE) if e["event_id"] == "obs-dup"]
        self.assertEqual(1, len(obs_events))

    def test_same_id_different_content_keeps_dispute_without_overwrite(self) -> None:
        a = self._observation("家长记录：配合")
        b = self._observation("家长记录：完全不配合")  # 同标识，内容被改
        r1 = self.svc.record_observation(**a)
        r2 = self.svc.record_observation(**b)
        self.assertEqual("conflict_kept", r2.status)
        self.assertIsNotNone(r2.dispute_id)

        timeline = self.svc.timeline(CASE)
        kept = [e for e in timeline if e["event_id"] == "obs-dup"]
        self.assertEqual(1, len(kept))
        self.assertEqual("家长记录：配合", kept[0]["payload"]["note"])  # 原文未被覆盖

        disputes = self.svc.disputes(CASE)
        self.assertEqual(1, len(disputes))
        self.assertEqual(r2.seq, disputes[0]["conflicting_seq"])

        # 冲突内容再次上报，争议登记本身也是幂等的。
        r3 = self.svc.record_observation(**b)
        self.assertEqual("conflict_kept", r3.status)
        self.assertEqual(1, len(self.svc.disputes(CASE)))


class ClaimConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def test_concurrent_claim_only_one_winner(self) -> None:
        self.svc.claim_service(
            CASE, "claim-A", "2026-09-02T08:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", responsibility="个训",
        )
        with self.assertRaises(ServiceClaimConflict) as ctx:
            self.svc.claim_service(
                CASE, "claim-B", "2026-09-02T08:01:00+08:00",
                slot_key="slot-1", provider_id="org-B", responsibility="个训",
            )
        self.assertEqual("org-A", ctx.exception.winner)

    def test_only_claiming_provider_may_sign(self) -> None:
        self.svc.claim_service(
            CASE, "claim-A", "2026-09-02T08:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", responsibility="个训",
        )
        with self.assertRaises(ProviderNotAuthorized):
            self.svc.record_service(
                CASE, "svc-forge", "2026-09-02T10:00:00+08:00",
                slot_key="slot-1", provider_id="org-B", plan_version=1, summary="冒签",
            )
        with self.assertRaises(ProviderNotAuthorized):
            self.svc.record_service(
                CASE, "svc-nobody", "2026-09-02T10:00:00+08:00",
                slot_key="slot-unknown", provider_id="org-A", plan_version=1, summary="未认领",
            )

    def test_true_cross_connection_concurrent_claim_has_single_winner(self) -> None:
        import threading

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "concurrent.db")

        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(provider: str, event_id: str) -> None:
            store = EventStore(db)
            svc = CareCoordinationService(store)
            barrier.wait()
            try:
                svc.claim_service(
                    CASE, event_id, "2026-09-02T08:00:00+08:00",
                    slot_key="race-slot", provider_id=provider, responsibility="个训",
                )
                results.append(f"{provider}:won")
            except ServiceClaimConflict as exc:
                results.append(f"{provider}:lost:{exc.winner}")
            finally:
                store.close()

        threads = [
            threading.Thread(target=worker, args=("org-A", "claim-race-a")),
            threading.Thread(target=worker, args=("org-B", "claim-race-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [r for r in results if r.endswith(":won")]
        self.assertEqual(1, len(wins), results)
        winner = wins[0].split(":")[0]
        loser = [r for r in results if ":lost:" in r][0]
        self.assertIn(winner, loser)
        # 持久库中槽位确实只有一条归属。
        check = EventStore(db)
        self.assertEqual(winner, check.slot_claim(CASE, "race-slot")["claimed_by"])
        check.close()


class ObservationCoexistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def test_family_and_professional_observations_coexist(self) -> None:
        self.svc.record_observation(
            CASE, "obs-p", "2026-09-04T15:00:00+08:00",
            goal_id="g1", observer_type="professional", observer_id="therapist-a",
            staff_id="therapist-a", progress_value=4, note="中心评估",
        )
        self.svc.record_observation(
            CASE, "obs-f", "2026-09-04T20:00:00+08:00",
            goal_id="g1", observer_type="family", observer_id="parent", progress_value=2,
            note="家中难以泛化",
        )
        obs = [
            e for e in self.svc.timeline(CASE) if e["event_type"] == "OBSERVATION_RECORDED"
        ]
        self.assertEqual(2, len(obs))
        self.assertEqual({"professional", "family"}, {o["payload"]["observer_type"] for o in obs})
        # 同一目标的观察构成一条追加流，版本各自依次递增，两类记录并存不覆盖。
        self.assertEqual([1, 2], [o["version"] for o in obs])

    def test_professional_observation_requires_case_access(self) -> None:
        with self.assertRaises(AccessDenied):
            self.svc.record_observation(
                CASE, "obs-x", "2026-09-04T15:00:00+08:00",
                goal_id="g1", observer_type="professional", observer_id="outsider",
                staff_id="staff-no-access", progress_value=4,
            )


class AssessmentChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def test_significant_change_flags_only_related_goals_plan_stays_effective(self) -> None:
        self.svc.accept_assessment(
            CASE, "asmt-2", "2026-10-01T10:00:00+08:00",
            instrument="PEP-3", instrument_version="C/2024",
            scores={"communication": 58, "daily_living": 36},
            significant_change=True, prior_assessment_event_id="asmt-1",
            affected_goal_ids=["g1"],
        )
        self.assertEqual({"g1"}, self.svc._flagged_goals(CASE))

        flag = next(
            e for e in self.svc.timeline(CASE)
            if e["event_type"] == "GOAL_RECONSIDERATION_FLAGGED"
        )
        self.assertEqual(["g1"], flag["payload"]["goal_ids"])
        self.assertEqual("asmt-2", flag["payload"]["trigger_event_id"])

        # 计划仍然有效：后续服务可继续签认，未被整体作废。
        self.svc.claim_service(
            CASE, "claim-A2", "2026-10-02T08:00:00+08:00",
            slot_key="slot-2", provider_id="org-A", responsibility="个训",
        )
        receipt = self.svc.record_service(
            CASE, "svc-continue", "2026-10-02T10:00:00+08:00",
            slot_key="slot-2", provider_id="org-A", plan_version=1,
            summary="计划继续执行，g1 同步复审", goal_ids=["g1", "g2"],
        )
        self.assertEqual("accepted", receipt.status)

        family = self.svc.family_view(CASE)
        statuses = {g["goal_id"]: g["status"] for g in family["goals"]}
        self.assertEqual("under_review", statuses["g1"])
        self.assertEqual("active", statuses["g2"])

    def test_screening_flag_is_never_a_diagnosis(self) -> None:
        r = self.svc.raise_screening_flag(
            CASE, "screen-1", "2026-09-05T10:00:00+08:00",
            instrument="M-CHAT-R/F", threshold=">=3 需转介", observed_score=6,
        )
        self.assertEqual("accepted", r.status)
        event = next(e for e in self.svc.timeline(CASE) if e["event_type"] == "SCREENING_FLAG_RAISED")
        self.assertFalse(event["payload"]["is_diagnosis"])
        self.assertIn("不构成诊断", event["payload"]["recommendation"])


class RiskEmergencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def test_emergency_action_first_then_time_boxed_authorization_and_review(self) -> None:
        at = "2026-09-06T22:00:00+08:00"
        self.svc.take_risk_action(
            CASE, "risk-1", at,
            reason="突发自伤", action_taken="移除周围尖锐物并肢体隔离 2 分钟",
            acted_by="therapist-a", minimal_necessary=True,
            authorization_due_at="2026-09-07T10:00:00+08:00",
            review_due_at="2026-09-09T22:00:00+08:00",
        )
        risk = next(e for e in self.svc.timeline(CASE) if e["event_type"] == "RISK_ACTION_TAKEN")
        self.assertFalse(risk["payload"]["prior_authorization"])
        self.assertEqual(1, risk["payload"]["plan_version"])

        # 授权时限已过但未补齐 -> 逾期可见。
        breaches = self.svc.open_sla_breaches("2026-09-07T10:01:00+08:00")
        kinds = {b["kind"] for b in breaches}
        self.assertIn("risk_authorization", kinds)

        # 限时内补齐授权，对应提醒关闭。
        self.svc.record_risk_authorization(
            CASE, "auth-1", "2026-09-07T09:30:00+08:00",
            risk_event_id="risk-1", guardian_consent=True, consenter_id="parent",
        )
        remaining = {
            b["ref_event_id"] + ":" + b["kind"]
            for b in self.svc.open_sla_breaches("2026-09-08T00:00:00+08:00")
        }
        self.assertNotIn("risk-1:risk_authorization", remaining)

        # 独立复核不能由实施者本人完成。
        with self.assertRaises(ReviewerNotIndependent):
            self.svc.record_independent_review(
                CASE, "rev-bad", "2026-09-08T10:00:00+08:00",
                risk_event_id="risk-1", reviewer_id="therapist-a",
                finding="自查", action_appropriate=True,
            )
        self.svc.record_independent_review(
            CASE, "rev-1", "2026-09-08T10:00:00+08:00",
            risk_event_id="risk-1", reviewer_id="sup-1",
            finding="处置符合最小必要原则", action_appropriate=True,
        )
        self.assertEqual(
            [], self.svc.open_sla_breaches("2026-09-09T00:00:00+08:00")
        )

    def test_action_below_minimal_necessary_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.svc.take_risk_action(
                CASE, "risk-x", "2026-09-06T22:00:00+08:00",
                reason="哭闹", action_taken="约束 30 分钟",
                acted_by="therapist-a", minimal_necessary=False,
            )

    def test_prior_authorization_creates_no_make_up_reminder(self) -> None:
        self.svc.take_risk_action(
            CASE, "risk-pre", "2026-09-06T22:00:00+08:00",
            reason="预先约定的居家隔离方案触发", action_taken="按预案执行",
            acted_by="therapist-a", prior_authorization=True,
            review_due_at="2026-09-09T22:00:00+08:00",
        )
        rows = self.svc.store.overdue_open("2026-09-20T00:00:00+08:00")
        self.assertNotIn("risk_authorization", {r["kind"] for r in rows})
        self.assertIn("risk_independent_review", {r["kind"] for r in rows})


class PlanVersionTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)

    def _approve_v2(self) -> None:
        self.svc.accept_assessment(
            CASE, "asmt-2", "2026-10-01T10:00:00+08:00",
            instrument="PEP-3", instrument_version="C/2024",
            scores={"communication": 58},
        )
        self.svc.approve_plan(
            CASE, "plan-2", "2026-10-02T09:00:00+08:00",
            assessment_event_id="asmt-2",
            goals=[
                {"goal_id": "g1", "wording": "主动提出需求 8 次/日", "target": "8 次/日"},
                {"goal_id": "g3", "wording": "轮流游戏 2 轮", "target": "2 轮"},
            ],
            methods=[{"name": "PRT", "evidence": "NCAEP 2020", "version": "manual-v3"}],
            frequency={"org-A": "每周 2 次", "home": "每日 1 个回合"},
            guardian_consent=True,
        )

    def test_observation_requires_goal_in_effective_plan_version(self) -> None:
        self._approve_v2()
        # g2 只存在于 v1；v2 生效后针对 g2 的观察必须拒绝，而不是静默挂到旧版。
        with self.assertRaises(PlanNotEffective):
            self.svc.record_observation(
                CASE, "obs-retired", "2026-10-03T09:00:00+08:00",
                goal_id="g2", observer_type="family", observer_id="parent", progress_value=1,
            )
        # 历史时间点的补录仍以当时有效的 v1 为准。
        receipt = self.svc.record_observation(
            CASE, "obs-backfill", "2026-09-20T09:00:00+08:00",
            goal_id="g2", observer_type="family", observer_id="parent", progress_value=1,
        )
        self.assertEqual("accepted", receipt.status)
        event = next(e for e in self.svc.timeline(CASE) if e["event_id"] == "obs-backfill")
        self.assertEqual(1, event["payload"]["plan_version"])

    def test_rephrasing_proposal_must_target_effective_plan_goal(self) -> None:
        self._approve_v2()
        with self.assertRaises(PlanNotEffective):
            self.svc.propose_goal_rephrasing(
                CASE, "rep-old", "2026-10-03T09:00:00+08:00",
                goal_id="g2", wording="某机构改述", source_party="kindergarten",
            )

    def test_old_services_remain_attributed_after_plan_revision(self) -> None:
        self.svc.claim_service(
            CASE, "claim-A", "2026-09-02T08:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", responsibility="个训",
        )
        self.svc.record_service(
            CASE, "svc-v1", "2026-09-05T10:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", plan_version=1,
            summary="v1 下的服务", goal_ids=["g2"],
        )
        self._approve_v2()
        # 新计划生效后，签认必须指向 v2 且只能引用 v2 的目标。
        with self.assertRaises(PlanNotEffective):
            self.svc.record_service(
                CASE, "svc-stale", "2026-10-03T10:00:00+08:00",
                slot_key="slot-1", provider_id="org-A", plan_version=1, summary="仍指 v1",
            )
        receipt = self.svc.record_service(
            CASE, "svc-v2", "2026-10-03T10:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", plan_version=2,
            summary="v2 下的服务", goal_ids=["g3"],
        )
        self.assertEqual("accepted", receipt.status)


class ReminderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "care.db")
        self.svc = make_service_persistent(self.db)
        seed_plan(self.svc)

    def tearDown(self) -> None:
        self.svc.store.close()
        self.tmp.cleanup()

    def test_reminders_resume_in_original_time_order_after_outage(self) -> None:
        # 两个风险动作，授权到期时间不同；系统"停机"期间都已过期。
        self.svc.take_risk_action(
            CASE, "risk-early", "2026-09-06T08:00:00+08:00",
            reason="自伤 A", action_taken="最小隔离", acted_by="therapist-a",
            authorization_due_at="2026-09-06T20:00:00+08:00",
            review_due_at="2026-09-09T08:00:00+08:00",
        )
        self.svc.take_risk_action(
            CASE, "risk-late", "2026-09-06T12:00:00+08:00",
            reason="自伤 B", action_taken="移除危险物", acted_by="therapist-b",
            authorization_due_at="2026-09-07T08:00:00+08:00",
            review_due_at="2026-09-09T12:00:00+08:00",
        )

        # 恢复时刻已晚于所有到期点。
        recovered = CareCoordinationService(EventStore(self.db))
        delivered = recovered.pump_reminders("2026-09-10T00:00:00+08:00")
        auth_due = [
            parse_time(d["due_at"]) for d in delivered if d["kind"] == "risk_authorization"
        ]
        self.assertEqual(sorted(auth_due), auth_due)  # 按原始到期时间排序
        self.assertEqual(4, len(delivered))

        # 再次泵送不重复投递。
        again = recovered.pump_reminders("2026-09-11T00:00:00+08:00")
        self.assertEqual([], again)


def make_service_persistent(db_path: str) -> CareCoordinationService:
    svc = CareCoordinationService(db_path)
    svc.grant_staff_access("sup-1", CASE, "supervisor")
    svc.grant_staff_access("therapist-a", CASE, "provider", provider_id="org-A")
    svc.grant_staff_access("therapist-b", CASE, "provider", provider_id="org-B")
    return svc


class AccessAndViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)
        self.svc.claim_service(
            CASE, "claim-A", "2026-09-02T08:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", responsibility="个训",
        )
        self.svc.record_service(
            CASE, "svc-1", "2026-09-02T10:00:00+08:00",
            slot_key="slot-1", provider_id="org-A", plan_version=1,
            summary="PRT 回合", goal_ids=["g1"],
        )
        self.svc.record_observation(
            CASE, "obs-p", "2026-09-03T15:00:00+08:00",
            goal_id="g1", observer_type="professional", observer_id="therapist-a",
            staff_id="therapist-a", progress_value=4, note="中心数据",
        )
        self.svc.record_observation(
            CASE, "obs-f", "2026-09-03T20:00:00+08:00",
            goal_id="g1", observer_type="family", observer_id="parent", progress_value=2,
            note="家中表现差，收入压力大",
        )
        self.svc.raise_screening_flag(
            CASE, "screen-1", "2026-09-05T10:00:00+08:00",
            instrument="M-CHAT-R/F", threshold=">=3", observed_score=6,
        )
        self.svc.issue_referral(
            CASE, "ref-1", "2026-09-05T11:00:00+08:00",
            issue="睡眠问题", target_specialty="儿童精神科", reason="夜间频繁觉醒",
        )

    def test_staff_without_grant_cannot_open_case(self) -> None:
        with self.assertRaises(AccessDenied):
            self.svc.staff_case_view("staff-random", CASE)

    def test_provider_view_is_minimum_necessary(self) -> None:
        view = self.svc.staff_case_view("therapist-a", CASE)
        self.assertEqual("provider", view["role"])
        # 能看到本机构的责任与签认。
        self.assertIn("slot-1", view["my_responsibilities"])
        self.assertEqual(1, len(view["my_commitments"]))
        # 视图中不存在评估分数、筛查、转介与家庭私密资料等任何事件。
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("communication", blob)
        self.assertNotIn("M-CHAT", blob)
        self.assertNotIn("睡眠问题", blob)
        self.assertNotIn("收入压力", blob)
        # org-B 与本案例服务无关：无槽位、无签认、无可服务目标。
        view_b = self.svc.staff_case_view("therapist-b", CASE)
        self.assertEqual([], view_b["my_responsibilities"])
        self.assertEqual([], view_b["goals_served"])

    def test_family_view_shows_progress_and_next_owner(self) -> None:
        self.svc.complete_review(
            CASE, "review-1", "2026-09-15T16:00:00+08:00",
            review_cycle_id="cycle-1",
            goal_decisions={"g1": "continue", "g2": "continue"},
            summary="9 月阶段复盘",
            next_actions=[
                {"action": "下周强化家庭泛化", "owner": "parent", "due_at": "2026-09-22T00:00:00+08:00"},
                {"action": "调整 PRT 辅助等级", "owner": "org-A", "due_at": "2026-09-19T00:00:00+08:00"},
            ],
        )
        view = self.svc.family_view(CASE)
        g1 = next(g for g in view["goals"] if g["goal_id"] == "g1")
        self.assertEqual(2, g1["latest_progress"])  # 最近一次为家庭观察
        owners = {a["owner"] for a in view["next_actions"]}
        self.assertEqual({"parent", "org-A"}, owners)
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("收入", blob)
        self.assertNotIn("M-CHAT", blob)


class ReviewReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_plan(self.svc)
        self.svc.record_observation(
            CASE, "obs-1", "2026-09-03T15:00:00+08:00",
            goal_id="g1", observer_type="professional", observer_id="therapist-a",
            staff_id="therapist-a", progress_value=4,
        )
        self.svc.take_risk_action(
            CASE, "risk-1", "2026-09-06T22:00:00+08:00",
            reason="自伤", action_taken="隔离 2 分钟", acted_by="therapist-a",
            authorization_due_at="2026-09-07T10:00:00+08:00",
            review_due_at="2026-09-09T22:00:00+08:00",
        )
        self.svc.record_risk_authorization(
            CASE, "auth-1", "2026-09-07T09:00:00+08:00",
            risk_event_id="risk-1", guardian_consent=True, consenter_id="parent",
        )
        self.svc.complete_review(
            CASE, "review-1", "2026-09-15T16:00:00+08:00",
            review_cycle_id="cycle-1",
            goal_decisions={"g1": "continue", "g2": "continue"},
            summary="复盘",
        )
        # 复盘之后才发生的观察，不应出现在重放中。
        self.svc.record_observation(
            CASE, "obs-after", "2026-09-20T15:00:00+08:00",
            goal_id="g1", observer_type="family", observer_id="parent", progress_value=5,
        )

    def test_supervisor_replays_materials_underpinning_review(self) -> None:
        replay = self.svc.replay_review("sup-1", CASE, "review-1")
        self.assertEqual(1, replay["effective_plan"]["version"])
        obs_ids = {o["event_id"] for o in replay["observations"]}
        self.assertIn("obs-1", obs_ids)
        self.assertNotIn("obs-after", obs_ids)
        self.assertEqual(["risk-1"], [e["event_id"] for e in replay["risk_actions"]])
        self.assertEqual(["auth-1"], [e["event_id"] for e in replay["risk_authorizations"]])
        # 方法循证版本可随重放追溯。
        methods = replay["effective_plan"]["payload"]["methods"]
        self.assertEqual("NCAEP 2020", methods[0]["evidence"])

    def test_provider_cannot_replay(self) -> None:
        with self.assertRaises(AccessDenied):
            self.svc.replay_review("therapist-a", CASE, "review-1")


if __name__ == "__main__":
    unittest.main()
