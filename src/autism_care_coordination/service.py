"""孤独症干预协同档案领域服务。

在事件交换契约之上落实协同不变量：

- 所有活动（观察、服务、复盘）都必须指向发生当时有效的计划版本；
- 计划修订只新开版本，关键评估变化只把相关目标送入复审，不作废整份计划；
- 家庭补记与专业观察分别追加、互不覆盖；目标表述分歧以争议形态保留；
- 服务责任先认领后签认，跨机构并发认领同一槽位只有一方成功；
- 紧急风险先执行最小必要动作，授权与独立复核限时补齐并接受逾期提醒；
- 上传按幂等键返回原回执；同键内容变化登记争议而不是静默覆盖；
- 提醒完全由事实时间推导，进程恢复后按原截止时间继续；
- 评估流程只记录量表与结果，服务不对儿童作自动诊断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .contracts import validate_event
from .timeline import ContentMismatch, Timeline

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


class DomainError(Exception):
    """业务规则被违反。"""


class ServiceSlotAlreadyClaimed(DomainError):
    def __init__(self, slot_id: str, winner: str) -> None:
        super().__init__(f"服务槽位 {slot_id} 已被 {winner} 认领")
        self.slot_id = slot_id
        self.winner = winner


class AccessDenied(DomainError):
    """工作人员访问与己无关的个案，或越权查看敏感资料。"""


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return parsed


@dataclass(frozen=True)
class PlanVersion:
    plan_version: int
    effective_from: datetime
    methods: tuple[dict[str, Any], ...]
    service_frequency: dict[str, Any]
    reason: Optional[str]
    source_event_id: str


@dataclass(frozen=True)
class RiskAction:
    event_id: str
    at: datetime
    actor_id: str
    reason: str
    minimal_action: str
    authorization_status: str
    authorization_due_at: Optional[datetime]
    independent_review_due_at: Optional[datetime]
    authorized_at: Optional[datetime] = None
    authorized_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    review_finding: Optional[str] = None


def load_schema(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or _SCHEMA_PATH).read_text(encoding="utf-8"))


class CareCoordinationService:
    def __init__(
        self,
        timeline: Timeline,
        schema: Mapping[str, Any] | None = None,
        clock: Any = None,
    ) -> None:
        self.timeline = timeline
        self.schema = dict(schema or load_schema())
        self.clock = clock or (lambda: datetime.now().astimezone())

    def _now(self) -> datetime:
        value = self.clock() if callable(self.clock) else self.clock
        return _as_datetime(value)

    # ------------------------------------------------------------------ 基础追加

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str | datetime,
        payload: Mapping[str, Any],
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _as_datetime(occurred_at).isoformat(),
            "version": self.timeline.next_version(aggregate_id) + 1,
            "payload": dict(payload),
        }
        issues = validate_event(event, self.schema)
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise DomainError(f"事件 {event_id} 不符合契约: {detail}")
        try:
            status, stored = self.timeline.append(event, idempotency_key)
        except ContentMismatch as mismatch:
            self._record_upload_dispute(mismatch)
            raise
        stored["_status"] = status
        return stored

    def _record_upload_dispute(self, mismatch: ContentMismatch) -> dict[str, Any]:
        incoming = mismatch.incoming
        case_id = incoming["payload"].get("case_id", "unknown")
        now = self._now().isoformat()
        dispute = {
            "event_id": f"dispute:{mismatch.key}",
            "event_type": "UPLOAD_DISPUTED",
            "aggregate_type": "child_case",
            "aggregate_id": case_id,
            "occurred_at": now,
            "version": self.timeline.next_version(case_id) + 1,
            "payload": {
                "case_id": case_id,
                "idempotency_key": mismatch.key,
                "original_event_id": mismatch.original["event_id"],
                "reason": "同一幂等标识再次上传但事件类型、聚合或载荷内容不同，双方表述均予保留",
            },
        }
        issues = validate_event(dispute, self.schema)
        if issues:  # pragma: no cover - 争议事件本身由服务构造
            raise DomainError("争议事件不符合契约")
        _, stored = self.timeline.append(dispute)
        return stored

    def submit(self, event: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        """供跨机构上传使用：校验契约后按幂等键收存。

        重复上传返回原事件回执；同键内容变化登记 UPLOAD_DISPUTED 并抛出 ContentMismatch，
        调用方可从时间线读取争议记录。
        """
        issues = validate_event(event, self.schema)
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise DomainError(f"上传事件不符合契约: {detail}")
        try:
            status, stored = self.timeline.append(dict(event), idempotency_key)
        except ContentMismatch as mismatch:
            self._record_upload_dispute(mismatch)
            raise
        stored["_status"] = status
        return stored

    # ------------------------------------------------------------------ 评估与优先事项

    def accept_assessment(
        self,
        case_id: str,
        scale: str,
        scale_version: str,
        scale_result: Mapping[str, Any],
        occurred_at: str | datetime,
        event_id: str,
        key_change: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记一版评估结果。关键变化会把相关目标送入复审，但不作废任何计划版本。"""
        payload = {
            "case_id": case_id,
            "scale": scale,
            "scale_version": scale_version,
            "scale_result": dict(scale_result),
            "key_change": bool(key_change),
        }
        stored = self._append(
            "ASSESSMENT_ACCEPTED", "child_case", case_id, occurred_at, payload,
            event_id, idempotency_key,
        )
        if stored.get("_status") == "stored" and key_change:
            state = self._fold(case_id)
            prior_versions = {
                assessment["scale_version"]
                for assessment in state["assessments"]
                if assessment["scale"] == scale and assessment["event_id"] != event_id
            }
            for goal in state["goals"].values():
                if goal["linked_assessment_version"] in prior_versions and goal["lifecycle"] != "retired":
                    self._append(
                        "GOAL_REVIEW_FLAGGED",
                        "functional_goal",
                        goal["aggregate_id"],
                        occurred_at,
                        {
                            "case_id": case_id,
                            "goal_id": goal["goal_id"],
                            "assessment_version": scale_version,
                            "reason": f"量表 {scale} 出现关键变化（{scale_version}），目标进入复审",
                        },
                        event_id=f"{event_id}:flag:{goal['goal_id']}",
                    )
        return stored

    def record_family_priority(
        self,
        case_id: str,
        priorities: Sequence[str],
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._append(
            "FAMILY_PRIORITY_RECORDED", "child_case", case_id, occurred_at,
            {"case_id": case_id, "priorities": list(priorities)},
            event_id, idempotency_key,
        )

    # ------------------------------------------------------------------ 计划与目标

    def approve_plan(
        self,
        case_id: str,
        plan_version: int,
        assessment_version: str,
        methods: Sequence[Mapping[str, Any]],
        service_frequency: Mapping[str, Any],
        occurred_at: str | datetime,
        event_id: str,
        guardian_consent: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        self._require_assessment(state, assessment_version)
        expected = max((p.plan_version for p in state["plans"]), default=0) + 1
        if plan_version != expected:
            raise DomainError(f"计划版本必须从 {expected} 连续递增，收到 {plan_version}")
        payload = {
            "case_id": case_id,
            "plan_version": plan_version,
            "assessment_version": assessment_version,
            "guardian_consent": guardian_consent or {"consented": True},
            "methods": [dict(m) for m in methods],
            "service_frequency": dict(service_frequency),
        }
        return self._append(
            "PLAN_APPROVED", "care_plan", f"{case_id}:plan", occurred_at,
            payload, event_id, idempotency_key,
        )

    def revise_plan(
        self,
        case_id: str,
        plan_version: int,
        supersedes_version: int,
        reason: str,
        methods: Sequence[Mapping[str, Any]],
        service_frequency: Mapping[str, Any],
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        current = max((p.plan_version for p in state["plans"]), default=0)
        if plan_version != current + 1:
            raise DomainError(f"新计划版本必须是 {current + 1}")
        if supersedes_version != current:
            raise DomainError(f"只能替代当前版本 {current}，收到 {supersedes_version}")
        payload = {
            "case_id": case_id,
            "plan_version": plan_version,
            "supersedes_version": supersedes_version,
            "reason": reason,
            "methods": [dict(m) for m in methods],
            "service_frequency": dict(service_frequency),
        }
        return self._append(
            "PLAN_REVISED", "care_plan", f"{case_id}:plan", occurred_at,
            payload, event_id, idempotency_key,
        )

    def define_goal(
        self,
        case_id: str,
        goal_id: str,
        plan_version: int,
        statement: str,
        linked_assessment_version: str,
        owner: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        at = _as_datetime(occurred_at)
        state = self._fold(case_id)
        self._require_assessment(state, linked_assessment_version)
        self._require_effective_plan(state, plan_version, at)
        if goal_id in state["goals"]:
            raise DomainError(f"目标 {goal_id} 已存在，改述请使用 propose_goal_restatement")
        payload = {
            "case_id": case_id,
            "goal_id": goal_id,
            "plan_version": plan_version,
            "statement": statement,
            "linked_assessment_version": linked_assessment_version,
            "owner": owner,
        }
        return self._append(
            "GOAL_DEFINED", "functional_goal", f"{case_id}:goal:{goal_id}",
            occurred_at, payload, event_id, idempotency_key,
        )

    def propose_goal_restatement(
        self,
        case_id: str,
        goal_id: str,
        proposed_by: str,
        proposed_text: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        goal = self._require_goal(state, goal_id)
        if goal["lifecycle"] == "retired":
            raise DomainError("已终止目标不能改述")
        stored = self._append(
            "GOAL_RESTATEMENT_PROPOSED",
            "functional_goal",
            goal["aggregate_id"],
            occurred_at,
            {
                "case_id": case_id,
                "goal_id": goal_id,
                "proposed_by": proposed_by,
                "proposed_text": proposed_text,
            },
            event_id,
            idempotency_key,
        )
        proposals = goal["pending_proposals"]
        if stored.get("_status") == "stored":
            conflicting = [p for p in proposals if p["by"] != proposed_by and p["text"] != proposed_text]
            if conflicting or goal["lifecycle"] == "disputed":
                held = [{"proposed_by": p["by"], "text": p["text"]} for p in proposals]
                held.append({"proposed_by": proposed_by, "text": proposed_text})
                self._append(
                    "GOAL_RESTATEMENT_DISPUTED",
                    "functional_goal",
                    goal["aggregate_id"],
                    occurred_at,
                    {
                        "case_id": case_id,
                        "goal_id": goal_id,
                        "held_texts": held,
                    },
                    event_id=f"{event_id}:dispute",
                )
        return stored

    def resolve_goal(
        self,
        case_id: str,
        goal_id: str,
        resolution: str,
        statement: str,
        resolved_by: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        goal = self._require_goal(state, goal_id)
        payload = {
            "case_id": case_id,
            "goal_id": goal_id,
            "resolution": resolution,
            "statement": statement,
            "resolved_by": resolved_by,
        }
        return self._append(
            "GOAL_REVIEW_RESOLVED", "functional_goal",
            goal["aggregate_id"], occurred_at, payload, event_id, idempotency_key,
        )

    # ------------------------------------------------------------------ 观察

    def record_observation(
        self,
        case_id: str,
        goal_id: str,
        observer_kind: str,
        observer_id: str,
        content: str,
        plan_version: int,
        observed_at: str | datetime,
        event_id: str,
        occurred_at: str | datetime | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """家庭补记与专业观察使用同一追加通道：只新增、永不互相覆盖。

        occurred_at 是记录写入时间（可晚于 observed_at），活动归属以 observed_at 当时有效计划为准。
        """
        state = self._fold(case_id)
        self._require_goal(state, goal_id)
        self._require_effective_plan(state, plan_version, _as_datetime(observed_at))
        payload = {
            "case_id": case_id,
            "goal_id": goal_id,
            "observer_kind": observer_kind,
            "observer_id": observer_id,
            "content": content,
            "plan_version": plan_version,
            "observed_at": _as_datetime(observed_at).isoformat(),
        }
        return self._append(
            "OBSERVATION_RECORDED", "functional_goal", f"{case_id}:goal:{goal_id}",
            occurred_at or observed_at, payload, event_id, idempotency_key,
        )

    # ------------------------------------------------------------------ 服务认领与签认

    def claim_service(
        self,
        case_id: str,
        service_slot_id: str,
        provider_id: str,
        plan_version: int,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """跨机构并发认领同一槽位：检查与追加在同一临界区，只有一方成功。"""
        at = _as_datetime(occurred_at)
        payload = {
            "case_id": case_id,
            "service_slot_id": service_slot_id,
            "provider_id": provider_id,
            "plan_version": plan_version,
        }
        with self.timeline.lock:
            state = self._fold(case_id)
            self._require_effective_plan(state, plan_version, at)
            existing = state["claims"].get(service_slot_id)
            if existing is not None and existing["provider_id"] != provider_id:
                raise ServiceSlotAlreadyClaimed(service_slot_id, existing["provider_id"])
            if existing is not None:
                first = next(
                    event for event in self.timeline.events
                    if event["event_type"] == "SERVICE_CLAIMED"
                    and event["payload"].get("service_slot_id") == service_slot_id
                )
                first["_status"] = "duplicate"
                return first
            return self._append(
                "SERVICE_CLAIMED", "service_commitment",
                f"{case_id}:slot:{service_slot_id}", occurred_at, payload,
                event_id, idempotency_key,
            )

    def record_service(
        self,
        case_id: str,
        service_slot_id: str,
        provider_id: str,
        plan_version: int,
        occurred_at: str | datetime,
        event_id: str,
        detail: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """机构只能签认自己认领并实际提供的服务。"""
        at = _as_datetime(occurred_at)
        state = self._fold(case_id)
        self._require_effective_plan(state, plan_version, at)
        claim = state["claims"].get(service_slot_id)
        if claim is None:
            raise DomainError(f"服务槽位 {service_slot_id} 尚未被认领，不能签认")
        if claim["provider_id"] != provider_id:
            raise DomainError(
                f"槽位由 {claim['provider_id']} 认领，{provider_id} 不能代为签认"
            )
        payload = {
            "case_id": case_id,
            "service_slot_id": service_slot_id,
            "provider_id": provider_id,
            "plan_version": plan_version,
        }
        if detail:
            payload["detail"] = dict(detail)
        return self._append(
            "SERVICE_RECORDED", "service_commitment",
            f"{case_id}:slot:{service_slot_id}", occurred_at, payload,
            event_id, idempotency_key,
        )

    # ------------------------------------------------------------------ 紧急风险

    def take_risk_action(
        self,
        case_id: str,
        actor_id: str,
        reason: str,
        minimal_action: str,
        occurred_at: str | datetime,
        event_id: str,
        authorization_status: str = "emergency_pending",
        authorization_due_at: str | datetime | None = None,
        independent_review_due_at: str | datetime | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not minimal_action.strip():
            raise DomainError("紧急处置必须记录最小必要动作")
        at = _as_datetime(occurred_at)
        payload: dict[str, Any] = {
            "case_id": case_id,
            "actor_id": actor_id,
            "reason": reason,
            "minimal_action": minimal_action,
            "authorization_status": authorization_status,
            "authorization_due_at": (
                _as_datetime(authorization_due_at).isoformat()
                if authorization_due_at is not None else None
            ),
            "independent_review_due_at": (
                _as_datetime(independent_review_due_at).isoformat()
                if independent_review_due_at is not None else None
            ),
        }
        if authorization_status == "emergency_pending":
            for name, value in (
                ("authorization_due_at", authorization_due_at),
                ("independent_review_due_at", independent_review_due_at),
            ):
                if value is None:
                    raise DomainError(f"先处置后补授权必须给出 {name}")
                if _as_datetime(value) <= at:
                    raise DomainError(f"{name} 必须是处置之后的限时截止点")
        stored = self._append(
            "RISK_ACTION_TAKEN", "child_case", case_id, occurred_at,
            payload, event_id, idempotency_key,
        )
        return stored

    def complete_risk_authorization(
        self,
        case_id: str,
        risk_event_id: str,
        authorized_by: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        risk = self._require_risk(state, risk_event_id)
        at = _as_datetime(occurred_at)
        on_time = risk.authorization_due_at is None or at <= risk.authorization_due_at
        payload = {
            "case_id": case_id,
            "risk_event_id": risk_event_id,
            "authorized_by": authorized_by,
            "on_time": on_time,
        }
        return self._append(
            "RISK_AUTHORIZATION_COMPLETED", "child_case", case_id,
            occurred_at, payload, event_id, idempotency_key,
        )

    def complete_risk_independent_review(
        self,
        case_id: str,
        risk_event_id: str,
        reviewer_id: str,
        finding: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        state = self._fold(case_id)
        risk = self._require_risk(state, risk_event_id)
        if reviewer_id == risk.actor_id:
            raise DomainError("独立复核人不能是现场处置人")
        at = _as_datetime(occurred_at)
        on_time = risk.independent_review_due_at is None or at <= risk.independent_review_due_at
        payload = {
            "case_id": case_id,
            "risk_event_id": risk_event_id,
            "reviewer_id": reviewer_id,
            "finding": finding,
            "on_time": on_time,
        }
        return self._append(
            "RISK_INDEPENDENT_REVIEW_COMPLETED", "child_case", case_id,
            occurred_at, payload, event_id, idempotency_key,
        )

    def fire_due_reminders(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        """按事实时间补发所有已到期未办提醒；恢复后重放即可续上，且每键只发一次。"""
        current = _as_datetime(now) if now is not None else self._now()
        fired: list[dict[str, Any]] = []
        cases = {
            event["payload"]["case_id"]
            for event in self.timeline.events
            if isinstance(event.get("payload"), dict) and "case_id" in event["payload"]
        }
        for case_id in sorted(cases):
            state = self._fold(case_id)
            fired_keys = state["reminder_keys"]
            for risk in state["risks"]:
                if risk.authorization_status != "emergency_pending":
                    continue
                pending = (
                    ("authorization", risk.authorization_due_at, risk.authorized_at),
                    ("independent_review", risk.independent_review_due_at, risk.reviewed_at),
                )
                for step, due_at, done_at in pending:
                    if due_at is None or done_at is not None:
                        continue
                    key = f"{risk.event_id}:{step}"
                    if key in fired_keys or current < due_at:
                        continue
                    stored = self._append(
                        "OVERDUE_REMINDER_FIRED",
                        "child_case",
                        case_id,
                        current,
                        {
                            "case_id": case_id,
                            "reminder_key": key,
                            "due_at": due_at.isoformat(),
                        },
                        event_id=f"reminder:{key}",
                    )
                    fired.append(stored)
        return fired

    # ------------------------------------------------------------------ 转介与复盘

    def issue_referral(
        self,
        case_id: str,
        referral_id: str,
        concern: str,
        target_specialty: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._append(
            "REFERRAL_ISSUED", "child_case", case_id, occurred_at,
            {
                "case_id": case_id,
                "referral_id": referral_id,
                "concern": concern,
                "target_specialty": target_specialty,
            },
            event_id, idempotency_key,
        )

    def resolve_referral(
        self,
        case_id: str,
        referral_id: str,
        outcome: str,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._append(
            "REFERRAL_RESOLVED", "child_case", case_id, occurred_at,
            {"case_id": case_id, "referral_id": referral_id, "outcome": outcome},
            event_id, idempotency_key,
        )

    def complete_review(
        self,
        case_id: str,
        review_id: str,
        plan_version: int,
        window_from: str | datetime,
        window_to: str | datetime,
        occurred_at: str | datetime,
        event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "case_id": case_id,
            "review_id": review_id,
            "plan_version": plan_version,
            "window_from": _as_datetime(window_from).isoformat(),
            "window_to": _as_datetime(window_to).isoformat(),
        }
        return self._append(
            "REVIEW_COMPLETED", "review_cycle", f"{case_id}:review:{review_id}",
            occurred_at, payload, event_id, idempotency_key,
        )

    # ------------------------------------------------------------------ 状态折叠

    def _fold(self, case_id: str) -> dict[str, Any]:
        assessments: list[dict[str, Any]] = []
        plans: list[PlanVersion] = []
        goals: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []
        claims: dict[str, dict[str, Any]] = {}
        services: list[dict[str, Any]] = []
        risks_by_id: dict[str, RiskAction] = {}
        referrals: dict[str, dict[str, Any]] = {}
        reminder_keys: set[str] = set()
        reviews: list[dict[str, Any]] = []

        for event in self.timeline.events:
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("case_id") != case_id:
                continue
            at = _as_datetime(event["occurred_at"])
            kind = event["event_type"]

            if kind == "ASSESSMENT_ACCEPTED":
                assessments.append(
                    {
                        "event_id": event["event_id"],
                        "at": at,
                        "scale": payload["scale"],
                        "scale_version": payload["scale_version"],
                        "scale_result": payload["scale_result"],
                        "key_change": payload["key_change"],
                    }
                )
            elif kind in ("PLAN_APPROVED", "PLAN_REVISED"):
                plans.append(
                    PlanVersion(
                        plan_version=payload["plan_version"],
                        effective_from=at,
                        methods=tuple(payload["methods"]),
                        service_frequency=payload["service_frequency"],
                        reason=payload.get("reason"),
                        source_event_id=event["event_id"],
                    )
                )
            elif kind == "GOAL_DEFINED":
                goals[payload["goal_id"]] = {
                    "goal_id": payload["goal_id"],
                    "aggregate_id": event["aggregate_id"],
                    "statement": payload["statement"],
                    "linked_assessment_version": payload["linked_assessment_version"],
                    "owner": payload["owner"],
                    "plan_version": payload["plan_version"],
                    "lifecycle": "active",
                    "pending_proposals": [],
                    "history": [{"at": at, "statement": payload["statement"], "via": "defined"}],
                }
            elif kind == "GOAL_RESTATEMENT_PROPOSED":
                goal = goals[payload["goal_id"]]
                goal["pending_proposals"].append(
                    {"by": payload["proposed_by"], "text": payload["proposed_text"], "at": at}
                )
            elif kind == "GOAL_RESTATEMENT_DISPUTED":
                goals[payload["goal_id"]]["lifecycle"] = "disputed"
                goals[payload["goal_id"]]["held_texts"] = payload["held_texts"]
            elif kind == "GOAL_REVIEW_FLAGGED":
                goal = goals[payload["goal_id"]]
                if goal["lifecycle"] != "retired":
                    goal["lifecycle"] = "under_review"
                    goal["flag_reason"] = payload["reason"]
            elif kind == "GOAL_REVIEW_RESOLVED":
                goal = goals[payload["goal_id"]]
                goal["lifecycle"] = {"kept": "active", "revised": "active", "retired": "retired"}[
                    payload["resolution"]
                ]
                goal["pending_proposals"] = []
                goal["held_texts"] = []
                goal["flag_reason"] = None
                goal["history"].append(
                    {"at": at, "statement": payload["statement"], "via": payload["resolution"]}
                )
                if payload["resolution"] in ("revised", "kept"):
                    goal["statement"] = payload["statement"]
                goal["owner"] = payload["resolved_by"]
            elif kind == "OBSERVATION_RECORDED":
                observations.append(
                    {
                        "event_id": event["event_id"],
                        "goal_id": payload["goal_id"],
                        "observer_kind": payload["observer_kind"],
                        "observer_id": payload["observer_id"],
                        "content": payload["content"],
                        "plan_version": payload["plan_version"],
                        "observed_at": _as_datetime(payload["observed_at"]),
                        "recorded_at": at,
                    }
                )
            elif kind == "SERVICE_CLAIMED":
                claims.setdefault(
                    payload["service_slot_id"],
                    {"provider_id": payload["provider_id"], "plan_version": payload["plan_version"], "at": at},
                )
            elif kind == "SERVICE_RECORDED":
                services.append(
                    {
                        "event_id": event["event_id"],
                        "slot_id": payload["service_slot_id"],
                        "provider_id": payload["provider_id"],
                        "plan_version": payload["plan_version"],
                        "at": at,
                        "detail": payload.get("detail", {}),
                    }
                )
            elif kind == "RISK_ACTION_TAKEN":
                risks_by_id[event["event_id"]] = RiskAction(
                    event_id=event["event_id"],
                    at=at,
                    actor_id=payload["actor_id"],
                    reason=payload["reason"],
                    minimal_action=payload["minimal_action"],
                    authorization_status=payload["authorization_status"],
                    authorization_due_at=(
                        _as_datetime(payload["authorization_due_at"])
                        if payload.get("authorization_due_at") else None
                    ),
                    independent_review_due_at=(
                        _as_datetime(payload["independent_review_due_at"])
                        if payload.get("independent_review_due_at") else None
                    ),
                )
            elif kind == "RISK_AUTHORIZATION_COMPLETED":
                risk = risks_by_id[payload["risk_event_id"]]
                risks_by_id[payload["risk_event_id"]] = RiskAction(
                    **{**risk.__dict__, "authorized_at": at, "authorized_by": payload["authorized_by"]}
                )
            elif kind == "RISK_INDEPENDENT_REVIEW_COMPLETED":
                risk = risks_by_id[payload["risk_event_id"]]
                risks_by_id[payload["risk_event_id"]] = RiskAction(
                    **{
                        **risk.__dict__,
                        "reviewed_at": at,
                        "reviewed_by": payload["reviewer_id"],
                        "review_finding": payload["finding"],
                    }
                )
            elif kind == "REFERRAL_ISSUED":
                referrals[payload["referral_id"]] = {
                    "concern": payload["concern"],
                    "target_specialty": payload["target_specialty"],
                    "status": "open",
                    "at": at,
                }
            elif kind == "REFERRAL_RESOLVED":
                referrals[payload["referral_id"]]["status"] = f"resolved:{payload['outcome']}"
            elif kind == "OVERDUE_REMINDER_FIRED":
                reminder_keys.add(payload["reminder_key"])
            elif kind == "REVIEW_COMPLETED":
                reviews.append(
                    {
                        "event_id": event["event_id"],
                        "review_id": payload["review_id"],
                        "plan_version": payload["plan_version"],
                        "window_from": _as_datetime(payload["window_from"]),
                        "window_to": _as_datetime(payload["window_to"]),
                        "at": at,
                    }
                )

        plans.sort(key=lambda plan: plan.effective_from)
        return {
            "assessments": assessments,
            "plans": plans,
            "goals": goals,
            "observations": observations,
            "claims": claims,
            "services": services,
            "risks": list(risks_by_id.values()),
            "referrals": referrals,
            "reminder_keys": reminder_keys,
            "reviews": reviews,
        }

    def effective_plan_at(self, case_id: str, moment: str | datetime) -> PlanVersion | None:
        state = self._fold(case_id)
        return self._plan_at(state, _as_datetime(moment))

    @staticmethod
    def _plan_at(state: Mapping[str, Any], moment: datetime) -> PlanVersion | None:
        active = [plan for plan in state["plans"] if plan.effective_from <= moment]
        return active[-1] if active else None

    def _require_effective_plan(
        self, state: Mapping[str, Any], plan_version: int, moment: datetime
    ) -> PlanVersion:
        plan = self._plan_at(state, moment)
        if plan is None:
            raise DomainError(f"{moment.isoformat()} 尚无生效计划，活动不能登记")
        if plan.plan_version != plan_version:
            raise DomainError(
                f"活动指向计划 v{plan_version}，但当时生效的是 v{plan.plan_version}"
            )
        return plan

    @staticmethod
    def _require_assessment(state: Mapping[str, Any], scale_version: str) -> None:
        if not any(a["scale_version"] == scale_version for a in state["assessments"]):
            raise DomainError(f"评估版本 {scale_version} 尚未登记")

    @staticmethod
    def _require_goal(state: Mapping[str, Any], goal_id: str) -> dict[str, Any]:
        goal = state["goals"].get(goal_id)
        if goal is None:
            raise DomainError(f"目标 {goal_id} 不存在")
        return goal

    @staticmethod
    def _require_risk(state: Mapping[str, Any], risk_event_id: str) -> RiskAction:
        for risk in state["risks"]:
            if risk.event_id == risk_event_id:
                return risk
        raise DomainError(f"风险事件 {risk_event_id} 不存在")

    # ------------------------------------------------------------------ 视图

    def family_dashboard(self, case_id: str) -> dict[str, Any]:
        """家庭视图：目标进展与下一责任人，不展示临床诊断标签。"""
        state = self._fold(case_id)
        current_plan = self._plan_at(state, self._now())
        goals_view = []
        for goal in state["goals"].values():
            related = [o for o in state["observations"] if o["goal_id"] == goal["goal_id"]]
            professional = sorted(
                (o for o in related if o["observer_kind"] == "professional"),
                key=lambda o: o["observed_at"],
            )
            family_notes = [o for o in related if o["observer_kind"] == "family"]
            next_owner = "复盘协调人" if goal["lifecycle"] in ("under_review", "disputed") else goal["owner"]
            goals_view.append(
                {
                    "goal_id": goal["goal_id"],
                    "statement": goal["statement"],
                    "lifecycle": goal["lifecycle"],
                    "next_owner": next_owner,
                    "progress": {
                        "professional_observations": len(professional),
                        "family_notes": len(family_notes),
                        "latest_professional_note": professional[-1]["content"] if professional else None,
                    },
                    "flag_reason": goal.get("flag_reason"),
                }
            )
        return {
            "case_id": case_id,
            "current_plan_version": current_plan.plan_version if current_plan else None,
            "goals": goals_view,
            "open_referrals": [
                {"referral_id": rid, "target_specialty": info["target_specialty"]}
                for rid, info in state["referrals"].items()
                if info["status"] == "open"
            ],
        }

    def staff_case_view(self, case_id: str, staff_provider_id: str) -> dict[str, Any]:
        """普通工作人员视图：只看本机构参与的个案，诊断结果与家庭资料按范围遮蔽。"""
        state = self._fold(case_id)
        own_slots = {
            slot_id for slot_id, claim in state["claims"].items()
            if claim["provider_id"] == staff_provider_id
        }
        if not own_slots:
            raise AccessDenied(f"{staff_provider_id} 与个案 {case_id} 无服务关系")
        return {
            "case_id": case_id,
            "assessments": [
                {"scale": a["scale"], "scale_version": a["scale_version"], "at": a["at"].isoformat()}
                for a in state["assessments"]
            ],
            "service_commitments": [
                {
                    "slot_id": slot_id,
                    "plan_version": state["claims"][slot_id]["plan_version"],
                    "delivered": [
                        {"at": s["at"].isoformat(), "detail": s["detail"]}
                        for s in state["services"] if s["slot_id"] == slot_id
                    ],
                }
                for slot_id in sorted(own_slots)
            ],
            "professional_observations": [
                {
                    "goal_id": o["goal_id"],
                    "observer_id": o["observer_id"],
                    "content": o["content"],
                    "observed_at": o["observed_at"].isoformat(),
                }
                for o in state["observations"] if o["observer_kind"] == "professional"
            ],
        }

    def replay_review(self, case_id: str, review_id: str) -> dict[str, Any]:
        """主管重放某次复盘所依据的全部有效事实：观察、风险授权与方法版本。"""
        state = self._fold(case_id)
        review = next((r for r in state["reviews"] if r["review_id"] == review_id), None)
        if review is None:
            raise DomainError(f"复盘 {review_id} 不存在")
        start, end = review["window_from"], review["window_to"]
        plan = self._plan_at(state, start)
        observations = [
            {
                "event_id": o["event_id"],
                "goal_id": o["goal_id"],
                "observer_kind": o["observer_kind"],
                "observer_id": o["observer_id"],
                "content": o["content"],
                "plan_version": o["plan_version"],
                "observed_at": o["observed_at"].isoformat(),
                "valid_for_window_plan": plan is not None and o["plan_version"] == plan.plan_version,
            }
            for o in state["observations"]
            if start <= o["observed_at"] <= end
        ]
        risk_basis = [
            {
                "event_id": risk.event_id,
                "reason": risk.reason,
                "minimal_action": risk.minimal_action,
                "authorization": {
                    "status": risk.authorization_status,
                    "due_at": risk.authorization_due_at.isoformat() if risk.authorization_due_at else None,
                    "completed_at": risk.authorized_at.isoformat() if risk.authorized_at else None,
                    "authorized_by": risk.authorized_by,
                },
                "independent_review": {
                    "due_at": risk.independent_review_due_at.isoformat()
                    if risk.independent_review_due_at else None,
                    "completed_at": risk.reviewed_at.isoformat() if risk.reviewed_at else None,
                    "reviewer_id": risk.reviewed_by,
                    "finding": risk.review_finding,
                },
            }
            for risk in state["risks"]
            if start <= risk.at <= end
        ]
        return {
            "case_id": case_id,
            "review_id": review_id,
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "plan_basis": None
            if plan is None
            else {
                "plan_version": plan.plan_version,
                "effective_from": plan.effective_from.isoformat(),
                "methods": list(plan.methods),
                "service_frequency": plan.service_frequency,
                "source_event_id": plan.source_event_id,
            },
            "observations": observations,
            "risk_actions": risk_basis,
            "services": [
                {
                    "slot_id": s["slot_id"],
                    "provider_id": s["provider_id"],
                    "plan_version": s["plan_version"],
                    "at": s["at"].isoformat(),
                }
                for s in state["services"]
                if start <= s["at"] <= end
            ],
        }
