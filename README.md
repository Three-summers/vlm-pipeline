# vlm-pipeline

**YOLO + VLM** 大小模型协同的视频/图片分析系统（单机版）：

- **分析引擎** `vlm_pipeline.py`：Producer（YOLO 检测/跟踪/ROI）→ 队列 → Consumer（VLM 语义判定）→ Saver（SQLite + 截图/录像）
- **管理平台** `server/`：Flask + Jinja Web，覆盖视频源、算法、任务、告警、推送、运维、开放 API 等

> **部署必读**：[docs/DEPLOY.md](docs/DEPLOY.md)（必备硬件/环境/模型、安装顺序、环境变量、安全边界）

本仓库**只含源码与模板**。GPU 环境、vLLM venv、VLM/YOLO 权重、生产数据库与日志需在目标机另行准备。

## 目录

```text
vlm-pipeline/
├── vlm_pipeline.py        # 分析引擎（可独立 CLI 运行）
├── vlmp/                  # 共用：db / rag / push
├── server/                # Web 平台
├── scripts/               # run-vlm-server / run-web / smoke / e2e / init
├── deploy/                # systemd 单元（安装前改 User 与路径）
├── configs/               # 引擎 YAML 示例（请按本机改路径）
├── docs/DEPLOY.md         # 部署与必备依赖
├── data/                  # 运行时 SQLite（空目录占位，不提交库文件）
├── models/                # 放置 VLM 权重（不提交）
├── logs/ output/          # 运行时（不提交）
├── .venv-vllm/            # vLLM 环境（不提交，自建）
├── .venv-web/             # YOLO/Web 环境（不提交，自建）
```

## 最低必备

| 类别 | 最低要求 |
|------|----------|
| GPU | NVIDIA，8 GB 显存（仅 YOLO）/ 12 GB+（含 VLM） |
| 环境 A | `VLMP_ENV`：Python 3.11 + Flask / OpenCV / Ultralytics / PyTorch |
| 环境 B | `VLM_VENV`：Python 3.12 + `vllm` |
| 模型 | Qwen2.5-VL-7B-Instruct-AWQ（~6.5 GB）+ YOLO `.pt` 权重（small_* 模式） |
| 输入 | 图片目录 / 视频文件 / RTSP 流 |

> 不依赖 MySQL、Redis、MinIO 等外部服务 —— 全部使用单机轻量替代。完整硬件清单与安装步骤见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 快速启动（依赖已就绪时）

```bash
cd /path/to/vlm-pipeline
./scripts/run-vlm-server.sh 8001          # 等模型加载完成（60-90s）
./scripts/run-web.sh 8090                 # http://<host>:8090
```

常用环境变量：`VLMP_ENV`、`VLMP_ROOT`、`VLM_MODEL_DIR`、`VLM_VENV`、`VLM_GPU_UTIL`。

首次 Web 启动会创建管理员；**立即改密**，不要把密码文件提交到 Git。

验证：

```bash
.venv-web/bin/python scripts/smoke_web.py
```

引擎单独跑（先修改 yaml 配置中的路径）：

```bash
export PATH="$(pwd)/.venv-web/bin:$PATH"
python3 vlm_pipeline.py selftest --config configs/demo-ppe-small-crop.yaml
python3 vlm_pipeline.py run      --config configs/demo-ppe-small-crop.yaml
```

## 五种分析模式

| 模式 | YOLO | VLM | 行为 |
| --- | --- | --- | --- |
| `small_only` | ✅ | ❌ | 目标驻留达 `dwell_seconds` 直接告警 |
| `small_crop` | ✅ | ✅ | 裁剪目标区域（外扩 15%）送 VLM 判定 |
| `small_full` | ✅ | ✅ | 检出目标后送完整帧给 VLM |
| `large_only` | ❌ | ✅ | 每 `large_interval_seconds` 定时全帧送审 |
| `temporal` | ❌ | ✅ | `window_seconds` 内等间隔采 `frames_count` 帧一次性送审 |

`target_actions` 为空时进入**巡检模式**：每次 VLM 分析都落库为记录（`is_alarm=false`）。

## 平台功能概要

| 能力 | 路由/说明 |
| --- | --- |
| 仪表板 | `/` KPI、趋势、任务与告警 |
| 视频源 / 算法 / 任务 | `/sources` `/algorithms` `/tasks` |
| 告警 | `/alarms` 筛选、确认、导出 |
| 智能对话 / 规范 RAG | `/chat` `/docs` |
| 用户权限 | viewer / operator / admin |
| 推送与开放 API | `/push` `/openapi/v1/*` |
| 运维与监控墙 | `/ops` `/wall` |
| VLM 端点 | `/endpoints` 多端点与故障转移 |
| 实时预览 | `/live/<id>.mjpg` |

## 与商用参考栈的差异

离网单机用功能等价替换，而非同名中间件：

| 参考常见组件 | 本实现 |
| --- | --- |
| MySQL | SQLite（WAL） |
| Redis 队列 | 进程内 `queue.Queue` |
| MinIO | 本地 `output/` |
| MediaMTX | MJPEG over HTTP |
| ChromaDB | 标准库 TF-IDF |
| NVENC | `cv2.VideoWriter`（短片段） |

## 安全约定

- 媒体读取限制在 `output/` 内，拒绝目录穿越。
- VLM API Key 只写不读；开放 API 不回显可能含凭据的源 `uri`。
- RTSP 凭据、管理员密码、令牌：**仅本地**，勿进 Git。

## 与 yolo-workflow

CVAT / 训练 / 数据集版本化见独立仓库 **yolo-workflow**。本仓可共用其 YOLO 环境与权重目录，部署与版本各自管理。详见 [docs/DEPLOY.md §7](docs/DEPLOY.md)。

## 来源说明

整理自离线工作站项目快照；已去除生产库、日志、密钥与主机专用账号。运行时路径通过环境变量与配置覆盖，默认值可兼容原工作站布局，新部署请按 [docs/DEPLOY.md](docs/DEPLOY.md) 设置。
