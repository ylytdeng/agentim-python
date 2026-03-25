"""Webhook 验签工具，供 agent 的 HTTP 服务端使用。

典型用法::

    from agentim.webhook import WebhookVerifier

    verifier = WebhookVerifier(secret="whsec_xxx")

    # 在 FastAPI / Flask 路由中：
    if not verifier.verify(body, signature, timestamp):
        raise HTTPException(status_code=401, detail="Invalid signature")
"""

from __future__ import annotations

import hashlib
import hmac
import time


class WebhookVerifier:
    """Verify HMAC-SHA256 signatures on incoming AgentIM webhook requests.

    The server signs each webhook delivery as::

        signature = HMAC-SHA256(secret, f"{timestamp}.{raw_body}")

    and sends the result in the ``X-AgentIM-Signature`` header along with
    ``X-AgentIM-Timestamp`` (Unix seconds as a string).

    Args:
        secret: The ``webhook_secret`` returned by
                :meth:`agentim.AgentIM.set_webhook`.
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("webhook secret must not be empty")
        self.secret = secret

    def verify(
        self,
        payload: bytes,
        signature: str,
        timestamp: str,
        max_age: int = 300,
    ) -> bool:
        """Verify a webhook signature.

        Args:
            payload: The raw (undecoded) request body bytes.
            signature: Value of the ``X-AgentIM-Signature`` header.
            timestamp: Value of the ``X-AgentIM-Timestamp`` header.
            max_age: Maximum allowed age of the request in seconds.
                     Defaults to 300 (5 minutes) to prevent replay attacks.

        Returns:
            True if the signature is valid and the request is within the
            allowed time window; False otherwise.
        """
        # 1. Validate timestamp to prevent replay attacks
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > max_age:
                return False
        except (ValueError, TypeError):
            return False

        # 2. Compute expected HMAC-SHA256 signature
        msg = f"{timestamp}.".encode() + payload
        expected = hmac.new(
            self.secret.encode(),
            msg,
            hashlib.sha256,
        ).hexdigest()

        # 3. Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(signature, expected)
