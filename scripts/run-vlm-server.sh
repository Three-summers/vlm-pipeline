#!/usr/bin/env bash
# 启动 vLLM OpenAI 兼容服务，供管线的 Consumer 调用。
# 用法: ./run-vlm-server.sh [port]
#
# 环境变量（均可选，覆盖自动探测）：
#   VLMP_ROOT     / 默认：本脚本所在仓库根目录
#   VLM_MODEL_DIR / 默认：$ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ
#   VLM_VENV      / 默认：$ROOT/.venv-vllm → $ROOT/venv
#   VLM_GPU_UTIL  / 默认 0.85
#   VLM_MAX_LEN   / 默认 8192
#   VLM_ENFORCE_EAGER / 默认 1；跳过耗时且额外占显存的 CUDA Graph
#   VLLM_USE_V2_MODEL_RUNNER / 默认 0；设为 1 可重新启用 vLLM V2 model runner
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${VLMP_ROOT:-$ROOT}"
PORT="${1:-8001}"
GPU_UTIL="${VLM_GPU_UTIL:-0.85}"
MAX_LEN="${VLM_MAX_LEN:-8192}"
ENFORCE_EAGER="${VLM_ENFORCE_EAGER:-1}"
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

# 特别说明：vLLM 0.26 已不再识别旧变量 VLLM_USE_V1。
# 在 WSL + Blackwell (sm_120，例如 RTX 5070) 环境中，V2 model runner
# 会依赖当前不可用的 UVA，并以 "RuntimeError: UVA is not available" 退出。
# 因此默认只回退到 V1 model runner；这不是旧版 vLLM 的 V0/V1 engine 切换。
# 若后续 vLLM/WSL 已支持 UVA，可在启动前设置 VLLM_USE_V2_MODEL_RUNNER=1 验证。
: "${VLLM_USE_V2_MODEL_RUNNER:=0}"
export VLLM_USE_V2_MODEL_RUNNER

# 12 GiB 显卡上，7B AWQ 模型与运行时本身约需 9 GiB；0.55 的显存预算
# 不足以创建任何 KV cache。默认提高到 0.85，并把上下文降到 8192。
# eager 模式可跳过启动时长达数分钟的 CUDA Graph profiling/编译；若更看重
# 吞吐且显存充足，可设置 VLM_ENFORCE_EAGER=0 恢复 CUDA Graph。
VLLM_EXTRA_ARGS=()
if [ "$ENFORCE_EAGER" = "1" ]; then
  VLLM_EXTRA_ARGS+=(--enforce-eager)
fi

exec "$VENV_DIR/bin/vllm" serve "$MODEL_DIR" \
  --served-model-name Qwen2.5-VL-7B-Instruct-AWQ \
  --host 0.0.0.0 \
  --port "$PORT" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --limit-mm-per-prompt '{"image": 10, "video": 0}' \
  "${VLLM_EXTRA_ARGS[@]}"
