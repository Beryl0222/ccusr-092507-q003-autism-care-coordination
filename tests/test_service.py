from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autism_care_coordination.service import (
    AccessDenied,
    CareCoordinationService,
    DomainError,
    ServiceSlotAlreadyClaimed,
)
from autism_care_coordination.timeline import ContentMismatch, Timeline

TZ = "+08:00"
CASE = "case-001"
ASSESSMENT_V1 = "CARS2-CN-2026.0"
ASSESSMENT_V2 = "CARS2-CN-2026.1"

METHODS_V1 = [
    {"method": "自然情景教学", "evidence": "2026版指南推荐", "version": "guide-2026"},
    {"method": "家长执行式干预", "evidence": "2026版指南推荐", "version": "guide-2026"},
]
FREQUENCY_V1 = {"weekly_sessions": 3, "kindergarten_support": "weekly"}
METHODS_V2 = [
    {"method": "关键反应训练", "evidence": "2026版指南推荐", "version": "guide-2026r1"},
]
FREQUENCY_V2 = {"weekly_sessions": 4, "kindergarten_support": "twice_weekly"}


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class CaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.timeline = Timeline()
        self.service = CareCoordinationService(
            self.timeline, clock=dt(f"2026-09-01T09:00:00{TZ}")
        )
        self.service.accept_assessment(
            CASE, "CARS2-CN", ASSESSMENT_V1, {"score": 36.5},
            f"2026-02-01T10:00:00{TZ}", "evt-assess-1",
        )
        self.service.approve_plan(
            CASE, 1, ASSESSMENT_V1, METHODS_V1, FREQUENCY_V1,
            f"2026-03-01T09:00:00{TZ}", "evt-plan-1",
            guardian_consent={"consented": True, "guardian": "家长甲"},
        )
        self.service.define_goal(
            CASE, "g-com", 1, "主动用词语表达需求", ASSESSMENT_V1, "康复中心A",
            f"2026-03-02T09:00:00{TZ}", "evt-goal-1",
        )


class PlanAndGoalTests(CaseFixture):
    def test_activity_before_any_plan_is_rejected(self) -> None:
        with self.assertRaises(DomainError):
            self.service.record_observation(
                CASE, "g-com", "professional", "therapist-1", "早期尝试",
                1, f"2026-02-10T09:00:00{TZ}", "evt-obs-too-early",
            )

    def test_observation_must_reference_plan_effective_at_observed_time(self) -> None:
        # 5 月仍为 v1：指向 v1 合法
        self.service.record_observation(
            CASE, "g-com", "professional", "therapist-1", "每周 2 次主动表达",
            1, f"2026-05-15T10:00:00{TZ}", "evt-obs-may",
            occurred_at=f"2026-05-15T20:00:00{TZ}",
        )
        # 6 月计划升版
        self.service.revise_plan(
            CASE, 2, 1, "阶段复盘后加强频次", METHODS_V2, FREQUENCY_V2,
            f"2026-06-01T09:00:00{TZ}", "evt-plan-2",
        )
        # 迟到的家庭补记：观察发生在 5 月，必须仍指向当时有效的 v1
        self.service.record_observation(
            CASE, "g-com", "family", "guardian-1", "在家会说要喝水",
            1, f"2026-05-20T19:00:00{TZ}", "evt-obs-late-family",
            occurred_at=f"2026-06-10T21:00:00{TZ}",
        )
        # 用新版计划去登记 5 月的活动必须拒绝
        with self.assertRaises(DomainError):
            self.service.record_observation(
                CASE, "g-com", "family", "guardian-1", "错挂版本",
                2, f"2026-05-21T19:00:00{TZ}", "evt-obs-wrong-version",
            )

    def test_plan_versions_are_sequential_and_old_version_remains_replayable(self) -> None:
        with self.assertRaises(DomainError):
            self.service.approve_plan(
                CASE, 3, ASSESSMENT_V1, METHODS_V1, FREQUENCY_V1,
                f"2026-04-01T09:00:00{TZ}", "evt-plan-bad",
            )
        self.service.revise_plan(
            CASE, 2, 1, "调整方法", METHODS_V2, FREQUENCY_V2,
            f"2026-06-01T09:00:00{TZ}", "evt-plan-2",
        )
        self.assertEqual(
            self.service.effective_plan_at(CASE, f"2026-05-31T23:59:59{TZ}").plan_version, 1
        )
        self.assertEqual(
            self.service.effective_plan_at(CASE, f"2026-06-02T08:00:00{TZ}").plan_version, 2
        )

    def test_key_assessment_change_flags_only_linked_goals_without_voiding_plan(self) -> None:
        self.service.define_goal(
            CASE, "g-play", 1, "平行游戏转共同游戏", ASSESSMENT_V1, "幼儿园支持岗",
            f"2026-03-03T09:00:00{TZ}", "evt-goal-2",
        )
        self.service.accept_assessment(
            CASE, "CARS2-CN", ASSESSMENT_V2, {"score": 32.0},
            f"2026-07-01T10:00:00{TZ}", "evt-assess-2", key_change=True,
        )
        state = self.service._fold(CASE)
        self.assertEqual(state["goals"]["g-com"]["lifecycle"], "under_review")
        self.assertEqual(state["goals"]["g-play"]["lifecycle"], "under_review")
        # 已按新评估定义的目标不被牵连
        self.service.define_goal(
            CASE, "g-sleep", 1, "建立规律就寝流程", ASSESSMENT_V2, "家庭",
            f"2026-07-05T09:00:00{TZ}", "evt-goal-3",
        )
        state = self.service._fold(CASE)
        self.assertEqual(state["goals"]["g-sleep"]["lifecycle"], "active")
        # 计划并未被自动作废：v1 依旧在其时段有效
        self.assertEqual(
            self.service.effective_plan_at(CASE, f"2026-07-06T09:00:00{TZ}").plan_version, 1
        )
        self.service.resolve_goal(
            CASE, "g-com", "revised", "在结构化活动中主动表达需求", "复盘协调人",
            f"2026-07-10T09:00:00{TZ}", "evt-goal-1-resolve",
        )
        state = self.service._fold(CASE)
        self.assertEqual(state["goals"]["g-com"]["lifecycle"], "active")
        self.assertEqual(state["goals"]["g-com"]["statement"], "在结构化活动中主动表达需求")

    def test_divergent_restatements_are_held_as_dispute_not_overwritten(self) -> None:
        self.service.propose_goal_restatement(
            CASE, "g-com", "康复中心A", "用短句主动提要求",
            f"2026-04-01T09:00:00{TZ}", "evt-prop-1",
        )
        self.service.propose_goal_restatement(
            CASE, "g-com", "幼儿园支持岗", "集体活动中举手表达",
            f"2026-04-02T09:00:00{TZ}", "evt-prop-2",
        )
        state = self.service._fold(CASE)
        goal = state["goals"]["g-com"]
        self.assertEqual(goal["lifecycle"], "disputed")
        self.assertEqual(len(goal["held_texts"]), 2)
        self.assertEqual(goal["statement"], "主动用词语表达需求")  # 原表述未被覆盖
        types = {e["event_type"] for e in self.timeline.events}
        self.assertIn("GOAL_RESTATEMENT_DISPUTED", types)


class ObservationTests(CaseFixture):
    def test_family_notes_and_professional_observations_coexist(self) -> None:
        self.service.record_observation(
            CASE, "g-com", "professional", "therapist-1", "治疗室主动表达 3 次",
            1, f"2026-03-10T10:00:00{TZ}", "evt-obs-p1",
        )
        self.service.record_observation(
            CASE, "g-com", "family", "guardian-1", "家里用手指+单字",
            1, f"2026-03-11T19:00:00{TZ}", "evt-obs-f1",
        )
        state = self.service._fold(CASE)
        observations = state["observations"]
        self.assertEqual(len(observations), 2)
        self.assertEqual({o["observer_kind"] for o in observations}, {"family", "professional"})
        self.assertEqual(
            {o["content"] for o in observations},
            {"治疗室主动表达 3 次", "家里用手指+单字"},
        )


class ServiceClaimTests(CaseFixture):
    def test_only_claiming_provider_may_sign_off(self) -> None:
        self.service.claim_service(
            CASE, "slot-mon", "康复中心A", 1,
            f"2026-03-05T08:00:00{TZ}", "evt-claim-1",
        )
        with self.assertRaises(DomainError):
            self.service.record_service(
                CASE, "slot-mon", "康复中心B", 1,
                f"2026-03-05T10:00:00{TZ}", "evt-srv-other",
            )
        with self.assertRaises(DomainError):
            self.service.record_service(
                CASE, "slot-tue", "康复中心A", 1,
                f"2026-03-06T10:00:00{TZ}", "evt-srv-unclaimed",
            )
        stored = self.service.record_service(
            CASE, "slot-mon", "康复中心A", 1,
            f"2026-03-05T10:00:00{TZ}", "evt-srv-1",
            detail={"attended": True},
        )
        self.assertEqual(stored["event_type"], "SERVICE_RECORDED")

    def test_concurrent_claims_have_single_winner(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def claim(provider: str) -> None:
            barrier.wait()
            try:
                self.service.claim_service(
                    CASE, "slot-race", provider, 1,
                    f"2026-03-07T08:00:00{TZ}", f"evt-claim-{provider}",
                )
                outcomes.append(provider)
            except ServiceSlotAlreadyClaimed:
                outcomes.append("lost")

        t1 = threading.Thread(target=claim, args=("康复中心A",))
        t2 = threading.Thread(target=claim, args=("康复中心B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [o for o in outcomes if o != "lost"]
        losers = [o for o in outcomes if o == "lost"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        state = self.service._fold(CASE)
        self.assertEqual(len(state["claims"]), 1)
        self.assertEqual(state["claims"]["slot-race"]["provider_id"], winners[0])


class RiskAndReminderTests(CaseFixture):
    def _emergency(self, when: str) -> None:
        self.service.take_risk_action(
            CASE, "staff-on-duty-1", "挑战性行为致自伤风险", "移去周围硬物并保护性陪伴",
            f"{when}T08:00:00{TZ}", "evt-risk-1",
            authorization_due_at=f"{when}T10:00:00{TZ}",
            independent_review_due_at=f"{when}T12:00:00{TZ}",
        )

    def test_emergency_requires_time_boxed_followups_and_independent_reviewer(self) -> None:
        with self.assertRaises(DomainError):
            self.service.take_risk_action(
                CASE, "staff-1", "自伤风险", "保护性约束",
                f"2026-05-01T08:00:00{TZ}", "evt-risk-bad",
            )
        self._emergency("2026-05-01")
        with self.assertRaises(DomainError):
            self.service.complete_risk_independent_review(
                CASE, "evt-risk-1", "staff-on-duty-1", "处置必要",
                f"2026-05-01T11:00:00{TZ}", "evt-review-self",
            )
        late_auth = self.service.complete_risk_authorization(
            CASE, "evt-risk-1", "supervisor-1",
            f"2026-05-01T10:30:00{TZ}", "evt-auth-1",
        )
        self.assertFalse(late_auth["payload"]["on_time"])
        review = self.service.complete_risk_independent_review(
            CASE, "evt-risk-1", "supervisor-2", "最小必要动作合理",
            f"2026-05-01T11:30:00{TZ}", "evt-review-1",
        )
        self.assertTrue(review["payload"]["on_time"])

    def test_overdue_reminders_resume_on_original_schedule_after_restart(self) -> None:
        self._emergency("2026-05-01")
        # 截止前没有提醒
        self.assertEqual(self.service.fire_due_reminders(f"2026-05-01T09:00:00{TZ}"), [])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.json"
            self.timeline.persist(path)
            # 模拟系统停机后在另一进程恢复
            restarted_timeline = Timeline(path)
            restarted = CareCoordinationService(restarted_timeline, clock=dt(f"2026-05-01T11:00:00{TZ}"))
            fired = restarted.fire_due_reminders()
            self.assertEqual(len(fired), 1)
            self.assertTrue(fired[0]["payload"]["reminder_key"].endswith("authorization"))
            # 重复触发不补发
            self.assertEqual(restarted.fire_due_reminders(), [])

            restarted.complete_risk_authorization(
                CASE, "evt-risk-1", "supervisor-1",
                f"2026-05-01T11:30:00{TZ}", "evt-auth-1",
            )
            fired = restarted.fire_due_reminders(f"2026-05-01T13:00:00{TZ}")
            self.assertEqual(len(fired), 1)
            self.assertTrue(fired[0]["payload"]["reminder_key"].endswith("independent_review"))
            restarted_timeline.persist()

            # 再次恢复，已发提醒不重发
            again = CareCoordinationService(Timeline(path))
            self.assertEqual(again.fire_due_reminders(f"2026-05-01T14:00:00{TZ}"), [])


class UploadTests(CaseFixture):
    def _event(self, text: str = "治疗记录") -> dict:
        return {
            "event_id": "evt-upload-1",
            "event_type": "OBSERVATION_RECORDED",
            "aggregate_type": "functional_goal",
            "aggregate_id": f"{CASE}:goal:g-com",
            "occurred_at": f"2026-03-12T10:00:00{TZ}",
            "version": 1,
            "payload": {
                "case_id": CASE,
                "goal_id": "g-com",
                "observer_kind": "professional",
                "observer_id": "therapist-1",
                "content": text,
                "plan_version": 1,
                "observed_at": f"2026-03-12T10:00:00{TZ}",
            },
        }

    def test_duplicate_upload_returns_original_receipt(self) -> None:
        first = self.service.submit(self._event(), "upload-key-1")
        second = self.service.submit(self._event(), "upload-key-1")
        self.assertEqual(first["status"] if "status" in first else first["_status"], "stored")
        self.assertEqual(second["_status"], "duplicate")
        self.assertEqual(second["event_id"], first["event_id"])
        observations = [e for e in self.timeline.events if e["event_type"] == "OBSERVATION_RECORDED"]
        self.assertEqual(len(observations), 1)

    def test_same_key_changed_content_keeps_dispute(self) -> None:
        self.service.submit(self._event("原始内容"), "upload-key-2")
        changed = self._event("被改写的内容")
        changed["event_id"] = "evt-upload-2"
        with self.assertRaises(ContentMismatch):
            self.service.submit(changed, "upload-key-2")
        disputes = [e for e in self.timeline.events if e["event_type"] == "UPLOAD_DISPUTED"]
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["payload"]["original_event_id"], "evt-upload-1")
        observations = [e for e in self.timeline.events if e["event_type"] == "OBSERVATION_RECORDED"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["payload"]["content"], "原始内容")


class ViewTests(CaseFixture):
    def setUp(self) -> None:
        super().setUp()
        self.service.record_observation(
            CASE, "g-com", "professional", "therapist-1", "主动表达 2 次",
            1, f"2026-03-10T10:00:00{TZ}", "evt-obs-p1",
        )
        self.service.record_observation(
            CASE, "g-com", "family", "guardian-1", "在家说要抱抱",
            1, f"2026-03-12T19:00:00{TZ}", "evt-obs-f1",
        )
        self.service.record_family_priority(
            CASE, ["睡眠规律", "集体活动适应"],
            f"2026-03-02T20:00:00{TZ}", "evt-prio-1",
        )
        self.service.issue_referral(
            CASE, "ref-1", "疑似共患睡眠障碍", "儿童发育行为科",
            f"2026-03-15T09:00:00{TZ}", "evt-ref-1",
        )

    def test_family_dashboard_shows_progress_and_next_owner(self) -> None:
        dashboard = self.service.family_dashboard(CASE)
        goal = next(g for g in dashboard["goals"] if g["goal_id"] == "g-com")
        self.assertEqual(goal["next_owner"], "康复中心A")
        self.assertEqual(goal["progress"]["professional_observations"], 1)
        self.assertEqual(goal["progress"]["family_notes"], 1)
        self.assertEqual(goal["progress"]["latest_professional_note"], "主动表达 2 次")
        self.assertEqual(dashboard["open_referrals"][0]["target_specialty"], "儿童发育行为科")
        rendered = json.dumps(dashboard, ensure_ascii=False)
        self.assertNotIn("diagnosis", rendered)
        self.assertNotIn("36.5", rendered)  # 量表分值不进入家庭视图

        self.service.propose_goal_restatement(
            CASE, "g-com", "幼儿园支持岗", "集体环节举手",
            f"2026-04-01T09:00:00{TZ}", "evt-prop-a",
        )
        self.service.propose_goal_restatement(
            CASE, "g-com", "康复中心A", "短句提要求",
            f"2026-04-02T09:00:00{TZ}", "evt-prop-b",
        )
        goal = next(g for g in self.service.family_dashboard(CASE)["goals"]
                    if g["goal_id"] == "g-com")
        self.assertEqual(goal["next_owner"], "复盘协调人")

    def test_staff_view_is_scoped_and_sensitive_fields_are_masked(self) -> None:
        self.service.claim_service(
            CASE, "slot-a", "康复中心A", 1,
            f"2026-03-06T08:00:00{TZ}", "evt-claim-a",
        )
        with self.assertRaises(AccessDenied):
            self.service.staff_case_view(CASE, "无关机构X")
        view = self.service.staff_case_view(CASE, "康复中心A")
        encoded = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("scale_result", encoded)
        self.assertNotIn("睡眠规律", encoded)  # 家庭优先事项不对普通工作人员开放
        self.assertEqual(view["assessments"][0]["scale"], "CARS2-CN")
        self.assertEqual(view["service_commitments"][0]["slot_id"], "slot-a")

    def test_supervisor_replays_review_basis_with_method_versions(self) -> None:
        self.service.take_risk_action(
            CASE, "staff-9", "咬人风险", "提供咀嚼胶并保持一臂距离",
            f"2026-04-10T08:30:00{TZ}", "evt-risk-apr",
            authorization_status="pre_authorized",
            authorization_due_at=None, independent_review_due_at=None,
        )
        self.service.record_observation(
            CASE, "g-com", "professional", "therapist-1", "窗口内观察",
            1, f"2026-04-15T10:00:00{TZ}", "evt-obs-in-window",
        )
        self.service.record_observation(
            CASE, "g-com", "professional", "therapist-1", "窗口外观察",
            1, f"2026-05-15T10:00:00{TZ}", "evt-obs-out-window",
        )
        self.service.complete_review(
            CASE, "review-q2", 1,
            f"2026-04-01T00:00:00{TZ}", f"2026-04-30T23:59:59{TZ}",
            f"2026-05-05T09:00:00{TZ}", "evt-review-1",
        )
        replay = self.service.replay_review(CASE, "review-q2")
        contents = {o["content"] for o in replay["observations"]}
        self.assertIn("窗口内观察", contents)
        self.assertNotIn("窗口外观察", contents)
        self.assertTrue(all(o["valid_for_window_plan"] for o in replay["observations"]))
        self.assertEqual(replay["plan_basis"]["plan_version"], 1)
        self.assertEqual(replay["plan_basis"]["methods"], METHODS_V1)
        self.assertEqual(replay["risk_actions"][0]["authorization"]["status"], "pre_authorized")

    def test_assessment_events_carry_no_diagnosis(self) -> None:
        for event in self.timeline.events:
            if event["event_type"] == "ASSESSMENT_ACCEPTED":
                self.assertNotIn("diagnosis", event["payload"])
                self.assertNotIn("auto_diagnosis", event["payload"])


if __name__ == "__main__":
    unittest.main()
