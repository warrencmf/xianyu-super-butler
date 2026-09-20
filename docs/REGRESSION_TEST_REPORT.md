# 闲鱼超级管家 —— 部署与回归测试报告（最终版）

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/23Star/xianyu-super-butler |
| 部署路径 | `E:\gitcode\xianyu-super-butler` |
| 上游版本 | `main` @ `13676cc`（Version 3.1.0） |
| 部署方式 | 源码运行（本机无 Docker） |
| 运行环境 | Windows / Python 3.13.12（venv）/ Node 22.22.2 |
| 测试日期 | 2026-09-20 |
| 最终状态 | **服务可启动，351 个回归用例全部通过** |

---

## 一、结论

1. **服务已成功启动**：`http://localhost:8080`，`/health` 返回
   `{"status":"healthy","services":{"cookie_manager":"ok","database":"ok"}}`，
   `/`、`/docs` 均 200。
2. **回归测试：351 个用例，全部通过（100%），0 失败 0 错误。**
   期间发现并修复了 2 个测试自身的问题（见第五节）。
3. **上游仓库存在严重发布缺陷**：4 个模块被 `reply_server.py` 引用但从未提交，
   导致**官方源码和官方 Docker 镜像都无法启动**。
4. 根因已定位到具体文件与行号（见第三节），其中 1 个是被 `.gitignore` 静默吞掉的。
5. 已按确认的方案**补齐 4 个缺失模块**（最小可用实现），并补了 39 个单元测试
   与 1 套 30 项的端到端冒烟测试。
6. 顺带修复了上游 2 个过期/不可移植的测试（见 5.3），回归套件现已全绿。

---

## 二、部署过程

### 2.1 完成的步骤

| 步骤 | 结果 |
| --- | --- |
| `git clone` 到 `E:\gitcode` | 成功，217 个文件 |
| 创建 venv（Python 3.13.12） | 成功 |
| `pip install -r requirements.txt` | 成功，74 个包 |
| `npm ci`（frontend） | 196 个包 |
| `tsc --noEmit` | **0 错误** |
| `vite build` | 成功，产出到 `static/` |
| `playwright install chromium` | 成功，Chrome for Testing 148.0.7778.96 |
| `compileall Start.py XianyuAutoAsync.py app utils tests` | **通过** |
| `python Start.py` | **成功启动**（补齐模块后） |

### 2.2 环境坑（3 个，均已解决）

**坑 1：清华 pip 镜像在本机不可用。**
`-i https://pypi.tuna.tsinghua.edu.cn/simple` 报
`Could not find a version that satisfies the requirement fastapi (from versions: none)`。
改用默认 PyPI 源正常。→ **本机请用默认源。**

**坑 2：`PATHEXT` 环境变量缺失，PyExecJS 找不到 Node。**
PyExecJS 的 `_find_executable()` 用 `os.environ["PATHEXT"]` 拼可执行文件扩展名。
本机 shell 里该变量为 `None` → 只会去找无后缀的 `node`（不存在）→ 抛
`Could not find an available JavaScript runtime`，被 `utils/xianyu_utils.py:54`
包装成「无法加载JavaScript文件」。

- 影响：**19 个用例 error，另有 52 个用例连加载都失败**（205 → 257）。
- 修复：运行时导出 `PATHEXT=.COM;.EXE;.BAT;.CMD`。
- 已固化进 `scripts/run_regression.cmd`。
- 正常的 Windows 终端本来就有这个变量，**这不是项目 bug**。

**坑 3：安全删除护栏挡住前端构建。**
`npm run build` 第一步 `clean-build-output.mjs` 要删 `static/assets`，被本机
safe-delete 护栏拦下（回收站二进制超时）。改用 `mv` 挪走旧产物后直接跑
`vite build`。旧产物已备份到 `static_prebuild_backup/`。

---

## 三、阻断问题：上游缺 4 个模块（根因已定位）

### 3.1 现象

```
ERROR | __main__:_start_api_server:724 - uvicorn服务器启动失败: No module named 'app.delivery_template'
```

### 3.2 缺失清单

| 缺失模块 | 引用位置 | 作用 | 根因 |
| --- | --- | --- | --- |
| `app/services/notification_test.py` | `reply_server.py:33` | 通知渠道测试发送 | **被 `.gitignore:94` 的 `*_test.py` 静默忽略** |
| `app/delivery_template.py` | `reply_server.py:23` | 自动发货内容分段发送 | 从未创建/提交 |
| `app/routers/logistics_quote.py` | `reply_server.py:39` | 物流报价本路由 | 从未创建/提交 |
| `app/routers/logistics_agent.py` | `reply_server.py:40` | 物流 Agent 路由 | 从未创建/提交 |

静态审计（`scripts/_audit_missing_modules.py`）确认：**除这 4 个之外没有其他缺失模块**。

### 3.3 根因：一条过宽的 .gitignore 规则

`.gitignore` 第 94 行：

```gitignore
# 测试文件
test_*.py
*_test.py          # ← 问题在这
```

`*_test.py` 本意是忽略测试文件，但 `app/services/notification_test.py` 这个
**生产模块**的文件名同样以 `_test.py` 结尾，于是被静默排除。

作者显然踩过这个坑——第 806-808 行专门加了豁免：

```gitignore
# Keep repository regression tests tracked
!tests/
!tests/test_*.py
```

**但漏了 `app/services/notification_test.py`。** 结果就是：这个文件在作者本地
一直存在（所以 `reply_server.py` 的 import 在本地能跑通），却从未进入 git，
任何 clone 下来的人都起不来服务。

> 本次已补上豁免规则 `!app/services/notification_test.py`，否则修复本身也提交不了。

### 3.4 证据链

1. `git log --all -- <4 个文件>` 全部为空 → **从未提交过**。
2. `main`、`beta` 两个分支、`v3.1.0` 标签里都不存在这 4 个文件。
3. GitHub raw 直连探测，4 个文件全部 **HTTP 404**。
4. 从 `ghcr.io/23star/xianyu-super-butler:latest` 拉下 amd64 镜像层解包
   （无 Docker，直接走 registry v2 API，脚本见交付物 `verify_upstream_complete.sh`）：
   - 镜像共 9 层。**`app/app/` 整个 Python 源码树只存在于 layer 7（1.3 MB，220 个条目）**；
     其余层分别是 base OS（28 MB）、系统库（16 MB）、ca-certificates（3.5 MB）、
     X11/字体（131 MB）、`/opt/venv` 依赖（194 MB）、`app/static/` 前端产物（343 MB）——
     均不含 `app/app/`。
   - 因此 layer 7 的清单就是权威答案。直接核对：

     ```
     PRESENT  app/app/reply_server.py
     PRESENT  app/app/db_manager.py
     ABSENT   app/app/delivery_template.py
     ABSENT   app/app/services/notification_test.py
     ```

   - 镜像内 `app/app/services/` 的**完整**内容只有 4 项：
     `__init__.py`、`delivery_block_rules.py`、`notification_channels.py`、
     `notification_sender.py` —— `notification_test.py` 确实不在里面。
   - 镜像内的 `reply_server.py` 与仓库版本 `diff` 结果为 **0 行差异**。
   - → **官方 Docker 镜像也会以同样的方式启动失败。** 这不是"仓库忘了提交"，
     而是"连发布的镜像都是坏的"。
   - 该结论已用**正负对照**验证过脚本本身可信：拿一个确定存在的文件
     （`app/app/reply_server.py`）跑，返回 `present (layer 7)`、退出码 0；
     再拿这 2 个缺失文件跑，返回 `ABSENT`、退出码 1。两个方向都符合预期，
     排除了"工具坏掉导致全部误报缺失"的可能。
5. 上游父项目 `zhinianboke/xianyu-auto-reply` 同样没有这些文件。

### 3.5 影响面

- **服务完全起不来**——不是功能降级，是进程在 uvicorn 启动阶段崩溃，
  `/health`、`/`、`/docs` 全部不可达。
- 8 个测试模块因 `import app.reply_server` 失败而整体无法加载。
- 前端 `frontend/services/api.ts` **已经写好了**物流报价的客户端函数，
  `db_manager.py` 里相关数据表（`logistics_quote_books`、
  `logistics_quote_routes`、`logistics_agent_settings` 等）**也都建好了**。
  → 功能是真实存在的，只是文件没提交。
- 补充发现：物流报价 / 物流 Agent 的 `api.ts` 函数**没有任何组件调用**
  （`parseQuoteSource` / `createQuoteBook` / `listQuoteBooks` 引用数均为 0），
  说明这是作者提交到一半搁置的功能。

---

## 四、补齐的 4 个模块

按确认的「写最小可用实现」方案补齐。**这些是新增代码，不是上游原版**，
实现依据是仓库内确实存在的两份契约：`frontend/services/api.ts` 与 `db_manager.py` 的表结构。

### 4.1 新增文件

| 文件 | 内容 | 实现程度 |
| --- | --- | --- |
| `app/services/notification_test.py` | 通知测试服务 + 限流器 + 结构化错误 | **完整**（契约明确） |
| `app/delivery_template.py` | 发货内容分段与发送（文本分段 / 图片标记） | **完整**（契约明确） |
| `app/services/logistics_quote_parser.py` | 报价表解析（xlsx/xlsm/xls/csv、表头识别、列映射、行评分） | 可用，启发式规则为重建 |
| `app/routers/logistics_quote.py` | 解析预览 / 列表 / 保存 / 删除 4 个接口 | **完整** |
| `app/routers/logistics_agent.py` | 账号配置读写 + 训练轮次读取；会话推理与训练生成返回 501 | 配置完整，推理**未实现** |

### 4.2 关键设计取舍

- **`delivery_template`**：按 1800 字符分段（平台上限 2000，留余量），
  优先在换行处切，只有单行超长才硬切；`__IMAGE_SEND__` 标记走图片通道。
- **`logistics_quote`**：上传限 20MB，只接受 4 种表格扩展名；
  同一文件按 `(user_id, sha256)` 幂等，重复上传不产生重复记录；
  解析不出的表只记 warning 不报错（报价表是手工维护的，格式很难统一）。
- **`logistics_agent`**：会话推理与训练样本生成**明确返回 501**，
  并在 `detail` 里说明原因。**刻意不返回编造的报价**——这条链路会真实影响报价输出。
- 全程未输出渠道密钥（`access_token` 等）到日志或错误信息，有对应测试锁定。

---

## 五、回归测试结果

运行方式（等价于官方 `docs/deployment.md` 的验证方式）：

```cmd
cd /d E:\gitcode\xianyu-super-butler
scripts\run_regression.cmd
```

### 5.1 五轮对比（每一轮都记录，用数字说明修复是否真的起作用）

| 轮次 | 用例数 | 通过 | 失败 | 错误 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 第一轮 | 205 | 172 | 2 | 31 | 未修环境 |
| 第二轮 | 257 | 247 | 2 | 8 | 修好 PATHEXT + Chromium |
| 第三轮 | 302 | 300 | 2 | 0 | 补齐 4 个模块 |
| 第四轮 | 351 | 349 | 2 | 0 | 加上新增 39 个单测 |
| **最终轮** | **351** | **351** | **0** | **0** | **修掉 2 个过期测试后全绿** |

**通过率 100%（`Ran 351 tests ... OK`）。**

### 5.2 修复效果逐项验证

- 原先因 `No module named 'app.delivery_template'` 阻塞的 8 个模块，
  现在全部加载并通过：

  | 模块 | 结果 |
  | --- | --- |
  | test_account_request_dedup | 10 通过 |
  | test_risk_status_presentation | 6 通过 |
  | test_ai_reply | 4 通过 |
  | test_delivery_rule_api_validation | 3 通过 |
  | test_message_filters_and_reply_logs | 2 通过 |
  | test_notification_and_risk_logs | 2 通过 |
  | test_risk_control | 2 通过 |
  | test_qr_login_flow | 1 通过 |

- 新增的 3 个测试文件全部通过：`test_delivery_template`（12）、
  `test_logistics_quote_parser`（18）、`test_notification_test_service`（9）。

### 5.3 已修复的 2 个上游过期测试（原本是套件里最后的 2 个红）

这 2 个失败**都不是产品缺陷**，是上游测试与代码漂移 / 不可移植。为了交付一个
真正全绿的套件，这 2 处已修好并复验。判断原则：先确认**代码的行为是有意的**，
再决定改测试还是改代码 —— 这两处都是改测试。

**① `test_buyer_interaction_per_account.PerAccountFlagTests.test_all_three_can_be_set_at_once`**

原始失败：

```
{'auto_flower_enabled': True, 'auto_rate_enabled': True,
 'auto_thanks_enabled': True, 'auto_receive_flower_enabled': False}
 != {'auto_flower_enabled': True, 'auto_rate_enabled': True, 'auto_thanks_enabled': True}
```

判定**代码是对的**，理由是第 4 个开关在 4 处独立存在、不可能是笔误：

| 位置 | 证据 |
| --- | --- |
| `app/db_manager.py:1503` | 建表/迁移里已有该列 |
| `app/reply_server.py:7779` | 业务逻辑已在读写该字段 |
| `BuyerInteractionUpdate` 模型 | 请求体已声明该字段 |
| `frontend/services/api.ts:383,390` | 前端类型与调用已包含该字段 |

修复内容：测试重命名为 `test_all_flags_can_be_set_at_once`，把 4 个开关
（含 `auto_receive_flower_enabled=True`）一起设置并断言；
`test_columns_exist_after_migration` 也补上了第 4 列的存在性断言。

**② `test_slider_watchdog.KillBrowserProcessTests.test_matches_only_own_user_data_dir`**

`utils/xianyu_slider_stealth.py:4266` 用 `f"browser_data{os.sep}slider_{id}"` 拼匹配串，
Windows 上 `os.sep` 是 `\`；而测试里硬编码了 POSIX 风格
`--user-data-dir=/app/browser_data/slider_...`（正斜杠），于是匹配不上、返回 `False`。

判定**代码是对的**：生产代码在 `utils/xianyu_slider_stealth.py:478` 用
`os.path.join` 构造 `user_data_dir`，`os.sep` 与之天然一致 —— 在各自平台上都对，
这是测试没做跨平台处理，在 Linux CI 上本来就会通过。

修复内容：`import os`，并按生产代码的方式构造路径：

```python
own_dir   = os.path.join(os.getcwd(), 'browser_data', f'slider_{instance.pure_user_id}')
other_dir = os.path.join(os.getcwd(), 'browser_data', f'slider_{other.pure_user_id}')
```

两处修完后复跑全量：`Ran 351 tests ... OK`，0 失败 0 错误。

---

## 六、端到端冒烟测试

`scripts/smoke_test_api.py` —— 覆盖 30 项断言，**全部通过**：

```
冒烟测试结果: 30/30 通过
```

覆盖范围：

- 登录拿 token、未带 token 被拒 401
- 报价表**解析预览**（生成真实 xlsx：4 行 → 2 valid / 1 review / 1 rejected）
- `rule_type` 正确识别为 `first_additional`；表头映射命中
- 拒绝 `.exe` 上传（400）
- 报价表**保存 → 列表 → 幂等复传 → 删除 → 重复删除 404** 全链路
- 物流 Agent 配置读写、不存在账号 404、未实现能力 501（带明确说明）
- 通知测试：规则不存在返回结构化 404

---

## 七、产出文件清单

**新增实现（5 个）**

```
app/delivery_template.py
app/services/notification_test.py
app/services/logistics_quote_parser.py
app/routers/logistics_quote.py
app/routers/logistics_agent.py
```

**新增测试（3 个，39 个用例）**

```
tests/test_delivery_template.py
tests/test_logistics_quote_parser.py
tests/test_notification_test_service.py
```

**新增脚本（4 个）**

```
scripts/run_regression.cmd              回归测试入口（自动补 PATHEXT）
scripts/_audit_missing_modules.py       缺失模块静态审计（AST，不 import 目标）
scripts/smoke_test_api.py               端到端冒烟测试（30 项断言）
scripts/verify_upstream_complete.sh     无 Docker 校验镜像层是否含指定文件
```

**修改（3 个）**

```
.gitignore                                    补 !app/services/notification_test.py 豁免
tests/test_buyer_interaction_per_account.py   第 4 个开关（auto_receive_flower_enabled）
tests/test_slider_watchdog.py                 user-data-dir 改为跨平台构造（os.path.join）
```

**备份与隔离（未删除，可自行清理）**

```
static_prebuild_backup/        原始预构建前端产物
E:\gitcode\_quarantine\        被隔离的旧产物 / 损坏的 npm 包 / 镜像层校验临时文件
```

**日志**

```
logs/regression_test.log            第一轮（未修环境）
logs/regression_test_run2.log       第二轮（修完环境）
logs/regression_test_run3.log       第三轮（补齐模块）
logs/regression_test_final.log      第四轮（含新增单测）
logs/regression_test_final2.log     最终轮 —— Ran 351 tests / OK，全绿
logs/startup_console.log            服务启动日志
```

---

## 八、后续建议

1. **别把当前实现当成作者原版。** `logistics_quote` 的解析启发式和
   `logistics_agent` 的配置结构是按仓库内残留契约重建的。如果上游后续补了这 4 个
   文件，建议直接覆盖回去再跑一遍 `scripts\run_regression.cmd` 对比。
2. **发货链路务必先用测试账号验证。** `app/delivery_template.py` 是会真实给买家
   发卡密的代码路径，虽然 12 个单测覆盖了分段与图片分支，但真实 IM 发送没有验过。
3. **物流 Agent 的会话推理没有实现**（接口返回 501）。如果要用这个功能，
   需要补的是 LLM 多轮状态机与训练样本生成，工作量不小。
4. **建议给上游提 issue**：`.gitignore` 的 `*_test.py` 会吞掉生产模块，
   这是任何人 clone 后都起不来的根因；顺带把 `*_test.py` 改成
   `tests/test_*.py` 之类的精确规则更安全。
5. **本次顺手修好的 2 个上游测试，改动要留意别被覆盖**（见 5.3）：
   `tests/test_slider_watchdog.py` 的跨平台路径处理、
   `tests/test_buyer_interaction_per_account.py` 的第 4 个字段断言。
   如果上游之后更新覆盖了 `tests/`，这两处需要重新应用，否则套件会重新变红。
6. 生产部署前请改掉默认密码 `admin / admin123`，并按官方文档配置
   `ADMIN_PASSWORD`（注意：该变量只在**首次建库**时生效）。
