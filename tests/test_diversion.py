import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService, ExpiredDiversion


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value = self.value + timedelta(seconds=seconds)


class DiversionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(_fixed_now())
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "diversion.db"),
            RuleEngine(),
            clock=self.clock,
        )
        self.coordinator = Actor("commander", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("gate-keeper", "operator")
        self.viewer = Actor("looker", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _two_zones(self, cap_a=100, cap_b=100):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "Stadium", "address": "1 Road"}
        )
        zone_a = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "A", "capacity": cap_a},
        )
        zone_b = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "B", "capacity": cap_b},
        )
        gate_b = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "GB", "zone_ids": [zone_b["id"]]},
        )
        self.service.transition(self.operator, zone_a["id"], "open", {"checklist": "ok"})
        self.service.transition(self.operator, zone_b["id"], "open", {"checklist": "ok"})
        self.service.transition(self.operator, gate_b["id"], "open", {"operator_id": "op"})
        return venue, zone_a, zone_b, gate_b

    def _iso(self, **delta):
        return (self.clock.value + timedelta(**delta)).isoformat()

    def test_create_holds_target_capacity_and_blocks_second_order(self):
        venue, a, b, _gate = self._two_zones(cap_b=100)
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 60,
            },
        )
        self.assertEqual(order["status"], "reserved")
        self.assertEqual(order["data"]["remaining_count"], 60)

        target = self.service.get(b["id"])
        self.assertEqual(target["data"]["held_count"], 60)
        self.assertEqual(target["data"]["available_count"], 40)
        # Source zone occupancy is untouched.
        self.assertEqual(self.service.get(a["id"])["data"]["current_occupancy"], 0)

        # Another order cannot reuse the same remaining capacity.
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": b["id"],
                    "requested_count": 41,
                },
            )
        # Within the remaining headroom it succeeds.
        second = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": b["id"],
                "target_zone_id": a["id"],
                "requested_count": 40,
            },
        )
        self.assertEqual(second["status"], "reserved")

    def test_source_and_target_must_differ_and_belong_to_venue(self):
        venue, a, _b, _gate = self._two_zones()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": a["id"],
                    "requested_count": 10,
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": "missing",
                    "target_zone_id": a["id"],
                    "requested_count": 10,
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": a["id"],
                    "requested_count": 0,
                },
            )

    def test_create_requires_command_role(self):
        venue, a, b, _gate = self._two_zones()
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": b["id"],
                    "requested_count": 10,
                },
            )

    def test_partial_admit_counts_in_and_releases_hold(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 60,
            },
        )
        admitted = self.service.transition(
            self.operator,
            order["id"],
            "admit",
            {"gate_id": gate["id"], "count": 25, "admitted_at": self._iso(seconds=1)},
        )
        self.assertEqual(admitted["status"], "active")
        self.assertEqual(admitted["data"]["admitted_count"], 25)
        self.assertEqual(admitted["data"]["remaining_count"], 35)

        target = self.service.get(b["id"])
        self.assertEqual(target["data"]["current_occupancy"], 25)
        # 25 admitted + 35 still held = full capacity accounted for.
        self.assertEqual(target["data"]["held_count"], 35)
        self.assertEqual(target["data"]["available_count"], 40)
        # Source zone occupancy unchanged.
        self.assertEqual(self.service.get(a["id"])["data"]["current_occupancy"], 0)

        # Over the pre-reserved amount is rejected.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate["id"], "count": 36, "admitted_at": self._iso(seconds=2)},
            )

        # Releasing the rest completes the order and frees nothing further.
        completed = self.service.transition(
            self.operator,
            order["id"],
            "admit",
            {"gate_id": gate["id"], "count": 35, "admitted_at": self._iso(seconds=3)},
        )
        self.assertEqual(completed["status"], "completed")
        target = self.service.get(b["id"])
        self.assertEqual(target["data"]["current_occupancy"], 60)
        self.assertEqual(target["data"]["held_count"], 0)
        self.assertEqual(target["data"]["available_count"], 40)

        # A completed order cannot admit again.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate["id"], "count": 1, "admitted_at": self._iso(seconds=4)},
            )

    def test_admit_rejects_closed_gate_and_wrong_gate(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 10,
            },
        )
        gate_a = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "GA", "zone_ids": [a["id"]]},
        )
        self.service.transition(self.operator, gate_a["id"], "open", {"operator_id": "op"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate_a["id"], "count": 5, "admitted_at": "t1"},
            )
        self.service.transition(self.coordinator, gate["id"], "close", {"reason": "x"})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate["id"], "count": 5, "admitted_at": "t2"},
            )

    def test_cancel_returns_hold_but_keeps_occupancy(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 60,
            },
        )
        self.service.transition(
            self.operator,
            order["id"],
            "admit",
            {"gate_id": gate["id"], "count": 20, "admitted_at": "t1"},
        )
        cancelled = self.service.transition(
            self.coordinator, order["id"], "cancel", {"reason": "flow restored"}
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["data"]["remaining_count"], 40)

        target = self.service.get(b["id"])
        self.assertEqual(target["data"]["current_occupancy"], 20)
        self.assertEqual(target["data"]["held_count"], 0)
        self.assertEqual(target["data"]["available_count"], 80)
        # Source occupancy stays untouched.
        self.assertEqual(self.service.get(a["id"])["data"]["current_occupancy"], 0)

        # Cancelled orders cannot be admitted or cancelled again.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate["id"], "count": 1, "admitted_at": "t2"},
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, order["id"], "cancel", {"reason": "again"}
            )

    def test_cancel_requires_reason(self):
        venue, a, b, _gate = self._two_zones()
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 5,
            },
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.coordinator, order["id"], "cancel", {})

    def test_expiry_releases_hold_on_admit_and_sweep(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 60,
                "expires_at": self._iso(minutes=5),
            },
        )
        self.assertEqual(self.service.get(b["id"])["data"]["available_count"], 40)

        # Cannot force expiry before due time.
        with self.assertRaises(ConflictError):
            self.service.transition(self.coordinator, order["id"], "expire", {})

        self.clock.advance(6 * 60)
        # Read view already treats it as expired and excludes the hold.
        target = self.service.get(b["id"])
        self.assertTrue(self.service.get(order["id"])["data"]["is_expired"])
        self.assertEqual(target["data"]["held_count"], 0)
        self.assertEqual(target["data"]["available_count"], 100)

        # Admit is refused and the order is persisted as expired.
        with self.assertRaises(ExpiredDiversion):
            self.service.transition(
                self.operator,
                order["id"],
                "admit",
                {"gate_id": gate["id"], "count": 1, "admitted_at": "late"},
            )
        self.assertEqual(self.service.get(order["id"])["status"], "expired")
        self.assertEqual(self.service.get(b["id"])["data"]["current_occupancy"], 0)

    def test_expire_sweep_releases_all_due_orders(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        first = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 30,
                "expires_at": self._iso(minutes=5),
            },
        )
        second = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 30,
                "expires_at": self._iso(minutes=10),
            },
        )
        self.clock.advance(7 * 60)
        expired = self.service.expire_due_diversions(self.coordinator)
        self.assertEqual([item["id"] for item in expired], [first["id"]])
        self.assertEqual(self.service.get(first["id"])["status"], "expired")
        self.assertEqual(self.service.get(second["id"])["status"], "reserved")
        # First hold returned; second still holds 30.
        self.assertEqual(self.service.get(b["id"])["data"]["held_count"], 30)
        self.assertEqual(self.service.get(b["id"])["data"]["available_count"], 70)

    def test_expired_hold_frees_capacity_for_new_order(self):
        venue, a, b, _gate = self._two_zones(cap_b=100)
        self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 100,
                "expires_at": self._iso(minutes=5),
            },
        )
        self.clock.advance(6 * 60)
        # A new full-size order can now take over the capacity.
        replacement = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 100,
            },
        )
        self.assertEqual(replacement["status"], "reserved")

    def test_diversion_holds_also_block_plain_zone_admission(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 80,
            },
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                b["id"],
                "admit",
                {"gate_id": gate["id"], "count": 21, "admitted_at": "t0"},
            )
        self.service.transition(
            self.operator,
            b["id"],
            "admit",
            {"gate_id": gate["id"], "count": 20, "admitted_at": "t1"},
        )

    def test_past_expires_at_rejected_at_creation(self):
        venue, a, b, _gate = self._two_zones()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": b["id"],
                    "requested_count": 5,
                    "expires_at": self._iso(minutes=-1),
                },
            )

    def test_limited_zone_admit_limit_counts_hold_toward_restriction(self):
        venue, a, b, gate = self._two_zones(cap_b=100)
        self.service.transition(
            self.coordinator,
            b["id"],
            "restrict",
            {"reason": "congestion", "admit_limit": 50},
        )
        order = self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": venue["id"],
                "source_zone_id": a["id"],
                "target_zone_id": b["id"],
                "requested_count": 40,
            },
        )
        self.assertEqual(self.service.get(b["id"])["data"]["available_count"], 10)
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": venue["id"],
                    "source_zone_id": a["id"],
                    "target_zone_id": b["id"],
                    "requested_count": 11,
                },
            )
        self.service.transition(
            self.operator,
            order["id"],
            "admit",
            {"gate_id": gate["id"], "count": 40, "admitted_at": "t1"},
        )
        self.assertEqual(self.service.get(b["id"])["data"]["current_occupancy"], 40)

    def test_idempotent_create_returns_same_order(self):
        venue, a, b, _gate = self._two_zones()
        payload = {
            "venue_id": venue["id"],
            "source_zone_id": a["id"],
            "target_zone_id": b["id"],
            "requested_count": 10,
        }
        first = self.service.create(self.coordinator, "diversion", payload, "radio-7")
        second = self.service.create(self.coordinator, "diversion", payload, "radio-7")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.service.get(b["id"])["data"]["held_count"], 10)


def _fixed_now():
    return datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
