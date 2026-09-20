"""回归：发货内容分段与发送。

上游 ``app/reply_server.py`` 引用了 ``app.delivery_template.send_payload``，
但该模块从未提交到仓库，服务因此无法启动。本文件锁定补齐实现的关键行为：
分段不超平台上限、不丢字符、图片标记走图片通道。
"""

import asyncio
import unittest

from app.delivery_template import (
    IMAGE_SEND_MARKER,
    MAX_SEGMENT_CHARS,
    send_payload,
    split_delivery_text,
)


class SplitDeliveryTextTests(unittest.TestCase):
    def test_blank_input_yields_no_segments(self):
        for value in ("", "   ", "\n\n", None):
            self.assertEqual(split_delivery_text(value), [])

    def test_short_text_stays_in_one_segment(self):
        self.assertEqual(split_delivery_text("卡密：ABC-123"), ["卡密：ABC-123"])

    def test_lines_are_kept_together_when_they_fit(self):
        segments = split_delivery_text("第一行\n第二行", max_chars=20)
        self.assertEqual(segments, ["第一行\n第二行"])

    def test_oversized_single_line_is_hard_split(self):
        segments = split_delivery_text("x" * 10, max_chars=3)
        self.assertEqual(segments, ["xxx", "xxx", "xxx", "x"])

    def test_no_segment_exceeds_the_limit(self):
        text = "\n".join(["行" * 7 for _ in range(50)])
        segments = split_delivery_text(text, max_chars=30)
        self.assertTrue(all(len(segment) <= 30 for segment in segments))

    def test_no_content_is_lost(self):
        text = "l1\nl2\n" + "z" * 30 + "\nl4"
        segments = split_delivery_text(text, max_chars=10)
        self.assertEqual("".join(segments).replace("\n", ""), "l1l2" + "z" * 30 + "l4")

    def test_exact_limit_is_a_single_segment(self):
        segments = split_delivery_text("y" * 20, max_chars=20)
        self.assertEqual(segments, ["y" * 20])

    def test_default_limit_stays_under_platform_ceiling(self):
        # 平台与 send_im_text 都按 2000 字符卡；留了余量。
        self.assertLess(MAX_SEGMENT_CHARS, 2000)
        segments = split_delivery_text("k" * 5000)
        self.assertTrue(all(len(segment) <= MAX_SEGMENT_CHARS for segment in segments))


class _FakeLive:
    """Minimal stand-in for XianyuLive."""

    def __init__(self):
        self.myid = "acc-1"
        self.texts = []
        self.images = []

    async def send_msg(self, ws, cid, toid, text):
        self.texts.append((ws, cid, toid, text))

    async def send_image_msg(self, ws, cid, toid, image_url, width=800, height=600, card_id=None):
        self.images.append((ws, cid, toid, image_url, card_id))


class SendPayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_text_payload_returns_segment_count(self):
        live = _FakeLive()

        async def run():
            return await send_payload(live, "WS", "chat-1", "buyer-1", "hello")

        count = asyncio.run(run())
        self.assertEqual(count, 1)
        self.assertEqual(live.texts, [("WS", "chat-1", "buyer-1", "hello")])

    def test_long_payload_is_split_and_all_segments_sent(self):
        live = _FakeLive()
        content = "\n".join(["a" * 900 for _ in range(5)])

        count = asyncio.run(send_payload(live, "WS", "chat-1", "buyer-1", content))

        self.assertGreater(count, 1)
        self.assertEqual(len(live.texts), count)
        self.assertTrue(all(len(item[3]) <= MAX_SEGMENT_CHARS for item in live.texts))

    def test_empty_payload_raises(self):
        live = _FakeLive()
        with self.assertRaises(ValueError):
            asyncio.run(send_payload(live, "WS", "chat-1", "buyer-1", "   "))

    def test_image_marker_goes_to_image_channel(self):
        live = _FakeLive()
        payload = f"{IMAGE_SEND_MARKER}42|https://example.com/a.png"

        count = asyncio.run(send_payload(live, "WS", "chat-1", "buyer-1", payload))

        self.assertEqual(count, 1)
        self.assertEqual(live.texts, [])
        self.assertEqual(live.images, [("WS", "chat-1", "buyer-1", "https://example.com/a.png", 42)])

    def test_legacy_image_marker_without_card_id(self):
        live = _FakeLive()
        payload = f"{IMAGE_SEND_MARKER}https://example.com/b.png"

        asyncio.run(send_payload(live, "WS", "chat-1", "buyer-1", payload))

        self.assertEqual(live.images, [("WS", "chat-1", "buyer-1", "https://example.com/b.png", None)])

    def test_image_marker_with_empty_url_raises(self):
        live = _FakeLive()
        with self.assertRaises(ValueError):
            asyncio.run(send_payload(live, "WS", "chat-1", "buyer-1", f"{IMAGE_SEND_MARKER}7|"))


if __name__ == "__main__":
    unittest.main()
