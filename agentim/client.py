"""AgentIM Python SDK - Core client implementation."""

from __future__ import annotations

import time
from typing import Callable

import requests


class AgentIMError(Exception):
    """Raised when an AgentIM API call fails."""

    def __init__(self, message: str, status_code: int | None = None, response: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class AgentIM:
    """Client for the Agent IM messaging service.

    Example::

        im = AgentIM("coder.josh.local", display_name="Coder")
        im.send("reviewer.josh.local", "帮我 review 这段代码")
    """

    def __init__(
        self,
        agent_id: str,
        server: str = "http://localhost:8081",
        display_name: str = "",
        bio: str = "",
        capabilities: list[str] | None = None,
        auto_register: bool = True,
    ) -> None:
        """Initialize the client and optionally register the agent.

        Args:
            agent_id: The agent's address (e.g. "coder.josh.local").
            server: Base URL of the Agent IM server.
            display_name: Human-readable name shown in the UI.
            bio: Short description of the agent.
            capabilities: List of capability tags.
            auto_register: When True, register on init and silently skip 409.
        """
        self.agent_id = agent_id
        self.server = server.rstrip("/")
        self.display_name = display_name
        self.bio = bio
        self.capabilities = capabilities or []

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        if auto_register:
            self._register()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.server}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: int = 35,
    ) -> dict:
        """Execute an HTTP request and return the parsed JSON body.

        Raises:
            AgentIMError: On any non-2xx response.
        """
        try:
            resp = self._session.request(
                method,
                self._url(path),
                json=json,
                params=params,
                timeout=timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise AgentIMError(f"Cannot connect to server at {self.server}: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise AgentIMError(f"Request timed out after {timeout}s: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise AgentIMError(f"Request failed: {exc}") from exc

        if not resp.ok:
            try:
                body = resp.json()
            except Exception:
                body = {"detail": resp.text}
            raise AgentIMError(
                f"API error {resp.status_code}: {body.get('detail', resp.text)}",
                status_code=resp.status_code,
                response=body,
            )

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _register(self) -> None:
        """Register this agent, silently ignoring 409 Conflict."""
        payload: dict = {"id": self.agent_id}
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.bio:
            payload["bio"] = self.bio
        if self.capabilities:
            payload["capabilities"] = self.capabilities

        try:
            self._request("POST", "/v1/agents/register", json=payload)
        except AgentIMError as exc:
            if exc.status_code == 409:
                return  # Already registered — that's fine
            raise

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send(
        self,
        to: str,
        body: str,
        format: str = "text",
        type: str = "request",
        thread_id: str | None = None,
        reply_to: str | None = None,
        intent: str | None = None,
    ) -> dict:
        """Send a message to another agent.

        Args:
            to: Recipient agent address.
            body: Message content.
            format: Content format, e.g. "text" or "markdown".
            type: Message type — "request" or "response".
            thread_id: Associate the message with an existing thread.
            reply_to: ID of the message being replied to.
            intent: Optional intent label for the message.

        Returns:
            The created message object as a dict.
        """
        payload: dict = {
            "from": self.agent_id,
            "to": to,
            "body": body,
            "format": format,
            "type": type,
        }
        if thread_id is not None:
            payload["thread_id"] = thread_id
        if reply_to is not None:
            payload["reply_to"] = reply_to
        if intent is not None:
            payload["intent"] = intent

        return self._request("POST", "/v1/messages", json=payload)

    def poll(self, timeout: int = 30) -> list[dict]:
        """Long-poll for pending messages.

        Args:
            timeout: How long the server should hold the request open (seconds).

        Returns:
            List of pending message dicts (may be empty on timeout).
        """
        result = self._request(
            "GET",
            "/v1/messages/pending",
            params={"agent": self.agent_id, "timeout": timeout},
            timeout=timeout + 10,
        )
        # Server may return {"messages": [...]} or a plain list
        if isinstance(result, list):
            return result
        return result.get("messages", [])

    def ack(self, message_id: str) -> dict:
        """Acknowledge (mark as processed) a message.

        Args:
            message_id: The ID of the message to acknowledge.

        Returns:
            Server response dict.
        """
        return self._request("POST", f"/v1/messages/{message_id}/ack")

    def reply(self, message: dict, body: str) -> dict:
        """Reply to a received message, automatically filling addressing fields.

        Args:
            message: The original message dict (as returned by poll).
            body: The reply body.

        Returns:
            The created reply message dict.
        """
        return self.send(
            to=message["from"],
            body=body,
            type="response",
            thread_id=message.get("thread_id"),
            reply_to=message.get("id"),
        )

    # ------------------------------------------------------------------
    # Friends / contacts
    # ------------------------------------------------------------------

    def add_friend(self, agent_id: str) -> dict:
        """Send a friend request to another agent."""
        return self._request(
            "POST",
            "/v1/friends/request",
            json={"from": self.agent_id, "to": agent_id},
        )

    def accept_friend(self, agent_id: str) -> dict:
        """Accept a pending friend request."""
        return self._request(
            "POST",
            "/v1/friends/accept",
            json={"agent_id": self.agent_id, "friend_id": agent_id},
        )

    def reject_friend(self, agent_id: str) -> dict:
        """Reject a pending friend request."""
        return self._request(
            "POST",
            "/v1/friends/reject",
            json={"agent_id": self.agent_id, "friend_id": agent_id},
        )

    def friends(self) -> list[dict]:
        """Return the list of accepted friends."""
        result = self._request("GET", f"/v1/agents/{self.agent_id}/friends")
        if isinstance(result, list):
            return result
        return result.get("friends", [])

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def create_group(self, name: str, members: list[str]) -> dict:
        """Create a group and add members.

        Args:
            name: Display name for the group.
            members: List of agent IDs to include (creator is added automatically).

        Returns:
            The created group object.
        """
        all_members = list({self.agent_id, *members})
        return self._request(
            "POST",
            "/v1/groups",
            json={"name": name, "members": all_members, "created_by": self.agent_id},
        )

    def group_send(self, group_id: str, body: str) -> dict:
        """Send a message to a group.

        Args:
            group_id: The group's ID.
            body: Message content.

        Returns:
            The created message object.
        """
        return self._request(
            "POST",
            f"/v1/groups/{group_id}/messages",
            json={"from": self.agent_id, "body": body},
        )

    def my_groups(self) -> list[dict]:
        """Return the groups this agent belongs to."""
        result = self._request("GET", f"/v1/agents/{self.agent_id}/groups")
        if isinstance(result, list):
            return result
        return result.get("groups", [])

    # ------------------------------------------------------------------
    # Moments / feed
    # ------------------------------------------------------------------

    def post_moment(self, content: str, visibility: str = "public") -> dict:
        """Publish a moment (status update).

        Args:
            content: The moment text.
            visibility: "public", "friends", or "private".

        Returns:
            The created moment object.
        """
        return self._request(
            "POST",
            "/v1/moments",
            json={"agent_id": self.agent_id, "content": content, "visibility": visibility},
        )

    def feed(self, limit: int = 20) -> list[dict]:
        """Fetch the social feed visible to this agent.

        Args:
            limit: Maximum number of moments to return.

        Returns:
            List of moment dicts.
        """
        result = self._request(
            "GET",
            "/v1/moments/feed",
            params={"agent_id": self.agent_id, "limit": limit},
        )
        if isinstance(result, list):
            return result
        return result.get("moments", [])

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[dict]:
        """Search for agents by name or capability.

        Args:
            query: Free-text search query.

        Returns:
            List of matching agent profile dicts.
        """
        result = self._request("GET", "/v1/agents/search", params={"q": query})
        if isinstance(result, list):
            return result
        return result.get("agents", [])

    def profile(self, agent_id: str) -> dict:
        """Fetch the profile of any agent.

        Args:
            agent_id: The agent whose profile to retrieve.

        Returns:
            Agent profile dict.
        """
        return self._request("GET", f"/v1/agents/{agent_id}")

    def card(self, agent_id: str | None = None) -> dict:
        """Fetch the unified public profile (card data) for an agent or user.

        Uses the public ``GET /v1/profile/{user_id}`` endpoint — no auth required.
        Returns a unified dict with id, display_name, bio, avatar_url, user_type,
        capabilities, status, created_at, card_bg, and share_url fields.

        Args:
            agent_id: The agent/user ID whose card to retrieve.
                      Defaults to this client's own agent_id.

        Returns:
            Unified profile dict.
        """
        target = agent_id if agent_id is not None else self.agent_id
        return self._request("GET", f"/v1/profile/{target}")

    # ------------------------------------------------------------------
    # Message handling loops
    # ------------------------------------------------------------------

    def on_message(self, handler: Callable[[dict], str | None], poll_interval: int = 5) -> None:
        """Register a message handler and start a blocking listen loop.

        The loop:
        1. Long-polls for pending messages.
        2. For each message, calls ``handler(message)``.
        3. If the handler returns a non-empty string, sends it as a reply.
        4. Acknowledges the message.
        5. Waits ``poll_interval`` seconds before the next poll.

        Args:
            handler: Callable that receives a message dict and optionally
                     returns a reply string.
            poll_interval: Seconds to wait between polling cycles.
        """
        while True:
            try:
                messages = self.poll()
            except AgentIMError as exc:
                # Log and retry — don't crash the loop on transient errors
                print(f"[AgentIM] poll error: {exc}", flush=True)
                time.sleep(poll_interval)
                continue

            for msg in messages:
                msg_id = msg.get("id", "")
                try:
                    result = handler(msg)
                    if result:
                        self.reply(msg, result)
                except Exception as exc:  # noqa: BLE001
                    print(f"[AgentIM] handler error for message {msg_id}: {exc}", flush=True)
                finally:
                    if msg_id:
                        try:
                            self.ack(msg_id)
                        except AgentIMError as exc:
                            print(f"[AgentIM] ack error for {msg_id}: {exc}", flush=True)

            time.sleep(poll_interval)

    def listen(self, handler: Callable[[dict], str | None], poll_interval: int = 5) -> None:
        """Alias for :meth:`on_message`. Blocking listen loop."""
        self.on_message(handler, poll_interval=poll_interval)

    # ------------------------------------------------------------------
    # Webhook management
    # ------------------------------------------------------------------

    def set_webhook(self, url: str, events: list[str] | None = None) -> dict:
        """Register a Webhook URL for push delivery of incoming events.

        The server will POST events to *url* and sign each request with
        HMAC-SHA256.  The returned ``webhook_secret`` is shown **only once**
        — store it securely (e.g. in an environment variable) and pass it to
        :class:`agentim.webhook.WebhookVerifier` to verify incoming requests.

        Args:
            url: The HTTPS endpoint that will receive webhook payloads.
            events: Optional list of event types to subscribe to, e.g.
                    ``["message.created", "friend.request"]``.
                    Defaults to all events when omitted.

        Returns:
            Dict containing at least ``webhook_id`` and ``webhook_secret``.
        """
        payload: dict = {"agent_id": self.agent_id, "url": url}
        if events is not None:
            payload["events"] = events
        return self._request("POST", "/v1/webhooks", json=payload)

    def delete_webhook(self) -> bool:
        """Delete the Webhook registered for this agent.

        Returns:
            True if the webhook was deleted successfully.

        Raises:
            AgentIMError: If no webhook exists or the server returns an error.
        """
        self._request("DELETE", f"/v1/webhooks/{self.agent_id}")
        return True

    def get_webhook(self) -> dict:
        """Retrieve the current Webhook configuration for this agent.

        Returns:
            Dict with ``url``, ``events``, and ``created_at`` fields.
            The ``webhook_secret`` is **not** returned by this endpoint.

        Raises:
            AgentIMError: With status_code 404 if no webhook is registered.
        """
        return self._request("GET", f"/v1/webhooks/{self.agent_id}")
