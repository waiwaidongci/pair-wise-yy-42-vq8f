from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, HOTSPOT_CORRECT_ROLES,
                    HOTSPOT_REGISTER_ROLES, HOTSPOT_REVIEW_ROLES, RECORD_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    line_summary, mopup_blockers_for_target, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_smoke_state, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        blockers += mopup_blockers_for_target(target, self._hotspots_by_line(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 火场看守与复燃判定 ----
    @staticmethod
    def _parse_instant(value: str, field: str) -> datetime:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field}必须是ISO-8601时间") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    def _hotspots_by_line(self, item_id: int) -> Dict[str, list]:
        lines: Dict[str, list] = {}
        for spot in self.repository.list_hotspots(item_id):
            lines.setdefault(spot["line_id"], []).append(spot)
        return lines

    @staticmethod
    def _serialize_hotspot(spot: Dict[str, Any], replay: bool = False) -> Dict[str, Any]:
        result = dict(spot)
        result["replay"] = replay
        return result

    def register_hotspot(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        ticket_no = require_text(payload.get("ticket_no"), "ticket_no", 100)
        line_id = require_text(payload.get("line_id"), "line_id", 100)
        detected_at = require_text(payload.get("detected_at"), "detected_at", 60)
        self._parse_instant(detected_at, "detected_at")
        surface_temperature = require_number(payload.get("surface_temperature"),
                                             "surface_temperature", -273.15)
        smoke_state = validate_smoke_state(payload.get("smoke_state"))
        # 同号重放：离线记录重发时返回首条，保留首次登记。
        existing = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if existing is not None:
            return self._serialize_hotspot(existing, replay=True)
        spot = self.repository.insert_hotspot(
            item_id, ticket_no, line_id, detected_at, surface_temperature,
            smoke_state, actor)
        self.repository.append_audit("hotspot_register", ENTITY, item_id, actor, {
            "hotspot_id": spot["id"], "ticket_no": ticket_no, "line_id": line_id,
            "detected_at": detected_at, "surface_temperature": surface_temperature,
            "smoke_state": smoke_state,
        })
        return self._serialize_hotspot(spot)

    def add_cooling_observation(self, item_id: int, ticket_no: str,
                                payload: Dict[str, Any], actor: str,
                                role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        spot = self._require_hotspot(item_id, ticket_no)
        detected_at = require_text(payload.get("detected_at"), "detected_at", 60)
        if self._parse_instant(detected_at, "detected_at") <= \
                self._parse_instant(spot["detected_at"], "detected_at"):
            raise ValidationError("降温观测必须晚于热点登记的探测时刻")
        temperature = require_number(payload.get("temperature"), "temperature", -273.15)
        if temperature >= float(spot["surface_temperature"]):
            raise ValidationError("降温观测温度必须低于登记地表温度")
        smoke_state = validate_smoke_state(payload.get("smoke_state"))
        # 另一名巡线员复核：观测人不能是原登记人。
        if actor == spot["registered_by"]:
            raise ConflictError("降温观测必须由另一名巡线员执行")
        if spot["cooling_observed_by"] is not None and \
                spot["cooling_observed_by"] != actor:
            raise ConflictError("该热点已有其他巡线员的降温观测")
        updated = self.repository.add_cooling_observation(
            spot["id"], detected_at, temperature, smoke_state, actor)
        self.repository.append_audit("hotspot_cooling", ENTITY, item_id, actor, {
            "hotspot_id": spot["id"], "ticket_no": ticket_no,
            "detected_at": detected_at, "temperature": temperature,
            "smoke_state": smoke_state, "reviewed": False,
        })
        return self._serialize_hotspot(updated)

    def review_hotspot(self, item_id: int, ticket_no: str, actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        spot = self._require_hotspot(item_id, ticket_no)
        if spot["cooling_observed_by"] is None:
            raise ConflictError("尚无降温观测可复核")
        if actor == spot["cooling_observed_by"]:
            raise ConflictError("复核人不能是执行降温观测的巡线员")
        updated = self.repository.review_hotspot(spot["id"], actor)
        self.repository.append_audit("hotspot_review", ENTITY, item_id, actor, {
            "hotspot_id": spot["id"], "ticket_no": ticket_no,
            "observed_by": spot["cooling_observed_by"],
        })
        return self._serialize_hotspot(updated)

    def correct_hotspot(self, item_id: int, ticket_no: str,
                        payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        spot = self._require_hotspot(item_id, ticket_no)
        line_id = require_text(payload.get("line_id", spot["line_id"]), "line_id", 100)
        surface_temperature = require_number(
            payload.get("surface_temperature", spot["surface_temperature"]),
            "surface_temperature", -273.15)
        smoke_state = validate_smoke_state(payload.get("smoke_state", spot["smoke_state"]))
        updated = self.repository.correct_hotspot(
            spot["id"], surface_temperature, smoke_state, line_id)
        invalidated = (surface_temperature != float(spot["surface_temperature"])
                       or smoke_state != spot["smoke_state"])
        self.repository.append_audit("hotspot_correct", ENTITY, item_id, actor, {
            "hotspot_id": spot["id"], "ticket_no": ticket_no,
            "line_id": {"from": spot["line_id"], "to": line_id},
            "surface_temperature": {"from": spot["surface_temperature"],
                                    "to": surface_temperature},
            "smoke_state": {"from": spot["smoke_state"], "to": smoke_state},
            "cooling_revoked": invalidated,
        })
        return self._serialize_hotspot(updated)

    def list_hotspots(self, item_id: int, role: str,
                      line_id: Optional[str] = None) -> list:
        self._view(role)
        return [self._serialize_hotspot(spot)
                for spot in self.repository.list_hotspots(item_id, line_id)]

    def line_status(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        lines = self._hotspots_by_line(item_id)
        result = {}
        for line_id, spots in sorted(lines.items()):
            summary = line_summary(spots)
            summary["hotspots"] = [self._serialize_hotspot(spot) for spot in spots]
            result[line_id] = summary
        blockers = mopup_blockers_for_target("controlled", lines)
        return {"lines": result, "mopup_blockers": blockers,
                "can_declare_controlled": not blockers}

    def _require_hotspot(self, item_id: int, ticket_no: str) -> Dict[str, Any]:
        ticket_no = require_text(ticket_no, "ticket_no", 100)
        spot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if spot is None:
            from .domain import NotFoundError
            raise NotFoundError("热点不存在")
        return spot

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
