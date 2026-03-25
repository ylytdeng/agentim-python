"""AgentIM SDK — 异常定义。"""


class AgentIMError(Exception):
    """AgentIM API 调用失败时抛出。"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class AuthError(AgentIMError):
    """API key 无效或未授权。"""


class ConnectionError(AgentIMError):
    """无法连接到 AgentIM 服务器。"""


class NotFoundError(AgentIMError):
    """请求的资源不存在。"""
