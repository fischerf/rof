"""
server.py — Signal MCP Server (FastMCP edition)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exposes Signal messaging as MCP tools backed by signal-cli REST API.

Run:
    python -m signal_mcp                     # stdio transport (Claude Desktop)
    python -m signal_mcp --transport sse     # SSE transport (remote / browser)

Tools are discovered automatically via tools/list.

Background receiving
────────────────────
A background asyncio task runs signal_client.stream_messages() for the entire
server lifetime.  Every incoming message is pushed into a MessageBuffer that
acts as both a live queue and a bounded history log.

Tool behaviour:
  • signal_receive_messages    — drain everything in the queue right now (no wait)
  • signal_wait_for_message    — block until a message arrives (or timeout)
  • signal_get_message_history — replay all messages seen since server start
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP, Context

from .config import SignalConfig, load_config
from .logging_config import get_logger, setup_logging
from .signal_client import SignalClient, SignalError, SignalMessage

log = get_logger("server")


# ──────────────────────────────────────────────────────────────────────────────
# Message buffer
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MessageBuffer:
    """
    Asyncio-native message store shared across all tool calls.

    queue   — unread messages waiting to be consumed by a tool call.
    history — all messages seen since server start (capped at max_history).
              Reading history does NOT remove items from the queue.
    """

    max_history: int = 500
    queue:   asyncio.Queue[SignalMessage] = field(default_factory=asyncio.Queue)
    history: deque[SignalMessage]         = field(init=False)

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.max_history)

    def put(self, msg: SignalMessage) -> None:
        """Record a new message (called from the background task)."""
        self.history.append(msg)
        self.queue.put_nowait(msg)

    def drain(self, limit: int = 0) -> list[SignalMessage]:
        """
        Return and remove all currently queued messages without waiting.
        If limit > 0, return at most that many (the rest stay in the queue).
        """
        messages: list[SignalMessage] = []
        while True:
            try:
                messages.append(self.queue.get_nowait())
                self.queue.task_done()
                if limit and len(messages) >= limit:
                    break
            except asyncio.QueueEmpty:
                break
        return messages

    async def wait_for_next(self, timeout: float) -> Optional[SignalMessage]:
        """
        Block until a message arrives or timeout expires.
        Returns the message, or None on timeout.
        """
        try:
            msg = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            self.queue.task_done()
            return msg
        except asyncio.TimeoutError:
            return None

    @property
    def pending_count(self) -> int:
        """Number of unread messages currently sitting in the queue."""
        return self.queue.qsize()


# ──────────────────────────────────────────────────────────────────────────────
# Background streaming task
# ──────────────────────────────────────────────────────────────────────────────

async def _background_receiver(
    client: SignalClient,
    buffer: MessageBuffer,
    poll_interval: float,
) -> None:
    """
    Long-running asyncio task.  Drives signal_client.stream_messages() and
    feeds every arriving message into the shared MessageBuffer.

    Runs for the entire server lifetime; cancelled cleanly on shutdown.
    stream_messages() itself retries on transient errors, so this task is
    resilient to temporary signal-cli-rest-api unavailability.
    """
    log.info("Background receiver started (poll_interval=%.1fs).", poll_interval)
    try:
        async for msg in client.stream_messages(poll_interval=poll_interval):
            log.info("← [background] %s", msg)
            buffer.put(msg)
    except asyncio.CancelledError:
        log.info("Background receiver stopped.")
    except Exception:  # noqa: BLE001
        log.exception(
            "Background receiver crashed — new messages will no longer be buffered. "
            "Restart the server to resume background receiving."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan — eager connection + background receiver
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """
    Startup sequence:
      1. Load config and configure logging.
      2. Open the SignalClient HTTP session (eager connection — no cold-start).
      3. Start the background receiver task so messages are buffered immediately.
      4. Yield the shared context dict to every tool handler.

    Shutdown sequence:
      5. Cancel the background receiver task gracefully.
      6. Close the HTTP session.
    """
    cfg: SignalConfig = load_config()
    setup_logging(cfg.log_level, cfg.log_file)

    if not cfg.account_number:
        log.error(
            "No account configured. "
            "Run `python -m signal_mcp onboard` first, "
            "or set SIGNAL_PHONE_NUMBER environment variable."
        )
        raise SystemExit(1)

    log.info(
        "Starting Signal MCP server — account=%s  api=%s",
        cfg.account_number,
        cfg.api_base_url,
    )

    buffer = MessageBuffer()

    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number,
        timeout=cfg.api_timeout,
        receive_timeout=cfg.receive_timeout,
    ) as client:

        # Start the background receiver before yielding so messages are
        # captured from the very first second the server is live.
        receiver_task = asyncio.create_task(
            _background_receiver(client, buffer, cfg.poll_interval),
            name="signal-background-receiver",
        )

        log.info(
            "Signal MCP server ready — "
            "eager connection established, background receiver running."
        )

        try:
            yield {"client": client, "cfg": cfg, "buffer": buffer}
        finally:
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass

    log.info("Signal MCP server shut down.")


# ──────────────────────────────────────────────────────────────────────────────
# Server instance
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="signal-mcp",
    instructions=(
        "Send and receive Signal messages, manage groups, contacts, and your profile. "
        "Phone numbers must be in E.164 format (e.g. +15551234567). "
        "Group IDs are base64 strings returned by signal_list_groups. "
        "Incoming messages are buffered automatically in the background — "
        "use signal_receive_messages to drain the queue instantly, "
        "signal_wait_for_message to block until the next reply arrives, or "
        "signal_get_message_history to review all messages since server start."
    ),
    lifespan=_lifespan,
)


# ──────────────────────────────────────────────────────────────────────────────
# Context helpers
# ──────────────────────────────────────────────────────────────────────────────

def _client(ctx: Context) -> SignalClient:
    return ctx.request_context.lifespan_context["client"]

def _cfg(ctx: Context) -> SignalConfig:
    return ctx.request_context.lifespan_context["cfg"]

def _buffer(ctx: Context) -> MessageBuffer:
    return ctx.request_context.lifespan_context["buffer"]

def _fmt_message(m: SignalMessage) -> dict[str, Any]:
    return {
        "sender":      m.sender,
        "sender_name": m.sender_name,
        "message":     m.message,
        "timestamp":   m.timestamp.isoformat(),
        "group_id":    m.group_id,
        "is_group":    m.is_group,
        "attachments": m.attachments,
        "has_quote":   m.quote is not None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Receive  (all three modes)
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_receive_messages(
    ctx: Context,
    limit: Annotated[
        int,
        "Maximum number of messages to return. 0 means return all pending.",
    ] = 0,
) -> str:
    """
    Return all messages that have arrived since the last call — instantly,
    with no network round-trip.

    Messages are collected continuously by the server's background receiver.
    Calling this tool drains the queue: the same messages will NOT be returned
    again on the next call.  Use signal_get_message_history to review past
    messages without consuming them.
    """
    buf = _buffer(ctx)
    messages = buf.drain(limit=limit)
    if not messages:
        return (
            f"No new messages in queue. "
            f"(background receiver is running; {buf.pending_count} message(s) pending)"
        )
    return json.dumps([_fmt_message(m) for m in messages], indent=2, default=str)


@mcp.tool()
async def signal_wait_for_message(
    ctx: Context,
    timeout: Annotated[
        float,
        "Maximum seconds to wait for a message to arrive. Defaults to 30.",
    ] = 30.0,
    sender_filter: Annotated[
        Optional[str],
        "E.164 phone number. If set, only return a message from this sender; "
        "messages from other senders are re-queued and not lost.",
    ] = None,
) -> str:
    """
    Block until the next Signal message arrives (or until timeout expires).

    Ideal for agentic send → wait → reply loops:
        1. Call signal_send_message to ask a question.
        2. Call signal_wait_for_message to pause until the human replies.
        3. Read the reply and continue the workflow.

    Returns the message as a JSON object, or {"timeout": true} if none arrived
    within the timeout window.
    """
    buf = _buffer(ctx)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return json.dumps({"timeout": True, "waited_seconds": timeout})

        msg = await buf.wait_for_next(timeout=remaining)
        if msg is None:
            return json.dumps({"timeout": True, "waited_seconds": timeout})

        # Sender filter: re-queue non-matching messages so they are not lost.
        if sender_filter and msg.sender != sender_filter:
            buf.put(msg)
            continue

        return json.dumps(_fmt_message(msg), indent=2, default=str)


@mcp.tool()
async def signal_get_message_history(
    ctx: Context,
    limit: Annotated[
        int,
        "Number of most-recent messages to return (1–500). Defaults to 50.",
    ] = 50,
    sender_filter: Annotated[
        Optional[str],
        "If set, only include messages from this phone number (E.164).",
    ] = None,
) -> str:
    """
    Return a read-only view of all messages received since the server started.

    Unlike signal_receive_messages, this does NOT consume messages from the
    queue — they remain available for future signal_receive_messages calls.
    Useful for reviewing conversation history or debugging.
    """
    buf = _buffer(ctx)
    history = list(buf.history)

    if sender_filter:
        history = [m for m in history if m.sender == sender_filter]

    # Return the most recent N, in chronological order.
    history = history[-max(1, limit):]

    if not history:
        qualifier = f" from {sender_filter}" if sender_filter else ""
        return f"No message history{qualifier} recorded yet."

    return json.dumps([_fmt_message(m) for m in history], indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Send
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_send_message(
    ctx: Context,
    recipients: Annotated[
        list[str],
        "One or more phone numbers (E.164) or base64 group IDs to send to.",
    ],
    message: Annotated[str, "Text body of the message."],
    attachments: Annotated[
        Optional[list[str]],
        "File paths or base64 data URIs to attach (optional).",
    ] = None,
) -> str:
    """Send a Signal message to one or more recipients or groups."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).send_message(
            recipients=recipients,
            message=message,
            attachments=attachments,
        ),
        timeout=cfg.api_timeout,
    )
    if result.success:
        return f"Message sent (timestamp={result.timestamp})."
    raise SignalError(result.error or "Unknown send error")


@mcp.tool()
async def signal_react(
    ctx: Context,
    recipient: Annotated[str, "Phone number (E.164) or group ID of the conversation."],
    emoji: Annotated[str, "Reaction emoji, e.g. 👍."],
    target_author: Annotated[str, "Phone number of the author of the message to react to."],
    target_timestamp: Annotated[int, "Unix-millisecond timestamp of the target message."],
    remove: Annotated[bool, "Set True to remove an existing reaction."] = False,
) -> str:
    """Add or remove an emoji reaction on a specific Signal message."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).send_reaction(
            recipient=recipient,
            emoji=emoji,
            target_author=target_author,
            target_timestamp=target_timestamp,
            remove=remove,
        ),
        timeout=cfg.api_timeout,
    )
    if result.success:
        action = "removed" if remove else "added"
        return f"Reaction {emoji} {action} (timestamp={result.timestamp})."
    raise SignalError(result.error or "Unknown reaction error")


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Groups
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_list_groups(ctx: Context) -> str:
    """List all Signal groups the configured account belongs to."""
    cfg = _cfg(ctx)
    groups = await asyncio.wait_for(
        _client(ctx).list_groups(),
        timeout=cfg.api_timeout,
    )
    if not groups:
        return "No groups found."
    return json.dumps(groups, indent=2, default=str)


@mcp.tool()
async def signal_create_group(
    ctx: Context,
    name: Annotated[str, "Display name for the new group."],
    members: Annotated[
        list[str],
        "Phone numbers (E.164) of the initial group members.",
    ],
    description: Annotated[str, "Optional group description."] = "",
) -> str:
    """Create a new Signal group and return its ID and invite link."""
    cfg = _cfg(ctx)
    result = await asyncio.wait_for(
        _client(ctx).create_group(
            name=name,
            members=members,
            description=description,
        ),
        timeout=cfg.api_timeout,
    )
    return json.dumps(result, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Contacts
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_list_contacts(ctx: Context) -> str:
    """Return the Signal address book for the configured account."""
    cfg = _cfg(ctx)
    contacts = await asyncio.wait_for(
        _client(ctx).list_contacts(),
        timeout=cfg.api_timeout,
    )
    if not contacts:
        return "No contacts found."
    return json.dumps(contacts, indent=2, default=str)


@mcp.tool()
async def signal_update_contact(
    ctx: Context,
    number: Annotated[str, "Contact phone number in E.164 format."],
    name: Annotated[str, "New local display name for the contact."],
    expiration_seconds: Annotated[
        int,
        "Disappearing-message timer in seconds. 0 disables the timer.",
    ] = 0,
) -> str:
    """Update a contact's local display name and disappearing-message timer."""
    cfg = _cfg(ctx)
    await asyncio.wait_for(
        _client(ctx).update_contact(
            number=number,
            name=name,
            expiration=expiration_seconds,
        ),
        timeout=cfg.api_timeout,
    )
    timer_info = f", timer={expiration_seconds}s" if expiration_seconds else ""
    return f"Contact {number} updated → name={name!r}{timer_info}."


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Profile
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_set_profile(
    ctx: Context,
    name: Annotated[str, "Profile display name."],
    about: Annotated[str, "About / bio text (optional)."] = "",
    emoji: Annotated[str, "Profile emoji (optional)."] = "",
) -> str:
    """Update the Signal profile name, bio, and emoji for the configured account."""
    cfg = _cfg(ctx)
    await asyncio.wait_for(
        _client(ctx).set_profile(name=name, about=about, emoji=emoji),
        timeout=cfg.api_timeout,
    )
    return f"Profile updated → name={name!r} about={about!r} emoji={emoji!r}."


# ──────────────────────────────────────────────────────────────────────────────
# Tools — Account
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def signal_account_info(ctx: Context) -> str:
    """
    Return registration and profile information for the configured Signal account.
    Useful to confirm which number is active and check registration status.
    """
    cfg = _cfg(ctx)
    info = await asyncio.wait_for(
        _client(ctx).account_info(),
        timeout=cfg.api_timeout,
    )
    return json.dumps(info, indent=2, default=str)


@mcp.tool()
async def signal_list_accounts(ctx: Context) -> str:
    """
    List all phone numbers registered on the signal-cli REST API instance.
    Helpful when the server manages multiple accounts.
    """
    cfg = _cfg(ctx)
    accounts = await asyncio.wait_for(
        _client(ctx).list_accounts(),
        timeout=cfg.api_timeout,
    )
    if not accounts:
        return "No accounts registered on this signal-cli instance."
    return "\n".join(accounts)


# ──────────────────────────────────────────────────────────────────────────────
# Resources — raw JSON for programmatic / agentic consumers
# ──────────────────────────────────────────────────────────────────────────────

@mcp.resource("signal://messages/live")
async def resource_live_messages() -> str:
    """
    Raw JSON snapshot of messages returned by a single REST poll.
    Useful for programmatic consumers that want a one-shot read without
    interacting with the background buffer.
    """
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
        receive_timeout=cfg.receive_timeout,
    ) as client:
        messages = await client.receive_messages()
    return json.dumps([_fmt_message(m) for m in messages], indent=2, default=str)


@mcp.resource("signal://groups/list")
async def resource_groups() -> str:
    """Raw JSON list of all groups the account belongs to."""
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
    ) as client:
        groups = await client.list_groups()
    return json.dumps(groups, indent=2, default=str)


@mcp.resource("signal://contacts/list")
async def resource_contacts() -> str:
    """Raw JSON address book for the configured Signal account."""
    cfg = load_config()
    async with SignalClient(
        base_url=cfg.api_base_url,
        account=cfg.account_number or "",
        timeout=cfg.api_timeout,
    ) as client:
        contacts = await client.list_contacts()
    return json.dumps(contacts, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Entry-point  (called by __main__.py and directly)
# ──────────────────────────────────────────────────────────────────────────────

def _pick_transport() -> str:
    """Return 'sse' if --transport sse is in argv, otherwise 'stdio'."""
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return "stdio"


async def serve(cfg: Optional[SignalConfig] = None) -> None:
    """Async entry-point kept for backward compatibility with __main__.py."""
    mcp.run(transport=_pick_transport())


if __name__ == "__main__":
    mcp.run(transport=_pick_transport())
