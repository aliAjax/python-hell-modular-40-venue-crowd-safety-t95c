import tempfile
import threading
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
from src.service import DomainService


class DiversionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 28, 18, 0, 0, tzinfo=timezone.utc)
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "diversion.db"),
            RuleEngine(),
            clock=lambda: self.now,
        )
        self.coordinator = Actor("commander", "coordinator")
        self.operator = Actor("gate-01", "operator")
        self.viewer = Actor("lookout", "viewer")
        self.venue, self.source, self.dest, self.gate = self._venue_with_zones()

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_with_zones(self, source_capacity=100, dest_capacity=100, dest_occupancy=0):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "Grand Hall", "address": "1 Stadium Road"}
        )
        source = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "North Stand", "capacity": source_capacity},
        )
        dest = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "South Stand", "capacity": dest_capacity},
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "Gate S", "zone_ids": [dest["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "gate-01"})
        self.service.transition(self.operator, source["id"], "open", {"checklist": "clear"})
        self.service.transition(self.operator, dest["id"], "open", {"checklist": "clear"})
        if dest_occupancy:
            dest = self.service.transition(
                self.operator,
                dest["id"],
                "admit",
                {"gate_id": gate["id"], "count": dest_occupancy, "admitted_at": "t0"},
            )
        return venue, source, dest, gate

    def _create_order(self, reserve_count=30, expires_in_seconds=600, **extra):
        return self.service.create(
            self.coordinator,
            "diversion",
            {
                "venue_id": self.venue["id"],
                "source_zone_id": self.source["id"],
                "destination_zone_id": self.dest["id"],
                "reserve_count": reserve_count,
                "expires_at": (self.now + timedelta(seconds=expires_in_seconds)).isoformat(),
                "reason": "north stand nearly full",
                **extra,
            },
        )

    def test_create_reserves_destination_quota(self):
        order = self._create_order(30)
        self.assertEqual(order["status"], "reserved")
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["remaining"], 70)
        self.assertEqual(quota["reserved"], 30)
        self.assertEqual(quota["current_occupancy"], 0)
        # 第二张单不能重复使用已占名额
        with self.assertRaises(ConflictError):
            self._create_order(71)
        # 剩余名额内仍可开单
        second = self._create_order(70)
        self.assertEqual(self.service.zone_quota(self.dest["id"])["remaining"], 0)
        self.assertEqual(second["status"], "reserved")

    def test_same_zone_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "diversion",
                {
                    "venue_id": self.venue["id"],
                    "source_zone_id": self.source["id"],
                    "destination_zone_id": self.source["id"],
                    "reserve_count": 10,
                    "expires_at": (self.now + timedelta(minutes=10)).isoformat(),
                    "reason": "bad",
                },
            )

    def test_release_counts_into_destination_and_frees_hold(self):
        order = self._create_order(30)
        released = self.service.transition(
            self.operator,
            order["id"],
            "release",
            {"gate_id": self.gate["id"], "actual_count": 20, "admitted_at": "t1"},
        )
        self.assertEqual(released["status"], "partially_released")
        self.assertEqual(released["data"]["admitted_count"], 20)
        quota = self.service.zone_quota(self.dest["id"])
        # 在场 20，未用占用 10，剩余 70
        self.assertEqual(quota["current_occupancy"], 20)
        self.assertEqual(quota["reserved"], 10)
        self.assertEqual(quota["remaining"], 70)
        # 原区域人数不变
        self.assertEqual(self.service.get(self.source["id"])["data"]["current_occupancy"], 0)
        # 放行剩余 10，单子关闭、占用清零
        done = self.service.transition(
            self.operator,
            order["id"],
            "release",
            {"gate_id": self.gate["id"], "actual_count": 10, "admitted_at": "t2"},
        )
        self.assertEqual(done["status"], "released")
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["reserved"], 0)
        self.assertEqual(quota["current_occupancy"], 30)
        self.assertEqual(quota["remaining"], 70)

    def test_release_over_reservation_is_rejected(self):
        order = self._create_order(30)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 31, "admitted_at": "t1"},
            )
        # 分批放行后超出预占同样拒绝
        self.service.transition(
            self.operator,
            order["id"],
            "release",
            {"gate_id": self.gate["id"], "actual_count": 20, "admitted_at": "t1"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 11, "admitted_at": "t2"},
            )

    def test_cancel_returns_hold_and_keeps_source_occupancy(self):
        order = self._create_order(30)
        cancelled = self.service.transition(
            self.coordinator, order["id"], "cancel", {"reason": "rerouted elsewhere"}
        )
        self.assertEqual(cancelled["status"], "cancelled")
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["reserved"], 0)
        self.assertEqual(quota["remaining"], 100)
        self.assertEqual(self.service.get(self.source["id"])["data"]["current_occupancy"], 0)
        # 已关闭的单不能再放行或取消
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 1, "admitted_at": "t1"},
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, order["id"], "cancel", {"reason": "again"}
            )

    def test_partial_release_then_cancel_returns_remaining_hold(self):
        order = self._create_order(30)
        self.service.transition(
            self.operator,
            order["id"],
            "release",
            {"gate_id": self.gate["id"], "actual_count": 10, "admitted_at": "t1"},
        )
        self.service.transition(
            self.coordinator, order["id"], "cancel", {"reason": "flow restored"}
        )
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["current_occupancy"], 10)
        self.assertEqual(quota["reserved"], 0)
        self.assertEqual(quota["remaining"], 90)

    def test_expiry_returns_hold(self):
        order = self._create_order(30, expires_in_seconds=300)
        # 未过期：放行有效
        self.now += timedelta(seconds=299)
        self.service.list("diversion")
        self.assertEqual(self.service.get(order["id"])["status"], "reserved")
        # 过期：惰性扫描归还占用，放行被拒绝
        self.now += timedelta(seconds=2)
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["remaining"], 100)
        self.assertEqual(quota["reserved"], 0)
        self.assertEqual(self.service.get(order["id"])["status"], "expired")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 1, "admitted_at": "late"},
            )

    def test_expired_hold_can_be_resold(self):
        order = self._create_order(80, expires_in_seconds=100)
        self.now += timedelta(seconds=101)
        replacement = self._create_order(100)
        self.assertEqual(replacement["status"], "reserved")
        self.assertEqual(self.service.zone_quota(self.dest["id"])["remaining"], 0)

    def test_release_through_closed_gate_rejected(self):
        order = self._create_order(10)
        self.service.transition(self.coordinator, self.gate["id"], "close", {"reason": "night"})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 5, "admitted_at": "t1"},
            )

    def test_roles(self):
        order = self._create_order(10)
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.operator,
                "diversion",
                {
                    "venue_id": self.venue["id"],
                    "source_zone_id": self.source["id"],
                    "destination_zone_id": self.dest["id"],
                    "reserve_count": 1,
                    "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
                    "reason": "x",
                },
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer,
                order["id"],
                "release",
                {"gate_id": self.gate["id"], "actual_count": 1, "admitted_at": "t1"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator, order["id"], "cancel", {"reason": "no"})

    def test_concurrent_orders_cannot_overbook(self):
        errors = []
        barrier = threading.Barrier(2)

        def worker(count):
            try:
                barrier.wait()
                self._create_order(count)
            except Exception as exc:  # noqa: BLE001 - assert on the collected type
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(60,))
        t2 = threading.Thread(target=worker, args=(60,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        quota = self.service.zone_quota(self.dest["id"])
        self.assertEqual(quota["reserved"], 60)
        self.assertEqual(quota["remaining"], 40)


if __name__ == "__main__":
    unittest.main()
