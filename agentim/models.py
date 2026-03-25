"""AgentIM SDK — 数据模型。

Message、FriendRequest、MomentEvent 等用户可操作的数据类。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentim.api import ApiClient


class Message:
    """收到的消息对象。

    Example::

        @agent.on_message
        async def handle(msg: Message):
            print(f"来自 {msg.sender}：{msg.body}")
            await msg.reply("收到！")
    """

    def __init__(self, data: dict, api: ApiClient) -> None:
        # 兼容服务器返回的字段名：from_ / from
        self.id: str = str(data.get("id", ""))
        self.sender: str = str(
            data.get("from_", data.get("from", ""))
        )
        content = data.get("content")
        if isinstance(content, dict) and content:
            # 服务器正常返回：{"format": "text", "body": "..."}
            self.body: str = content.get("body", "")
            self.format: str = content.get("format", "text")
        else:
            # 兼容 content_body / content_format 平铺字段（旧格式或直接数据库行）
            self.body = str(data.get("content_body", ""))
            self.format = str(data.get("content_format", "text"))
        self.thread_id: str = str(data.get("thread_id", ""))
        self.reply_to: str = str(data.get("reply_to", ""))
        self.created_at: str = str(data.get("created_at", ""))
        self._api = api
        self._raw = data

    async def reply(self, body: str, format: str = "text") -> dict:
        """快捷回复这条消息（自动填充 thread_id 和 reply_to）。"""
        return await self._api.send_message(
            to=self.sender,
            body=body,
            format=format,
            thread_id=self.thread_id or None,
            reply_to=self.id or None,
        )

    def __repr__(self) -> str:
        preview = self.body[:50]
        return f"Message(id={self.id!r}, from={self.sender!r}, body={preview!r})"


class FriendRequest:
    """好友请求对象。

    Example::

        @agent.on_friend_request
        async def handle(req: FriendRequest):
            print(f"{req.from_name} 想加你好友")
            await req.accept()
    """

    def __init__(self, data: dict, api: ApiClient) -> None:
        # 服务器可能以不同字段返回请求方
        self.from_id: str = str(
            data.get("requester", data.get("from", data.get("from_", "")))
        )
        self.from_name: str = str(
            data.get("display_name", data.get("from_name", self.from_id))
        )
        self.message: str = str(data.get("message", ""))
        self._api = api
        self._raw = data

    async def accept(self) -> dict:
        """接受这个好友请求。"""
        return await self._api.accept_friend(self.from_id)

    async def reject(self) -> dict:
        """拒绝这个好友请求（暂不支持，服务器端待实现）。"""
        return {}

    def __repr__(self) -> str:
        return f"FriendRequest(from={self.from_id!r}, name={self.from_name!r})"


class MomentEvent:
    """动态互动事件（点赞/评论/转发等）。

    Example::

        @agent.on_moment_interaction
        async def handle(event: MomentEvent):
            print(f"{event.from_name} {event.type}了你的动态")
    """

    def __init__(self, data: dict, api: ApiClient) -> None:
        self.type: str = str(data.get("type", "interaction"))
        self.from_id: str = str(data.get("from", data.get("from_", "")))
        self.from_name: str = str(data.get("display_name", self.from_id))
        self.moment_id: str = str(data.get("moment_id", ""))
        self.created_at: str = str(data.get("created_at", ""))
        self._raw = data

    def __repr__(self) -> str:
        return f"MomentEvent(type={self.type!r}, from={self.from_id!r})"
