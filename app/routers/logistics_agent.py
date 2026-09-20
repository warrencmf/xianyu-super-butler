"""Authenticated management API for the logistics quoting agent (物流 Agent).

Reconstructed module
--------------------
``reply_server`` imports ``create_logistics_agent_router`` from here, but the
upstream repository never committed this file, so the service could not start.
This implementation was rebuilt from the parts that *are* committed:

* ``app/db_manager.py`` - the ``logistics_agent_settings``,
  ``logistics_quote_sessions``, ``logistics_quote_send_logs``,
  ``logistics_agent_training_samples`` and ``logistics_agent_training_rounds``
  schemas, including every column default.
* ``frontend/services/api.ts`` - contains **no** logistics-agent calls, so no
  response shape could be recovered.  Nothing in the UI depends on this router.

Implemented here (fully backed by the schema):

=================================================  ========================
``GET    /api/logistics/agent/settings``            列出账号配置
``GET    /api/logistics/agent/settings/{cookie}``   读取单个账号配置
``PUT    /api/logistics/agent/settings/{cookie}``   写入账号配置
``GET    /api/logistics/agent/training/rounds``     列出训练轮次
``DELETE /api/logistics/agent/training/rounds/{id}``删除训练轮次
=================================================  ========================

Deliberately **not** implemented (returns 501 with an explicit reason):

* the conversational quoting runtime (multi-turn state machine driven by an
  LLM), and
* training-sample generation.

Those are behavioural features whose original logic is not recoverable from the
repository.  Returning 501 keeps the failure visible instead of silently
answering with fabricated quotes.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from loguru import logger

__all__ = ["create_logistics_agent_router"]


#: 与 ``logistics_agent_settings`` 的列默认值保持一致。
SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "model_name": "deepseek-v4-flash",
    "book_ids": [],
    "auto_send": False,
    "recommend_mode": "lowest",
    "no_route_policy": "manual",
    "item_scope": "all",
    "item_ids": [],
    "carrier_config": {},
    "default_volume_ratios": {},
    "pricing_config": {},
    "templates": {},
}

_JSON_COLUMNS = (
    "book_ids",
    "item_ids",
    "carrier_config",
    "default_volume_ratios",
    "pricing_config",
    "templates",
)

_UNIMPLEMENTED_DETAIL = {
    "code": "logistics_agent_not_implemented",
    "message": (
        "物流 Agent 的会话推理与训练样本生成未包含在上游仓库中，"
        "本次部署未实现该能力。账号配置读写不受影响。"
    ),
}


class AgentSettingsUpdate(BaseModel):
    """Partial update; omitted fields keep their stored value."""

    enabled: Optional[bool] = None
    model_name: Optional[str] = Field(default=None, max_length=120)
    book_ids: Optional[list[int]] = None
    auto_send: Optional[bool] = None
    recommend_mode: Optional[str] = Field(default=None, max_length=32)
    no_route_policy: Optional[str] = Field(default=None, max_length=32)
    item_scope: Optional[str] = Field(default=None, max_length=32)
    item_ids: Optional[list[str]] = None
    carrier_config: Optional[dict[str, Any]] = None
    default_volume_ratios: Optional[dict[str, Any]] = None
    pricing_config: Optional[dict[str, Any]] = None
    templates: Optional[dict[str, Any]] = None


def create_logistics_agent_router(
    get_current_user: Callable[..., dict[str, Any]],
    db_manager: Any,
) -> APIRouter:
    router = APIRouter()

    def require_account(cookie_id: str, current_user: dict[str, Any]) -> dict[str, Any]:
        details = db_manager.get_cookie_details(cookie_id)
        if not details or details.get("user_id") != current_user["user_id"]:
            raise HTTPException(status_code=404, detail="账号不存在或无权限")
        return details

    # ------------------------------------------------------------------
    # 账号配置
    # ------------------------------------------------------------------
    @router.get("/api/logistics/agent/settings")
    def list_agent_settings(current_user: dict[str, Any] = Depends(get_current_user)):
        user_id = int(current_user["user_id"])
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                """
                SELECT s.cookie_id, s.enabled, s.model_name, s.book_ids, s.auto_send,
                       s.recommend_mode, s.no_route_policy, s.item_scope, s.item_ids,
                       s.carrier_config, s.default_volume_ratios, s.pricing_config,
                       s.templates, s.updated_at
                FROM logistics_agent_settings s
                JOIN cookies c ON c.id = s.cookie_id
                WHERE c.user_id = ?
                ORDER BY s.updated_at DESC
                """,
                (user_id,),
            )
            rows = cursor.fetchall()

        settings = []
        for row in rows:
            # row[0] 是 cookie_id，其余 13 列与 _decode_row 的列顺序对齐。
            record = _decode_row(row[1:])
            record["cookie_id"] = row[0]
            settings.append(record)
        return {"success": True, "settings": settings}

    @router.get("/api/logistics/agent/settings/{cookie_id}")
    def get_agent_settings(
        cookie_id: str,
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        require_account(cookie_id, current_user)
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                """
                SELECT enabled, model_name, book_ids, auto_send, recommend_mode,
                       no_route_policy, item_scope, item_ids, carrier_config,
                       default_volume_ratios, pricing_config, templates, updated_at
                FROM logistics_agent_settings
                WHERE cookie_id = ?
                """,
                (cookie_id,),
            )
            row = cursor.fetchone()

        if not row:
            # 未配置过就返回默认值，前端无需特判 404。
            return {
                "success": True,
                "configured": False,
                "cookie_id": cookie_id,
                "settings": dict(SETTINGS_DEFAULTS),
            }

        record = _decode_row(row)
        record["cookie_id"] = cookie_id
        return {"success": True, "configured": True, "settings": record}

    @router.put("/api/logistics/agent/settings/{cookie_id}")
    def update_agent_settings(
        cookie_id: str,
        update: AgentSettingsUpdate,
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        require_account(cookie_id, current_user)
        changes = update.model_dump(exclude_unset=True)

        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM logistics_agent_settings WHERE cookie_id = ?",
                (cookie_id,),
            )
            exists = cursor.fetchone() is not None

            if exists and changes:
                assignments = ", ".join(f"{column} = ?" for column in changes)
                values = [_encode_value(column, value) for column, value in changes.items()]
                cursor.execute(
                    f"UPDATE logistics_agent_settings SET {assignments}, "
                    "updated_at = CURRENT_TIMESTAMP WHERE cookie_id = ?",
                    (*values, cookie_id),
                )
            elif not exists:
                merged = dict(SETTINGS_DEFAULTS)
                merged.update(changes)
                columns = list(SETTINGS_DEFAULTS.keys())
                placeholders = ", ".join("?" for _ in columns)
                cursor.execute(
                    f"INSERT INTO logistics_agent_settings (cookie_id, {', '.join(columns)}) "
                    f"VALUES (?, {placeholders})",
                    (cookie_id, *[_encode_value(name, merged[name]) for name in columns]),
                )
            db_manager.conn.commit()

        logger.info(
            f"物流 Agent 配置已更新 user_id={current_user.get('user_id')} "
            f"cookie_id={cookie_id} fields={sorted(changes)}"
        )
        return get_agent_settings(cookie_id, current_user)

    # ------------------------------------------------------------------
    # 训练轮次（只读 + 删除）
    # ------------------------------------------------------------------
    @router.get("/api/logistics/agent/training/rounds")
    def list_training_rounds(
        cookie_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        user_id = int(current_user["user_id"])
        if cookie_id:
            require_account(cookie_id, current_user)

        sql = (
            "SELECT id, cookie_id, thread_id, name, created_at, "
            "LENGTH(messages_json) FROM logistics_agent_training_rounds WHERE user_id = ?"
        )
        params: list[Any] = [user_id]
        if cookie_id:
            sql += " AND cookie_id = ?"
            params.append(cookie_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

        return {
            "success": True,
            "rounds": [
                {
                    "id": row[0],
                    "cookie_id": row[1],
                    "thread_id": row[2],
                    "name": row[3],
                    "created_at": row[4],
                    "messages_bytes": row[5],
                }
                for row in rows
            ],
        }

    @router.delete("/api/logistics/agent/training/rounds/{round_id}")
    def delete_training_round(
        round_id: str,
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        user_id = int(current_user["user_id"])
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                "DELETE FROM logistics_agent_training_rounds WHERE id = ? AND user_id = ?",
                (round_id, user_id),
            )
            deleted = cursor.rowcount
            db_manager.conn.commit()

        if not deleted:
            raise HTTPException(status_code=404, detail="训练轮次不存在")
        return {"success": True}

    # ------------------------------------------------------------------
    # 未实现的能力：显式 501，绝不返回编造的报价
    # ------------------------------------------------------------------
    @router.post("/api/logistics/agent/quote")
    def run_agent_quote(current_user: dict[str, Any] = Depends(get_current_user)):
        raise HTTPException(status_code=501, detail=_UNIMPLEMENTED_DETAIL)

    @router.post("/api/logistics/agent/training/rounds")
    def create_training_round(current_user: dict[str, Any] = Depends(get_current_user)):
        raise HTTPException(status_code=501, detail=_UNIMPLEMENTED_DETAIL)

    return router


def _decode_value(column: str, value: Any) -> Any:
    """Decode a stored JSON column into its Python value."""
    if column not in _JSON_COLUMNS:
        return bool(value) if column in ("enabled", "auto_send") else value
    if value is None:
        return SETTINGS_DEFAULTS.get(column)
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        logger.warning(f"物流 Agent 配置列 {column} 不是合法 JSON，已回退默认值")
        return SETTINGS_DEFAULTS.get(column)


def _decode_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Decode a settings row (without cookie_id) into a dict."""
    columns = [
        "enabled",
        "model_name",
        "book_ids",
        "auto_send",
        "recommend_mode",
        "no_route_policy",
        "item_scope",
        "item_ids",
        "carrier_config",
        "default_volume_ratios",
        "pricing_config",
        "templates",
        "updated_at",
    ]
    return {
        column: _decode_value(column, value)
        for column, value in zip(columns, row)
    }


def _encode_value(column: str, value: Any) -> Any:
    """Encode a Python value for storage in a settings column."""
    if column in _JSON_COLUMNS:
        return json.dumps(value if value is not None else SETTINGS_DEFAULTS[column], ensure_ascii=False)
    if column in ("enabled", "auto_send"):
        return 1 if value else 0
    return value
