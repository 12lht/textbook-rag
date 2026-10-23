"""探针：书单有没有进系统提示、格式对不对、没书时是否一个字不加。

跑法：python tests/probes/probe-textbook-catalog.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
import service  # noqa: E402

USER_WITH_BOOKS = "u-671d242584"
USER_WITHOUT_BOOKS = "u-bench-nobody"

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(("  OK  " if cond else "  FAIL") + f" {name}" + (f"  {detail}" if detail else ""))
    ok &= cond


# 1) 注入点已生效
check("set_textbook_catalog 已注入", agent_core._textbook_catalog is not None)

# 2) 有书的用户：书单出现在系统提示里
prompt = service._textbook_catalog_text(USER_WITH_BOOKS)
check("书单非空", bool(prompt), f"{len(prompt)} 字")
check("每行一本（- 开头）", all(line.startswith("- ") for line in prompt.splitlines()))
first = prompt.splitlines()[0]
print("  示例:", first)

system = agent_core._system_prompt(textbook_catalog=prompt)
check("提示含「现有教材」段", "现有教材" in system)
check("书单在提示里", first in system)
check("提示引导先选书", "先从书单里挑最相关的一本" in system)

# 3) 没书的用户：不加书单段，也不报错
empty = service._textbook_catalog_text(USER_WITHOUT_BOOKS)
check("没书时书单为空串", empty == "")
system_bare = agent_core._system_prompt(textbook_catalog=empty)
check("没书时提示不含书单段", "现有教材" not in system_bare)

# 4) 注入点拿不到时（模拟未注入）不炸
saved = agent_core._textbook_catalog
agent_core._textbook_catalog = None
check("未注入时 _catalog_text 返回空", agent_core._catalog_text(USER_WITH_BOOKS) == "")
agent_core._textbook_catalog = saved

# 5) provider 抛异常时不炸（书单只是提示的一部分）
def boom(user_id: str) -> str:
    raise RuntimeError("db gone")

agent_core._textbook_catalog = boom
check("provider 异常被吞", agent_core._catalog_text(USER_WITH_BOOKS) == "")
agent_core._textbook_catalog = service._textbook_catalog_text

print("\n结果:", "通过" if ok else "有失败项")
raise SystemExit(0 if ok else 1)
