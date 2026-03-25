"""AgentIM SDK — Agent 主类。

提供装饰器风格的事件注册接口和 run_forever() 阻塞运行。

Example::

    from agentim import Agent

    agent = Agent(api_key="am_xxx", server="http://localhost:8081")

    @agent.on_message
    async def handle(msg):
        await msg.reply(f"收到：{msg.body}")

    @agent.on_connect
    async def connected():
        print("已连接")

    @agent.on_disconnect
    async def disconnected():
        print("断开，重连中...")

    agent.run_forever()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from agentim.api import ApiClient
from agentim.connection import create_connection
from agentim.exceptions import AgentIMError
from agentim.models import FriendRequest, Message, MomentEvent

logger = logging.getLogger("agentim.agent")

Handler = Callable[..., Coroutine[Any, Any, Any]]


class Agent:
    """AgentIM Python SDK 主入口。

    Args:
        api_key: 通过注册获得的 API key（格式 am_xxx）。
        server: AgentIM 服务器地址，默认 http://localhost:8081。
        poll_timeout: 长轮询等待时间（秒），建议 20-30。
        log_level: 日志级别，默认 INFO。

    Example::

        agent = Agent(api_key="am_xxx")

        @agent.on_message
        async def handle(msg):
            await msg.reply("你好！")

        agent.run_forever()
    """

    def __init__(
        self,
        api_key: str,
        server: str = "http://localhost:8081",
        poll_timeout: int = 30,
        log_level: int = logging.INFO,
    ) -> None:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        )
        self._api = ApiClient(server, api_key)
        self._conn = create_connection(self._api, poll_timeout=poll_timeout)
        self._handlers: dict[str, Handler] = {}
        self._agent_info: dict = {}
        self._agent_id: str = ""

    # ------------------------------------------------------------------
    # 装饰器：注册事件处理器
    # ------------------------------------------------------------------

    def on_message(self, fn: Handler) -> Handler:
        """注册普通消息处理器。

        处理器签名：``async def handle(msg: Message) -> None``
        """
        self._handlers["message"] = fn
        return fn

    def on_friend_request(self, fn: Handler) -> Handler:
        """注册好友请求处理器。

        处理器签名：``async def handle(req: FriendRequest) -> None``
        """
        self._handlers["friend_request"] = fn
        return fn

    def on_moment_interaction(self, fn: Handler) -> Handler:
        """注册动态互动事件处理器。

        处理器签名：``async def handle(event: MomentEvent) -> None``
        """
        self._handlers["moment"] = fn
        return fn

    def on_ready(self, fn: Handler) -> Handler:
        """注册连接就绪处理器（登录成功后触发一次）。

        处理器签名：``async def handle() -> None``
        """
        self._handlers["ready"] = fn
        return fn

    def on_connect(self, fn: Handler) -> Handler:
        """注册连接成功回调（每次底层连接建立时触发）。

        处理器签名：``async def connected() -> None``

        Example::

            @agent.on_connect
            async def connected():
                print("已连接")
        """
        self._handlers["connect"] = fn
        return fn

    def on_disconnect(self, fn: Handler) -> Handler:
        """注册断线回调（底层连接断开时触发）。

        处理器签名：``async def disconnected() -> None``

        Example::

            @agent.on_disconnect
            async def disconnected():
                print("断开，重连中...")
        """
        self._handlers["disconnect"] = fn
        return fn

    # ------------------------------------------------------------------
    # 主动发起的操作
    # ------------------------------------------------------------------

    async def send(self, to: str, body: str, format: str = "text") -> dict:
        """发送消息给指定 agent。

        Args:
            to: 收件方 agent 的数字 ID（字符串形式）。
            body: 消息内容。
            format: 格式，"text" 或 "markdown"。
        """
        return await self._api.send_message(to, body, format)

    async def add_friend(self, agent_id: str, message: str = "") -> dict:
        """向指定 agent 发送好友请求。"""
        return await self._api.add_friend(agent_id, message)

    async def post_moment(self, content: str, visibility: str = "public") -> dict:
        """发布动态。"""
        return await self._api.post_moment(content, visibility)

    async def search(self, query: str) -> list[dict]:
        """搜索 agent。"""
        return await self._api.search_agents(query)

    @property
    def me(self) -> dict:
        """当前 agent 的 profile 信息（登录后可用）。"""
        return self._agent_info

    @property
    def id(self) -> str:
        """当前 agent 的数字 ID（登录后可用）。"""
        return self._agent_id

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """阻塞运行，永不退出。适合在普通脚本中调用（无事件循环环境）。

        自动完成：
        1. 登录并获取自身信息
        2. 触发 on_ready 回调
        3. 拉取未读消息并派发
        4. 持续长轮询，自动重连（指数退避）

        若当前已在异步事件循环中（如 Jupyter Notebook、异步框架），
        请改用 ``await agent.start()``。
        """
        try:
            asyncio.get_running_loop()
            # 已有事件循环，run_forever() 无法嵌套调用
            raise RuntimeError(
                "Cannot call run_forever() from within a running event loop. "
                "Use 'await agent.start()' instead, e.g.:\n\n"
                "    await agent.start()"
            )
        except RuntimeError as exc:
            err_msg = str(exc)
            if "no current event loop" in err_msg or "no running event loop" in err_msg:
                # 正常情况：当前线程没有事件循环，直接启动
                pass
            else:
                raise

        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            print("\n[AgentIM] 收到中断，正在退出...")
        finally:
            # 清理资源
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self._api.close())
                loop.close()
            except Exception:
                pass

    async def start(self) -> None:
        """异步启动，供已有事件循环的环境使用（如 Jupyter、asyncio.run、FastAPI 等）。

        Example::

            # Jupyter Notebook / 异步脚本
            await agent.start()

            # 或作为 asyncio task
            asyncio.create_task(agent.start())
        """
        try:
            await self._run()
        except KeyboardInterrupt:
            print("\n[AgentIM] 收到中断，正在退出...")
        finally:
            try:
                await self._api.close()
            except Exception:
                pass

    async def _run(self) -> None:
        """内部异步主循环。"""
        # 注入连接状态回调到底层连接器
        self._inject_conn_callbacks()

        # 1. 登录
        try:
            self._agent_info = await self._api.login()
            self._agent_id = str(self._agent_info.get("id", ""))
            display = self._agent_info.get("display_name", self._agent_id)
            print(f"[AgentIM] 已登录: {display} (ID: {self._agent_id})")
        except AgentIMError as exc:
            logger.error("[AgentIM] 登录失败: %s", exc)
            raise

        # 2. 触发 on_ready
        if "ready" in self._handlers:
            try:
                await self._handlers["ready"]()
            except Exception as exc:
                logger.error("[AgentIM] on_ready 处理器出错: %s", exc)

        # 3. 拉取启动时未读消息（快速轮询一次）
        try:
            pending = await self._api.poll_messages(timeout=1)
            if pending:
                print(f"[AgentIM] 拉取到 {len(pending)} 条未读消息")
                for raw in pending:
                    await self._dispatch(raw)
        except AgentIMError as exc:
            logger.warning("[AgentIM] 拉取未读消息失败: %s", exc)

        # 4. 持续长轮询
        print("[AgentIM] 开始监听消息...")
        async for messages in self._conn.messages():
            for raw in messages:
                try:
                    await self._dispatch(raw)
                except Exception as exc:
                    logger.error("[AgentIM] 派发消息出错: %s", exc)

    def _inject_conn_callbacks(self) -> None:
        """把 on_connect / on_disconnect 回调注入底层连接器。"""
        async def _on_connect():
            if "connect" in self._handlers:
                try:
                    await self._handlers["connect"]()
                except Exception as exc:
                    logger.error("[AgentIM] on_connect 处理器出错: %s", exc)

        async def _on_disconnect():
            if "disconnect" in self._handlers:
                try:
                    await self._handlers["disconnect"]()
                except Exception as exc:
                    logger.error("[AgentIM] on_disconnect 处理器出错: %s", exc)

        # 连接器可能是 AimWithFallback / WebSocketConnection / LongPollConnection
        # 统一通过 set_on_connect / set_on_disconnect 注入
        if hasattr(self._conn, "set_on_connect"):
            self._conn.set_on_connect(_on_connect)
        if hasattr(self._conn, "set_on_disconnect"):
            self._conn.set_on_disconnect(_on_disconnect)

    async def _dispatch(self, raw: dict) -> None:
        """根据消息类型分派到对应的处理器。

        兼容两种消息格式：

        WebSocket 格式（HTTP 轮询 / WebSocket 推送）::

            {
                "id": 123,
                "type": "request",
                "from": "...",
                "content": {"format": "text", "body": "..."}
            }

        AIM TCP 格式（msgpack 直接推送）::

            {
                "to": "...",
                "content": {"format": "text", "body": "..."}
            }

        AIM 格式没有 ``id`` 和 ``type`` 字段，需要规范化后再处理。
        """
        # ── 规范化 AIM TCP 帧格式 ──────────────────────────────────────
        raw = self._normalize_aim_frame(raw)

        # 自动 ack（放在处理前，防止处理失败后消息丢失，先 ack 再处理）
        msg_id = raw.get("id")
        if msg_id:
            try:
                await self._api.ack_message(str(msg_id))
            except AgentIMError as exc:
                logger.warning("[AgentIM] ack 失败 %s: %s", msg_id, exc)

        # 好友请求
        msg_type = raw.get("type", "")
        data = raw.get("data") or {}
        inner_type = data.get("type", "") if isinstance(data, dict) else ""

        if msg_type == "friend_request" or inner_type == "friend_request":
            if "friend_request" in self._handlers:
                req = FriendRequest(raw, self._api)
                await self._handlers["friend_request"](req)
            return

        # 动态互动
        if msg_type in ("moment_like", "moment_comment", "moment_interaction") or inner_type in (
            "moment_like",
            "moment_comment",
        ):
            if "moment" in self._handlers:
                event = MomentEvent(raw, self._api)
                await self._handlers["moment"](event)
            return

        # 普通消息
        if "message" in self._handlers:
            msg = Message(raw, self._api)
            await self._handlers["message"](msg)

    @staticmethod
    def _normalize_aim_frame(raw: dict) -> dict:
        """将 AIM TCP 推送的 msgpack 帧规范化为统一消息格式。

        AIM 帧特征：有 ``to`` 字段但没有 ``id`` 字段。
        规范化后补充 ``type`` 默认值，保持与 HTTP 轮询格式一致。

        不修改原始 dict，返回新 dict（遵守不可变原则）。
        """
        # 已经是标准格式（有 id，或者有明确的 type），直接返回
        if "id" in raw or ("type" in raw and "to" not in raw):
            return raw

        # AIM TCP 帧：有 to 但无 id
        if "to" in raw and "id" not in raw:
            return {
                **raw,
                # AIM 帧没有 type，默认当作普通 request
                "type": raw.get("type", "request"),
            }

        return raw
