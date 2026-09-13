"""后台任务编排：统一创建、周期执行、异常隔离与优雅退出。

AstrBot 没有定时任务装饰器，插件需在 initialize() 里起任务、terminate() 里取消。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TaskSpec:
    """一个周期任务的定义。"""

    name: str
    factory: Callable[[], Awaitable[None]]
    interval: float
    run_immediately: bool = False
    jitter: float = 0.1


@dataclass
class TaskState:
    """任务运行状态（供 WebUI 展示）。"""

    name: str
    interval: float
    runs: int = 0
    failures: int = 0
    last_started: int = 0
    last_finished: int = 0
    last_error: str = ""
    running: bool = False
    history: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval": self.interval,
            "runs": self.runs,
            "failures": self.failures,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_error": self.last_error,
            "running": self.running,
        }


class TaskScheduler:
    """极简周期任务调度器（间隔固定，失败不中断）。"""

    def __init__(
        self,
        *,
        logger: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.logger = logger
        self._clock = clock
        self._sleep = sleep
        self._specs: dict[str, TaskSpec] = {}
        self._states: dict[str, TaskState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    def add(self, spec: TaskSpec) -> None:
        """注册一个任务（start 之前调用）。"""
        self._specs[spec.name] = spec
        self._states.setdefault(spec.name, TaskState(name=spec.name, interval=spec.interval))

    def remove(self, name: str) -> None:
        self._specs.pop(name, None)

    async def start(self) -> None:
        """启动全部已注册任务。"""
        if self._running:
            return
        self._running = True
        for name, spec in self._specs.items():
            self._tasks[name] = asyncio.create_task(self._loop(spec), name=f"qqgm-task-{name}")
        if self.logger is not None and self._specs:
            self.logger.info("已启动 %d 个后台任务：%s", len(self._specs), ", ".join(self._specs))

    async def stop(self) -> None:
        """取消全部任务并等待退出（最多 5 秒）。"""
        self._running = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                pass
        self._tasks.clear()

    async def _loop(self, spec: TaskSpec) -> None:
        state = self._states.setdefault(
            spec.name, TaskState(name=spec.name, interval=spec.interval)
        )
        if not spec.run_immediately:
            await self._sleep(self._next_delay(spec))
        while self._running:
            state.running = True
            state.runs += 1
            state.last_started = int(self._clock())
            try:
                await spec.factory()
                state.last_error = ""
            except asyncio.CancelledError:
                state.running = False
                raise
            except Exception as exc:  # pragma: no cover - 单个任务失败不影响其它任务
                state.failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                if self.logger is not None:
                    self.logger.error("后台任务 %s 执行失败：%s", spec.name, exc, exc_info=True)
            finally:
                state.last_finished = int(self._clock())
                state.running = False
            await self._sleep(self._next_delay(spec))

    def _next_delay(self, spec: TaskSpec) -> float:
        base = max(1.0, float(spec.interval))
        if spec.jitter <= 0:
            return base
        return base * (1.0 + random.uniform(-spec.jitter, spec.jitter))

    async def run_once(self, name: str) -> bool:
        """立即执行一次某个任务（WebUI/指令手动触发）。"""
        spec = self._specs.get(name)
        if spec is None:
            return False
        state = self._states.setdefault(
            spec.name, TaskState(name=spec.name, interval=spec.interval)
        )
        state.runs += 1
        state.last_started = int(self._clock())
        try:
            await spec.factory()
            state.last_error = ""
            return True
        except Exception as exc:
            state.failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            if self.logger is not None:
                self.logger.error("任务 %s 手动执行失败：%s", name, exc, exc_info=True)
            return False
        finally:
            state.last_finished = int(self._clock())

    def states(self) -> list[dict[str, Any]]:
        """返回全部任务状态（供 WebUI）。"""
        return [state.to_dict() for state in self._states.values()]

    @property
    def running(self) -> bool:
        return self._running
