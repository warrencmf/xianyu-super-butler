"""端到端冒烟测试：验证本次补齐的 4 个模块对外可用。

前置：服务已在 127.0.0.1:8080 运行（`python Start.py`）。

用法：
    .venv\\Scripts\\python.exe scripts\\smoke_test_api.py

覆盖：
    * 登录拿 token
    * 物流报价表 解析 / 列表 / 保存 / 删除 全链路
    * 物流 Agent 配置读写、训练轮次列表、未实现能力的 501
    * 通知规则测试接口的 404（规则不存在）
"""

from __future__ import annotations

import io
import json
import sys

import httpx

BASE = "http://127.0.0.1:8080"

results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((bool(condition), name))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail else ""))


def build_rate_book() -> bytes:
    """生成一份小体积报价表，覆盖首重/续重与缺少目的地两种行。"""
    try:
        import openpyxl
    except ImportError:
        print("缺少 openpyxl，无法生成测试文件")
        sys.exit(2)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "顺丰报价"
    sheet.append(["顺丰速运 2026 年 9 月报价表"])
    sheet.append([])
    sheet.append(["承运商", "始发城市", "目的城市", "首重(kg)", "首重价格(元)", "续重(kg)", "续重价格(元)"])
    sheet.append(["顺丰速运", "广州市", "深圳市", 1, 12, 1, 2])
    sheet.append(["顺丰速运", "广州市", "北京市", 1, 18, 1, 5])
    sheet.append(["顺丰速运", "广州市", None, 1, 20, 1, 6])          # 缺目的地 -> rejected
    sheet.append(["顺丰速运", "广州市", "杭州市", 1, -1, 1, 3])        # 负价 -> 记 issue

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True)

    # ---------------- 登录 ----------------
    response = client.post("/login", json={"username": "admin", "password": "admin123"})
    check("POST /login 返回 200", response.status_code == 200, str(response.status_code))
    token = (response.json() or {}).get("token")
    check("登录拿到 token", bool(token))
    if not token:
        print("\n无法登录，后续用例跳过。请确认服务已启动且管理员密码未被修改。")
        return 1

    headers = {"Authorization": f"Bearer {token}"}

    # ---------------- 未鉴权应被拒 ----------------
    response = client.get("/api/logistics/quote-books")
    check("未带 token 访问被拒 401", response.status_code == 401, str(response.status_code))

    # ---------------- 列表（空） ----------------
    response = client.get("/api/logistics/quote-books", headers=headers)
    check("GET /api/logistics/quote-books 200", response.status_code == 200, str(response.status_code))
    check("响应含 success/books", "books" in (response.json() or {}))

    # ---------------- 解析预览 ----------------
    payload = build_rate_book()
    response = client.post(
        "/api/logistics/quote-sources/parse",
        headers=headers,
        files={"file": ("顺丰报价.xlsx", payload,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    check("POST quote-sources/parse 200", response.status_code == 200,
          f"{response.status_code} {response.text[:160]}")
    parsed = response.json() if response.status_code == 200 else {}
    check("解析出 4 行数据", parsed.get("summary", {}).get("total") == 4,
          json.dumps(parsed.get("summary", {}), ensure_ascii=False))
    check("识别到表头映射", bool(parsed.get("mapping", {}).get("matched")))
    check("rule_type 识别为首重+续重", (parsed.get("services") or [{}])[0].get("rule_type") == "first_additional",
          str((parsed.get("services") or [{}])[0].get("rule_type")))
    check("缺目的地的行被判 rejected",
          parsed.get("summary", {}).get("rejected", 0) >= 1,
          json.dumps(parsed.get("summary", {}), ensure_ascii=False))

    # ---------------- 不支持的扩展名 ----------------
    response = client.post(
        "/api/logistics/quote-sources/parse",
        headers=headers,
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    check("拒绝 .exe 上传 400", response.status_code == 400, str(response.status_code))

    # ---------------- 保存 ----------------
    response = client.post(
        "/api/logistics/quote-books",
        headers=headers,
        files={"file": ("顺丰报价.xlsx", payload,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    check("POST /api/logistics/quote-books 200", response.status_code == 200,
          f"{response.status_code} {response.text[:160]}")
    created = response.json() if response.status_code == 200 else {}
    book = created.get("book") or {}
    book_id = book.get("id")
    check("返回 book.id", bool(book_id))
    check("book 带 payload", bool(book.get("payload")))
    check("route_import 已生成", bool(created.get("route_import")),
          str(created.get("route_warning", "")))

    # ---------------- 列表（有数据） ----------------
    response = client.get("/api/logistics/quote-books", headers=headers)
    books = (response.json() or {}).get("books", [])
    check("列表里能查到刚保存的报价表", any(item.get("id") == book_id for item in books),
          f"{len(books)} 本")

    # ---------------- 重复上传应幂等 ----------------
    response = client.post(
        "/api/logistics/quote-books",
        headers=headers,
        files={"file": ("顺丰报价.xlsx", payload,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    response2 = client.get("/api/logistics/quote-books", headers=headers)
    check("同一文件重复上传不产生重复记录",
          len((response2.json() or {}).get("books", [])) == len(books),
          f"{len(books)} -> {len((response2.json() or {}).get('books', []))}")

    # ---------------- 删除 ----------------
    response = client.delete(f"/api/logistics/quote-books/{book_id}", headers=headers)
    check("DELETE quote-books/{id} 200", response.status_code == 200, str(response.status_code))
    response = client.get("/api/logistics/quote-books", headers=headers)
    check("删除后列表为空",
          all(item.get("id") != book_id for item in (response.json() or {}).get("books", [])))
    response = client.delete(f"/api/logistics/quote-books/{book_id}", headers=headers)
    check("重复删除返回 404", response.status_code == 404, str(response.status_code))

    # ---------------- 物流 Agent 配置 ----------------
    response = client.get("/api/logistics/agent/settings", headers=headers)
    check("GET agent/settings 200", response.status_code == 200, str(response.status_code))
    check("settings 是数组", isinstance((response.json() or {}).get("settings"), list))

    response = client.get("/api/logistics/agent/settings/nonexistent-account", headers=headers)
    check("不存在的账号返回 404", response.status_code == 404, str(response.status_code))

    response = client.post("/api/logistics/agent/quote", headers=headers)
    check("未实现的 agent/quote 返回 501", response.status_code == 501, str(response.status_code))
    detail = (response.json() or {}).get("detail") if response.status_code == 501 else {}
    check("501 带明确说明", isinstance(detail, dict) and "message" in detail,
          json.dumps(detail, ensure_ascii=False)[:120])

    response = client.post("/api/logistics/agent/training/rounds", headers=headers)
    check("未实现的训练轮次创建返回 501", response.status_code == 501, str(response.status_code))

    response = client.get("/api/logistics/agent/training/rounds", headers=headers)
    check("GET training/rounds 200", response.status_code == 200, str(response.status_code))
    check("rounds 是数组", isinstance((response.json() or {}).get("rounds"), list))

    # ---------------- 通知测试 ----------------
    response = client.post("/message-notifications/rule/999999/test", headers=headers)
    check("通知测试：规则不存在返回 404", response.status_code == 404, str(response.status_code))
    detail = (response.json() or {}).get("detail") if response.status_code == 404 else {}
    check("404 detail 是结构化对象", isinstance(detail, dict) and "code" in detail,
          json.dumps(detail, ensure_ascii=False)[:120])

    # ---------------- 汇总 ----------------
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"冒烟测试结果: {passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for ok, name in results:
            if not ok:
                print(f"  - {name}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
