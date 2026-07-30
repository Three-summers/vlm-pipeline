#!/usr/bin/env bash
# 启动 Web 平台（gunicorn）。用法：scripts/run-web.sh [端口]
#
# 环境变量（均可选，覆盖自动探测）：
#   VLMP_ROOT / 默认：仓库根目录
#   VLMP_ENV  / 默认：$ROOT/.venv-web
#   VLMP_DB   / 默认：$ROOT/data/vlmp.db
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${VLMP_ROOT:-$ROOT}"
PORT="${1:-8090}"

_detect_venv() {
  if [ -n "${VLMP_ENV:-}" ]; then
    echo "$VLMP_ENV"
  elif [ -x "$ROOT/.venv-web/bin/python" ]; then
    echo "$ROOT/.venv-web"
  else
    echo ""
  fi
}
ENV_DIR="$(_detect_venv)"

if [ -n "$ENV_DIR" ]; then
  PY="$ENV_DIR/bin/python"
  GUNICORN="$ENV_DIR/bin/gunicorn"
else
  PY="$(command -v python3 2>/dev/null || true)"
  GUNICORN="$(command -v gunicorn 2>/dev/null || true)"
  if [ -z "$PY" ]; then
    echo "未设置 VLMP_ENV 且找不到 python3，请创建 $ROOT/.venv-web 或设置 VLMP_ENV" >&2
    exit 1
  fi
fi

[ -x "$PY" ] || { echo "找不到 Python 环境: $PY（可设置 VLMP_ENV）" >&2; exit 1; }

mkdir -p "$ROOT/logs" "$ROOT/data" "$ROOT/output"

export PYTHONPATH="$ROOT"
export PYTHONUNBUFFERED=1
export VLMP_DB="${VLMP_DB:-$ROOT/data/vlmp.db}"
export VLMP_PYTHON="$PY"        # 供 supervisor 拉起分析子进程
export VLMP_PORT="$PORT"
export VLMP_ROOT="$ROOT"

# 建库 / 建初始管理员 / 登记默认端点，并打印初始密码
"$PY" -c "
import sys; sys.path.insert(0, '$ROOT')
from server.app import init_app
init_app()
print('初始化完成')
"

if [ -x "$GUNICORN" ]; then
  # 单 worker + 多线程：任务进程表与看门狗只需一份，MJPEG 长连接靠线程支撑
  exec "$GUNICORN" -w 1 --threads 24 -b "0.0.0.0:$PORT" \
    --timeout 120 --graceful-timeout 20 \
    --access-logfile "$ROOT/logs/web-access.log" \
    --error-logfile "$ROOT/logs/web.log" \
    --chdir "$ROOT" "server.wsgi:app"
else
  echo "未找到 gunicorn，回退到 Flask 内置服务器" >&2
  exec "$PY" "$ROOT/server/app.py"
fi
