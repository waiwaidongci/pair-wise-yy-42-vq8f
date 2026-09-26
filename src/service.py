from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     normalize_smoke_status, require_number, require_text,
                     require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, HOTSPOT_ROLES,
                    MOPUP_STATE, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, effective_surface_temp,
                    escalation_required, fireline_summary, guard_blockers,
                    priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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
            from .domain import ValidationError
            raise ValidationError("status必须是open或closed")
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
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        blockers += guard_blockers(target, self.repository.list_hotspots(item_id))
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

    def _ensure_mopup(self, item_id: int) -> Dict[str, Any]:
        # 看守登记从火场转入看守（contained）后开始
        item = self.repository.get_item(item_id)
        if item["status"] != MOPUP_STATE:
            raise ConflictError("火场尚未转入看守阶段，不能登记看守热点")
        return item

    def register_hotspot(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_mopup(item_id)
        ticket_no = require_text(payload.get("ticket_no"), "ticket_no", 100)
        fireline = require_text(payload.get("fireline"), "fireline", 100)
        detected_at = require_timestamp(payload.get("detected_at"), "detected_at")
        surface_temp = require_number(payload.get("surface_temp"), "surface_temp", -100.0)
        smoke_status = normalize_smoke_status(payload.get("smoke_status"))
        hotspot = self.repository.insert_hotspot(
            item_id, ticket_no, fireline, detected_at, surface_temp,
            smoke_status, actor)
        replayed = hotspot is None
        if replayed:
            # 同号重放：原样返回首条，不重复计数、不覆盖原记录
            hotspot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        else:
            self.repository.append_audit("hotspot_register", ENTITY, item_id, actor, {
                "ticket_no": ticket_no, "fireline": fireline,
                "detected_at": detected_at, "surface_temp": surface_temp,
                "smoke_status": smoke_status,
            })
        result = self._hotspot_view(hotspot)
        result["replayed"] = replayed
        return result

    def record_cooling(self, item_id: int, ticket_no: str, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_mopup(item_id)
        observed_at = require_timestamp(payload.get("observed_at"), "observed_at")
        surface_temp = require_number(payload.get("surface_temp"), "surface_temp", -100.0)
        hotspot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if hotspot["cooling_temp"] is not None:
            raise ConflictError("该热点已存在降温观测，原记录保留不可覆盖")
        if observed_at <= hotspot["detected_at"]:
            raise ConflictError("降温观测时刻必须晚于探测时刻")
        self.repository.record_cooling(hotspot["id"], observed_at, surface_temp, actor)
        self.repository.append_audit("hotspot_cooling", ENTITY, item_id, actor, {
            "ticket_no": ticket_no, "observed_at": observed_at,
            "surface_temp": surface_temp,
        })
        return self._hotspot_view(self.repository.get_hotspot(hotspot["id"]))

    def review_cooling(self, item_id: int, ticket_no: str, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_mopup(item_id)
        review_at = require_timestamp(payload.get("review_at"), "review_at")
        confirmed_temp = require_number(payload.get("confirmed_temp"), "confirmed_temp", -100.0)
        hotspot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if hotspot["cooling_temp"] is None:
            raise ConflictError("该热点尚无降温观测，无法复核")
        if hotspot["review_by"]:
            raise ConflictError("降温结果已复核，不能重复复核")
        # 复核必须由另一名巡线员完成（与登记人、降温观测人均不同）
        others = {hotspot["created_by"], hotspot.get("cooling_by")}
        if actor in others:
            raise ConflictError("降温结果必须由另一名巡线员复核")
        self.repository.review_cooling(hotspot["id"], review_at, confirmed_temp, actor)
        self.repository.append_audit("hotspot_review", ENTITY, item_id, actor, {
            "ticket_no": ticket_no, "review_at": review_at,
            "confirmed_temp": confirmed_temp,
        })
        return self._hotspot_view(self.repository.get_hotspot(hotspot["id"]))

    def retest_smoke(self, item_id: int, ticket_no: str, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_mopup(item_id)
        retest_at = require_timestamp(payload.get("retest_at"), "retest_at")
        smoke_status = normalize_smoke_status(payload.get("smoke_status"))
        hotspot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if not hotspot["cooling_temp"]:
            raise ConflictError("应先完成降温观测再复测烟点")
        if retest_at <= hotspot["detected_at"]:
            raise ConflictError("复测时刻必须晚于探测时刻")
        self.repository.retest_smoke(hotspot["id"], retest_at, smoke_status, actor)
        self.repository.append_audit("hotspot_smoke_retest", ENTITY, item_id, actor, {
            "ticket_no": ticket_no, "retest_at": retest_at,
            "smoke_status": smoke_status,
        })
        return self._hotspot_view(self.repository.get_hotspot(hotspot["id"]))

    def correct_hotspot(self, item_id: int, ticket_no: str, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOTSPOT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_mopup(item_id)
        hotspot = self.repository.get_hotspot_by_ticket(item_id, ticket_no)
        if hotspot["review_by"]:
            raise ConflictError("降温结果已复核，不能再更正热点")
        fields: Dict[str, Any] = {}
        before: Dict[str, Any] = {}
        if "surface_temp" in payload:
            fields["surface_temp"] = require_number(payload.get("surface_temp"), "surface_temp", -100.0)
            before["surface_temp"] = hotspot["surface_temp"]
        if "smoke_status" in payload:
            fields["smoke_status"] = normalize_smoke_status(payload.get("smoke_status"))
            before["smoke_status"] = hotspot["smoke_status"]
        if "fireline" in payload:
            fields["fireline"] = require_text(payload.get("fireline"), "fireline", 100)
            before["fireline"] = hotspot["fireline"]
        if not fields:
            from .domain import ValidationError
            raise ValidationError("没有可更正的字段")
        self.repository.correct_hotspot(hotspot["id"], fields)
        # 原始值随审计保留；火线最高温和待复测点按新值自然重算
        self.repository.append_audit("hotspot_correct", ENTITY, item_id, actor, {
            "ticket_no": ticket_no, "before": before, "after": fields,
        })
        return self._hotspot_view(self.repository.get_hotspot(hotspot["id"]))

    def list_hotspots(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        hotspots = self.repository.list_hotspots(item_id)
        views = [self._hotspot_view(h) for h in hotspots]
        return {"hotspots": views, "firelines": fireline_summary(hotspots)}

    @staticmethod
    def _hotspot_view(hotspot: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(hotspot)
        result["effective_temp"] = effective_surface_temp(hotspot)
        result["smoke_retest_pending"] = (
            hotspot.get("smoke_status") == "smoking"
            and not hotspot.get("smoke_retest_status"))
        return result

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        hotspots = self.repository.list_hotspots(item["id"])
        result["firelines"] = fireline_summary(hotspots)
        result["guard_blockers"] = guard_blockers("controlled", hotspots)
        return result
