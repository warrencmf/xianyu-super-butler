"""Authenticated management API for carrier rate books (物流报价表).

Reconstructed module
--------------------
``reply_server`` imports ``create_logistics_quote_router`` from here, but the
upstream repository never committed this file (nor ``logistics_agent`` /
``delivery_template`` / ``services.notification_test``), so the service could
not start at all.  This implementation was rebuilt from the two artefacts that
*are* in the repository:

* ``frontend/services/api.ts`` - the request paths and the exact response shape
  the UI expects (``LogisticsQuoteParseResponse``, ``LogisticsQuoteBook``).
* ``app/db_manager.py`` - the ``logistics_quote_books`` /
  ``logistics_quote_route_imports`` / ``logistics_quote_routes`` schemas.

Endpoints:

===============================  ===========================================
``POST /api/logistics/quote-sources/parse``  解析上传文件并预览（不落库）
``GET  /api/logistics/quote-books``          列出当前用户的报价表
``POST /api/logistics/quote-books``          解析并保存，同时导入线路明细
``DELETE /api/logistics/quote-books/{id}``   删除报价表及其线路
===============================  ===========================================

Note: the frontend defines these calls but no component invokes them yet, so
this router is currently reachable only by API clients.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger

from app.services.logistics_quote_parser import (
    ParseError,
    SUPPORTED_EXTENSIONS,
    parse_rate_book,
)

__all__ = ["create_logistics_quote_router"]

#: 报价表通常是几千行的 Excel，20MB 足够，也挡住了恶意大文件。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def create_logistics_quote_router(
    get_current_user: Callable[..., dict[str, Any]],
    db_manager: Any,
) -> APIRouter:
    router = APIRouter()

    async def read_upload(file: UploadFile) -> tuple[str, bytes]:
        """Read and validate an uploaded rate book."""
        filename = (file.filename or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="缺少文件名")

        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail="不支持的文件类型，请上传 "
                + " / ".join(f".{item}" for item in SUPPORTED_EXTENSIONS),
            )

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="上传文件为空")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限",
            )
        return filename, content

    def parse_or_400(filename: str, content: bytes) -> dict[str, Any]:
        try:
            return parse_rate_book(filename, content)
        except ParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # 解析预览：只解析，不落库
    # ------------------------------------------------------------------
    @router.post("/api/logistics/quote-sources/parse")
    async def parse_quote_source(
        file: UploadFile = File(...),
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        filename, content = await read_upload(file)
        result = parse_or_400(filename, content)
        logger.info(
            f"物流报价解析预览 user_id={current_user.get('user_id')} "
            f"file={filename} rows={result['summary']['total']}"
        )
        return result

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    @router.get("/api/logistics/quote-books")
    def list_quote_books(current_user: dict[str, Any] = Depends(get_current_user)):
        user_id = int(current_user["user_id"])
        books = _list_books(db_manager, user_id)
        return {"success": True, "books": books}

    # ------------------------------------------------------------------
    # 保存：解析 + 落库 + 导入线路
    # ------------------------------------------------------------------
    @router.post("/api/logistics/quote-books")
    async def create_quote_book(
        file: UploadFile = File(...),
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        user_id = int(current_user["user_id"])
        filename, content = await read_upload(file)
        result = parse_or_400(filename, content)

        payload = {key: value for key, value in result.items() if key != "success"}
        book = _upsert_book(db_manager, user_id, filename, content, payload)
        if book is None:
            raise HTTPException(status_code=500, detail="保存报价表失败")

        route_import, route_warning = _import_routes(
            db_manager, user_id, filename, content, payload
        )

        logger.info(
            f"物流报价表已保存 user_id={user_id} book_id={book['id']} "
            f"file={filename} routes={book['route_count']}"
        )
        return {
            "success": True,
            "book": book,
            "route_import": route_import,
            "route_warning": route_warning,
        }

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    @router.delete("/api/logistics/quote-books/{book_id}")
    def delete_quote_book(
        book_id: int,
        current_user: dict[str, Any] = Depends(get_current_user),
    ):
        user_id = int(current_user["user_id"])
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                "SELECT sha256 FROM logistics_quote_books WHERE id = ? AND user_id = ?",
                (book_id, user_id),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="报价表不存在")

            sha256 = row[0]
            cursor.execute(
                "DELETE FROM logistics_quote_books WHERE id = ? AND user_id = ?",
                (book_id, user_id),
            )
            # 线路明细按 sha256 关联到导入批次，一并清掉，避免孤儿数据参与计费匹配。
            cursor.execute(
                "SELECT id FROM logistics_quote_route_imports WHERE user_id = ? AND sha256 = ?",
                (user_id, sha256),
            )
            import_ids = [item[0] for item in cursor.fetchall()]
            for import_id in import_ids:
                cursor.execute(
                    "DELETE FROM logistics_quote_routes WHERE user_id = ? AND import_id = ?",
                    (user_id, import_id),
                )
            cursor.execute(
                "DELETE FROM logistics_quote_route_imports WHERE user_id = ? AND sha256 = ?",
                (user_id, sha256),
            )
            db_manager.conn.commit()

        logger.info(f"物流报价表已删除 user_id={user_id} book_id={book_id}")
        return {"success": True}

    return router


# ----------------------------------------------------------------------
# 存储辅助（db_manager 未提供物流报价的读写方法，这里就近实现）
# ----------------------------------------------------------------------
def _list_books(db_manager: Any, user_id: int) -> list[dict[str, Any]]:
    with db_manager.lock:
        cursor = db_manager.conn.cursor()
        cursor.execute(
            """
            SELECT id, filename, file_type, size_bytes, sha256, book_kind,
                   service_count, route_count, payload, created_at, updated_at
            FROM logistics_quote_books
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

    books: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[8]) if row[8] else {}
        except (TypeError, ValueError):
            payload = {}
        books.append(
            {
                "id": row[0],
                "filename": row[1],
                "file_type": row[2],
                "size_bytes": row[3],
                "sha256": row[4],
                "book_kind": row[5],
                "service_count": row[6],
                "route_count": row[7],
                "payload": payload,
                "created_at": row[9],
                "updated_at": row[10],
            }
        )
    return books


def _upsert_book(
    db_manager: Any,
    user_id: int,
    filename: str,
    content: bytes,
    payload: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Insert or refresh a book, keyed on (user_id, sha256)."""
    source = payload.get("source", {})
    sha256 = source.get("sha256", "")
    with db_manager.lock:
        cursor = db_manager.conn.cursor()
        cursor.execute(
            """
            INSERT INTO logistics_quote_books
                (user_id, filename, file_type, size_bytes, sha256, book_kind,
                 service_count, route_count, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, sha256) DO UPDATE SET
                filename = excluded.filename,
                file_type = excluded.file_type,
                size_bytes = excluded.size_bytes,
                book_kind = excluded.book_kind,
                service_count = excluded.service_count,
                route_count = excluded.route_count,
                payload = excluded.payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                filename,
                source.get("file_type", ""),
                len(content),
                sha256,
                payload.get("book_kind"),
                int(payload.get("service_count") or 0),
                int(payload.get("route_count") or 0),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        db_manager.conn.commit()

        cursor.execute(
            """
            SELECT id, filename, file_type, size_bytes, sha256, book_kind,
                   service_count, route_count, payload, created_at, updated_at
            FROM logistics_quote_books
            WHERE user_id = ? AND sha256 = ?
            """,
            (user_id, sha256),
        )
        row = cursor.fetchone()

    if not row:
        return None
    try:
        stored_payload = json.loads(row[8]) if row[8] else {}
    except (TypeError, ValueError):
        stored_payload = {}
    return {
        "id": row[0],
        "filename": row[1],
        "file_type": row[2],
        "size_bytes": row[3],
        "sha256": row[4],
        "book_kind": row[5],
        "service_count": row[6],
        "route_count": row[7],
        "payload": stored_payload,
        "created_at": row[9],
        "updated_at": row[10],
    }


def _import_routes(
    db_manager: Any,
    user_id: int,
    filename: str,
    content: bytes,
    payload: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], str]:
    """Persist normalized routes for address matching and pricing lookups.

    Returns ``(route_import, warning)``.  Import failures never fail the upload:
    the book itself is still usable for preview, so a warning is returned
    instead of an error.
    """
    rows = payload.get("rows") or []
    source = payload.get("source", {})
    sha256 = source.get("sha256", "")

    if not rows:
        return None, "未解析出可导入的线路明细，已仅保存报价表"

    try:
        with db_manager.lock:
            cursor = db_manager.conn.cursor()
            cursor.execute(
                """
                INSERT INTO logistics_quote_route_imports
                    (user_id, filename, file_type, size_bytes, sha256, book_kind,
                     service_count, route_count, status, warnings, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, sha256) DO UPDATE SET
                    filename = excluded.filename,
                    file_type = excluded.file_type,
                    size_bytes = excluded.size_bytes,
                    book_kind = excluded.book_kind,
                    service_count = excluded.service_count,
                    route_count = excluded.route_count,
                    warnings = excluded.warnings
                """,
                (
                    user_id,
                    filename,
                    source.get("file_type", ""),
                    len(content),
                    sha256,
                    payload.get("book_kind"),
                    int(payload.get("service_count") or 0),
                    len(rows),
                    json.dumps(payload.get("warnings") or [], ensure_ascii=False),
                ),
            )
            cursor.execute(
                "SELECT id FROM logistics_quote_route_imports WHERE user_id = ? AND sha256 = ?",
                (user_id, sha256),
            )
            import_row = cursor.fetchone()
            if not import_row:
                db_manager.conn.commit()
                return None, "线路导入批次写入失败，已仅保存报价表"
            import_id = import_row[0]

            # 重新导入同一份文件时先清旧线路，避免重复累积。
            cursor.execute(
                "DELETE FROM logistics_quote_routes WHERE user_id = ? AND import_id = ?",
                (user_id, import_id),
            )
            cursor.executemany(
                """
                INSERT INTO logistics_quote_routes
                    (user_id, import_id, carrier, book_kind, origin_province,
                     origin_city, dest_province, dest_city, price_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        import_id,
                        row.get("carrier") or "未标注承运商",
                        row.get("book_kind") or payload.get("book_kind"),
                        row.get("origin_province") or "",
                        row.get("origin_city") or "",
                        row.get("destination_province") or "",
                        row.get("destination_city") or "",
                        json.dumps(
                            {
                                "rule_type": row.get("rule_type"),
                                "first_weight_kg": row.get("first_weight_kg"),
                                "first_price": row.get("first_price"),
                                "continued_unit_kg": row.get("continued_unit_kg"),
                                "continued_price": row.get("continued_price"),
                                "quote": row.get("quote"),
                                "confidence": row.get("confidence"),
                                "review_state": row.get("review_state"),
                            },
                            ensure_ascii=False,
                        ),
                    )
                    for row in rows
                ],
            )
            db_manager.conn.commit()
    except Exception as exc:  # noqa: BLE001 - 导入失败不该让上传整体失败
        logger.error(f"物流线路导入失败 user_id={user_id} file={filename}: {exc}")
        return None, "线路导入失败，已仅保存报价表"

    return {"id": import_id, "route_count": len(rows)}, ""
