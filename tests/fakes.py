"""测试替身：FakeTransport / FakeKV。"""

from __future__ import annotations

from typing import Any

from src.utils import now_ts


class FakeTransport:
    """可编程的 QQ API 传输层。

    routes: {(method, path): payload | Exception | callable}
    未命中的路由返回 {}。
    """

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = dict(routes or {})
        for key in list(self.routes):
            if self.routes[key] is None:  # 显式表达"返回 None"
                self.routes[key] = lambda *args, **kwargs: None
        self.calls: list[dict[str, Any]] = []
        self.available = True

    async def request(
        self,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "path_params": dict(path_params or {}),
                "query": dict(query or {}),
                "json": json_body,
            }
        )
        if (method, path) not in self.routes:
            return {}
        handler = self.routes[(method, path)]
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(path_params or {}, query or {}, json_body)
        return handler

    def calls_for(self, method: str, path: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == method and call["path"] == path]


class FakeKV:
    """内存版插件 KV。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})
        self.writes: list[str] = []
        self.fail_keys: set[str] = set()

    async def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    async def put(self, key: str, value: Any) -> None:
        if key in self.fail_keys:
            raise RuntimeError("kv write failed")
        self.data[key] = value
        self.writes.append(key)


class RaisingError(Exception):
    """模拟 botpy.errors.ServerError（按类名匹配）。"""

    def __init__(self, message: str = "server error", *, name: str = "ServerError") -> None:
        super().__init__(message)
        self.__class__.__name__ = name


class FakeAudit:
    """内存版审计库替身：记录调用并按需返回预置数据。"""

    def __init__(self, *, events: list | None = None, joins: dict | None = None) -> None:
        self.events = list(events or [])
        self.joins = dict(joins or {})
        self.api_calls: list[dict] = []
        self.capabilities: list[dict] = []
        self.actions: list[dict] = []
        self.mutes: dict[tuple[str, str], dict] = {}
        self.appeals: list[dict] = []
        self.whitelist: dict[str, dict] = {}
        self.next_event_id = 1

    async def insert_event(self, **payload):
        event_id = self.next_event_id
        self.next_event_id += 1
        record = dict(payload)
        record["id"] = event_id
        self.events.append(record)
        return event_id

    def record_action(self, **payload):
        self.actions.append(payload)
        return True

    def record_api_call(self, **payload):
        self.api_calls.append(payload)
        return True

    def record_capability(self, **payload):
        self.capabilities.append(payload)
        return True

    async def record_join(self, **payload):
        key = str(payload.get("join_request_id") or "")
        self.joins[key] = dict(payload)

    async def get_join(self, join_request_id):
        return self.joins.get(join_request_id)

    async def list_joins(self, group_id=None, *, decision="", limit=50):
        rows = list(self.joins.values())
        if group_id:
            rows = [row for row in rows if row.get("group_id") == group_id]
        if decision:
            rows = [row for row in rows if row.get("decision") == decision]
        return rows[:limit]

    async def upsert_mute(self, **payload):
        key = (str(payload.get("group_id")), str(payload.get("member_openid")))
        self.mutes[key] = dict(payload)

    async def set_mute_active(self, group_id, member_openid, active):
        key = (group_id, member_openid)
        if key in self.mutes:
            self.mutes[key]["active"] = active

    async def list_mutes(self, group_id=None, *, active_only=True):
        rows = [dict(row) for row in self.mutes.values()]
        if group_id:
            rows = [row for row in rows if row.get("group_id") == group_id]
        if active_only:
            rows = [row for row in rows if row.get("active", True)]
        return rows

    def _find(self, event_id):
        for row in self.events:
            if int(row.get("id") or 0) == int(event_id or 0):
                return row
        return None

    async def find_last_event(self, group_id, member_openid):
        for row in reversed(self.events):
            if row.get("group_id") == group_id and row.get("sender_openid") == member_openid:
                return row
        return None

    async def find_event_by_msg_id(self, group_id, msg_id):
        if not msg_id:
            return None
        for row in reversed(self.events):
            if row.get("group_id") == group_id and row.get("msg_id") == msg_id:
                return row
        return None

    async def get_event(self, event_id):
        return self._find(event_id)

    async def event_has_action(self, event_id, action="mute"):
        return any(
            int(item.get("event_id") or 0) == int(event_id or 0)
            and item.get("action") == action
            and item.get("ok", True)
            for item in self.actions
        )

    async def create_appeal(self, event_id, text):
        row = self._find(event_id)
        if row is None:
            return False
        row["appealed"] = 1
        row["appeal_text"] = text
        row["appeal_state"] = "pending"
        self.appeals.append({"event_id": event_id, "text": text, "state": "pending"})
        return True

    async def mark_appeal(self, event_id, text, *, state="pending"):
        row = self._find(event_id)
        if row is not None:
            row["appealed"] = 1
            row["appeal_text"] = text
            row["appeal_state"] = state
        self.appeals.append({"event_id": event_id, "text": text, "state": state})
        return True

    async def resolve_appeal(self, event_id, *, accepted, by="system", note=""):
        row = self._find(event_id)
        state = "accepted" if accepted else "rejected"
        if row is None:
            return False
        row["appeal_state"] = state
        row["appeal_by"] = by
        row["appeal_at"] = float(now_ts())
        row["appeal_note"] = note
        self.appeals.append(
            {"event_id": event_id, "text": row.get("appeal_text") or "", "state": state,
             "by": by, "note": note}
        )
        return True

    async def list_appeals(self, *, state="pending", group_id="", days=30, limit=100):
        rows = [row for row in self.events if row.get("appealed")]
        if state and state != "all":
            rows = [row for row in rows if row.get("appeal_state") == state]
        if group_id:
            rows = [row for row in rows if row.get("group_id") == group_id]
        if days and int(days) > 0:
            since = now_ts() - int(days) * 86400
            rows = [row for row in rows if int(row.get("ts_unix") or 0) >= since]
        rows = sorted(rows, key=lambda item: int(item.get("ts_unix") or 0), reverse=True)
        return [dict(row) for row in rows[: max(1, int(limit))]]

    async def whitelist_contains(self, digest):
        return str(digest or "") in self.whitelist

    async def whitelist_add(self, digest, skeleton, reason, by):
        self.whitelist[str(digest)] = {
            "digest": str(digest),
            "skeleton": skeleton,
            "reason": reason,
            "added_by": by,
            "added_at": float(now_ts()),
        }

    async def whitelist_remove(self, digest):
        return self.whitelist.pop(str(digest or ""), None) is not None

    async def whitelist_list(self, limit=200):
        return list(self.whitelist.values())[:limit]

    async def summary_by_category(self, days=7):
        return []

    async def summary_by_group(self, days=7):
        return []

    async def summary(self, days=1):
        return {"days": days, "verdicts": {}, "events_total": 0, "actions": {}}

    async def query_logs(self, kind, **kwargs):
        return {"items": [], "total": 0, "page": 1, "page_size": 20}
