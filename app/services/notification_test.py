"""On-demand test delivery for account notification rules.

The account runtime sends notifications opportunistically and swallows provider
errors.  The management API needs the opposite contract: an operator clicks
"test" and must get one unambiguous, non-sensitive answer about whether the
configured channel actually accepts messages.

This module owns three things:

* :class:`NotificationTestRateLimiter` - keeps a single operator from hammering
  a provider (and from tripping the provider's own abuse protection).
* :class:`NotificationTestError` - a structured failure the API layer can turn
  into an HTTP response without leaking provider internals.
* :class:`NotificationTestService` - looks up the rule, sends one real message
  and reports what happened.

Nothing here reads or logs channel secrets: only the channel name and type ever
leave this module.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from loguru import logger

from app.services.notification_channels import NotificationChannelConfigError
from app.services.notification_sender import (
    NotificationProviderRejected,
    NotificationSendError,
    NotificationSender,
    NotificationSendTimeout,
)

__all__ = [
    "NotificationTestError",
    "NotificationTestRateLimiter",
    "NotificationTestService",
    "notification_test_rate_limiter",
]

# 北京时间，和系统其它面向用户的输出保持一致。
_CST = timezone(timedelta(hours=8))

# 单用户默认限流：60 秒窗口内最多 5 次，两次之间至少间隔 3 秒。
_DEFAULT_WINDOW_SECONDS = 60.0
_DEFAULT_WINDOW_LIMIT = 5
_DEFAULT_COOLDOWN_SECONDS = 3.0

_TEST_MESSAGE_TEMPLATE = (
    "【闲鱼超级管家】这是一条测试通知。\n"
    "通知规则：{rule_name}\n"
    "通知渠道：{channel_name}（{channel_type}）\n"
    "发送时间：{sent_at}\n"
    "请求 ID：{request_id}\n"
    "收到本条消息说明该渠道配置可用。"
)

# 渠道类型 -> 中文标签，只用于拼测试文案。
_CHANNEL_LABELS = {
    "dingtalk": "钉钉",
    "feishu": "飞书",
    "bark": "Bark",
    "email": "邮件",
    "webhook": "Webhook",
    "wechat": "微信",
    "telegram": "Telegram",
    "qq": "QQ",
}


class NotificationTestError(RuntimeError):
    """Structured, non-sensitive failure for the test-send endpoint."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after

    def detail(self) -> dict[str, Any]:
        """Shape the payload the frontend already knows how to read."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after:
            payload["retry_after"] = self.retry_after
        return payload


class NotificationTestRateLimiter:
    """Per-user sliding-window limiter with a minimum gap between sends.

    Deliberately in-process and in-memory: the test endpoint exists to debug
    configuration, so a restart clearing the counters is acceptable and keeps
    the service free of shared state.
    """

    def __init__(
        self,
        *,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        window_limit: int = _DEFAULT_WINDOW_LIMIT,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = max(float(window_seconds), 1.0)
        self.window_limit = max(int(window_limit), 1)
        self.cooldown_seconds = max(float(cooldown_seconds), 0.0)
        self._clock = clock
        self._history: dict[str, list[float]] = {}

    def check(self, user_id: Any) -> None:
        """Raise :class:`NotificationTestError` when the caller must wait."""
        key = str(user_id)
        now = self._clock()
        stamps = [t for t in self._history.get(key, []) if now - t < self.window_seconds]

        if stamps:
            gap = now - stamps[-1]
            if gap < self.cooldown_seconds:
                wait = int(self.cooldown_seconds - gap) + 1
                self._history[key] = stamps
                raise NotificationTestError(
                    "notification_test_too_frequent",
                    f"操作过于频繁，请 {wait} 秒后再试",
                    status_code=429,
                    retry_after=wait,
                )

        if len(stamps) >= self.window_limit:
            wait = int(self.window_seconds - (now - stamps[0])) + 1
            self._history[key] = stamps
            raise NotificationTestError(
                "notification_test_rate_limited",
                f"{int(self.window_seconds)} 秒内最多测试 {self.window_limit} 次，请 {wait} 秒后再试",
                status_code=429,
                retry_after=wait,
            )

        stamps.append(now)
        self._history[key] = stamps

    def reset(self, user_id: Any = None) -> None:
        """Clear counters for one user, or everyone when omitted."""
        if user_id is None:
            self._history.clear()
        else:
            self._history.pop(str(user_id), None)


# 模块级单例，供 reply_server 直接注入。
notification_test_rate_limiter = NotificationTestRateLimiter()


class NotificationTestService:
    """Send one real test message through one rule's configured channel."""

    def __init__(
        self,
        db_manager: Any,
        *,
        sender: Optional[NotificationSender] = None,
        limiter: Optional[NotificationTestRateLimiter] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db_manager = db_manager
        self.sender = sender or NotificationSender()
        self.limiter = limiter if limiter is not None else notification_test_rate_limiter
        self._clock = clock

    async def send_rule_test(
        self,
        rule_id: Any,
        user_id: int,
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Validate the rule, send one message, and describe the outcome."""
        normalized_rule_id = self._normalize_rule_id(rule_id)

        target = self.db_manager.get_notification_test_target(normalized_rule_id, user_id)
        if not target:
            raise NotificationTestError(
                "notification_rule_not_found",
                "通知规则不存在或无权访问",
                status_code=404,
            )

        if self.limiter is not None:
            self.limiter.check(user_id)

        request_id = uuid.uuid4().hex
        sent_at = datetime.now(_CST)
        channel_type = str(target.get("channel_type") or "")
        channel_name = str(target.get("channel_name") or "")

        message = _TEST_MESSAGE_TEMPLATE.format(
            rule_name=str(target.get("name") or f"规则 {normalized_rule_id}"),
            channel_name=channel_name or "未命名渠道",
            channel_type=_CHANNEL_LABELS.get(channel_type, channel_type or "未知类型"),
            sent_at=sent_at.strftime("%Y-%m-%d %H:%M:%S"),
            request_id=request_id,
        )

        started = self._clock()
        try:
            await self.sender.send(
                channel_type,
                target.get("channel_config"),
                message,
                request_id=request_id,
            )
        except NotificationChannelConfigError as exc:
            logger.warning(
                f"通知测试配置无效 rule_id={normalized_rule_id} "
                f"channel_type={channel_type}: {exc}"
            )
            raise NotificationTestError(
                "notification_channel_invalid",
                f"通知渠道配置无效：{exc}",
                status_code=400,
            ) from exc
        except NotificationSendTimeout as exc:
            raise NotificationTestError(
                "notification_send_timeout",
                "通知渠道响应超时，请检查网络或渠道服务状态",
                status_code=504,
            ) from exc
        except NotificationProviderRejected as exc:
            logger.warning(
                f"通知测试被渠道拒绝 rule_id={normalized_rule_id} "
                f"channel_type={channel_type}"
            )
            raise NotificationTestError(
                "notification_provider_rejected",
                "通知渠道拒绝了这条消息，请检查渠道配置或配额",
                status_code=502,
            ) from exc
        except NotificationSendError as exc:
            logger.warning(
                f"通知测试发送失败 rule_id={normalized_rule_id} "
                f"channel_type={channel_type}: {exc}"
            )
            raise NotificationTestError(
                "notification_send_failed",
                "通知发送失败，请检查渠道配置与网络",
                status_code=502,
            ) from exc

        duration_ms = max(0, int((self._clock() - started) * 1000))
        logger.info(
            f"通知测试发送成功 rule_id={normalized_rule_id} "
            f"channel_type={channel_type} duration_ms={duration_ms}"
        )
        return {
            "success": True,
            "message": "测试通知已发送",
            "request_id": request_id,
            "channel": {
                "id": target.get("channel_id"),
                "name": channel_name,
                "type": channel_type,
            },
            "sent_at": sent_at.isoformat(),
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _normalize_rule_id(rule_id: Any) -> int:
        try:
            value = int(rule_id)
        except (TypeError, ValueError) as exc:
            raise NotificationTestError(
                "notification_rule_invalid",
                "通知规则 ID 无效",
                status_code=400,
            ) from exc
        if value <= 0:
            raise NotificationTestError(
                "notification_rule_invalid",
                "通知规则 ID 无效",
                status_code=400,
            )
        return value
