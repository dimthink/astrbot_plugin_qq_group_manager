"""审核链路集成测试：用假事件跑真实的规则 → LLM → 处置 → 审计全链路。

这一层专门防"方法名写错 / 属性缺失"这类**只在真实运行路径上才暴露**的错误：
生产环境曾出现 PluginStore.member_first_seen 未实现，导致审核链路一进来就抛异常、
群内既不警告也无任何记录（用户侧表现为"没反应"），而当时的单元测试没有覆盖到。

注意：AuditStore 内部有 asyncio 队列与写协程，所有等待必须发生在**同一个事件循环**里，
因此每个用例都用单个 asyncio.run(scenario()) 包住整个流程。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from src.actions import ActionExecutor
from src.api_client import QQGroupAPI
from src.audit import AuditStore
from src.models import CapabilityResult
from src.moderator import LLMModerator
from src.rules import RuleEngine
from src.store import PluginStore
from tests.fakes import FakeKV, FakeTransport

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

RECALL_PATH = "/v2/groups/{group_openid}/messages/{message_id}"
MUTE_PATH = "/v2/groups/{group_openid}/restrict_chat_setting"

AD_MESSAGE = "加群领资料 https://example.com/abc"
LLM_VIOLATION = (
    '{"verdict":"violation","category":"广告引流","severity":4,'
    '"confidence":0.95,"reason":"含引流链接","suggested_action":"mute_and_recall"}'
)
LLM_ALLOW = '{"verdict":"allow","category":"无","severity":1,"confidence":0.9,"reason":"正常"}'


def load_main():
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    return importlib.import_module(f"{PLUGIN_ROOT.name}.main")


class FakeEvent:
    """模拟 AstrBot 的群消息事件（只实现审核链路用到的接口）。"""

    def __init__(self, text: str, *, group_id: str = "g1", admin: bool = False) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(id="msg-1", attachments=[], author=None),
            group_id=group_id,
            message_id="msg-1",
            group=None,
        )
        self.unified_msg_origin = f"platform:GroupMessage:{group_id}"
        self.sent: list[object] = []
        self.llm_flag: bool | None = None
        self._admin = admin

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "小号"

    def is_admin(self) -> bool:
        return self._admin

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def should_call_llm(self, flag: bool) -> None:
        self.llm_flag = flag


class Chain:
    """一次完整的审核链路运行环境。"""

    def __init__(self, store, audit, event, transport, service, main) -> None:
        self.store = store
        self.audit = audit
        self.event = event
        self.transport = transport
        self.service = service
        self.main = main

    async def close(self) -> None:
        await self.audit.close()

    async def events(self) -> dict:
        await self.audit.flush()
        return await self.audit.query_logs("events")

    async def actions(self) -> dict:
        await self.audit.flush()
        return await self.audit.query_logs("actions")


async def run_chain(
    tmp_path: Path,
    *,
    text: str = AD_MESSAGE,
    mode: str = "lenient",
    dry_run: bool = True,
    llm_response: str | Exception = LLM_VIOLATION,
    admin: bool = False,
    with_capabilities: bool = True,
) -> Chain:
    """在同一个事件循环内跑一次审核链路。"""
    main = load_main()
    store = PluginStore(FakeKV())
    await store.load()
    await store.update_settings({"dry_run": dry_run, "mode": mode, "llm_min_confidence": 0.5})
    await store.ensure_group("g1", name="测试群")
    await store.update_group("g1", {"moderation_enabled": True})
    if with_capabilities:
        await store.set_capabilities(
            "g1",
            {
                "recall": CapabilityResult("recall", True),
                "mute": CapabilityResult("mute", True),
            },
        )

    transport = FakeTransport(
        {("DELETE", RECALL_PATH): {"trace_id": "t1"}, ("POST", MUTE_PATH): {}}
    )
    audit = AuditStore(tmp_path / "audit.db", flush_interval=0.02)
    await audit.initialize()
    api = QQGroupAPI(transport, audit=audit, dry_run_getter=store.dry_run)
    actions = ActionExecutor(api=api, store=store, audit=audit)

    async def provider_call(request, system_prompt, user_prompt):
        del request, system_prompt, user_prompt
        if isinstance(llm_response, Exception):
            raise llm_response
        return llm_response

    moderator = LLMModerator(provider_call, settings_getter=store.settings)

    service = object.__new__(main.QQGroupManager)
    service.store = store
    service.api = api
    service.audit = audit
    service.actions = actions
    service.moderator = moderator
    service.rules = RuleEngine(store.keywords())
    service.logger = logging.getLogger("qqgm-chain-test")
    service._seen_messages = {}
    service._last_provider_id = ""
    service._platform_id = ""

    event = FakeEvent(text, admin=admin)
    await main.QQGroupManager._moderate(
        service,
        event,
        group_id="g1",
        config=store.group("g1"),
        sender_openid="u1",
        sender_name="小号",
        sender_role="member",
    )
    return Chain(store, audit, event, transport, service, main)


def test_chain_records_event_and_warns_in_lenient_dry_run(tmp_path):
    """lenient + dry-run：仍要真的发出警告（非破坏性），但不得撤回/禁言。"""

    async def scenario():
        chain = await run_chain(tmp_path)
        events = await chain.events()
        assert events["total"] == 1
        row = events["items"][0]
        assert row["verdict"] == "violation"
        assert row["category"] == "广告引流"
        assert row["dry_run"] == 1
        assert row["sender_name"] == "小号"
        assert len(chain.event.sent) == 1, "dry-run 期间警告必须真的发出去"
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.transport.calls_for("POST", MUTE_PATH) == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_warn_can_be_silenced_in_dry_run(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path)
        await chain.store.update_settings({"dry_run_warn": False})
        chain.service.actions.stats["skipped"] = 0
        chain.event.sent.clear()
        chain.service._seen_messages.clear()
        await chain.main.QQGroupManager._moderate(
            chain.service,
            chain.event,
            group_id="g1",
            config=chain.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_standard_mode_executes_recall_and_mute(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard", dry_run=False)
        assert len(chain.transport.calls_for("DELETE", RECALL_PATH)) == 1
        assert len(chain.transport.calls_for("POST", MUTE_PATH)) == 1
        actions = await chain.actions()
        assert {"recall", "mute"} <= {item["action"] for item in actions["items"]}
        mutes = await chain.audit.list_mutes("g1")
        assert mutes and mutes[0]["member_openid"] == "u1"
        await chain.close()

    asyncio.run(scenario())


def test_chain_dry_run_blocks_destructive_actions(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, mode="standard", dry_run=True)
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.transport.calls_for("POST", MUTE_PATH) == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_skips_exempt_admin(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, admin=True)
        assert (await chain.events())["total"] == 0
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_degrades_when_llm_fails(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, llm_response=RuntimeError("boom"))
        events = await chain.events()
        assert events["total"] == 1
        assert events["items"][0]["verdict"] == "review"
        assert chain.transport.calls_for("DELETE", RECALL_PATH) == []
        assert chain.event.sent == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_allows_normal_message_without_actions(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path, text="大家好呀", llm_response=LLM_ALLOW)
        await chain.events()
        assert chain.event.sent == []
        assert chain.transport.calls == []
        await chain.close()

    asyncio.run(scenario())


def test_chain_duplicate_message_is_processed_once(tmp_path):
    async def scenario():
        chain = await run_chain(tmp_path)
        await chain.main.QQGroupManager._moderate(
            chain.service,
            chain.event,
            group_id="g1",
            config=chain.store.group("g1"),
            sender_openid="u1",
            sender_name="小号",
            sender_role="member",
        )
        assert (await chain.events())["total"] == 1
        assert len(chain.event.sent) == 1
        await chain.close()

    asyncio.run(scenario())


def test_store_tracks_member_first_seen():
    async def scenario():
        store = PluginStore(FakeKV())
        await store.load()
        assert store.member_first_seen("g1", "u1") is None
        await store.remember_member("g1", "u1", name="小号", role="member")
        first = store.member_first_seen("g1", "u1")
        assert isinstance(first, int) and first > 0
        await store.remember_member("g1", "u1", name="小号二号", role="member")
        assert store.member_first_seen("g1", "u1") == first
        assert store.member_name("g1", "u1") == "小号二号"

    asyncio.run(scenario())
