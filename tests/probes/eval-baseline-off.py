# -*- coding: utf-8 -*-
"""以「关掉上标还原」的状态跑一次解析评测，用来重建改动前的基线。

用法: python eval-baseline-off.py <pdf> <pages> <tag>
"""
import runpy
import sys

sys.path.insert(0, r"D:\agent")
import documents  # noqa: E402

documents._is_superscript = lambda *args, **kwargs: False

sys.argv = ["parse.py", "--pdf", sys.argv[1], "--pages", sys.argv[2], "--tag", sys.argv[3]]
runpy.run_path(r"D:\agent\eval\parse.py", run_name="__main__")
