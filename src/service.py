from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import (
    DIVERSION_ACTIVE_STATUSES,
    DIVERSION_ADMIT_ROLES,
    DIVERSION_CANCEL_ROLES,
    DIVERSION_CREATE_ROLES,
    DIVERSION_KIND,
    RuleEngine,
    active_diversion_hold,
    diversion_is_expired,
    diversion_remaining,
    parse_timestamp,
)


class ExpiredDiversion(ConflictError):
    """A diversion order reached its expiry time and was released."""


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _now(self):
        return self.clock()

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------
    # Generic entities
    # ------------------------------------------------------------------
    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == DIVERSION_KIND:
            return self.create_diversion(actor, data, idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
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
        return self._with_view(entity)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == DIVERSION_KIND:
            return self.diversion_action(
                actor,
                entity_id,
                action,
                data or {},
                expected_version=expected_version,
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
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
        return self._with_view(updated)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._with_view(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        return [self._with_view(entity) for entity in items]

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # Read models: remaining headcount, held capacity
    # ------------------------------------------------------------------
    def _with_view(self, entity):
        now = self._now()
        if entity["kind"] == DIVERSION_KIND:
            data = dict(entity["data"])
            data["remaining_count"] = diversion_remaining(entity)
            data["is_expired"] = diversion_is_expired(entity, now)
            return dict(entity, data=data)
        if entity["kind"] == "zone":
            capacity = int(entity["data"].get("capacity", 0))
            occupancy = int(entity["data"].get("current_occupancy", 0))
            ceiling = capacity
            if entity["status"] == "limited":
                ceiling = min(capacity, int(entity["data"].get("admit_limit", capacity)))
            held = active_diversion_hold(self._lookup, entity["id"], now)
            data = dict(entity["data"])
            data["held_count"] = held
            data["available_count"] = max(ceiling - occupancy - held, 0)
            return dict(entity, data=data)
        return entity

    # ------------------------------------------------------------------
    # Diversion orders (区域疏导单)
    # ------------------------------------------------------------------
    @staticmethod
    def _require_actor(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require_fields(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def _expire_due_conn(self, connection, diversion, events):
        """Mark a due diversion as expired inside an open transaction."""
        merged = dict(diversion["data"])
        updated = self.repository.conn_update(
            connection, diversion["id"], diversion["version"], "expired", merged
        )
        events.append(
            {
                "kind": "expired",
                "id": updated["id"],
                "from_status": diversion["status"],
                "remaining": diversion_remaining(diversion),
            }
        )
        return updated

    def _holds_conn(self, connection, zone_id, now):
        total = 0
        rows = connection.execute(
            "SELECT * FROM entities WHERE kind = ? ORDER BY id", (DIVERSION_KIND,)
        ).fetchall()
        for row in rows:
            diversion = self.repository._entity_from_row(row)
            if diversion["data"].get("target_zone_id") != zone_id:
                continue
            if diversion["status"] not in DIVERSION_ACTIVE_STATUSES:
                continue
            if diversion_is_expired(diversion, now):
                continue
            total += diversion_remaining(diversion)
        return total

    def _diversion_events_audit(self, actor, events):
        for event in events:
            if event["kind"] == "expired":
                self.audit.record(
                    event["id"],
                    actor,
                    "expire",
                    event["from_status"],
                    "expired",
                    {"released_count": event["remaining"], "auto": True},
                )

    def create_diversion(self, actor, data, idempotency_key=None):
        self._require_actor(actor, DIVERSION_CREATE_ROLES)
        payload = dict(data or {})
        self._require_fields(
            payload, ("venue_id", "source_zone_id", "target_zone_id", "requested_count")
        )
        try:
            requested = int(payload["requested_count"])
        except (TypeError, ValueError):
            raise ValidationError("requested_count must be an integer")
        if requested <= 0:
            raise ValidationError("requested_count must be positive")
        source_id = payload["source_zone_id"]
        target_id = payload["target_zone_id"]
        if source_id == target_id:
            raise ValidationError("source and target zones must differ")
        expires_at = payload.get("expires_at")
        expire_dt = parse_timestamp(expires_at, "expires_at") if expires_at else None
        now = self._now()
        if expire_dt and expire_dt <= now:
            raise ValidationError("expires_at is already in the past")

        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return self._with_view(entity)

        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)

        with self.repository.transaction() as connection:
            venue = self.repository.conn_get(connection, payload["venue_id"])
            if not venue or venue["kind"] != "venue":
                raise ValidationError("venue does not exist")
            source = self.repository.conn_get(connection, source_id)
            target = self.repository.conn_get(connection, target_id)
            if (
                not source
                or source["kind"] != "zone"
                or source["data"].get("venue_id") != venue["id"]
            ):
                raise ValidationError("source zone must belong to the venue")
            if (
                not target
                or target["kind"] != "zone"
                or target["data"].get("venue_id") != venue["id"]
            ):
                raise ValidationError("target zone must belong to the venue")
            if target["status"] in ("closed", "evacuating"):
                raise ConflictError("target zone is not receiving crowds")

            held = self._holds_conn(connection, target_id, now)
            capacity = int(target["data"]["capacity"])
            occupancy = int(target["data"].get("current_occupancy", 0))
            if target["status"] == "limited":
                capacity = min(capacity, int(target["data"].get("admit_limit", capacity)))
            if occupancy + held + requested > capacity:
                raise ConflictError(
                    "target zone remaining capacity is %s"
                    % max(capacity - occupancy - held, 0)
                )

            diversion_data = {
                "venue_id": venue["id"],
                "source_zone_id": source_id,
                "target_zone_id": target_id,
                "requested_count": requested,
                "admitted_count": 0,
                "gate_id": None,
            }
            if expire_dt:
                diversion_data["expires_at"] = expires_at
            diversion = self.repository.conn_create(
                connection, entity_id, DIVERSION_KIND, "reserved", diversion_data, actor.user_id
            )

        self.audit.record(
            diversion["id"],
            actor,
            "create",
            None,
            "reserved",
            {
                "kind": DIVERSION_KIND,
                "source_zone_id": source_id,
                "target_zone_id": target_id,
                "requested_count": requested,
                "held_count": requested,
            },
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, diversion["id"])
        return self._with_view(diversion)

    def diversion_action(self, actor, diversion_id, action, data, expected_version=None):
        if action == "admit":
            return self._diversion_admit(actor, diversion_id, data, expected_version)
        if action == "cancel":
            return self._diversion_cancel(actor, diversion_id, data, expected_version)
        if action == "expire":
            return self._diversion_expire(actor, diversion_id, expected_version)
        raise InvalidTransition("unknown action %s for %s" % (action, DIVERSION_KIND))

    def _diversion_admit(self, actor, diversion_id, data, expected_version):
        self._require_actor(actor, DIVERSION_ADMIT_ROLES)
        self._require_fields(data, ("gate_id", "count", "admitted_at"))
        try:
            count = int(data["count"])
        except (TypeError, ValueError):
            raise ValidationError("admission count must be an integer")
        if count <= 0:
            raise ValidationError("admission count must be positive")

        now = self._now()
        events = []
        expired_held = None
        with self.repository.transaction() as connection:
            diversion = self.repository.conn_get(connection, diversion_id)
            if not diversion or diversion["kind"] != DIVERSION_KIND:
                raise NotFoundError("diversion not found: " + diversion_id)
            expected = (
                int(expected_version) if expected_version is not None else diversion["version"]
            )
            if diversion["status"] not in DIVERSION_ACTIVE_STATUSES:
                raise InvalidTransition(
                    "cannot admit from status %s" % diversion["status"]
                )
            if diversion_is_expired(diversion, now):
                self._expire_due_conn(connection, diversion, events)
                expired_held = diversion_remaining(diversion)
            else:
                remaining = diversion_remaining(diversion)
                if count > remaining:
                    raise ConflictError(
                        "admission %s exceeds reserved remaining %s" % (count, remaining)
                    )
                gate = self.repository.conn_get(connection, data["gate_id"])
                if not gate or gate["kind"] != "gate" or gate["status"] != "open":
                    raise ConflictError("entry gate is not open")
                target = self.repository.conn_get(
                    connection, diversion["data"]["target_zone_id"]
                )
                if diversion["data"]["target_zone_id"] not in (
                    gate["data"].get("zone_ids") or []
                ):
                    raise ValidationError("gate does not serve the target zone")
                if target["status"] not in ("open", "limited"):
                    raise ConflictError("target zone is not receiving crowds")

                occupancy = int(target["data"].get("current_occupancy", 0))
                capacity = int(target["data"]["capacity"])
                limit = capacity
                if target["status"] == "limited":
                    limit = int(target["data"].get("admit_limit", capacity))
                # This order's remaining hold is replaced by admitted people.
                held_other = self._holds_conn(connection, target["id"], now) - remaining
                if occupancy + held_other + count > min(capacity, limit):
                    raise ConflictError("target zone capacity would be exceeded")

                zone_data = dict(target["data"])
                zone_data["current_occupancy"] = occupancy + count
                zone_data["last_admission_at"] = data.get("admitted_at")
                zone_data["last_gate_id"] = gate["id"]
                self.repository.conn_update(
                    connection, target["id"], None, target["status"], zone_data
                )

                diversion_data = dict(diversion["data"])
                diversion_data["admitted_count"] = (
                    int(diversion_data.get("admitted_count", 0)) + count
                )
                diversion_data["gate_id"] = gate["id"]
                diversion_data["last_admitted_at"] = data.get("admitted_at")
                next_status = "active" if diversion["status"] == "reserved" else diversion["status"]
                if diversion_data["admitted_count"] >= int(diversion_data["requested_count"]):
                    next_status = "completed"
                updated = self.repository.conn_update(
                    connection, diversion_id, expected, next_status, diversion_data
                )

        self._diversion_events_audit(actor, events)
        if expired_held is not None:
            raise ExpiredDiversion("diversion order has expired")
        self.audit.record(
            target["id"],
            actor,
            "diversion_admit",
            target["status"],
            target["status"],
            {
                "diversion_id": diversion_id,
                "gate_id": data["gate_id"],
                "count": count,
            },
        )
        self.audit.record(
            diversion_id,
            actor,
            "admit",
            diversion["status"],
            updated["status"],
            {"count": count, "gate_id": data["gate_id"]},
        )
        return self._with_view(updated)

    def _diversion_cancel(self, actor, diversion_id, data, expected_version):
        self._require_actor(actor, DIVERSION_CANCEL_ROLES)
        reason = (data or {}).get("reason")
        if not reason:
            raise ValidationError("cancellation reason is required")
        with self.repository.transaction() as connection:
            diversion = self.repository.conn_get(connection, diversion_id)
            if not diversion or diversion["kind"] != DIVERSION_KIND:
                raise NotFoundError("diversion not found: " + diversion_id)
            expected = (
                int(expected_version) if expected_version is not None else diversion["version"]
            )
            if diversion["status"] not in DIVERSION_ACTIVE_STATUSES:
                raise InvalidTransition(
                    "cannot cancel from status %s" % diversion["status"]
                )
            released = diversion_remaining(diversion)
            diversion_data = dict(diversion["data"])
            diversion_data["cancel_reason"] = reason
            updated = self.repository.conn_update(
                connection, diversion_id, expected, "cancelled", diversion_data
            )
        self.audit.record(
            diversion_id,
            actor,
            "cancel",
            diversion["status"],
            "cancelled",
            {"released_count": released, "reason": reason},
        )
        return self._with_view(updated)

    def _diversion_expire(self, actor, diversion_id, expected_version):
        self._require_actor(actor, DIVERSION_CANCEL_ROLES)
        now = self._now()
        with self.repository.transaction() as connection:
            diversion = self.repository.conn_get(connection, diversion_id)
            if not diversion or diversion["kind"] != DIVERSION_KIND:
                raise NotFoundError("diversion not found: " + diversion_id)
            expected = (
                int(expected_version) if expected_version is not None else diversion["version"]
            )
            if diversion["status"] not in DIVERSION_ACTIVE_STATUSES:
                raise InvalidTransition(
                    "cannot expire from status %s" % diversion["status"]
                )
            if not diversion_is_expired(diversion, now):
                raise ConflictError("diversion order has not reached expires_at")
            released = diversion_remaining(diversion)
            updated = self.repository.conn_update(
                connection, diversion_id, expected, "expired", dict(diversion["data"])
            )
        self.audit.record(
            diversion_id,
            actor,
            "expire",
            diversion["status"],
            "expired",
            {"released_count": released, "auto": False},
        )
        return self._with_view(updated)

    def expire_due_diversions(self, actor=None, now=None):
        """Release every active diversion whose expires_at has passed."""
        now = now or self._now()
        expired = []
        with self.repository.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = ? ORDER BY created_at, id",
                (DIVERSION_KIND,),
            ).fetchall()
            for row in rows:
                diversion = self.repository._entity_from_row(row)
                if (
                    diversion["status"] in DIVERSION_ACTIVE_STATUSES
                    and diversion_is_expired(diversion, now)
                ):
                    updated = self.repository.conn_update(
                        connection,
                        diversion["id"],
                        diversion["version"],
                        "expired",
                        dict(diversion["data"]),
                    )
                    released = diversion_remaining(diversion)
                    expired.append((diversion, updated, released))
        if actor:
            for diversion, _updated, released in expired:
                self.audit.record(
                    diversion["id"],
                    actor,
                    "expire",
                    diversion["status"],
                    "expired",
                    {"released_count": released, "auto": True},
                )
        return [self._with_view(item[1]) for item in expired]
