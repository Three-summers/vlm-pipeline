# 部署指南：必备依赖与安装顺序

本文说明如何部署 **vlm-pipeline**（YOLO + VLM 视频分析引擎与 Web 管理平台），以及仓库**不包含**、但运行所**必须**的外部依赖。

---

## 1. 本仓库提供什么 / 不提供什么

### 提供（可 Git 版本化）

| 内容 | 路径 |
|------|------|
| 分析引擎 | `vlm_pipeline.py`、`vlmp/` |
| Web 平台 | `server/`（Flask + Jinja + 静态资源） |
| 启动与自检脚本 | `scripts/` |
| 示例 / 模板配置 | `configs/` |
| systemd 单元与安装脚本 | `deploy/` |
| 本文档与 README | `docs/`、`README.md` |

### 不提供（必须在目标机另行准备）

| 必备项 | 典型位置 / 规模 | 谁需要 |
|--------|-----------------|--------|
| **NVIDIA GPU** | 建议 ≥ 24 GB；原现场 RTX 5090 32GB，vLLM 约占 55% 显存 | 全部真实分析 |
| **CUDA + 驱动** | 与 vLLM / torch 匹配；现场 12.8+ | vLLM、YOLO |
| **YOLO Python 环境** | 默认 `/opt/offline/envs/yolo-py311` | Web、分析子进程、YOLO |
| **vLLM 专用 venv** | 默认 `$VLMP_ROOT/venv`（数 GB 级） | `scripts/run-vlm-server.sh` |
| **VLM 权重** | 默认 `$VLMP_ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ`（约 6.5GB AWQ） | Consumer / large 模式 |
| **YOLO 权重** | 常与 yolo-workflow 共用，如 `/srv/data/models/yolo/weights/` | small_* 模式 |
| **输入源** | 图片目录 / 视频文件 / RTSP | 任务运行 |
| **（可选）离线 wheelhouse** | `/srv/offline/current/python/...` | 断网装包 |

仓库**故意不包含**：生产 `data/vlmp.db`、访问日志、任务 output、模型权重、venv、初始管理员密码文件。

---

## 2. 组件关系

```text
                    ┌─────────────────────────┐
   GPU 显存         │  vLLM (:8001)           │  ← scripts/run-vlm-server.sh
   ~55% 预留        │  Qwen2.5-VL-7B-AWQ      │     环境: $VLM_VENV
                    └───────────▲─────────────┘
                                │ OpenAI API
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

两个 Python 环境是常见拆法：

- **`VLMP_ENV`（yolo-py311）**：Flask/gunicorn、OpenCV、Ultralytics、任务子进程  
- **`VLM_VENV`（项目内 venv）**：仅 vLLM 服务，避免与 YOLO 栈互相拖垮依赖

---

## 3. 路径与环境变量

| 变量 | 默认 | 含义 |
|------|------|------|
| `VLMP_ROOT` | 仓库根（脚本自动探测） | 项目根目录 |
| `VLMP_ENV` | `/opt/offline/envs/yolo-py311` | YOLO + Web 的 Python 前缀 |
| `VLMP_PYTHON` | `$VLMP_ENV/bin/python` | 分析子进程解释器（run-web 会设置） |
| `VLMP_DB` | `$VLMP_ROOT/data/vlmp.db` | SQLite 主库 |
| `VLMP_PORT` | `8090` | Web 端口 |
| `VLM_VENV` | `$VLMP_ROOT/venv` | 含 `vllm` 的环境 |
| `VLM_MODEL_DIR` | `$VLMP_ROOT/models/Qwen2.5-VL-7B-Instruct-AWQ` | VLM 权重目录 |
| `VLM_GPU_UTIL` | `0.55` | vLLM `gpu_memory_utilization` |
| `VLM_MAX_LEN` | `16384` | 最大上下文 |

示例配置里的数据路径请改成你机器上的真实目录（见 `configs/*.yaml` 注释）。

---

## 4. 推荐部署顺序

### 步骤 1 — 硬件与驱动

- 安装 NVIDIA 驱动与 CUDA（需满足所选 vLLM / torch 版本）。
- 确认 `nvidia-smi` 可用，显存足够同时跑 vLLM（~55%）+ YOLO。

### 步骤 2 — 放置本仓库

建议（与离线工作站一致）：

```bash
sudo install -d -o "$USER" -g "$USER" /srv/data/projects
git clone <your-fork-or-url> /srv/data/projects/vlm-pipeline
cd /srv/data/projects/vlm-pipeline
```

任意路径亦可，只要设置 `VLMP_ROOT` 或直接在该目录执行脚本。

### 步骤 3 — YOLO / Web 环境（`VLMP_ENV`）

必须能 `import` 至少：Flask（或 gunicorn）、OpenCV、PyYAML、Ultralytics/torch（若跑 YOLO 模式）。

断网现场通常直接使用已有：

```bash
export VLMP_ENV=/opt/offline/envs/yolo-py311
"$VLMP_ENV/bin/python" -c "import flask, cv2, yaml; print('ok')"
```

联网自建时，在 3.11 venv 中安装与现场接近的版本（torch 需匹配 CUDA）。本仓库不绑定单一 `requirements.txt` 锁文件时，以你工作站已验证的 yolo 环境为准；可与 **yolo-workflow** 共用同一环境。

### 步骤 4 — vLLM 环境与模型（`VLM_VENV` + 权重）

```bash
cd "$VLMP_ROOT"
python3.12 -m venv venv          # 版本按 vLLM 文档选择
source venv/bin/activate
# 按 https://docs.vllm.ai 安装匹配 CUDA 的 vllm
# 下载 AWQ 权重到 models/Qwen2.5-VL-7B-Instruct-AWQ/
# 来源示例：ModelScope 或 Hugging Face  Qwen/Qwen2.5-VL-7B-Instruct-AWQ
```

断网时从离线发布或移动硬盘导入 `venv` 与 `models/`，**不要**把它们 commit 进 Git。

### 步骤 5 — YOLO 权重（small_* 模式）

```bash
# 示例
ls /srv/data/models/yolo/weights/yolo11m.pt
```

在 Web「算法管理」中扫描登记，或在 YAML 的 `yolo.weights` 填写绝对路径。

### 步骤 6 — 启动

```bash
cd /path/to/vlm-pipeline
mkdir -p logs data output models

# 终端 1：VLM（首次加载 60–90s）
./scripts/run-vlm-server.sh 8001

# 终端 2：Web
./scripts/run-web.sh 8090
```

- 浏览器：`http://<主机>:8090`  
- 首次启动会创建管理员；初始密码打印在初始化输出 / 历史上可能写在 `logs/initial-admin-password.txt`——**登录后立刻改密并删除该文件，且勿提交 Git**。  
- 探活：`curl -s http://127.0.0.1:8090/healthz`

### 步骤 7 — 验证

```bash
"$VLMP_ENV/bin/python" scripts/smoke_web.py
# 端到端（平台已启动、且有演示数据路径时）：
# VLMP_ADMIN_PW=... "$VLMP_ENV/bin/python" scripts/e2e_check.py
```

引擎也可脱离 Web：

```bash
export PATH="$VLMP_ENV/bin:$PATH"
python3 vlm_pipeline.py selftest --config configs/demo-ppe-small-crop.yaml
# 先按机器改 yaml 中的 uri / weights / output_dir
```

### 步骤 8 — 可选 systemd

1. 编辑 `deploy/vlm-server.service`、`deploy/vlm-web.service`：  
   - 将 `User=` / `Group=` 的 `CHANGE_ME` 改为实际用户  
   - 核对 `WorkingDirectory`、`VLMP_*`、`VLM_*`、`PATH`  
2. `sudo deploy/install.sh`  
3. `sudo systemctl start vlm-server vlm-web`

---

## 5. 配置文件怎么改

`configs/` 内示例仍可能带有 `/srv/data/...` 占位，表示**原离线工作站布局**，不是你的机器保证存在的路径。部署时请：

1. 复制一份 yaml，改 `source.uri`、`yolo.weights`、`alarm.output_dir`  
2. RTSP 模板中的账号密码**只放本地**，不要推送远程仓库  
3. `output_dir` 建议指向本仓库下 `output/` 或数据盘上的专用目录  

---

## 6. 安全与 Git 边界

**可提交**：源码、模板、示例配置（无真实口令）、deploy 单元模板、文档。  

**禁止提交**：

- `data/vlmp.db*`、任务 `output/`、`logs/`（尤其含密码的文件）  
- `venv/`、`models/` 权重  
- RTSP URL 中的账号密码、开放 API 令牌、VLM endpoint key  

`.gitignore` 已覆盖常见项；`git status` 仍应人工确认。

---

## 7. 与 yolo-workflow 的关系

| | yolo-workflow | vlm-pipeline |
|--|---------------|--------------|
| 职责 | 标注、数据集、训练、离线推理 CLI | 视频/图片流上的 YOLO+VLM 分析与 Web |
| CVAT | 本仓 deploy 模板管理 | 不依赖 |
| 环境 | `yolo-py311` | Web/YOLO 可共用；vLLM 单独 venv |
| 权重 | `/srv/data/models/yolo/weights` | 可读同一目录 |

两者应保持**两个 Git 仓库**；通过约定路径与环境变量协作，而不是 monorepo 强耦合。
