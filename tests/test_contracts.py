from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autism_care_coordination.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_envelope_fields_have_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_specific_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="PLAN_APPROVED", payload={})
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.assessment_version", "required"), [(issue.field, issue.code) for issue in issues])

    def test_unknown_event_is_rejected(self) -> None:
        event = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(event, self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(issue.field, issue.code) for issue in issues])

    def test_goal_flag_requires_non_empty_goal_list(self) -> None:
        event = dict(
            self.sample,
            event_type="GOAL_RECONSIDERATION_FLAGGED",
            aggregate_type="care_plan",
            payload={"goal_ids": [], "trigger_event_id": "ev-1"},
        )
        issues = validate_event(event, self.schema)
        self.assertIn(
            ("payload.goal_ids", "non_empty_string_list"),
            [(issue.field, issue.code) for issue in issues],
        )

    def test_risk_due_time_requires_timezone(self) -> None:
        event = dict(
            self.sample,
            event_type="RISK_ACTION_TAKEN",
            aggregate_type="child_case",
            payload={"reason": "自伤", "review_due_at": "2026-09-25T12:00:00"},
        )
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.review_due_at", "timezone_required"), [(i.field, i.code) for i in issues])

    def test_consent_must_be_boolean(self) -> None:
        event = dict(
            self.sample,
            event_type="PLAN_APPROVED",
            aggregate_type="care_plan",
            payload={"assessment_version": 1, "guardian_consent": "yes"},
        )
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.guardian_consent", "boolean_required"), [(i.field, i.code) for i in issues])

    def test_plan_version_must_be_positive_integer(self) -> None:
        event = dict(
            self.sample,
            event_type="SERVICE_RECORDED",
            aggregate_type="service_commitment",
            payload={"plan_version": 0, "provider_id": "org-A"},
        )
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.plan_version", "positive_integer"), [(i.field, i.code) for i in issues])


if __name__ == "__main__":
    unittest.main()
