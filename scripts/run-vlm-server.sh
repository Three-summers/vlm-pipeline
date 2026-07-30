#!/usr/bin/env bash
# 启动 vLLM OpenAI 兼容服务，供管线的 Consumer 调用。
# 用法: ./run-vlm-server.sh [port]
#
# 环境变量（均可选，覆盖自动探测）：
#   VLMP_ROOT     / 默认：本脚本所在仓库根目录
#   VLM_MODEL_DIR / 默认：$ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ
#   VLM_VENV      / 默认：$ROOT/.venv-vllm → $ROOT/venv
#   VLM_GPU_UTIL  / 默认 0.55
#   VLM_MAX_LEN   / 默认 16384
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${VLMP_ROOT:-$ROOT}"
PORT="${1:-8001}"
GPU_UTIL="${VLM_GPU_UTIL:-0.55}"
MAX_LEN="${VLM_MAX_LEN:-16384}"
MODEL_DIR="${VLM_MODEL_DIR:-$ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ}"

_detect_venv() {
  if [ -n "${VLM_VENV:-}" ]; then
    echo "$VLM_VENV"
  elif [ -x "$ROOT/.venv-vllm/bin/vllm" ]; then
    echo "$ROOT/.venv-vllm"
  elif [ -x "$ROOT/venv/bin/vllm" ]; then
    echo "$ROOT/venv"
  else
    echo ""
  fi
}
VENV_DIR="$(_detect_venv)"

if [ -z "$VENV_DIR" ] || [ ! -x "$VENV_DIR/bin/vllm" ]; then
  echo "找不到 vllm venv。请创建 $ROOT/.venv-vllm 或设置 VLM_VENV。" >&2
  exit 1
fi
if [ ! -d "$MODEL_DIR" ]; then
  echo "找不到模型目录: $MODEL_DIR" >&2
  exit 1
fi

# FlashInfer 运行时 JIT 需要 ninja(venv 内) 与 nvcc
# 常见 CUDA 安装路径：/opt/cuda (Arch) / /usr/local/cuda (Ubuntu)
export PATH="$VENV_DIR/bin:/opt/cuda/bin:/usr/local/cuda/bin:${PATH:-}"

exec "$VENV_DIR/bin/vllm" serve "$MODEL_DIR" \
  --served-model-name Qwen2.5-VL-7B-Instruct-AWQ \
  --host 0.0.0.0 \
  --port "$PORT" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --limit-mm-per-prompt '{"image": 10}'
