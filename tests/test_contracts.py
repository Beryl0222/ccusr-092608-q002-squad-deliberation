import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squad_deliberation.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="PROPOSAL_SUBMITTED", payload={})
        self.assertIn(("payload.competition_ref", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_nested_payload_enum_is_checked(self) -> None:
        proposal = {
            "event_id": "e-1",
            "event_type": "PROPOSAL_SUBMITTED",
            "aggregate_type": "lineup_proposal",
            "aggregate_id": "p1",
            "occurred_at": "2026-09-28T09:00:00+08:00",
            "version": 1,
            "payload": {
                "competition_ref": "m1",
                "proposed_by": "coach-li",
                "roster": [{"athlete_id": "a1", "slot": "starter", "order": 1},
                           {"athlete_id": "a2", "slot": "banished"}],
            },
        }
        fields = [(x.field, x.code) for x in validate_event(proposal, self.schema)]
        self.assertIn(("payload.roster.slot", "unsupported_value"), fields)

    def test_medical_clearance_requires_medical_role(self) -> None:
        event = {
            "event_id": "e-2",
            "event_type": "MEDICAL_CLEARANCE_GRANTED",
            "aggregate_type": "athlete_profile",
            "aggregate_id": "athlete-1",
            "occurred_at": "2026-09-25T09:00:00+08:00",
            "version": 1,
            "payload": {
                "subject_id": "athlete-1",
                "granted_by": "coach-li",
                "granted_by_role": "head_coach",
                "restriction_ref": "r-1",
            },
        }
        self.assertIn(
            ("payload.granted_by_role", "unsupported_value"),
            [(x.field, x.code) for x in validate_event(event, self.schema)],
        )


if __name__ == "__main__":
    unittest.main()
