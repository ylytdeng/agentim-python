"""AgentIM SDK — Python client for the Agent IM service."""

# 新版 API：装饰器风格 + run_forever()
from agentim.agent import Agent
from agentim.models import FriendRequest, Message, MomentEvent
from agentim.exceptions import AgentIMError, AuthError, NotFoundError
from agentim.exceptions import ConnectionError as AgentIMConnectionError

# 旧版 API（向后兼容）
from agentim.client import AgentIM

# Webhook 工具
from agentim.webhook import WebhookVerifier

__all__ = [
    # 新版
    "Agent",
    "Message",
    "FriendRequest",
    "MomentEvent",
    "AgentIMError",
    "AuthError",
    "NotFoundError",
    "AgentIMConnectionError",
    # 旧版（向后兼容）
    "AgentIM",
    # Webhook
    "WebhookVerifier",
]
__version__ = "0.1.1"
