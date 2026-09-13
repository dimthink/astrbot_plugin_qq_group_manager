"""测试替身：FakeTransport / FakeKV。"""

from __future__ import annotations

from typing import Any


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
