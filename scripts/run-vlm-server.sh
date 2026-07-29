#!/usr/bin/env bash
# 启动 vLLM OpenAI 兼容服务，供管线的 Consumer 调用。
# 用法: ./run-vlm-server.sh [port]
#
# 环境变量（均可选）：
#   VLMP_ROOT / 默认：本脚本所在仓库根目录
#   VLM_MODEL_DIR / 默认：$ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ
#   VLM_VENV / 默认：$ROOT/venv （内含 vllm）
#   VLM_GPU_UTIL / 默认 0.55（给同卡 YOLO 留显存）
#   VLM_MAX_LEN / 默认 16384
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${VLMP_ROOT:-$ROOT}"
VENV_DIR="${VLM_VENV:-$ROOT/venv}"
MODEL_DIR="${VLM_MODEL_DIR:-$ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ}"
PORT="${1:-8001}"
GPU_UTIL="${VLM_GPU_UTIL:-0.55}"
MAX_LEN="${VLM_MAX_LEN:-16384}"

if [ ! -x "$VENV_DIR/bin/vllm" ]; then
  echo "找不到 vllm: $VENV_DIR/bin/vllm" >&2
  echo "请先按 docs/DEPLOY.md 准备 vLLM 环境与模型权重。" >&2
  exit 1
fi
if [ ! -d "$MODEL_DIR" ]; then
  echo "找不到模型目录: $MODEL_DIR" >&2
  exit 1
fi

# FlashInfer 运行时 JIT 需要 ninja(venv 内) 与 nvcc
export PATH="$VENV_DIR/bin:/usr/local/cuda/bin:${PATH:-}"

exec "$VENV_DIR/bin/vllm" serve "$MODEL_DIR" \
  --served-model-name Qwen2.5-VL-7B-Instruct-AWQ \
  --host 0.0.0.0 \
  --port "$PORT" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --limit-mm-per-prompt '{"image": 10}'
