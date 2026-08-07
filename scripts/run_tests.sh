#!/usr/bin/env bash
# 本地总验收：环境探测 → 全量单测 → CLI 自检 → HTTP 契约 smoke。
#
#   ./scripts/run_tests.sh            # 不跑评测（快，约十几秒）
#   ./scripts/run_tests.sh --with-eval  # 追加合成评测消融（数分钟）
#
# 全程零第三方依赖、不联网、只用合成数据；临时库跑完即删。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

WITH_EVAL=0
[[ "${1:-}" == "--with-eval" ]] && WITH_EVAL=1

section() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

section "0. 环境"
"$PY" - <<'EOF'
import platform, sqlite3, sys
print(f"python  : {sys.version.split()[0]} ({platform.python_implementation()})")
print(f"platform: {platform.platform()}")
print(f"sqlite  : {sqlite3.sqlite_version}")
con = sqlite3.connect(":memory:")
try:
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    print("fts5    : available")
except Exception as exc:
    print(f"fts5    : MISSING ({exc})"); raise SystemExit(1)
for mod in ("numpy", "sentence_transformers", "faiss"):
    try:
        __import__(mod); print(f"vector  : {mod} importable")
        break
    except Exception:
        continue
else:
    print("vector  : unavailable (向量档位将被跳过，属预期)")
EOF

section "1. 全量单元测试"
"$PY" -m unittest discover -s tests

section "2. CLI 端到端自检"
"$PY" -m aml_retriever.cli selfcheck

section "3. HTTP 官方契约 smoke"
"$PY" scripts/smoke_api.py

if [[ "$WITH_EVAL" == "1" ]]; then
  section "4. 合成评测消融"
  "$PY" scripts/run_eval.py --scale medium --difficulty mixed --seed 20260806 --top-k 100
  section "5. 参数扫描"
  "$PY" scripts/run_scan.py --scan all --scale medium --difficulties plain,paraphrase,mixed
else
  printf '\n(跳过评测；加 --with-eval 可一并执行)\n'
fi

printf '\n\033[1;32m全部通过。\033[0m\n'
