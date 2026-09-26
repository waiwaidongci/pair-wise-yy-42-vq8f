from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS hotspots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    ticket_no TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    surface_temperature REAL NOT NULL,
                    smoke_state TEXT NOT NULL,
                    registered_by TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    latest_temperature REAL,
                    latest_smoke TEXT,
                    cooling_detected_at TEXT,
                    cooling_observed_by TEXT,
                    cooling_recorded_at TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_hotspots_ticket_no
                    ON hotspots(item_id, ticket_no);
                CREATE INDEX IF NOT EXISTS ix_hotspots_item_line
                    ON hotspots(item_id, line_id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 火场看守：热点归档 ----
    @staticmethod
    def _hotspot(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def insert_hotspot(self, item_id: int, ticket_no: str, line_id: str,
                       detected_at: str, surface_temperature: float,
                       smoke_state: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO hotspots(item_id, ticket_no, line_id, detected_at,
                       surface_temperature, smoke_state, registered_by, registered_at,
                       latest_temperature, latest_smoke)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, ticket_no, line_id, detected_at, surface_temperature,
                     smoke_state, actor, now, surface_temperature, smoke_state),
                )
                hotspot_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("现场单号已登记") from exc
        return self.get_hotspot(hotspot_id)

    def get_hotspot(self, hotspot_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM hotspots WHERE id=?", (hotspot_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("热点不存在")
        return self._hotspot(row)

    def get_hotspot_by_ticket(self, item_id: int, ticket_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM hotspots WHERE item_id=? AND ticket_no=?",
                (item_id, ticket_no),
            ).fetchone()
        return self._hotspot(row) if row is not None else None

    def list_hotspots(self, item_id: int, line_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM hotspots WHERE item_id=?"
        params: tuple = (item_id,)
        if line_id is not None:
            sql += " AND line_id=?"
            params = (item_id, line_id)
        sql += " ORDER BY line_id, detected_at, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._hotspot(row) for row in rows]

    def add_cooling_observation(self, hotspot_id: int, detected_at: str,
                                temperature: float, smoke_state: str,
                                observer: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE hotspots SET latest_temperature=?, latest_smoke=?,
                   cooling_detected_at=?, cooling_observed_by=?, cooling_recorded_at=?,
                   reviewed_by=NULL, reviewed_at=NULL
                   WHERE id=?""",
                (temperature, smoke_state, detected_at, observer, now, hotspot_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("热点不存在")
        return self.get_hotspot(hotspot_id)

    def review_hotspot(self, hotspot_id: int, reviewer: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE hotspots SET reviewed_by=?, reviewed_at=? WHERE id=?",
                (reviewer, now, hotspot_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("热点不存在")
        return self.get_hotspot(hotspot_id)

    def correct_hotspot(self, hotspot_id: int, surface_temperature: float,
                        smoke_state: str, line_id: str) -> Dict[str, Any]:
        """更正登记信息；最高温与待复测点由汇总逻辑按新值重算，不落地缓存。
        登记温度或烟点变化时，基于旧登记的降温观测与复核一并作废，须重新复核。"""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT surface_temperature, smoke_state FROM hotspots WHERE id=?",
                (hotspot_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("热点不存在")
            invalidate = (float(row["surface_temperature"]) != float(surface_temperature)
                          or row["smoke_state"] != smoke_state)
            if invalidate:
                self.conn.execute(
                    """UPDATE hotspots SET surface_temperature=?, smoke_state=?, line_id=?,
                       latest_temperature=?, latest_smoke=?, cooling_detected_at=NULL,
                       cooling_observed_by=NULL, cooling_recorded_at=NULL,
                       reviewed_by=NULL, reviewed_at=NULL WHERE id=?""",
                    (surface_temperature, smoke_state, line_id,
                     surface_temperature, smoke_state, hotspot_id),
                )
            else:
                self.conn.execute(
                    "UPDATE hotspots SET line_id=? WHERE id=?",
                    (line_id, hotspot_id),
                )
        return self.get_hotspot(hotspot_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
