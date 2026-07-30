# 部署指南

本文说明如何部署 **vlm-pipeline**（YOLO + VLM 视频分析引擎与 Web 管理平台），列出**最低要求**与安装顺序。

---

## 1. 最低硬件要求

| 组件 | 最低 | 推荐 | 说明 |
|------|------|------|------|
| **GPU 显存** | 8 GB（仅 small_only）<br>12 GB（含 VLM 模式） | 24 GB+ | 仅跑 YOLO 不触发 VLM 只需普通显卡；启用 VLM 时，vLLM + Qwen2.5-VL-7B-AWQ 约占 8–10 GB，YOLO 额外约 2 GB |
| **系统内存** | 16 GB | 32 GB+ | vLLM 加载模型与 KV cache 需要额外 CPU 内存 |
| **磁盘可用空间** | 20 GB | 50 GB+ SSD | 模型权重 ~7 GB + YOLO 权重 + 运行输出与日志 |
| **CUDA** | 11.8+ | 12.x+ | 需与 PyTorch、vLLM 版本匹配 |
| **NVIDIA 驱动** | ≥ 525 | 最新稳定版 | 需对应 CUDA 版本 |

## 2. 软件环境

| 组件 | 版本 | 用途 | 说明 |
|------|------|------|------|
| 操作系统 | Linux (x86_64) | 运行环境 | 需要 NVIDIA 驱动与 CUDA 支持 |
| Python 环境 A | 3.11 | YOLO + Web | Flask、OpenCV、Ultralytics、PyTorch 等 |
| Python 环境 B | 3.12 | vLLM 推理服务 | `vllm` 包，独立 venv 避免依赖冲突 |

> **注意**：两个 Python 环境是独立的 —— 环境 A 供 Web 平台和分析子进程使用，环境 B 仅供 vLLM 推理服务使用，避免 vLLM 与 YOLO 栈的依赖互相拖垮。

## 3. 模型权重（需自行下载）

| 模型 | 大小 | 用途 | 来源 |
|------|------|------|------|
| **Qwen2.5-VL-7B-Instruct-AWQ** | ~6.5 GB | 所有 VLM 模式 | HuggingFace / ModelScope：`Qwen/Qwen2.5-VL-7B-Instruct-AWQ` |
| **YOLO 权重**（如 `yolo11m.pt`） | ~50–100 MB | `small_*` 模式 | Ultralytics 官方 |
| **YOLO 权重**（如 `yolo11n.pt`） | ~5 MB | 轻量 small_only | Ultralytics 官方 |

## 4. 本仓库不提供的文件

以下内容需在目标机自行准备，**不要**提交到 Git：

| 文件/目录 | 说明 |
|-----------|------|
| `models/` 下 VLM 权重 | Qwen2.5-VL-7B-Instruct-AWQ |
| `venv/` | vLLM 专用 Python 环境 |
| YOLO Python 环境 | `VLMP_ENV` 指向的 venv |
| YOLO `.pt` 权重文件 | 可放在任意路径，Web UI 中登记 |
| `data/vlmp.db*` | 生产数据库 |
| `output/`、`logs/` | 运行时输出与日志 |

---

## 5. 组件架构

```text
                    ┌─────────────────────────┐
   GPU 显存         │  vLLM (:8001)           │  ← scripts/run-vlm-server.sh
   （可调比例）     │  Qwen2.5-VL-7B-AWQ      │     环境: $VLM_VENV
                    └───────────▲─────────────┘
                                │ OpenAI 兼容 API
┌──────────────┐    ┌───────────┴─────────────┐
│ YOLO 权重    │───▶│  分析子进程              │  ← vlm_pipeline.py
│ + yolo env   │    │  Producer→Consumer→Saver │     环境: $VLMP_ENV
└──────────────┘    └───────────▲─────────────┘
                                │ 拉起/看门狗
                    ┌───────────┴─────────────┐
                    │  Web (:8090)            │  ← scripts/run-web.sh
                    │  Flask + Gunicorn       │     SQLite: $VLMP_DB
                    └─────────────────────────┘
```

---

## 6. 环境变量

所有变量均可选，未设置时使用默认值：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VLMP_ROOT` | 脚本自动探测（仓库根目录） | 项目根目录 |
| `VLMP_ENV` | `$VLMP_ROOT/.venv-web` | YOLO + Web 的 Python venv 路径 |
| `VLMP_PYTHON` | `$VLMP_ENV/bin/python` | 分析子进程解释器 |
| `VLMP_DB` | `$VLMP_ROOT/data/vlmp.db` | SQLite 主库路径 |
| `VLMP_PORT` | `8090` | Web 端口 |
| `VLM_VENV` | `$VLMP_ROOT/.venv-vllm` → `$VLMP_ROOT/venv` | 含 `vllm` 的 Python venv |
| `VLM_MODEL_DIR` | `$VLMP_ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ` | VLM 权重目录 |
| `VLM_GPU_UTIL` | `0.85` | vLLM `gpu_memory_utilization`；12 GiB 显卡运行 7B AWQ 所需的基准值 |
| `VLM_MAX_LEN` | `8192` | 最大上下文长度；可按可用 KV cache 调整 |
| `VLM_ENFORCE_EAGER` | `1` | 跳过 CUDA Graph profiling/编译，缩短启动时间并减少显存占用；设为 `0` 可恢复 |
| `VLLM_USE_V2_MODEL_RUNNER` | `0` | 规避 WSL + Blackwell 上的 `UVA is not available`；环境支持 UVA 后可设为 `1` |

---

## 7. 部署步骤

### 步骤 1 — 克隆仓库

```bash
git clone <your-repo-url> /path/to/vlm-pipeline
cd /path/to/vlm-pipeline
mkdir -p logs data output models
```

### 步骤 2 — 安装 NVIDIA 驱动与 CUDA

```bash
# 确认 GPU 可用
nvidia-smi
# 确认 CUDA 版本
nvcc --version
```

### 步骤 3 — 准备 YOLO / Web 环境（环境 A）

创建 Python 3.11 venv，安装核心依赖：

```bash
python3.11 -m venv .venv-web
source .venv-web/bin/activate
pip install torch opencv-python ultralytics flask gunicorn pyyaml numpy werkzeug requests
```

验证：
```bash
".venv-web/bin/python" -c "import flask, cv2, ultralytics, torch; print('ok')"
```

### 步骤 4 — 准备 vLLM 环境与模型（环境 B）

```bash
cd /path/to/vlm-pipeline

# 创建 vLLM venv
python3.12 -m venv .venv-vllm
source .venv-vllm/bin/activate
# 按 https://docs.vllm.ai 安装匹配 CUDA 版本的 vllm
pip install vllm

# 下载 VLM 权重到 models/
# 方式一：huggingface-cli download Qwen/Qwen2.5-VL-7B-Instruct-AWQ --local-dir models/Qwen2.5-VL-7B-Instruct-AWQ
# 方式二：modelscope download Qwen/Qwen2.5-VL-7B-Instruct-AWQ --local_dir models/Qwen2.5-VL-7B-Instruct-AWQ
```

**小显存调优**：`gpu_memory_utilization` 是 vLLM 可用显存上限，不是节省显存的比例。若出现 `No available memory for the cache blocks`，需适当提高 `VLM_GPU_UTIL`；若运行请求时 CUDA OOM，则降低 `VLM_MAX_LEN`、图片数或并发数。12 GiB 显卡同时运行 vLLM 与 YOLO 时，建议使用较小的 YOLO 模型并监控剩余显存。

### 步骤 5 — 准备 YOLO 权重

```bash
# 下载 YOLO 权重到任意目录，后续在 Web UI 中登记
wget https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m.pt -P /path/to/yolo/weights/
```

### 步骤 6 — 启动

```bash
cd /path/to/vlm-pipeline

# 终端 1：启动 VLM 推理服务（首次加载 60–90 秒）
./scripts/run-vlm-server.sh 8001

# 终端 2：启动 Web 平台
./scripts/run-web.sh 8090
```

- 浏览器访问：`http://<主机IP>:8090`
- 首次启动自动创建管理员，密码打印在终端输出中
- 登录后**立即修改密码**
- 健康检查：`curl -s http://127.0.0.1:8090/healthz`

### 步骤 7 — 验证

```bash
# 冒烟测试（无需 GPU/VLM）
".venv-web/bin/python" scripts/smoke_web.py

# 引擎自检（需 VLM 服务已启动，先修改 config yaml 中的路径）
export PATH="$(pwd)/.venv-web/bin:$PATH"
python3 vlm_pipeline.py selftest --config configs/demo-ppe-small-crop.yaml
```

引擎也可脱离 Web 独立运行：
```bash
export PATH="$(pwd)/.venv-web/bin:$PATH"
python3 vlm_pipeline.py run --config configs/demo-ppe-small-crop.yaml
```

### 步骤 8 — （可选）安装 systemd 服务

1. 编辑 `deploy/vlm-server.service` 和 `deploy/vlm-web.service`：
   - 将 `<VLMP_ENV>` 替换为实际 YOLO/Web 环境路径
   - 将 `<PROJECT_ROOT>` 替换为仓库实际路径
   - 将 `CHANGE_ME` 替换为实际运行用户/组
2. 安装：
```bash
sudo deploy/install.sh
sudo systemctl start vlm-server vlm-web
```

---

## 8. 配置文件修改

`configs/` 下的示例 YAML 包含占位路径，部署时需根据实际环境修改：

| 字段 | 修改为 |
|------|--------|
| `source.uri` | 实际图片目录 / 视频文件 / RTSP 地址 |
| `yolo.weights` | YOLO `.pt` 权重的绝对路径 |
| `alarm.output_dir` | 告警输出目录（建议指向 `output/` 子目录） |

> **注意**：RTSP 地址若含用户名密码，仅保留在本地 YAML 中，**切勿**提交到 Git。

---

## 9. 安全边界

**可以提交 Git**：源码、模板 YAML、示例配置（无真实密码）、deploy 模板、文档。

**禁止提交 Git**：
- `data/vlmp.db*`、`output/`、`logs/`（尤其含密码文件）
- `venv/`、`models/` 目录下所有权重文件
- RTSP URL 中的账号密码、API 令牌、VLM endpoint key

`.gitignore` 已覆盖常见项，但 `git add` 前仍需人工确认。

---

## 10. 故障排查

| 问题 | 检查 |
|------|------|
| `nvidia-smi` 无输出 | 驱动未安装或与内核不匹配 |
| vLLM 报 `No available memory for the cache blocks` | 提高 `VLM_GPU_UTIL`；同时降低 `VLM_MAX_LEN`，并关闭其他 GPU 进程 |
| vLLM 启动长时间停在 CUDA Graph profiling | 保持 `VLM_ENFORCE_EAGER=1`；若设为 `0`，首次编译可能持续数分钟 |
| vLLM 处理请求时 CUDA OOM | 降低 `VLM_MAX_LEN`、图片数或并发数，关闭其他 GPU 进程 |
| vLLM 找不到 CUDA | 确认 `nvcc --version` 版本，确认 venv 内 vllm 与 CUDA 匹配 |
| Web 启动报找不到模块 | 确认 `VLMP_ENV` 指向正确 venv，venv 内已安装 Flask/OpenCV 等 |
| YOLO 加载失败 | 确认 `.pt` 文件路径正确，Ultralytics 版本兼容 |
| 分析任务无告警 | 检查 VLM endpoint 配置（Web → 端点管理），确认 vLLM 服务可达 |
