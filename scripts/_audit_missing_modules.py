"""静态审计：找出项目里被 import 但实际不存在的本地模块。

只做静态解析，不执行任何项目代码，不写任何文件。
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PREFIXES = ("app", "utils", "XianyuAutoAsync")

# 收集磁盘上真实存在的模块
existing = set()
for path in ROOT.rglob("*.py"):
    if any(p in path.parts for p in (".venv", "node_modules", ".git", "static", "data", "logs")):
        continue
    rel = path.relative_to(ROOT).with_suffix("")
    existing.add(".".join(rel.parts))
    if rel.name == "__init__":
        existing.add(".".join(rel.parts[:-1]))
existing.discard("")

missing = {}
for path in ROOT.rglob("*.py"):
    if any(p in path.parts for p in (".venv", "node_modules", ".git", "static", "data", "logs", "tests")):
        continue
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError as exc:
        print(f"[语法错误] {path.relative_to(ROOT)}: {exc}")
        continue

    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mod = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in LOCAL_PREFIXES:
                    mod = alias.name
        if not mod or mod.split(".")[0] not in LOCAL_PREFIXES:
            continue
        if mod in existing:
            continue
        # 可能是包内子模块未展开，逐级确认
        if any(m == mod or m.startswith(mod + ".") for m in existing):
            continue
        missing.setdefault(mod, []).append(f"{path.relative_to(ROOT)}:{node.lineno}")

print("=" * 70)
if not missing:
    print("未发现缺失的本地模块。")
else:
    print(f"发现 {len(missing)} 个被引用但不存在的本地模块：\n")
    for mod in sorted(missing):
        print(f"  ✗ {mod}")
        for ref in missing[mod][:6]:
            print(f"      ← {ref}")
print("=" * 70)
