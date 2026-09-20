"""Split a delivery payload into platform-sized messages and send them.

Why this module exists
----------------------
``XianyuLive.send_msg`` hands the text straight to the IM socket and the
platform rejects oversized payloads.  ``XianyuLive.send_im_text`` is stricter
still: it refuses anything above 2000 characters.  A card secret (卡密) can
easily be longer than that once a seller puts several codes, a usage note and a
thank-you line into one delivery rule.

The manual "resend delivery" path therefore needs to slice one payload into
several sendable messages and report how many went out, so the API can tell the
operator what happened.  That is the entire job of :func:`send_payload`.

Delivery payloads come in two shapes, matching what ``_auto_delivery`` returns:

* ``__IMAGE_SEND__<card_id>|<image_url>`` - an image card, sent as one image
  message.  ``card_id`` may be missing in the legacy ``__IMAGE_SEND__<url>``
  form.
* anything else - plain text, sliced by :func:`split_delivery_text`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

__all__ = [
    "IMAGE_SEND_MARKER",
    "MAX_SEGMENT_CHARS",
    "SEGMENT_INTERVAL_SECONDS",
    "split_delivery_text",
    "send_payload",
]

#: Prefix ``_auto_delivery`` uses to flag an image card.
IMAGE_SEND_MARKER = "__IMAGE_SEND__"

#: The platform rejects longer messages, and ``send_im_text`` enforces the same
#: ceiling locally.  Keep a margin so multi-byte content never trips it.
MAX_SEGMENT_CHARS = 1800

#: Breathing room between consecutive segments of the same payload.  Sending
#: them back-to-back gets the conversation rate-limited by the platform.
SEGMENT_INTERVAL_SECONDS = 0.8


def split_delivery_text(text: str, *, max_chars: int = MAX_SEGMENT_CHARS) -> list[str]:
    """Slice ``text`` into segments of at most ``max_chars`` characters.

    Splits on line boundaries first, because card secrets and usage notes are
    line-oriented and a mid-line cut looks broken to the buyer.  Only a single
    line longer than the limit is hard-split.

    Returns an empty list for blank input so callers can treat "nothing to send"
    distinctly from "sent one segment".
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.strip()
    if not text:
        return []

    limit = max(int(max_chars), 1)
    segments: list[str] = []
    buffer: list[str] = []

    def buffered_length() -> int:
        """Length the buffer would have once joined with newlines."""
        if not buffer:
            return 0
        return sum(len(item) for item in buffer) + len(buffer) - 1

    for line in text.split("\n"):
        # 单行本身就超限：先把已攒的整段发掉，再按长度硬切这一行。
        while len(line) > limit:
            if buffer:
                segments.append("\n".join(buffer))
                buffer = []
            segments.append(line[:limit])
            line = line[limit:]

        if buffer and buffered_length() + 1 + len(line) > limit:
            segments.append("\n".join(buffer))
            buffer = []

        buffer.append(line)

    if buffer:
        segments.append("\n".join(buffer))

    return [segment for segment in segments if segment.strip()]


async def send_payload(
    live_instance: Any,
    ws: Any,
    chat_id: str,
    buyer_id: str,
    content: Any,
) -> int:
    """Send one delivery payload to a buyer and return the segment count.

    ``live_instance`` is the account runtime (``XianyuLive``); ``ws`` is the
    socket it is currently listening on.  Both are passed in rather than read
    off globals so the caller stays in control of which connection is used.

    Raises ``ValueError`` when there is nothing to send, and lets socket errors
    propagate - the caller records them per payload and moves on.
    """
    text = "" if content is None else str(content)

    if text.startswith(IMAGE_SEND_MARKER):
        await _send_image_payload(live_instance, ws, chat_id, buyer_id, text)
        return 1

    segments = split_delivery_text(text)
    if not segments:
        raise ValueError("发货内容为空，无法发送")

    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(SEGMENT_INTERVAL_SECONDS)
        await live_instance.send_msg(ws, chat_id, buyer_id, segment)

    logger.info(
        f"【{getattr(live_instance, 'myid', '')}】发货内容已分段发送: "
        f"{len(segments)} 段 / 共 {sum(len(s) for s in segments)} 字"
    )
    return len(segments)


async def _send_image_payload(
    live_instance: Any,
    ws: Any,
    chat_id: str,
    buyer_id: str,
    payload: str,
) -> None:
    """Send the ``__IMAGE_SEND__`` form of a delivery payload."""
    image_data = payload[len(IMAGE_SEND_MARKER):]
    card_id: Optional[int] = None

    if "|" in image_data:
        card_id_str, image_url = image_data.split("|", 1)
        try:
            card_id = int(card_id_str)
        except ValueError:
            logger.error(f"无效的卡券ID: {card_id_str}")
            card_id = None
    else:
        # 兼容旧格式（没有卡券ID）
        image_url = image_data

    image_url = image_url.strip()
    if not image_url:
        raise ValueError("发货图片地址为空，无法发送")

    await live_instance.send_image_msg(
        ws, chat_id, buyer_id, image_url, card_id=card_id
    )
