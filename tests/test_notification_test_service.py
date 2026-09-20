"""回归：通知规则的"测试发送"。

上游 ``app/services/notification_test.py`` 从未提交，服务因此无法启动。
本文件锁定重建实现的关键约束：限流生效、错误不泄露渠道密钥、
成功回执与前端 ``NotificationTestResponse`` 契约一致。
"""

import asyncio
import unittest

from app.services.notification_channels import NotificationChannelConfigError
from app.services.notification_sender import (
    NotificationNetworkError,
    NotificationSendTimeout,
)
from app.services.notification_test import (
    NotificationTestError,
    NotificationTestRateLimiter,
    NotificationTestService,
)


class _FakeDB:
    def __init__(self, target):
        self.target = target

    def get_notification_test_target(self, rule_id, user_id):
        if self.target is None:
            return None
        return self.target


class _FakeSender:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send(self, channel_type, config, message, *, request_id=None):
        self.calls.append(
            {"channel_type": channel_type, "config": config,
             "message": message, "request_id": request_id}
        )
        if self.error:
            raise self.error
        return None


TARGET = {
    "id": 7,
    "cookie_id": "acc-1",
    "channel_id": 3,
    "name": "订单通知",
    "event_types": ["order_paid"],
    "enabled": True,
    "channel_name": "我的钉钉",
    "channel_type": "dingtalk",
    "channel_config": '{"webhook": "https://example.com/hook?access_token=SECRET"}',
    "channel_enabled": True,
}


def make_service(target=None, error=None, limiter=None):
    """Build a service with a permissive limiter unless one is supplied.

    不传 limiter 时用的是**独立**实例而不是模块级单例：单例的计数会跨用例累积，
    导致后续用例莫名拿到 429。需要验证限流本身时显式传入 limiter。
    """
    if limiter is None:
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=1000, cooldown_seconds=0
        )
    sender = _FakeSender(error=error)
    service = NotificationTestService(
        _FakeDB(TARGET if target is None else target),
        sender=sender,
        limiter=limiter,
    )
    return service, sender


class NotificationTestRateLimiterTests(unittest.TestCase):
    def test_allows_requests_within_the_window(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=3, cooldown_seconds=0, clock=lambda: now[0]
        )
        for _ in range(3):
            limiter.check(1)
            now[0] += 1

    def test_blocks_beyond_the_window_limit(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=2, cooldown_seconds=0, clock=lambda: now[0]
        )
        limiter.check(1)
        now[0] += 1
        limiter.check(1)
        now[0] += 1
        with self.assertRaises(NotificationTestError) as ctx:
            limiter.check(1)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIsNotNone(ctx.exception.retry_after)

    def test_cooldown_blocks_back_to_back_requests(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=10, cooldown_seconds=3, clock=lambda: now[0]
        )
        limiter.check(1)
        with self.assertRaises(NotificationTestError):
            limiter.check(1)

    def test_limits_are_per_user(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=1, cooldown_seconds=0, clock=lambda: now[0]
        )
        limiter.check(1)
        limiter.check(2)  # 另一个用户不受影响

    def test_window_slides(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=10, window_limit=1, cooldown_seconds=0, clock=lambda: now[0]
        )
        limiter.check(1)
        with self.assertRaises(NotificationTestError):
            limiter.check(1)
        now[0] = 11.0
        limiter.check(1)  # 窗口已滑过

    def test_reset_clears_state(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=1, cooldown_seconds=0, clock=lambda: now[0]
        )
        limiter.check(1)
        limiter.reset(1)
        limiter.check(1)


class NotificationTestServiceTests(unittest.TestCase):
    def test_success_receipt_matches_frontend_contract(self):
        service, sender = make_service()

        result = asyncio.run(service.send_rule_test(7, 1))

        self.assertTrue(result["success"])
        self.assertEqual(result["channel"]["id"], 3)
        self.assertEqual(result["channel"]["name"], "我的钉钉")
        self.assertEqual(result["channel"]["type"], "dingtalk")
        self.assertTrue(result["request_id"])
        self.assertIn("sent_at", result)
        self.assertIsInstance(result["duration_ms"], int)
        self.assertEqual(len(sender.calls), 1)

    def test_request_id_is_passed_to_the_sender(self):
        service, sender = make_service()

        result = asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(sender.calls[0]["request_id"], result["request_id"])

    def test_missing_rule_raises_404(self):
        service, _ = make_service(target=False)

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "notification_rule_not_found")

    def test_invalid_rule_id_raises_400(self):
        service, _ = make_service()

        for bad in ("abc", None, 0, -1):
            with self.assertRaises(NotificationTestError) as ctx:
                asyncio.run(service.send_rule_test(bad, 1))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_test_message_never_leaks_channel_secrets(self):
        service, sender = make_service()

        asyncio.run(service.send_rule_test(7, 1))

        message = sender.calls[0]["message"]
        self.assertNotIn("SECRET", message)
        self.assertNotIn("access_token", message)

    def test_error_detail_never_leaks_channel_secrets(self):
        service, _ = make_service(error=NotificationNetworkError())

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        detail = ctx.exception.detail()
        self.assertNotIn("SECRET", str(detail))
        self.assertIn("message", detail)

    def test_config_error_maps_to_400(self):
        service, _ = make_service(error=NotificationChannelConfigError("webhook 缺失"))

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "notification_channel_invalid")

    def test_timeout_maps_to_504(self):
        service, _ = make_service(error=NotificationSendTimeout())

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(ctx.exception.status_code, 504)

    def test_network_error_maps_to_502(self):
        service, _ = make_service(error=NotificationNetworkError())

        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, "notification_send_failed")

    def test_rate_limit_is_enforced_by_the_service(self):
        now = [0.0]
        limiter = NotificationTestRateLimiter(
            window_seconds=60, window_limit=1, cooldown_seconds=0, clock=lambda: now[0]
        )
        service, sender = make_service(limiter=limiter)

        asyncio.run(service.send_rule_test(7, 1))
        with self.assertRaises(NotificationTestError) as ctx:
            asyncio.run(service.send_rule_test(7, 1))

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(len(sender.calls), 1, "被限流时不应真的发出第二条")

    def test_failed_send_does_not_return_a_receipt(self):
        service, _ = make_service(error=NotificationNetworkError())

        with self.assertRaises(NotificationTestError):
            asyncio.run(service.send_rule_test(7, 1))


if __name__ == "__main__":
    unittest.main()
