"""孤独症干预协同档案业务服务。

在交换契约（见 :mod:`autism_care_coordination.contracts`）之上落实协同规则：

* 每项活动必须指向活动发生时有效的计划版本；
* 机构只能签认自己实际认领并提供的服务，槽位并发认领只有一方成功；
* 家长补记与专业观察并存，任何一方都不能覆盖另一方；
* 关键评估变化只把相关目标送入复审，整份计划不自动作废；
* 紧急风险措施可先执行最小必要动作，授权与独立复核限时补齐；
* 重复上报返回原回执，同标识异内容保留为争议；
* 系统只登记评估分数与筛查提示，不对儿童作自动诊断。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import validate_event
from .store import EventStore, parse_time, rows_to_events, utc_now_iso

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

FAMILY = "family"
PROFESSIONAL = "professional"

# 紧急处置后补齐手续的默认时限（调用方可显式覆盖）。
DEFAULT_AUTHORIZATION_SLA = timedelta(hours=24)
DEFAULT_INDEPENDENT_REVIEW_SLA = timedelta(hours=72)


class CoordinationError(Exception):
    """业务规则冲突的基类。"""


class ContractViolation(CoordinationError):
    def __init__(self, issues: Sequence[Any]) -> None:
        self.issues = issues
        super().__init__("；".join(f"{i.field}:{i.code}" for i in issues))


class PlanNotEffective(CoordinationError):
    """活动未指向其发生时有效的计划版本。"""


class ProviderNotAuthorized(CoordinationError):
    """机构试图签认并非由自己认领/提供的服务。"""


class ServiceClaimConflict(CoordinationError):
    def __init__(self, slot_key: str, winner: str, claim_event_id: str) -> None:
        self.slot_key = slot_key
        self.winner = winner
        self.claim_event_id = claim_event_id
        super().__init__(f"服务责任 {slot_key} 已由 {winner} 认领")


class AccessDenied(CoordinationError):
    """工作人员对该案例或该类资料没有可见权限。"""


class UnknownReference(CoordinationError):
    """事件引用了不存在的评估、风险动作或目标。"""


class ReviewerNotIndependent(CoordinationError):
    """独立复核人不能是紧急处置的实施者本人。"""


@dataclass(frozen=True)
class Receipt:
    """一次上报的稳定回执；重复上报返回同一回执。"""

    event_id: str
    seq: int
    status: str  # accepted | accepted_duplicate | conflict_kept
    duplicate: bool = False
    dispute_id: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "status": self.status,
            "duplicate": self.duplicate,
            "dispute_id": self.dispute_id,
            **self.extra,
        }


@dataclass(frozen=True)
class EffectivePlan:
    version: int
    event_id: str
    occurred_at: str
    goals: list[dict[str, Any]]
    methods: list[dict[str, Any]]
    frequency: dict[str, Any]
    family_priorities: list[str]
    assessment_event_id: str

    def goal_ids(self) -> set[str]:
        return {g["goal_id"] for g in self.goals}


class CareCoordinationService:
    def __init__(
        self,
        store: EventStore | str | Path = ":memory:",
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        if schema is None:
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.schema = schema

    # ------------------------------------------------------------------ #
    # 内部：事件追加与幂等/争议
    # ------------------------------------------------------------------ #

    @staticmethod
    def _content_hash(event: Mapping[str, Any]) -> str:
        # version 是服务端按聚合流分配的序号元数据，不属于"业务内容"：
        # 同一提交重试时序号可能已推进，但不影响其作为重复件的判定。
        body = {k: v for k, v in event.items() if k != "version"}
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _append(self, case_id: str, event: Mapping[str, Any], *, now: str | None = None) -> Receipt:
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation(issues)

        now = now or utc_now_iso()
        digest = self._content_hash(event)
        with self.store.transaction() as conn:
            existing = self.store.find_event(case_id, event["event_id"])
            if existing is not None:
                if existing["content_hash"] == digest:
                    # 重复上传：原样返回首次回执，不再产生副作用。
                    return Receipt(
                        event_id=event["event_id"],
                        seq=existing["seq"],
                        status="accepted_duplicate",
                        duplicate=True,
                    )
                # 标识相同而内容变化：冲突副本单独保留（不进有效时间线），并存争议。
                conflict_seq = self.store.append_event(
                    event, case_id=case_id, content_hash=digest, recorded_at=now,
                    conflict_of=existing["event_id"],
                )
                dispute_id = self.store.open_dispute(
                    case_id=case_id,
                    event_id=event["event_id"],
                    original_seq=existing["seq"],
                    conflicting_seq=conflict_seq,
                    existing_hash=existing["content_hash"],
                    conflicting_hash=digest,
                    created_at=now,
                )
                return Receipt(
                    event_id=event["event_id"],
                    seq=conflict_seq,
                    status="conflict_kept",
                    dispute_id=dispute_id,
                )

            try:
                seq = self.store.append_event(
                    event, case_id=case_id, content_hash=digest, recorded_at=now
                )
                self._after_append(case_id, event, now)
            except sqlite3.IntegrityError as exc:
                # 并发兜底：同一事件标识被别的连接抢先写入。
                existing = self.store.find_event(case_id, event["event_id"])
                if existing is not None:
                    if existing["content_hash"] == digest:
                        return Receipt(
                            event_id=event["event_id"],
                            seq=existing["seq"],
                            status="accepted_duplicate",
                            duplicate=True,
                        )
                    raise CoordinationError(f"事件 {event['event_id']} 存在未决争议") from exc
                raise

        return Receipt(event_id=event["event_id"], seq=seq, status="accepted")

    def _after_append(self, case_id: str, event: Mapping[str, Any], now: str) -> None:
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "RISK_ACTION_TAKEN":
            # 事先已取得授权的紧急处置，只需安排独立复核，不再产生补授权提醒。
            if not payload.get("prior_authorization"):
                auth_due = payload.get("authorization_due_at")
                if auth_due is None:
                    auth_due = (
                        parse_time(event["occurred_at"]) + DEFAULT_AUTHORIZATION_SLA
                    ).isoformat()
                self.store.add_reminder(
                    case_id=case_id, kind="risk_authorization", due_at=auth_due,
                    ref_event_id=event["event_id"], ref_aggregate_id=event["aggregate_id"], now=now,
                )
            review_due = payload["review_due_at"]
            self.store.add_reminder(
                case_id=case_id, kind="risk_independent_review", due_at=review_due,
                ref_event_id=event["event_id"], ref_aggregate_id=event["aggregate_id"], now=now,
            )
        elif etype == "RISK_AUTHORIZATION_RECORDED":
            self.store.close_reminders(case_id, "risk_authorization", payload["risk_event_id"])
        elif etype == "RISK_INDEPENDENT_REVIEW_RECORDED":
            self.store.close_reminders(case_id, "risk_independent_review", payload["risk_event_id"])
        elif etype == "REFERRAL_ISSUED" and payload.get("follow_up_due_at"):
            self.store.add_reminder(
                case_id=case_id, kind="referral_follow_up",
                due_at=payload["follow_up_due_at"], ref_event_id=event["event_id"],
                ref_aggregate_id=event["aggregate_id"], now=now,
            )

    # ------------------------------------------------------------------ #
    # 时间线查询
    # ------------------------------------------------------------------ #

    def timeline(self, case_id: str) -> list[dict[str, Any]]:
        return rows_to_events(self.store.timeline(case_id))

    def disputes(self, case_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.disputes(case_id)]

    def plans(self, case_id: str) -> list[dict[str, Any]]:
        return [
            e for e in self.timeline(case_id) if e["event_type"] == "PLAN_APPROVED"
        ]

    def effective_plan(self, case_id: str, at: str) -> EffectivePlan | None:
        """某时刻有效的计划：该时刻之前最新批准的版本。"""
        candidate = None
        for event in self.plans(case_id):
            if parse_time(event["occurred_at"]) <= parse_time(at):
                candidate = event
        if candidate is None:
            return None
        p = candidate["payload"]
        return EffectivePlan(
            version=candidate["version"],
            event_id=candidate["event_id"],
            occurred_at=candidate["occurred_at"],
            goals=p["goals"],
            methods=p["methods"],
            frequency=p["frequency"],
            family_priorities=p.get("family_priorities", []),
            assessment_event_id=p["assessment_event_id"],
        )

    def _all_goal_ids(self, case_id: str) -> set[str]:
        ids: set[str] = set()
        for plan_event in self.plans(case_id):
            ids.update(g["goal_id"] for g in plan_event["payload"]["goals"])
        return ids

    def _require_goal(self, case_id: str, goal_id: str) -> None:
        if goal_id not in self._all_goal_ids(case_id):
            raise UnknownReference(f"目标 {goal_id} 尚未在任何计划版本中登记")

    def _require_event(self, case_id: str, event_id: str, event_type: str | None = None) -> dict[str, Any]:
        for event in self.timeline(case_id):
            if event["event_id"] == event_id and (event_type is None or event["event_type"] == event_type):
                return event
        raise UnknownReference(f"事件 {event_id} 不存在")

    def _flagged_goals(self, case_id: str) -> set[str]:
        flagged: set[str] = set()
        for event in self.timeline(case_id):
            if event["event_type"] == "GOAL_RECONSIDERATION_FLAGGED":
                flagged.update(event["payload"]["goal_ids"])
        return flagged

    # ------------------------------------------------------------------ #
    # 评估与筛查（只记录，不诊断）
    # ------------------------------------------------------------------ #

    def accept_assessment(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        instrument: str,
        instrument_version: str,
        scores: Mapping[str, Any],
        significant_change: bool = False,
        prior_assessment_event_id: str | None = None,
        affected_goal_ids: Sequence[str] | None = None,
        raw_report_ref: str | None = None,
        now: str | None = None,
    ) -> Receipt:
        """登记一次评估结果（量表及版本）。系统不据此下诊断结论。"""
        payload: dict[str, Any] = {
            "instrument": instrument,
            "instrument_version": instrument_version,
            "guideline_version": "2026",
            "scores": dict(scores),
            "significant_change": bool(significant_change),
            "raw_report_ref": raw_report_ref,
        }
        if prior_assessment_event_id is not None:
            payload["prior_assessment_event_id"] = prior_assessment_event_id

        receipt = self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "ASSESSMENT_ACCEPTED",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": payload,
            },
            now=now,
        )
        if receipt.status == "accepted" and significant_change:
            self._flag_goals_after_assessment(
                case_id, event_id, occurred_at, affected_goal_ids, now=now
            )
        return receipt

    def _flag_goals_after_assessment(
        self,
        case_id: str,
        trigger_event_id: str,
        occurred_at: str,
        affected_goal_ids: Sequence[str] | None,
        *,
        now: str | None,
    ) -> None:
        plan = self.effective_plan(case_id, occurred_at)
        if plan is None:
            return  # 尚无计划：评估留档，首版计划批准时自然以其为依据。
        if affected_goal_ids:
            goal_ids = [g for g in affected_goal_ids if g in plan.goal_ids()]
        else:
            goal_ids = sorted(plan.goal_ids())
        if not goal_ids:
            return
        self._append(
            case_id,
            {
                "event_id": f"{trigger_event_id}-goal-review",
                "event_type": "GOAL_RECONSIDERATION_FLAGGED",
                "aggregate_type": "care_plan",
                "aggregate_id": plan.event_id,
                "occurred_at": occurred_at,
                "version": plan.version,
                "payload": {
                    "goal_ids": goal_ids,
                    "trigger_event_id": trigger_event_id,
                    "reason": "关键评估结果发生显著变化",
                },
            },
            now=now,
        )

    def raise_screening_flag(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        instrument: str,
        threshold: str,
        observed_score: float | int,
        recommendation: str = "建议转专科进一步评估，本提示不构成诊断",
        now: str | None = None,
    ) -> Receipt:
        """量表越线只产生"建议进一步评估"的筛查提示，绝不自动诊断。"""
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "SCREENING_FLAG_RAISED",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": {
                    "instrument": instrument,
                    "threshold": threshold,
                    "observed_score": observed_score,
                    "recommendation": recommendation,
                    "is_diagnosis": False,
                },
            },
            now=now,
        )

    # ------------------------------------------------------------------ #
    # 计划批准与目标措辞
    # ------------------------------------------------------------------ #

    def _next_case_version(self, case_id: str) -> int:
        # 案例级事件版本（评估/筛查/风险），从 1 起递增。
        return (
            max(
                (e["version"] for e in self.timeline(case_id) if e["aggregate_type"] == "child_case"),
                default=0,
            )
            + 1
        )

    def _next_plan_version(self, case_id: str) -> int:
        return len(self.plans(case_id)) + 1

    def approve_plan(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        assessment_event_id: str,
        goals: Sequence[Mapping[str, Any]],
        methods: Sequence[Mapping[str, Any]],
        frequency: Mapping[str, Any],
        guardian_consent: bool,
        family_priorities: Sequence[str] = (),
        family_private: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> Receipt:
        assessment = self._require_event(case_id, assessment_event_id, "ASSESSMENT_ACCEPTED")
        if not guardian_consent:
            raise CoordinationError("批准计划必须取得监护人明确同意")

        version = self._next_plan_version(case_id)
        payload: dict[str, Any] = {
            "assessment_event_id": assessment_event_id,
            "assessment_version": assessment["version"],
            "guardian_consent": True,
            "family_priorities": list(family_priorities),
            "goals": [dict(g) for g in goals],
            "methods": [dict(m) for m in methods],
            "frequency": dict(frequency),
        }
        if family_private is not None:
            payload["family_private"] = dict(family_private)
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "PLAN_APPROVED",
                "aggregate_type": "care_plan",
                "aggregate_id": f"{case_id}-plan-v{version}",
                "occurred_at": occurred_at,
                "version": version,
                "payload": payload,
            },
            now=now,
        )

    def propose_goal_rephrasing(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        goal_id: str,
        wording: str,
        source_party: str,
        rationale: str | None = None,
        now: str | None = None,
    ) -> Receipt:
        """康复中心、基层、幼儿园等可对同一目标提出不同表述。

        提议只追加、不覆盖原措辞，待复盘时决定是否纳入下一计划版本。
        提议必须指向发生时刻有效的计划中的目标。
        """
        plan = self.effective_plan(case_id, occurred_at)
        if plan is None or goal_id not in plan.goal_ids():
            raise PlanNotEffective(
                f"{occurred_at} 时有效的计划中不存在目标 {goal_id}，提议无法挂接"
            )
        payload: dict[str, Any] = {
            "goal_id": goal_id,
            "wording": wording,
            "source_party": source_party,
            "plan_version": plan.version,
        }
        if rationale is not None:
            payload["rationale"] = rationale
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "GOAL_REPHRASING_PROPOSED",
                "aggregate_type": "goal",
                "aggregate_id": f"{case_id}-{goal_id}",
                "occurred_at": occurred_at,
                "version": plan.version,
                "payload": payload,
            },
            now=now,
        )

    # ------------------------------------------------------------------ #
    # 服务认领与签认
    # ------------------------------------------------------------------ #

    def claim_service(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        slot_key: str,
        provider_id: str,
        responsibility: str,
        now: str | None = None,
    ) -> Receipt:
        now = now or utc_now_iso()
        with self.store.transaction():
            # 唯一约束保证跨机构并发时只有一方成功。
            try:
                row = self.store.claim_slot(
                    case_id=case_id, slot_key=slot_key, provider_id=provider_id,
                    event_id=event_id, now=now,
                )
            except sqlite3.IntegrityError:
                winner = self.store.slot_claim(case_id, slot_key)
                raise ServiceClaimConflict(slot_key, winner["claimed_by"], winner["claim_event_id"])

            event = {
                "event_id": event_id,
                "event_type": "SERVICE_CLAIMED",
                "aggregate_type": "service_slot",
                "aggregate_id": f"{case_id}-slot-{slot_key}",
                "occurred_at": occurred_at,
                "version": self._slot_version(case_id, slot_key),
                "payload": {
                    "provider_id": provider_id,
                    "slot_key": slot_key,
                    "responsibility": responsibility,
                },
            }
            issues = validate_event(event, self.schema)
            if issues:
                raise ContractViolation(issues)
            seq = self.store.append_event(
                event, case_id=case_id, content_hash=self._content_hash(event), recorded_at=now
            )
        return Receipt(
            event_id=event_id, seq=seq, status="accepted",
            extra={"provider_id": row["claimed_by"], "slot_key": slot_key},
        )

    def _slot_version(self, case_id: str, slot_key: str) -> int:
        return (
            max(
                (
                    e["version"]
                    for e in self.timeline(case_id)
                    if e["event_type"] == "SERVICE_CLAIMED"
                    and e["payload"].get("slot_key") == slot_key
                ),
                default=0,
            )
            + 1
        )

    def record_service(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        slot_key: str,
        provider_id: str,
        plan_version: int,
        summary: str,
        methods_used: Sequence[str] = (),
        goal_ids: Sequence[str] = (),
        now: str | None = None,
    ) -> Receipt:
        claim = self.store.slot_claim(case_id, slot_key)
        if claim is None or claim["claimed_by"] != provider_id:
            raise ProviderNotAuthorized(
                f"{provider_id} 未认领服务责任 {slot_key}，不能代为签认"
            )

        plan = self.effective_plan(case_id, occurred_at)
        if plan is None:
            raise PlanNotEffective(f"{occurred_at} 尚无生效计划，活动不能签认")
        if plan.version != plan_version:
            raise PlanNotEffective(
                f"活动指向计划 v{plan_version}，但 {occurred_at} 时有效的是 v{plan.version}"
            )
        for goal_id in goal_ids:
            if goal_id not in plan.goal_ids():
                raise UnknownReference(f"签认引用了当前有效计划之外的目标 {goal_id}")

        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "SERVICE_RECORDED",
                "aggregate_type": "service_commitment",
                "aggregate_id": f"{case_id}-commitment-{slot_key}",
                "occurred_at": occurred_at,
                "version": self._commitment_version(case_id, slot_key),
                "payload": {
                    "provider_id": provider_id,
                    "slot_key": slot_key,
                    "plan_version": plan_version,
                    "summary": summary,
                    "methods_used": list(methods_used),
                    "goal_ids": list(goal_ids),
                    "claim_event_id": claim["claim_event_id"],
                },
            },
            now=now,
        )

    def _commitment_version(self, case_id: str, slot_key: str) -> int:
        return (
            max(
                (
                    e["version"]
                    for e in self.timeline(case_id)
                    if e["event_type"] == "SERVICE_RECORDED"
                    and e["payload"].get("slot_key") == slot_key
                ),
                default=0,
            )
            + 1
        )

    # ------------------------------------------------------------------ #
    # 观察：家庭补记与专业观察并存
    # ------------------------------------------------------------------ #

    def record_observation(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        goal_id: str,
        observer_type: str,
        observer_id: str,
        progress_value: float | int,
        note: str = "",
        staff_id: str | None = None,
        now: str | None = None,
    ) -> Receipt:
        # 观察必须挂在其发生时刻有效计划的目标上；计划尚未生效或目标只存在于
        # 历史版本中，都不能记录。
        plan = self.effective_plan(case_id, occurred_at)
        if plan is None or goal_id not in plan.goal_ids():
            raise PlanNotEffective(
                f"{occurred_at} 时有效的计划中不存在目标 {goal_id}，观察无法挂接"
            )
        if observer_type not in (FAMILY, PROFESSIONAL):
            raise CoordinationError("observer_type 只能是 family 或 professional")
        if not isinstance(progress_value, (int, float)) or isinstance(progress_value, bool):
            raise CoordinationError("progress_value 必须是数值")
        if observer_type == PROFESSIONAL:
            if staff_id is None:
                raise CoordinationError("专业观察必须携带工作人员身份")
            grant = self.store.access_for(staff_id, case_id)
            if grant is None:
                raise AccessDenied(f"工作人员 {staff_id} 无权访问该案例")

        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "OBSERVATION_RECORDED",
                "aggregate_type": "goal",
                "aggregate_id": f"{case_id}-{goal_id}",
                "occurred_at": occurred_at,
                "version": self._observation_version(case_id, goal_id),
                "payload": {
                    "goal_id": goal_id,
                    "observer_type": observer_type,
                    "observer_id": observer_id,
                    "staff_id": staff_id,
                    "plan_version": plan.version,
                    "progress_value": progress_value,
                    "note": note,
                },
            },
            now=now,
        )

    def _observation_version(self, case_id: str, goal_id: str) -> int:
        return (
            max(
                (
                    e["version"]
                    for e in self.timeline(case_id)
                    if e["event_type"] == "OBSERVATION_RECORDED"
                    and e["payload"].get("goal_id") == goal_id
                ),
                default=0,
            )
            + 1
        )

    # ------------------------------------------------------------------ #
    # 共患问题转介
    # ------------------------------------------------------------------ #

    def issue_referral(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        issue: str,
        target_specialty: str,
        reason: str,
        urgency: str = "routine",
        follow_up_due_at: str | None = None,
        now: str | None = None,
    ) -> Receipt:
        payload: dict[str, Any] = {
            "issue": issue,
            "target_specialty": target_specialty,
            "reason": reason,
            "urgency": urgency,
            "status": "issued",
        }
        if follow_up_due_at is not None:
            payload["follow_up_due_at"] = follow_up_due_at
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "REFERRAL_ISSUED",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": payload,
            },
            now=now,
        )

    # ------------------------------------------------------------------ #
    # 紧急风险：先执行、限时授权、独立复核
    # ------------------------------------------------------------------ #

    def take_risk_action(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        reason: str,
        action_taken: str,
        acted_by: str,
        minimal_necessary: bool = True,
        prior_authorization: bool = False,
        authorization_due_at: str | None = None,
        review_due_at: str | None = None,
        now: str | None = None,
    ) -> Receipt:
        """记录紧急处置。允许无事先授权先执行，但动作必须是最小必要。"""
        if not minimal_necessary:
            raise CoordinationError("紧急处置必须限定为最小必要动作")
        if review_due_at is None:
            review_due_at = (
                parse_time(occurred_at) + DEFAULT_INDEPENDENT_REVIEW_SLA
            ).isoformat()
        payload: dict[str, Any] = {
            "reason": reason,
            "action_taken": action_taken,
            "acted_by": acted_by,
            "minimal_necessary": True,
            "prior_authorization": bool(prior_authorization),
            "review_due_at": review_due_at,
        }
        if authorization_due_at is not None:
            payload["authorization_due_at"] = authorization_due_at
        plan = self.effective_plan(case_id, occurred_at)
        if plan is not None:
            payload["plan_version"] = plan.version
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "RISK_ACTION_TAKEN",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": payload,
            },
            now=now,
        )

    def record_risk_authorization(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        risk_event_id: str,
        guardian_consent: bool,
        consenter_id: str,
        now: str | None = None,
    ) -> Receipt:
        self._require_event(case_id, risk_event_id, "RISK_ACTION_TAKEN")
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "RISK_AUTHORIZATION_RECORDED",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": {
                    "risk_event_id": risk_event_id,
                    "guardian_consent": bool(guardian_consent),
                    "consenter_id": consenter_id,
                },
            },
            now=now,
        )

    def record_independent_review(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        risk_event_id: str,
        reviewer_id: str,
        finding: str,
        action_appropriate: bool,
        now: str | None = None,
    ) -> Receipt:
        risk = self._require_event(case_id, risk_event_id, "RISK_ACTION_TAKEN")
        if risk["payload"]["acted_by"] == reviewer_id:
            raise ReviewerNotIndependent("独立复核人不得是处置实施者本人")
        grant = self.store.access_for(reviewer_id, case_id)
        if grant is None or grant["role_scope"] != "supervisor":
            raise ReviewerNotIndependent("独立复核必须由与本案例相关的主管人员执行")
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "RISK_INDEPENDENT_REVIEW_RECORDED",
                "aggregate_type": "child_case",
                "aggregate_id": case_id,
                "occurred_at": occurred_at,
                "version": self._next_case_version(case_id),
                "payload": {
                    "risk_event_id": risk_event_id,
                    "reviewer_id": reviewer_id,
                    "finding": finding,
                    "action_appropriate": bool(action_appropriate),
                },
            },
            now=now,
        )

    # ------------------------------------------------------------------ #
    # 阶段复盘
    # ------------------------------------------------------------------ #

    def complete_review(
        self,
        case_id: str,
        event_id: str,
        occurred_at: str,
        *,
        review_cycle_id: str,
        goal_decisions: Mapping[str, str],
        summary: str = "",
        next_actions: Sequence[Mapping[str, Any]] = (),
        now: str | None = None,
    ) -> Receipt:
        plan = self.effective_plan(case_id, occurred_at)
        if plan is None:
            raise PlanNotEffective("复盘时必须存在有效计划")
        known = plan.goal_ids()
        for goal_id in goal_decisions:
            if goal_id not in known:
                raise UnknownReference(f"复盘决定引用了未知目标 {goal_id}")
        return self._append(
            case_id,
            {
                "event_id": event_id,
                "event_type": "REVIEW_COMPLETED",
                "aggregate_type": "review_cycle",
                "aggregate_id": f"{case_id}-review-{review_cycle_id}",
                "occurred_at": occurred_at,
                "version": self._review_version(case_id),
                "payload": {
                    "review_cycle_id": review_cycle_id,
                    "plan_version_at_review": plan.version,
                    "plan_event_id": plan.event_id,
                    "goal_decisions": dict(goal_decisions),
                    "summary": summary,
                    "next_actions": [dict(a) for a in next_actions],
                },
            },
            now=now,
        )

    def _review_version(self, case_id: str) -> int:
        return (
            sum(1 for e in self.timeline(case_id) if e["event_type"] == "REVIEW_COMPLETED") + 1
        )

    # ------------------------------------------------------------------ #
    # 提醒：恢复后按原始到期时间继续
    # ------------------------------------------------------------------ #

    def pump_reminders(self, now: str | None = None) -> list[dict[str, Any]]:
        """派发所有已到期提醒。

        停机期间错过的提醒在恢复时按其原始到期时间顺序补发，且每条只投递一次；
        授权/复核补齐后对应提醒关闭，未补齐的仍可通过 :meth:`open_sla_breaches` 追踪。
        """
        now = now or utc_now_iso()
        delivered: list[dict[str, Any]] = []
        for row in self.store.due_reminders(now):
            self.store.mark_reminder_sent(row["reminder_id"], now)
            delivered.append(dict(row))
        self.store.conn.commit()
        return delivered

    def open_sla_breaches(self, now: str | None = None) -> list[dict[str, Any]]:
        """授权或独立复核超过时限仍未补齐的风险事项。

        依据风险动作事件本身与其后续授权/复核事件实时计算，而不依赖提醒是否
        已投递：即便系统在到期前宕机、恢复后补发了提醒，"是否补齐手续"这一
        事实仍只以时间线上的授权/复核事件为准。
        """
        now = now or utc_now_iso()
        now_dt = parse_time(now)
        breaches: list[dict[str, Any]] = []
        case_rows = self.store.conn.execute("SELECT DISTINCT case_id FROM events")
        for (case_id,) in case_rows:
            events = self.timeline(case_id)
            for event in events:
                if event["event_type"] != "RISK_ACTION_TAKEN":
                    continue
                risk_id = event["event_id"]
                payload = event["payload"]
                acted = parse_time(event["occurred_at"])
                if not payload.get("prior_authorization"):
                    auth_due = parse_time(
                        payload.get("authorization_due_at")
                        or (acted + DEFAULT_AUTHORIZATION_SLA).isoformat()
                    )
                    if auth_due < now_dt and not self._has_followup(
                        events, "RISK_AUTHORIZATION_RECORDED", risk_id
                    ):
                        breaches.append(
                            {
                                "case_id": case_id,
                                "kind": "risk_authorization",
                                "ref_event_id": risk_id,
                                "due_at": auth_due.isoformat(),
                            }
                        )
                review_due = parse_time(payload["review_due_at"])
                if review_due < now_dt and not self._has_followup(
                    events, "RISK_INDEPENDENT_REVIEW_RECORDED", risk_id
                ):
                    breaches.append(
                        {
                            "case_id": case_id,
                            "kind": "risk_independent_review",
                            "ref_event_id": risk_id,
                            "due_at": review_due.isoformat(),
                        }
                    )
        breaches.sort(key=lambda b: (b["due_at"], b["case_id"], b["kind"]))
        return breaches

    @staticmethod
    def _has_followup(
        events: Sequence[Mapping[str, Any]], event_type: str, risk_event_id: str
    ) -> bool:
        return any(
            e["event_type"] == event_type and e["payload"].get("risk_event_id") == risk_event_id
            for e in events
        )

    # ------------------------------------------------------------------ #
    # 访问授权与视图
    # ------------------------------------------------------------------ #

    def grant_staff_access(
        self, staff_id: str, case_id: str, role_scope: str, provider_id: str | None = None
    ) -> None:
        if role_scope not in ("provider", "supervisor"):
            raise CoordinationError("role_scope 只能是 provider 或 supervisor")
        if role_scope == "provider" and not provider_id:
            raise CoordinationError("机构工作人员授权必须绑定 provider_id")
        self.store.grant_access(staff_id, case_id, role_scope, provider_id)

    def family_view(self, case_id: str) -> dict[str, Any]:
        """家庭视图：目标进展 + 下一责任人（不含内部诊断细节）。"""
        latest_plan_event = self.plans(case_id)[-1] if self.plans(case_id) else None
        if latest_plan_event is None:
            return {"case_id": case_id, "goals": [], "next_actions": []}
        plan_payload = latest_plan_event["payload"]
        flagged = self._flagged_goals(case_id)
        timeline = self.timeline(case_id)

        goals_out = []
        for goal in plan_payload["goals"]:
            goal_id = goal["goal_id"]
            obs = [
                e for e in timeline
                if e["event_type"] == "OBSERVATION_RECORDED"
                and e["payload"]["goal_id"] == goal_id
            ]
            latest = obs[-1] if obs else None
            goals_out.append(
                {
                    "goal_id": goal_id,
                    "wording": goal["wording"],
                    "status": "under_review" if goal_id in flagged else "active",
                    "latest_progress": latest["payload"]["progress_value"] if latest else None,
                    "latest_progress_at": latest["occurred_at"] if latest else None,
                    "observation_count": len(obs),
                }
            )
        return {
            "case_id": case_id,
            "plan_version": latest_plan_event["version"],
            "goals": goals_out,
            "next_actions": self._next_actions(case_id, timeline),
        }

    def _next_actions(self, case_id: str, timeline: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        reviews = [e for e in timeline if e["event_type"] == "REVIEW_COMPLETED"]
        if not reviews:
            return []
        return list(reviews[-1]["payload"].get("next_actions", []))

    def staff_case_view(self, staff_id: str, case_id: str) -> dict[str, Any]:
        grant = self.store.access_for(staff_id, case_id)
        if grant is None:
            raise AccessDenied(f"工作人员 {staff_id} 与案例 {case_id} 无关，禁止查看")
        if grant["role_scope"] == "supervisor":
            return self._supervisor_view(case_id)
        return self._provider_view(case_id, grant["provider_id"])

    def _supervisor_view(self, case_id: str) -> dict[str, Any]:
        timeline = self.timeline(case_id)
        return {
            "case_id": case_id,
            "role": "supervisor",
            "timeline": timeline,
            "disputes": self.disputes(case_id),
            "open_sla_breaches": self.open_sla_breaches(),
        }

    def _provider_view(self, case_id: str, provider_id: str) -> dict[str, Any]:
        """普通机构工作人员的最小必要视图。

        可见：本机构认领的责任与签认、所服务目标的措辞/进展观察、服务频次与方法。
        不可见：评估原始分数与报告、筛查提示、非本机构的转介、风险处置明细、
        家庭私密资料。
        """
        timeline = self.timeline(case_id)
        # 以时间线上最新事件时刻确定"当前有效计划"，避免依赖系统时钟。
        anchor = timeline[-1]["occurred_at"] if timeline else None
        plan = self.effective_plan(case_id, anchor) if anchor else None

        my_slots = {
            e["payload"]["slot_key"]
            for e in timeline
            if e["event_type"] == "SERVICE_CLAIMED" and e["payload"]["provider_id"] == provider_id
        }
        commitments = [
            e for e in timeline
            if e["event_type"] == "SERVICE_RECORDED" and e["payload"]["provider_id"] == provider_id
        ]
        served_goals = {
            g
            for e in commitments
            for g in e["payload"].get("goal_ids", [])
        }
        observations = []
        for e in timeline:
            if e["event_type"] != "OBSERVATION_RECORDED":
                continue
            payload = e["payload"]
            if payload["goal_id"] in served_goals:
                observations.append(self._redact_family_note(e))
            elif (
                payload["observer_type"] == PROFESSIONAL
                and payload.get("staff_id")
                and (grant_row := self.store.access_for(payload["staff_id"], case_id)) is not None
                and grant_row["provider_id"] == provider_id
            ):
                observations.append(e)

        goals = []
        if plan is not None:
            visible_goal_ids = served_goals | {o["payload"]["goal_id"] for o in observations}
            goals = [
                {k: g[k] for k in ("goal_id", "wording", "target") if k in g}
                for g in plan.goals
                if g["goal_id"] in visible_goal_ids
            ]

        return {
            "case_id": case_id,
            "role": "provider",
            "provider_id": provider_id,
            "my_responsibilities": sorted(my_slots),
            "my_commitments": commitments,
            "goals_served": goals,
            "observations": observations,
            "methods": plan.methods if plan else [],
            "frequency": plan.frequency if plan else {},
            "redacted": [
                "assessment_scores", "screening_flags", "referrals",
                "risk_actions", "family_private", "family_priorities",
            ],
        }

    @staticmethod
    def _redact_family_note(event: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(event)
        if event["payload"].get("observer_type") == FAMILY:
            payload = dict(event["payload"])
            payload["note"] = "【家庭记录，按需向协同团队公开】"
            out["payload"] = payload
        return out

    # ------------------------------------------------------------------ #
    # 主管复盘重放
    # ------------------------------------------------------------------ #

    def replay_review(self, staff_id: str, case_id: str, review_event_id: str) -> dict[str, Any]:
        """重放某次复盘当时所依据的全部有效材料。

        包括：复盘时刻有效的计划版本（目标/方法及循证版本/频次）、截至当时的
        家庭与专业观察、触发复审的评估与标记、风险处置及其授权/独立复核、
        当时未决的争议。冲突副本不进入重放。
        """
        grant = self.store.access_for(staff_id, case_id)
        if grant is None or grant["role_scope"] != "supervisor":
            raise AccessDenied("只有主管人员可以重放复盘依据")

        review = self._require_event(case_id, review_event_id, "REVIEW_COMPLETED")
        cutoff = parse_time(review["occurred_at"])
        earlier = [
            e for e in self.timeline(case_id)
            if parse_time(e["occurred_at"]) <= cutoff and e["event_id"] != review_event_id
        ]

        plan_event_id = review["payload"].get("plan_event_id")
        plan_event = next((e for e in earlier if e["event_id"] == plan_event_id), None)

        return {
            "case_id": case_id,
            "review": review,
            "effective_plan": plan_event,
            "observations": [e for e in earlier if e["event_type"] == "OBSERVATION_RECORDED"],
            "assessments": [e for e in earlier if e["event_type"] == "ASSESSMENT_ACCEPTED"],
            "reconsideration_flags": [
                e for e in earlier if e["event_type"] == "GOAL_RECONSIDERATION_FLAGGED"
            ],
            "risk_actions": [e for e in earlier if e["event_type"] == "RISK_ACTION_TAKEN"],
            "risk_authorizations": [
                e for e in earlier if e["event_type"] == "RISK_AUTHORIZATION_RECORDED"
            ],
            "independent_reviews": [
                e for e in earlier if e["event_type"] == "RISK_INDEPENDENT_REVIEW_RECORDED"
            ],
            "disputes_open_at_review": [
                d for d in self.disputes(case_id)
                if parse_time(d["created_at"]) <= cutoff and not d["resolved_at"]
            ],
        }
