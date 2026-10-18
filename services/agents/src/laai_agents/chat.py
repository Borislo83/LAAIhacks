"""Agent Chat Protocol support so ASI:One (and Agentverse chat) can talk to the agents."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from . import llm

# Takes the handler context and a product name (or the raw text), returns the reply.
Answer = Callable[[Context, str], Awaitable[str]]


def message_text(msg: ChatMessage) -> str:
    return " ".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()


def text_message(text: str, end_session: bool = True) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid4(), content=content)


def build_chat_protocol(answer: Answer, extract_product: bool = True) -> Protocol:
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(
            sender,
            ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=msg.msg_id),
        )
        text = message_text(msg)
        if not text:
            return
        product = await llm.extract_product(text) if extract_product else text
        # Text can be a private message (the messenger agent): keep it out of info logs.
        ctx.logger.info(f"Chat from {sender}")
        ctx.logger.debug(f"Chat text {text!r} -> {product!r}")
        await ctx.send(sender, text_message(await answer(ctx, product)))

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.debug(f"Ack from {sender} for {msg.acknowledged_msg_id}")

    return proto
