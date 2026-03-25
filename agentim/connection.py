"""AgentIM SDK — 连接管理器。

连接优先级：AIM TCP > WebSocket > 长轮询。
指数退避策略防止失败时频繁重试。

AIM TCP 降级策略：
  - 尝试连接失败 3 次后，自动降级到 WebSocket（或长轮询）。
  - 降级后记录一次 warning，避免用户困惑。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from agentim.exceptions import AgentIMError, AuthError
from agentim.exceptions import ConnectionError as AgentIMConnectionError

if TYPE_CHECKING:
    from agentim.api import ApiClient

logger = logging.getLogger("agentim.connection")

# AIM TCP 最多尝试次数，超过后降级
AIM_MAX_ATTEMPTS = 3

# 尝试导入 websockets
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


class WebSocketConnection:
    """基于 WebSocket 的实时连接，自动重连 + 指数退避。"""

    def __init__(self, api: "ApiClient", poll_timeout: int = 30) -> None:
        self._api = api
        self._poll_timeout = poll_timeout
        self._retry_delay = 1.0
        self._max_delay = 60.0
        self._connected = False

        # 连接状态回调（由 Agent 层注入）
        self._on_connect_cb = None
        self._on_disconnect_cb = None

    def set_on_connect(self, cb) -> None:
        self._on_connect_cb = cb

    def set_on_disconnect(self, cb) -> None:
        self._on_disconnect_cb = cb

    def _ws_url(self) -> str:
        """从 HTTP URL 推导 WebSocket URL。"""
        server = self._api._server
        ws = server.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws}/v1/ws?token={self._api._api_key}"

    async def _fire_connect(self) -> None:
        if self._on_connect_cb:
            try:
                await self._on_connect_cb()
            except Exception as exc:
                logger.warning("on_connect 回调出错: %s", exc)

    async def _fire_disconnect(self) -> None:
        if self._on_disconnect_cb:
            try:
                await self._on_disconnect_cb()
            except Exception as exc:
                logger.warning("on_disconnect 回调出错: %s", exc)

    async def messages(self) -> AsyncIterator[list[dict]]:
        """异步生成器：每次 yield 一批消息（来自 WebSocket 推送）。"""
        while True:
            try:
                url = self._ws_url()
                async with websockets.connect(
                    url, ping_interval=30, ping_timeout=10,
                    close_timeout=5, open_timeout=10,
                ) as ws:
                    self._connected = True
                    self._retry_delay = 1.0  # 重置退避
                    logger.info("WebSocket 已连接")
                    await self._fire_connect()

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = data.get("type")
                        if msg_type == "msg":
                            # 实时消息推送
                            message = data.get("data", {}).get("message")
                            if message:
                                yield [message]
                        elif msg_type == "notifications":
                            # 连接时的通知摘要，不作为消息处理
                            logger.info("收到通知摘要: %s", data.get("data", {}))

            except Exception as e:
                was_connected = self._connected
                self._connected = False
                logger.warning("WebSocket 断开: %s, %.1fs 后重连...", e, self._retry_delay)
                if was_connected:
                    await self._fire_disconnect()
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, self._max_delay)


class LongPollConnection:
    """基于 HTTP 长轮询的持续连接（WebSocket 不可用时的 fallback）。"""

    def __init__(self, api: "ApiClient", poll_timeout: int = 30) -> None:
        self._api = api
        self._poll_timeout = poll_timeout
        self._retry_delay = 1.0
        self._max_delay = 60.0

        # 连接状态回调（由 Agent 层注入）
        self._on_connect_cb = None
        self._on_disconnect_cb = None

    def set_on_connect(self, cb) -> None:
        self._on_connect_cb = cb

    def set_on_disconnect(self, cb) -> None:
        self._on_disconnect_cb = cb

    async def messages(self) -> AsyncIterator[list[dict]]:
        """异步生成器：每次 yield 一批消息。"""
        while True:
            try:
                msgs = await self._api.poll_messages(timeout=self._poll_timeout)
                self._retry_delay = 1.0  # 重置退避
                if msgs:
                    yield msgs
            except AuthError:
                raise
            except AgentIMError as exc:
                logger.warning("轮询出错: %s, %.1fs 后重试...", exc, self._retry_delay)
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, self._max_delay)
            except Exception as exc:
                logger.warning("轮询异常: %s, %.1fs 后重试...", exc, self._retry_delay)
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, self._max_delay)


class AimWithFallback:
    """AIM TCP 连接包装器，失败 3 次后自动降级到 WebSocket / 长轮询。

    降级后不再尝试 AIM TCP，直到进程重启。
    """

    def __init__(
        self,
        aim_conn,
        fallback_conn,
    ) -> None:
        self._aim = aim_conn
        self._fallback = fallback_conn
        self._use_fallback = False
        self._aim_fail_count = 0

    def set_on_connect(self, cb) -> None:
        self._aim.set_on_connect(cb)
        self._fallback.set_on_connect(cb)

    def set_on_disconnect(self, cb) -> None:
        self._aim.set_on_disconnect(cb)
        self._fallback.set_on_disconnect(cb)

    async def messages(self) -> AsyncIterator[list[dict]]:
        """先尝试 AIM TCP，失败 3 次后降级到 fallback。"""
        if not self._use_fallback:
            aim_gen = self._aim.messages()
            while True:
                try:
                    batch = await aim_gen.__anext__()
                    # 收到消息，重置失败计数（连接正常）
                    self._aim_fail_count = 0
                    yield batch
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    self._aim_fail_count += 1
                    if self._aim_fail_count >= AIM_MAX_ATTEMPTS:
                        fallback_name = type(self._fallback).__name__
                        logger.warning(
                            "AIM TCP 连续失败 %d 次，自动降级到 %s",
                            self._aim_fail_count,
                            fallback_name,
                        )
                        self._use_fallback = True
                        break
                    # 还没超过阈值，继续重试（aim 内部已做退避）
                    raise

        # 降级后使用 fallback
        if self._use_fallback:
            async for batch in self._fallback.messages():
                yield batch


def create_connection(api: "ApiClient", poll_timeout: int = 30):
    """创建连接：优先 AIM TCP，其次 WebSocket，最后长轮询。

    优先级说明：
    1. AIM TCP（需要 msgpack）— 自研二进制协议，最高性能，心跳 25s；失败 3 次后降级
    2. WebSocket（需要 websockets）— 标准协议，兼容性好
    3. 长轮询 — 无额外依赖，兼容性最佳
    """
    # ── 1. AIM TCP（优先，失败 3 次后自动降级）─────────────────────
    try:
        import msgpack  # noqa: F401
        from urllib.parse import urlparse
        from .aim_connection import AimTcpConnection

        parsed = urlparse(api._server)
        aim_host = parsed.hostname or "localhost"
        aim_port = 8082
        # localhost 不走 TLS（本地开发），公网走 TLS
        use_tls = aim_host not in ("localhost", "127.0.0.1", "::1")

        # AIM 失败后降级到 WebSocket（如果可用），否则长轮询
        if HAS_WEBSOCKETS:
            fallback = WebSocketConnection(api, poll_timeout)
        else:
            fallback = LongPollConnection(api, poll_timeout)

        aim_conn = AimTcpConnection(aim_host, aim_port, api._api_key, tls=use_tls)

        logger.info(
            "使用 AIM TCP 连接（host=%s port=%d），失败 %d 次后降级到 %s",
            aim_host, aim_port, AIM_MAX_ATTEMPTS, type(fallback).__name__,
        )
        return AimWithFallback(aim_conn, fallback)

    except ImportError:
        logger.info("msgpack 未安装，跳过 AIM TCP（pip install msgpack 可启用）")

    # ── 2. WebSocket ──────────────────────────────────────────────
    if HAS_WEBSOCKETS:
        logger.info("使用 WebSocket 连接（实时推送）")
        return WebSocketConnection(api, poll_timeout)

    # ── 3. 长轮询（兜底）────────────────────────────────────────────
    logger.info("使用长轮询（pip install websockets 可启用 WebSocket，pip install msgpack 可启用 AIM TCP）")
    return LongPollConnection(api, poll_timeout)
