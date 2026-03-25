"""AIM TCP 连接 — 使用自研二进制协议，最高性能。

协议帧格式（16 字节头）：
  [0:2]  MAGIC    = 0x41 0x4D ('AM')
  [2]    VERSION  = 0x01
  [3]    FLAGS    （FLAG_RESPONSE = 0x80）
  [4]    TYPE     （HANDSHAKE/HEARTBEAT/MESSAGE/ACK/ERROR）
  [5:9]  STREAM_ID（大端 uint32）
  [9:12] SEQ      （大端 3 字节）
  [12:16] PAYLOAD_LEN（大端 uint32）
  [16:]  PAYLOAD  （msgpack 编码）
"""

from __future__ import annotations

import asyncio
import struct
import time
import logging
from typing import AsyncIterator, Callable, Coroutine, Any

logger = logging.getLogger("agentim.aim_tcp")

# 协议常量
MAGIC = b'\x41\x4d'
VERSION = 0x01
HEADER_SIZE = 16

# 帧类型
TYPE_HANDSHAKE = 0x00
TYPE_HEARTBEAT = 0x01
TYPE_MESSAGE = 0x02
TYPE_ACK = 0x03
TYPE_ERROR = 0x07

# 标志位
FLAG_RESPONSE = 0x80

# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 25
# 心跳等待 PONG 的超时（秒）
HEARTBEAT_PONG_TIMEOUT = 10
# 连接/读取超时（秒）
CONNECT_TIMEOUT = 10
READ_HEADER_TIMEOUT = 60
READ_PAYLOAD_TIMEOUT = 30

# 降噪阈值：连续失败超过此次数后，每隔 N 次才打一条日志
_LOG_SUPPRESS_AFTER = 3
_LOG_SUPPRESS_INTERVAL = 10

ConnectCallback = Callable[[], Coroutine[Any, Any, None]]


class AimTcpConnection:
    """AIM TCP 长连接客户端。

    特性：
    - msgpack 二进制编码，比 JSON/WebSocket 开销更低
    - 握手认证（token）
    - 自动心跳（25s 间隔），超时未收到 PONG 触发重连
    - 断线自动重连 + 指数退避（最长 30s）
    - 连续重连失败只打第 1 次和每 10 次日志，避免日志洪泛
    - 连接状态回调：on_connect / on_disconnect
    """

    def __init__(self, host: str, port: int, api_key: str) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._agent_id: str = ""
        self._heartbeat_task: asyncio.Task | None = None

        # 重连失败计数，用于日志降噪
        self._fail_count: int = 0

        # 心跳 PONG 等待队列（asyncio.Event per ping）
        self._pong_event: asyncio.Event = asyncio.Event()

        # 连接状态回调
        self._on_connect_cb: ConnectCallback | None = None
        self._on_disconnect_cb: ConnectCallback | None = None

    # ──────────────────────────── 状态回调 ────────────────────────────

    def set_on_connect(self, cb: ConnectCallback) -> None:
        """注册连接成功回调。"""
        self._on_connect_cb = cb

    def set_on_disconnect(self, cb: ConnectCallback) -> None:
        """注册断线回调。"""
        self._on_disconnect_cb = cb

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

    # ──────────────────────────── 帧编解码 ────────────────────────────

    def encode_frame(
        self,
        type_: int,
        payload: dict | None = None,
        stream_id: int = 0,
        seq: int = 0,
        flags: int = 0,
    ) -> bytes:
        """将 payload 打包成二进制帧。"""
        import msgpack  # 延迟导入，调用者已保证可用

        body = msgpack.packb(payload or {}, use_bin_type=True) if payload else b""
        header = bytearray(HEADER_SIZE)
        header[0:2] = MAGIC
        header[2] = VERSION
        header[3] = flags & 0xFF
        header[4] = type_ & 0xFF
        struct.pack_into(">I", header, 5, stream_id)
        header[9] = (seq >> 16) & 0xFF
        header[10] = (seq >> 8) & 0xFF
        header[11] = seq & 0xFF
        struct.pack_into(">I", header, 12, len(body))
        return bytes(header) + body

    async def read_frame(self) -> dict:
        """从流中读取并解析一个完整帧。"""
        import msgpack

        assert self._reader is not None, "未建立连接"
        header = await asyncio.wait_for(
            self._reader.readexactly(HEADER_SIZE), timeout=READ_HEADER_TIMEOUT
        )

        type_ = header[4]
        flags = header[3]
        stream_id = struct.unpack_from(">I", header, 5)[0]
        seq = (header[9] << 16) | (header[10] << 8) | header[11]
        payload_len = struct.unpack_from(">I", header, 12)[0]

        payload: dict = {}
        if payload_len > 0:
            raw = await asyncio.wait_for(
                self._reader.readexactly(payload_len), timeout=READ_PAYLOAD_TIMEOUT
            )
            payload = msgpack.unpackb(raw, raw=False)

        return {
            "type": type_,
            "flags": flags,
            "stream_id": stream_id,
            "seq": seq,
            "payload": payload,
        }

    # ──────────────────────────── 连接管理 ────────────────────────────

    async def connect(self) -> bool:
        """建立 TCP 连接并完成握手认证。成功返回 True，失败返回 False。"""
        addr = f"{self.host}:{self.port}"
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
        except (ConnectionRefusedError, OSError) as exc:
            self._log_connect_fail(f"AIM 连接失败: 服务器 {addr} 不可达 ({exc})")
            return False
        except asyncio.TimeoutError:
            self._log_connect_fail(f"AIM 连接失败: 连接 {addr} 超时（{CONNECT_TIMEOUT}s）")
            return False

        # 发送握手帧
        handshake = self.encode_frame(
            TYPE_HANDSHAKE,
            payload={"token": self.api_key, "protocol_version": 1},
        )
        self._writer.write(handshake)
        await self._writer.drain()

        # 等待握手响应
        try:
            resp = await asyncio.wait_for(self.read_frame(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self._log_connect_fail(f"AIM 握手失败: 等待服务器响应超时（{addr}）")
            self._close_transport()
            return False

        # 握手被拒：服务器返回 ERROR 帧
        if resp["type"] == TYPE_ERROR:
            err_payload = resp["payload"]
            reason = err_payload.get("message") or err_payload.get("reason") or str(err_payload)
            self._log_connect_fail(f"AIM 握手失败: 服务器拒绝认证 — {reason}（{addr}）")
            self._close_transport()
            return False

        # 握手响应格式不对
        is_handshake_resp = (
            resp["type"] == TYPE_HANDSHAKE and (resp["flags"] & FLAG_RESPONSE)
        )
        if not is_handshake_resp:
            self._log_connect_fail(
                f"AIM 握手失败: 意外帧 type=0x{resp['type']:02x} flags=0x{resp['flags']:02x}（{addr}）"
            )
            self._close_transport()
            return False

        self._agent_id = resp["payload"].get("agent_id", "")
        self._connected = True
        self._fail_count = 0  # 连接成功，重置失败计数
        logger.info("AIM TCP 已连接 %s，agent_id=%s", addr, self._agent_id)
        return True

    def _log_connect_fail(self, msg: str) -> None:
        """连接失败日志，超过阈值后降噪。"""
        self._fail_count += 1
        n = self._fail_count
        if n == 1 or n % _LOG_SUPPRESS_INTERVAL == 0:
            suffix = f"（已连续失败 {n} 次）" if n > 1 else ""
            logger.warning("%s%s", msg, suffix)
        # 中间的失败静默处理（只记录 debug）
        elif n <= _LOG_SUPPRESS_AFTER or n % _LOG_SUPPRESS_INTERVAL == 1:
            logger.debug("AIM 连接失败（第 %d 次，已降噪）", n)

    def _close_transport(self) -> None:
        """关闭底层 transport，不抛异常。"""
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._connected = False

    def close(self) -> None:
        """主动关闭连接，取消心跳任务。"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._close_transport()

    # ──────────────────────────── 心跳 ────────────────────────────────

    async def send_heartbeat(self) -> None:
        """发送心跳帧（PING）。"""
        assert self._writer is not None
        frame = self.encode_frame(
            TYPE_HEARTBEAT,
            payload={"ts": int(time.time() * 1000)},
        )
        self._writer.write(frame)
        await self._writer.drain()

    async def _heartbeat_loop(self) -> None:
        """后台心跳循环。

        每 HEARTBEAT_INTERVAL 秒发一次 PING，等待 HEARTBEAT_PONG_TIMEOUT 秒内
        收到 PONG（由主读帧循环设置 _pong_event）。超时则标记断线。
        """
        while self._connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._connected:
                break
            try:
                self._pong_event.clear()
                await self.send_heartbeat()
                logger.debug("心跳已发送，等待 PONG...")

                # 等待主循环通知 PONG 到达
                try:
                    await asyncio.wait_for(
                        self._pong_event.wait(),
                        timeout=HEARTBEAT_PONG_TIMEOUT,
                    )
                    logger.debug("收到心跳响应")
                except asyncio.TimeoutError:
                    logger.warning(
                        "心跳超时（%ds 未收到 PONG），触发断线重连", HEARTBEAT_PONG_TIMEOUT
                    )
                    self._connected = False
                    break

            except Exception as exc:
                logger.warning("心跳发送失败: %s，触发断线重连", exc)
                self._connected = False
                break

    # ──────────────────────────── 发消息 ──────────────────────────────

    async def send_message(
        self,
        to: str,
        body: str,
        msg_type: str = "request",
        seq: int = 0,
    ) -> dict:
        """发送消息并等待 ACK 帧，返回 ACK 的 payload。"""
        assert self._writer is not None, "未建立连接"
        frame = self.encode_frame(
            TYPE_MESSAGE,
            stream_id=1,
            seq=seq,
            payload={
                "to": to,
                "content": {"format": "text", "body": body},
                "type": msg_type,
            },
        )
        self._writer.write(frame)
        await self._writer.drain()

        # 等待服务器返回 ACK
        resp = await self.read_frame()
        return resp

    # ──────────────────────────── 消息接收 ────────────────────────────

    async def messages(self) -> AsyncIterator[list[dict]]:
        """异步生成器：持续接收服务器推送的消息。

        断线后自动重连，指数退避（1s → 最长 30s）。
        每次 yield 一个消息列表，格式与 HTTP 轮询保持一致。
        """
        retry_delay = 1.0

        while True:
            if not self._connected:
                connected = await self.connect()
                if not connected:
                    # 连接失败，退避后重试
                    await self._fire_disconnect()
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
                    continue
                # 连接成功
                retry_delay = 1.0
                await self._fire_connect()

            # 启动心跳后台任务
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._pong_event = asyncio.Event()
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                # 持续读帧
                while self._connected:
                    frame = await self.read_frame()
                    ftype = frame["type"]

                    if ftype == TYPE_MESSAGE:
                        # 服务器推送的消息
                        yield [frame["payload"]]

                    elif ftype == TYPE_HEARTBEAT:
                        # PONG — 通知心跳循环
                        self._pong_event.set()

                    elif ftype == TYPE_ERROR:
                        err_payload = frame["payload"]
                        reason = (
                            err_payload.get("message")
                            or err_payload.get("reason")
                            or str(err_payload)
                        )
                        logger.warning("收到服务器错误帧: %s", reason)

                    elif ftype == TYPE_ACK:
                        # 服务器主动推 ACK，忽略（一般由 send_message 消费）
                        pass

                    else:
                        logger.debug("未知帧类型 0x%02x，忽略", ftype)

            except Exception as exc:
                was_connected = self._connected
                self._connected = False
                # 取消心跳任务，等下次重连再重启
                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None

                addr = f"{self.host}:{self.port}"
                self._log_connect_fail(
                    f"AIM 连接断开 {addr}: {exc}，{retry_delay:.0f}s 后重连..."
                )
                if was_connected:
                    await self._fire_disconnect()
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)
