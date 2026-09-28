from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import (
    DIVERSION_ACTIVE_STATUSES,
    RuleEngine,
    diversion_zone_quota,
    parse_timestamp,
)


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _now(self):
        value = self.clock()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "diversion":
            return self._create_diversion(actor, payload, idempotency_key)
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    # ------------------------------------------------------------------
    # 区域疏导单（diversion）
    # ------------------------------------------------------------------
    def _expire_due_orders(self, tx, now, actor_id="system"):
        """Move active orders past their expiry into ``expired`` (lazy sweep)."""
        expired = []
        for order in tx.list_entities(kind="diversion"):
            if order["status"] not in DIVERSION_ACTIVE_STATUSES:
                continue
            if parse_timestamp(order["data"]["expires_at"]) <= now:
                data = dict(order["data"])
                data["closed_at"] = now.isoformat(timespec="seconds")
                tx.update_entity(order["id"], order["version"], "expired", data)
                tx.append_audit(
                    order["id"],
                    actor_id,
                    "system",
                    "expire",
                    order["status"],
                    "expired",
                    {"released_hold": data["reserve_count"] - data["admitted_count"]},
                )
                expired.append(order["id"])
        return expired

    def expire_due(self):
        """Public sweep used by a timer or before reads that need fresh quota."""
        now = self._now()
        with self.repository.transaction() as tx:
            return self._expire_due_orders(tx, now)

    def _create_diversion(self, actor, payload, idempotency_key):
        validated = self.rules.validate_create(actor, "diversion", payload, self._lookup)
        reserve_count = validated["reserve_count"]
        destination_id = payload["destination_zone_id"]
        now = self._now()
        with self.repository.transaction() as tx:
            self._expire_due_orders(tx, now, actor.user_id)
            destination = tx.get_entity(destination_id)
            if not destination:
                raise NotFoundError("entity not found: " + destination_id)
            active = [
                order
                for order in tx.list_entities(kind="diversion")
                if order["status"] in DIVERSION_ACTIVE_STATUSES
                and order["data"].get("destination_zone_id") == destination_id
            ]
            quota = diversion_zone_quota(destination, active)
            if reserve_count > quota["remaining"]:
                raise ConflictError(
                    "destination zone only has %s remaining (capacity %s, occupancy %s, reserved %s)"
                    % (quota["remaining"], quota["capacity"], quota["current_occupancy"], quota["reserved"])
                )
            data = dict(payload)
            data.update(validated)
            entity_id = str(data.pop("id", "") or uuid4())
            if tx.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            data["created_by"] = actor.user_id
            tx.create_entity(entity_id, "diversion", "reserved", data, actor.user_id)
            tx.append_audit(
                entity_id,
                actor.user_id,
                actor.role,
                "create",
                None,
                "reserved",
                {"kind": "diversion", "reserve_count": reserve_count, "destination_zone_id": destination_id},
            )
            order = tx.get_entity(entity_id)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return order

    def _get_diversion(self, order_id):
        order = self.repository.get_entity(order_id)
        if not order:
            raise NotFoundError("entity not found: " + order_id)
        if order["kind"] != "diversion":
            raise InvalidTransition("entity %s is not a diversion" % order_id)
        return order

    @staticmethod
    def _ensure_diversion_role(actor):
        if actor.role not in ("operator", "supervisor", "coordinator", "admin"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def _release_gate(self, tx, data, destination):
        try:
            actual_count = int(data.get("actual_count"))
        except (TypeError, ValueError):
            raise ConflictError("actual_count must be an integer")
        if actual_count <= 0:
            raise ConflictError("actual_count must be positive")
        gate = tx.get_entity(data.get("gate_id"))
        if not gate or gate["kind"] != "gate":
            raise ConflictError("entry gate does not exist")
        if gate["status"] != "open":
            raise ConflictError("entry gate is not open")
        if destination["id"] not in (gate["data"].get("zone_ids") or []):
            raise ConflictError("gate does not serve the destination zone")
        return actual_count

    def _release_diversion(self, actor, order, data):
        if not data.get("admitted_at"):
            raise ConflictError("admitted_at is required")
        self._ensure_diversion_role(actor)
        now = self._now()
        with self.repository.transaction() as tx:
            self._expire_due_orders(tx, now, actor.user_id)
            order = tx.get_entity(order["id"])
            if order["status"] == "expired":
                raise InvalidTransition("diversion has expired")
            if order["status"] not in DIVERSION_ACTIVE_STATUSES:
                raise InvalidTransition("cannot release from status %s" % order["status"])
            if parse_timestamp(order["data"]["expires_at"]) <= now:
                raise InvalidTransition("diversion has expired")
            destination = tx.get_entity(order["data"]["destination_zone_id"])
            actual_count = self._release_gate(tx, data, destination)

            admitted = int(order["data"].get("admitted_count", 0))
            reserve_count = int(order["data"]["reserve_count"])
            if admitted + actual_count > reserve_count:
                raise ConflictError(
                    "admission %s exceeds pre-reserved %s (already admitted %s)"
                    % (actual_count, reserve_count, admitted)
                )

            others = [
                item
                for item in tx.list_entities(kind="diversion")
                if item["id"] != order["id"]
                and item["status"] in DIVERSION_ACTIVE_STATUSES
                and item["data"].get("destination_zone_id") == destination["id"]
            ]
            occupancy = int(destination["data"].get("current_occupancy", 0))
            reserved_by_others = diversion_zone_quota(destination, others)["reserved"]
            if occupancy + reserved_by_others + actual_count > int(destination["data"]["capacity"]):
                raise ConflictError("destination zone capacity would be exceeded")

            order_data = dict(order["data"])
            order_data["admitted_count"] = admitted + actual_count
            order_data["last_admitted_at"] = data["admitted_at"]
            order_data["last_gate_id"] = data["gate_id"]
            released_hold = reserve_count - order_data["admitted_count"]
            next_status = "released" if released_hold == 0 else "partially_released"
            if next_status == "released":
                order_data["closed_at"] = now.isoformat(timespec="seconds")
            tx.update_entity(order["id"], order["version"], next_status, order_data)
            tx.append_audit(
                order["id"],
                actor.user_id,
                actor.role,
                "release",
                order["status"],
                next_status,
                {"actual_count": actual_count, "released_hold": max(0, released_hold)},
            )

            zone_data = dict(destination["data"])
            zone_data["current_occupancy"] = occupancy + actual_count
            zone_data["last_admission_at"] = data["admitted_at"]
            zone_data["last_gate_id"] = data["gate_id"]
            tx.update_entity(destination["id"], destination["version"], destination["status"], zone_data)
            tx.append_audit(
                destination["id"],
                actor.user_id,
                actor.role,
                "admit",
                destination["status"],
                destination["status"],
                {"count": actual_count, "diversion_id": order["id"], "gate_id": data["gate_id"]},
            )
            return tx.get_entity(order["id"])

    def _close_diversion(self, actor, order, action, reason=None):
        if actor.role not in ("supervisor", "coordinator", "admin"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        now = self._now()
        with self.repository.transaction() as tx:
            self._expire_due_orders(tx, now, actor.user_id)
            order = tx.get_entity(order["id"])
            if order["status"] not in DIVERSION_ACTIVE_STATUSES:
                raise InvalidTransition("cannot %s from status %s" % (action, order["status"]))
            data = dict(order["data"])
            data["closed_at"] = now.isoformat(timespec="seconds")
            released_hold = data["reserve_count"] - int(data.get("admitted_count", 0))
            tx.update_entity(order["id"], order["version"], "cancelled", data)
            tx.append_audit(
                order["id"],
                actor.user_id,
                actor.role,
                action,
                order["status"],
                "cancelled",
                {"released_hold": released_hold, "reason": reason},
            )
            return tx.get_entity(order["id"])

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        data = dict(data or {})
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "diversion":
            if expected_version is not None and entity["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, entity["version"])
                )
            if action == "release":
                return self._release_diversion(actor, entity, data)
            if action == "cancel":
                reason = data.get("reason")
                if not reason:
                    raise ValidationError("cancel reason is required")
                return self._close_diversion(actor, entity, "cancel", reason=reason)
            raise InvalidTransition("unknown action %s for diversion" % action)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def zone_quota(self, zone_id):
        """Capacity view with live diversion holds folded in."""
        self.expire_due()
        zone = self.repository.get_entity(zone_id)
        if not zone or zone["kind"] != "zone":
            raise NotFoundError("zone not found: " + zone_id)
        active = [
            order
            for order in self.repository.list_entities(kind="diversion")
            if order["status"] in DIVERSION_ACTIVE_STATUSES
            and order["data"].get("destination_zone_id") == zone_id
        ]
        quota = diversion_zone_quota(zone, active)
        quota["zone_id"] = zone_id
        quota["active_diversion_ids"] = [order["id"] for order in active]
        return quota

    def list(self, kind=None, status=None):
        kind = self.rules.normalize_kind(kind) if kind else kind
        if kind == "diversion":
            self.expire_due()
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
