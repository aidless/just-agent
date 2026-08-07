#!/usr/bin/env python3
"""提交材料静态核对。

零依赖、不联网、不执行任何外部动作。只做一次**本地静态检查**：
仓库是否齐备官方提交所需的文档与结构，以及关键清单项是否到位。

用法::

    python3 scripts/check_submission_materials.py
    python3 scripts/check_submission_materials.py --run-tests   # 顺便跑单测并核对计数

退出码：所有 REQUIRED 项通过则 0；任一 REQUIRED 缺失则 1。
BLOCKED（需人工确认）项只作为提醒打印，不导致非零退出——
因为它们是「设计上不自动执行」的门，而非缺失。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 期望存在的产物 ----------------------------------------------------------
REQUIRED_FILES = [
    "README.md",
    "docs/API_CONTRACT.md",
    "docs/EVAL.md",
    "docs/DATA_LIFECYCLE.md",
    "docs/SUBMISSION_READINESS.md",
    "Dockerfile",
    "config.example.json",
    "aml_retriever/__init__.py",
    "aml_retriever/api.py",
    "aml_retriever/server.py",
    "aml_retriever/retriever.py",
    "tests",
]

# ---- README 必须包含的章节（提交材料可读性门槛）-----------------------------
README_REQUIRED_SECTIONS = [
    "快速开始",
    "官方 Add",
    "证据状态表",
    "未做的事",
    "并发、隐私与删除",
]

# ---- SUBMISSION_READINESS.md 必须覆盖的清单项 ------------------------------
READINESS_REQUIRED_MARKERS = [
    ("版本", "版本"),
    ("API 入口", "API 入口"),
    ("gpt-4o-mini 门控", "gpt-4o-mini"),
    ("未执行的外部动作", "未执行"),
    ("原创性与来源披露", "原创性"),
    ("数据隐私与删除", "数据隐私与删除"),
]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, note: str = "") -> None:
    RESULTS.append((name, bool(passed), note))
    mark = "PASS" if passed else "FAIL"
    tail = f"  ({note})" if note else ""
    print(f"[{mark}] {name}{tail}")


def _read(path: str) -> str | None:
    try:
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="提交材料静态核对")
    parser.add_argument("--run-tests", action="store_true",
                        help="顺便运行单测并核对计数（否则只做静态检查）")
    args = parser.parse_args(argv)

    print("=== 提交材料静态核对 ===\n")

    # 1) 关键文件齐备性
    for rel in REQUIRED_FILES:
        full = os.path.join(REPO, rel)
        check(f"文件存在：{rel}", os.path.exists(full),
              "缺失" if not os.path.exists(full) else "")

    # 2) README 章节齐备性
    readme = _read("README.md") or ""
    for sec in README_REQUIRED_SECTIONS:
        check(f"README 含章节：{sec}", sec in readme,
              "" if sec in readme else "未在 README 中找到")

    # 3) SUBMISSION_READINESS.md 关键标记
    readiness = _read("docs/SUBMISSION_READINESS.md") or ""
    for label, marker in READINESS_REQUIRED_MARKERS:
        check(f"SUBMISSION_READINESS 含：{label}", marker in readiness,
              "" if marker in readiness else "缺失标记")

    # 4) 版本号可从 __init__ 解析
    init_src = _read("aml_retriever/__init__.py") or ""
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init_src)
    check("aml_retriever/__init__.py 含 __version__", bool(m),
          m.group(1) if m else "未找到 __version__")

    # 5) 不含明显密钥泄露（轻量扫描）
    leak = False
    for root, _dirs, files in os.walk(os.path.join(REPO, "aml_retriever")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if re.search(r'(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16,}|api_key\s*=\s*["\'][A-Za-z0-9]{16,}["\'])', txt):
                leak = True
                check(f"无密钥泄露：{fn}", False, "疑似硬编码密钥")
                break
        if leak:
            break
    if not leak:
        check("无疑似硬编码密钥", True)

    # 6)（可选）跑单测并核对计数
    if args.run_tests:
        print("\n--- 运行单测 ---")
        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(REPO, "tests"))
        runner = unittest.TextTestRunner(verbosity=0)
        result = runner.run(suite)
        n = result.testsRun
        check(f"单测全部通过（{n} 项）",
              n > 0 and not (result.failures or result.errors),
              f"failures={len(result.failures)} errors={len(result.errors)}")

    # 7) BLOCKED 提醒（非失败，仅提示人工确认）
    print("\n--- 需人工确认（BLOCKED，非缺失）---")
    blocked_items = [
        "gpt-4o-mini 门控：官方当前 Full 清单明确要求 Add/Search 使用该模型；本实现无 LLM 路径，当前不能勾选（见 docs/SUBMISSION_READINESS.md §6）",
        "外部动作（报名/Key/部署/正式评测/邮件/费用/联网下载/真实数据）均未由自动化执行，保留人工确认门（见 §5）",
        "官方契约抓取于 2026-08-06，提交前须人工复核官方页面最新口径",
    ]
    for item in blocked_items:
        print(f"  [BLOCKED] {item}")

    # 汇总
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n=== 汇总 ===")
    print(f"通过 {sum(1 for _, ok, _ in RESULTS if ok)} / 共 {len(RESULTS)} 项静态检查")
    if failed:
        print(f"失败 {len(failed)} 项（REQUIRED 缺失）：")
        for n in failed:
            print(f"  - {n}")
        return 1
    print("所有 REQUIRED 项通过。BLOCKED 项请人工确认后再提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
