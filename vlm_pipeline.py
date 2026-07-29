#!/usr/bin/env python3
"""YOLO + VLM 大小模型协同视频分析引擎。

架构与《AI视频数据分析系统》参考文档一致，压缩为单进程三角色：
  Producer(YOLO 检测/跟踪/ROI) -> 任务队列 -> Consumer(VLM 判定) -> Saver(落库/截图/录像/推送)

分析模式：small_only / small_crop / small_full / large_only / temporal
输入类型：image(目录或单图) / video(文件) / rtsp(实时流)
两种启动方式：
  run --config task.yaml        独立运行（不依赖平台）
  run --task <id>               由平台调度，配置读自 SQLite，结果回写 DB
"""

import argparse
import base64
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlmp import db, push, rag                      # noqa: E402

LOG = logging.getLogger("vlm-pipeline")

MODES = ("small_only", "small_crop", "small_full", "large_only", "temporal")
ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    task_id: str
    source_type: str            # image | video | rtsp
    source_uri: str
    mode: str
    yolo_weights: str = ""
    yolo_device: str = "0"
    yolo_conf: float = 0.25
    yolo_classes: list = field(default_factory=list)
    yolo_imgsz: int = 640
    frame_stride: int = 5
    roi_include: list = field(default_factory=list)
    roi_exclude: list = field(default_factory=list)
    dwell_seconds: float = 2.0
    vlm_interval_seconds: float = 2.0
    vlm_endpoints: list = field(default_factory=list)   # [{base_url,api_key,model,weight}]
    vlm_timeout: int = 90
    vlm_max_tokens: int = 512
    vlm_temperature: float = 0.0
    target_actions: list = field(default_factory=list)
    custom_prompt: str = ""
    rule_prompt: str = ""                              # RAG 注入的规范条款
    min_confidence: float = 0.0
    cooldown_seconds: float = 30.0
    output_dir: str = "output"
    large_interval_seconds: float = 2.0
    temporal_window_seconds: float = 5.0
    temporal_frames_count: int = 8
    consumer_threads: int = 2
    max_frames: int = 0
    save_annotated: bool = True
    jpeg_quality: int = 85
    # 平台集成
    db_task_id: int = 0
    run_id: int = 0
    task_name: str = ""
    live_preview: bool = True
    live_fps: float = 2.0
    record_clip: bool = False
    clip_pre_frames: int = 15
    clip_post_frames: int = 25
    clip_fps: float = 5.0

    @property
    def inspection_mode(self) -> bool:
        return not self.target_actions

    @property
    def needs_yolo(self) -> bool:
        return self.mode in ("small_only", "small_crop", "small_full")

    @property
    def needs_vlm(self) -> bool:
        return self.mode != "small_only"


def _apply_common(cfg: Config) -> Config:
    if cfg.mode not in MODES:
        raise SystemExit(f"invalid mode: {cfg.mode}, expected one of {MODES}")
    if cfg.source_type not in ("image", "video", "rtsp"):
        raise SystemExit(f"invalid source.type: {cfg.source_type}")
    if cfg.needs_yolo and not cfg.yolo_weights:
        raise SystemExit(f"mode {cfg.mode} requires yolo weights")
    if cfg.needs_vlm and not cfg.vlm_endpoints:
        raise SystemExit(f"mode {cfg.mode} requires at least one VLM endpoint")
    if cfg.mode == "small_only" and cfg.inspection_mode:
        LOG.warning("small_only 模式不调用 VLM，target_actions 为空仅表示按驻留告警")
    return cfg


def load_config(path: str) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    src = raw.get("source", {})
    y = raw.get("yolo", {})
    roi = raw.get("roi", {}) or {}
    v = raw.get("vlm", {})
    a = raw.get("alarm", {})
    t = raw.get("temporal", {})
    endpoints = v.get("endpoints") or [{
        "base_url": v.get("base_url", "http://127.0.0.1:8001/v1").rstrip("/"),
        "api_key": v.get("api_key", "EMPTY"),
        "model": v.get("model", "Qwen2.5-VL-7B-Instruct-AWQ"),
        "weight": 1,
    }]
    cfg = Config(
        task_id=str(raw.get("task_id", "task")),
        source_type=src.get("type", "video"),
        source_uri=str(src.get("uri", "")),
        mode=raw.get("mode", "small_crop"),
        yolo_weights=y.get("weights", ""),
        yolo_device=str(y.get("device", "0")),
        yolo_conf=float(y.get("conf", 0.25)),
        yolo_classes=y.get("classes") or [],
        yolo_imgsz=int(y.get("imgsz", 640)),
        frame_stride=int(raw.get("frame_stride", 5)),
        roi_include=roi.get("include") or [],
        roi_exclude=roi.get("exclude") or [],
        dwell_seconds=float(raw.get("dwell_seconds", 2.0)),
        vlm_interval_seconds=float(raw.get("vlm_interval_seconds", 2.0)),
        vlm_endpoints=endpoints,
        vlm_timeout=int(v.get("timeout", 90)),
        vlm_max_tokens=int(v.get("max_tokens", 512)),
        vlm_temperature=float(v.get("temperature", 0.0)),
        target_actions=raw.get("target_actions") or [],
        custom_prompt=raw.get("prompt", "") or "",
        min_confidence=float(a.get("min_confidence", 0.0)),
        cooldown_seconds=float(a.get("cooldown_seconds", 30.0)),
        output_dir=a.get("output_dir", "output"),
        large_interval_seconds=float(raw.get("large_interval_seconds", 2.0)),
        temporal_window_seconds=float(t.get("window_seconds", 5.0)),
        temporal_frames_count=int(t.get("frames_count", 8)),
        consumer_threads=int(raw.get("consumer_threads", 2)),
        max_frames=int(raw.get("max_frames", 0)),
        save_annotated=bool(raw.get("save_annotated", True)),
        live_preview=bool(raw.get("live_preview", False)),
        record_clip=bool(raw.get("record_clip", False)),
    )
    return _apply_common(cfg)


def load_config_from_db(task_db_id: int) -> Config:
    """平台模式：从 tasks/sources/algorithms/vlm_endpoints/rule_docs 组装配置。"""
    db.init_db()
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_db_id,))
    if not row:
        raise SystemExit(f"任务不存在: {task_db_id}")
    c = db.task_config(row)
    src = db.query_one("SELECT * FROM sources WHERE id=?", (row["source_id"],))
    if not src:
        raise SystemExit(f"任务 {task_db_id} 的视频源不存在")
    algo = db.query_one("SELECT * FROM algorithms WHERE id=?", (row["algorithm_id"],))

    if row["endpoint_id"]:
        eps = db.query("SELECT * FROM vlm_endpoints WHERE id=? AND enabled=1", (row["endpoint_id"],))
    else:
        eps = db.query("SELECT * FROM vlm_endpoints WHERE enabled=1 ORDER BY weight DESC, id")
    endpoints = [{"base_url": e["base_url"].rstrip("/"), "api_key": e["api_key"],
                  "model": e["model"], "weight": e["weight"]} for e in eps]

    # RAG：把绑定的规范文档按目标行为检索出条款，注入 system prompt
    rule_prompt = ""
    doc_ids = [int(x) for x in (row["rule_doc_ids"] or "").split(",") if x.strip().isdigit()]
    if doc_ids:
        actions = c.get("target_actions") or []
        q = "、".join(actions) or (c.get("prompt") or row["name"])
        top_k = int(db.get_setting("rag_top_k", "3"))
        rule_prompt = rag.build_rule_prompt(rag.search(db, q, top_k, doc_ids))

    cfg = Config(
        task_id=f"task-{task_db_id}",
        source_type=src["type"],
        source_uri=src["uri"],
        mode=row["mode"],
        yolo_weights=(algo["weights_path"] if algo else ""),
        yolo_device=str(c.get("device", db.get_setting("gpu_index", "0"))),
        yolo_conf=float(c.get("conf", 0.25)),
        yolo_classes=c.get("classes") or [],
        yolo_imgsz=int(c.get("imgsz", 640)),
        frame_stride=int(c.get("frame_stride", db.get_setting("default_frame_stride", "5"))),
        roi_include=c.get("roi_include") or [],
        roi_exclude=c.get("roi_exclude") or [],
        dwell_seconds=float(c.get("dwell_seconds", 2.0)),
        vlm_interval_seconds=float(c.get("vlm_interval_seconds", 2.0)),
        vlm_endpoints=endpoints,
        vlm_timeout=int(c.get("vlm_timeout", 90)),
        vlm_max_tokens=int(c.get("max_tokens", 512)),
        vlm_temperature=float(c.get("temperature", 0.0)),
        target_actions=c.get("target_actions") or [],
        custom_prompt=c.get("prompt", "") or "",
        rule_prompt=rule_prompt,
        min_confidence=float(c.get("min_confidence", 0.0)),
        cooldown_seconds=float(c.get("cooldown_seconds", 30.0)),
        output_dir=str(ROOT / "output"),
        large_interval_seconds=float(c.get("large_interval_seconds", 2.0)),
        temporal_window_seconds=float(c.get("temporal_window_seconds", 5.0)),
        temporal_frames_count=int(c.get("temporal_frames_count", 8)),
        consumer_threads=int(c.get("consumer_threads", 2)),
        max_frames=int(c.get("max_frames", 0)),
        save_annotated=bool(c.get("save_annotated", True)),
        jpeg_quality=int(db.get_setting("snapshot_quality", "85")),
        db_task_id=task_db_id,
        task_name=row["name"],
        live_preview=bool(c.get("live_preview", True)),
        record_clip=bool(c.get("record_clip", False)),
        clip_pre_frames=int(c.get("clip_pre_frames", 15)),
        clip_post_frames=int(c.get("clip_post_frames", 25)),
    )
    return _apply_common(cfg)


# ---------------------------------------------------------------------------
# 队列消息
# ---------------------------------------------------------------------------

@dataclass
class VlmTask:
    frame_idx: int
    ts: float
    images_jpeg: list           # list[bytes]，temporal 模式为多帧
    full_frame_jpeg: bytes      # 告警取证用全帧
    track_id: int = -1
    det_label: str = ""
    det_conf: float = 0.0
    box: tuple = ()


@dataclass
class AlarmEvent:
    frame_idx: int
    ts: float
    is_alarm: bool
    action: str
    confidence: float
    description: str
    raw_text: str
    images_jpeg: list
    full_frame_jpeg: bytes
    track_id: int = -1
    det_label: str = ""
    det_conf: float = 0.0
    box: tuple = ()
    vlm_latency: float = 0.0


# ---------------------------------------------------------------------------
# VLM 客户端（OpenAI 兼容协议，多端点轮询 + 故障转移）
# ---------------------------------------------------------------------------

class VlmClient:
    COOLDOWN = 30.0                    # 端点失败后的冷却秒数

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.endpoints = list(cfg.vlm_endpoints)
        self._idx = 0
        self._lock = threading.Lock()
        self._down_until = {}          # base_url -> ts

    def pick(self) -> list:
        """按轮询顺序返回候选端点，冷却中的排到最后（全挂时仍会尝试）。"""
        with self._lock:
            n = len(self.endpoints)
            order = [self.endpoints[(self._idx + i) % n] for i in range(n)]
            self._idx = (self._idx + 1) % n
        now = time.time()
        healthy = [e for e in order if self._down_until.get(e["base_url"], 0) < now]
        return healthy + [e for e in order if e not in healthy]

    def build_system_prompt(self) -> str:
        base = self.cfg.custom_prompt
        if not base:
            if self.cfg.inspection_mode:
                base = (
                    "你是视频监控巡检分析助手。请仔细观察画面，用 2-4 句话描述场景、人员、"
                    "设备状态与任何异常情况（必须写画面中真实可见的内容）。"
                    "最后另起一行输出一个 JSON 对象："
                    '{"hit": false, "action": "", "confidence": 0.0, "description": "<把你的巡检结论压缩为一句话>"}'
                )
            else:
                actions = "、".join(self.cfg.target_actions)
                base = (
                    "你是视频监控行为判定助手。判断画面中是否存在以下目标行为之一："
                    f"【{actions}】。判定规则：只有画面清晰、明确地显示某个目标行为时才算命中；"
                    "画面模糊、被遮挡或与行为描述相反（例如目标行为是“未佩戴”而画面中人员已佩戴）"
                    "一律判为不命中。description 必须描述画面中支持该判定的具体证据，"
                    "不允许与 action 矛盾。"
                    "只输出一个 JSON 对象，不要输出其他文字："
                    '{"hit": true或false, "action": "命中的行为名，未命中为空字符串", '
                    '"confidence": 0到1之间的小数, "description": "画面证据描述"}'
                )
        if self.cfg.rule_prompt:
            base = f"{self.cfg.rule_prompt}\n\n{base}"
        return base

    def build_user_content(self, task: VlmTask) -> list:
        content = []
        for jpg in task.images_jpeg:
            b64 = base64.b64encode(jpg).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        if len(task.images_jpeg) > 1:
            text = (
                f"以上 {len(task.images_jpeg)} 张图片是同一画面按时间顺序的连续采样，"
                "请综合时序变化进行判定。"
            )
        elif task.det_label:
            text = f"图中检测到目标（类别 {task.det_label}），请按要求判定。"
        else:
            text = "请按要求分析画面并输出判定结果。"
        content.append({"type": "text", "text": text})
        return content

    def infer(self, task: VlmTask) -> dict:
        messages = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": self.build_user_content(task)},
        ]
        last_exc = None
        for ep in self.pick():
            payload = {
                "model": ep["model"], "messages": messages,
                "max_tokens": self.cfg.vlm_max_tokens,
                "temperature": self.cfg.vlm_temperature,
            }
            t0 = time.time()
            try:
                resp = self.session.post(
                    f"{ep['base_url']}/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {ep['api_key']}"},
                    timeout=self.cfg.vlm_timeout)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"].get("content") or ""
            except Exception as exc:
                last_exc = exc
                self._down_until[ep["base_url"]] = time.time() + self.COOLDOWN
                LOG.warning("VLM 端点 %s 失败，切换下一个: %s", ep["base_url"], exc)
                continue
            verdict = self.parse_verdict(content)
            verdict["latency"] = time.time() - t0
            verdict["raw"] = content
            verdict["endpoint"] = ep["base_url"]
            return verdict
        raise RuntimeError(f"所有 VLM 端点均不可用: {last_exc}")

    def parse_verdict(self, text: str) -> dict:
        # 三级容错：直接 JSON -> 正则提取 JSON（含嵌套） -> 关键词匹配
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.M).strip()
        candidates = [cleaned]
        candidates += re.findall(r"\{[^{}]*\}", cleaned, flags=re.S)
        m = re.search(r"\{.*\}", cleaned, flags=re.S)      # 兜底：最外层大括号（允许嵌套）
        if m:
            candidates.append(m.group(0))
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and "hit" in obj:
                    return {
                        "hit": bool(obj.get("hit")),
                        "action": str(obj.get("action") or ""),
                        "confidence": float(obj.get("confidence") or 0.0),
                        "description": str(obj.get("description") or ""),
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        hit_action = next((a for a in self.cfg.target_actions if a in cleaned), "")
        return {
            "hit": bool(hit_action),
            "action": hit_action,
            "confidence": 0.5 if hit_action else 0.0,
            "description": cleaned[:200],
        }


# ---------------------------------------------------------------------------
# 帧源
# ---------------------------------------------------------------------------

def iter_frames(cfg: Config, stop: threading.Event):
    """统一产出 (frame_idx, ts, frame_bgr)。按 frame_stride 抽帧。"""
    if cfg.source_type == "image":
        p = Path(cfg.source_uri)
        paths = sorted(p.glob("*")) if p.is_dir() else [p]
        paths = [x for x in paths if x.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")]
        for i, img_path in enumerate(paths):
            if stop.is_set():
                return
            frame = cv2.imread(str(img_path))
            if frame is None:
                LOG.warning("无法读取图片: %s", img_path)
                continue
            yield i, time.time(), frame
        return

    cap = cv2.VideoCapture(cfg.source_uri)
    if not cap.isOpened():
        raise SystemExit(f"无法打开视频源: {cfg.source_uri}")
    # 视频文件解码快于实时：窗口/驻留计时须用媒体时间；RTSP 用挂钟时间
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:        # 0/NaN 都回退到 25
        fps = 25.0
    idx = 0
    try:
        while not stop.is_set():
            ok, frame = cap.read()
            if not ok:
                if cfg.source_type == "rtsp":
                    LOG.warning("RTSP 断流，5 秒后重连")
                    cap.release()
                    time.sleep(5)
                    cap = cv2.VideoCapture(cfg.source_uri)
                    continue
                return
            if idx % cfg.frame_stride == 0:
                ts = time.time() if cfg.source_type == "rtsp" else idx / fps
                yield idx, ts, frame
            idx += 1
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# ROI
# ---------------------------------------------------------------------------

def point_in_polys(pt, polys) -> bool:
    for poly in polys:
        arr = np.array(poly, dtype=np.int32)
        if cv2.pointPolygonTest(arr, (float(pt[0]), float(pt[1])), False) >= 0:
            return True
    return False


def box_in_roi(cfg: Config, box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if cfg.roi_include and not point_in_polys((cx, cy), cfg.roi_include):
        return False
    if cfg.roi_exclude and point_in_polys((cx, cy), cfg.roi_exclude):
        return False
    return True


def scale_roi_to_frame(cfg: Config, w: int, h: int):
    """Web 端 ROI 编辑器输出归一化坐标（0~1），YAML 里习惯写像素坐标。

    这里按「所有顶点分量都 <= 1」判定为归一化，并就地放大到帧尺寸；
    像素坐标的配置保持原样。
    """
    def norm(polys):
        out = []
        for poly in polys:
            pts = [(float(x), float(y)) for x, y in poly]
            if pts and all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in pts):
                pts = [(x * w, y * h) for x, y in pts]
            out.append([[int(round(x)), int(round(y))] for x, y in pts])
        return out
    cfg.roi_include = norm(cfg.roi_include)
    cfg.roi_exclude = norm(cfg.roi_exclude)
    if cfg.roi_include or cfg.roi_exclude:
        LOG.info("ROI 生效: include=%d 个多边形, exclude=%d 个（帧尺寸 %dx%d）",
                 len(cfg.roi_include), len(cfg.roi_exclude), w, h)


def jpeg(frame, quality) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def draw_overlay(frame, boxes_info, cfg: Config):
    """在帧上画 ROI 与检测框（cv2 无中文字形，标签用英文/ID）。"""
    for poly in cfg.roi_include:
        cv2.polylines(frame, [np.array(poly, np.int32)], True, (0, 255, 0), 2)
    for poly in cfg.roi_exclude:
        cv2.polylines(frame, [np.array(poly, np.int32)], True, (0, 0, 255), 2)
    for label, tid, conf, box in boxes_info:
        x1, y1, x2, y2 = (int(v) for v in box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2)
        cv2.putText(frame, f"{label} #{tid} {conf:.2f}", (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
    return frame


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.task_q: "queue.Queue[VlmTask]" = queue.Queue(maxsize=16)
        self.alarm_q: "queue.Queue[AlarmEvent]" = queue.Queue(maxsize=64)
        self.vlm = VlmClient(cfg) if cfg.needs_vlm else None
        self.yolo = None
        self.stats = {"frames": 0, "detections": 0, "vlm_calls": 0,
                      "vlm_errors": 0, "alarms": 0, "records": 0, "dropped": 0}
        self.stats_lock = threading.Lock()
        self.out_dir = Path(cfg.output_dir) / cfg.task_id
        (self.out_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        if cfg.record_clip:
            (self.out_dir / "clips").mkdir(parents=True, exist_ok=True)
        self.alarm_file = self.out_dir / "alarms.jsonl"
        self.live_path = self.out_dir / "live.jpg"
        self._last_live = 0.0
        # 驻留与冷却状态
        self.track_state = {}       # track_id -> {"first": ts, "last_vlm": ts, "alarmed": bool}
        self.last_alarm_ts = {}     # action -> ts
        # 告警录像：抽帧环形缓冲 + 待补录片段
        self.ring = deque(maxlen=max(cfg.clip_pre_frames, 1))
        self.pending_clips = []
        self.clip_lock = threading.Lock()
        self._roi_scaled = not (cfg.roi_include or cfg.roi_exclude)

    # -- Producer ----------------------------------------------------------

    def load_yolo(self):
        from ultralytics import YOLO
        LOG.info("加载 YOLO 权重: %s (device=%s)", self.cfg.yolo_weights, self.cfg.yolo_device)
        self.yolo = YOLO(self.cfg.yolo_weights)

    def submit(self, task: VlmTask):
        # 队列满时丢弃最旧任务，保证实时性（与参考文档一致）
        try:
            self.task_q.put_nowait(task)
        except queue.Full:
            try:
                self.task_q.get_nowait()
                with self.stats_lock:
                    self.stats["dropped"] += 1
            except queue.Empty:
                pass
            self.task_q.put_nowait(task)

    def publish_live(self, frame, boxes_info):
        """写实时预览帧，供 Web 端 MJPEG 拉取（原子替换避免读到半张图）。"""
        if not self.cfg.live_preview:
            return
        now = time.time()
        if now - self._last_live < 1.0 / max(self.cfg.live_fps, 0.1):
            return
        self._last_live = now
        vis = draw_overlay(frame.copy(), boxes_info, self.cfg)
        cv2.putText(vis, time.strftime("%Y-%m-%d %H:%M:%S"), (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        tmp = self.live_path.with_suffix(".tmp.jpg")
        tmp.write_bytes(jpeg(vis, 75))
        os.replace(tmp, self.live_path)

    def feed_clip(self, jpg: bytes):
        """把当前抽帧喂给环形缓冲与所有待补录片段。"""
        if not self.cfg.record_clip:
            return
        self.ring.append(jpg)
        with self.clip_lock:
            done = []
            for c in self.pending_clips:
                if len(c["frames"]) < c["want"]:
                    c["frames"].append(jpg)
                if len(c["frames"]) >= c["want"]:
                    done.append(c)
            for c in done:
                self.pending_clips.remove(c)
        for c in done:
            self.write_clip(c)

    def write_clip(self, clip: dict):
        frames = [cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
                  for b in clip["frames"]]
        frames = [f for f in frames if f is not None]
        if not frames:
            return
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(clip["path"], cv2.VideoWriter_fourcc(*"mp4v"),
                                 self.cfg.clip_fps, (w, h))
        for f in frames:
            writer.write(f if f.shape[:2] == (h, w) else cv2.resize(f, (w, h)))
        writer.release()
        LOG.info("告警录像已生成: %s (%d 帧)", clip["path"], len(frames))

    def request_clip(self, path: str):
        """告警发生：以环形缓冲为前置画面，再补录 clip_post_frames 帧。"""
        if not self.cfg.record_clip:
            return ""
        pre = list(self.ring)
        clip = {"path": path, "frames": pre, "want": len(pre) + self.cfg.clip_post_frames}
        with self.clip_lock:
            self.pending_clips.append(clip)
        return path

    def flush_clips(self):
        with self.clip_lock:
            pending, self.pending_clips = self.pending_clips, []
        for c in pending:
            self.write_clip(c)

    def producer(self):
        cfg = self.cfg
        temporal_buf = []
        last_large_ts = 0.0
        next_temporal_ts = 0.0
        for frame_idx, ts, frame in iter_frames(cfg, self.stop):
            if not self._roi_scaled:
                h, w = frame.shape[:2]
                scale_roi_to_frame(cfg, w, h)
                self._roi_scaled = True
            if cfg.max_frames and self.stats["frames"] >= cfg.max_frames:
                break
            with self.stats_lock:
                self.stats["frames"] += 1

            if cfg.mode == "large_only":
                # 只在需要（送审/录像）时才做 JPEG 编码，空转帧零编码开销
                submit_due = (ts - last_large_ts >= cfg.large_interval_seconds
                              or cfg.source_type == "image")
                frame_jpg = jpeg(frame, cfg.jpeg_quality) \
                    if (submit_due or cfg.record_clip) else None
                if cfg.record_clip:
                    self.feed_clip(frame_jpg)
                self.publish_live(frame, [])
                if submit_due:
                    last_large_ts = ts
                    self.submit(VlmTask(frame_idx, ts, [frame_jpg], frame_jpg))
                continue

            if cfg.mode == "temporal":
                interval = cfg.temporal_window_seconds / max(cfg.temporal_frames_count, 1)
                sample_due = cfg.source_type == "image" or ts >= next_temporal_ts
                frame_jpg = jpeg(frame, cfg.jpeg_quality) \
                    if (sample_due or cfg.record_clip) else None
                if cfg.record_clip:
                    self.feed_clip(frame_jpg)
                self.publish_live(frame, [])
                if sample_due:
                    next_temporal_ts = ts + interval
                    temporal_buf.append(frame_jpg)
                if len(temporal_buf) >= cfg.temporal_frames_count:
                    self.submit(VlmTask(frame_idx, ts, list(temporal_buf), temporal_buf[-1]))
                    temporal_buf = []
                continue

            # small_* 模式：YOLO 检测/跟踪
            self.process_small(frame_idx, ts, frame)

        # 收尾：temporal 剩余不足一窗但至少 2 帧时仍送审一次
        if cfg.mode == "temporal" and len(temporal_buf) >= 2:
            self.submit(VlmTask(-1, time.time(), list(temporal_buf), temporal_buf[-1]))
        LOG.info("Producer 结束（帧读取完毕或达到 max_frames）")

    def process_small(self, frame_idx, ts, frame):
        cfg = self.cfg
        kwargs = dict(conf=cfg.yolo_conf, imgsz=cfg.yolo_imgsz,
                      device=cfg.yolo_device, verbose=False)
        if cfg.yolo_classes:
            kwargs["classes"] = cfg.yolo_classes
        if cfg.source_type == "image":
            results = self.yolo.predict(frame, **kwargs)
        else:
            results = self.yolo.track(frame, persist=True, tracker="botsort.yaml", **kwargs)
        res = results[0]
        boxes = res.boxes
        names = res.names

        # 惰性编码：仅在录像或产生检测/事件时才编码全帧
        full_jpg_cache = []

        def full_jpg():
            if not full_jpg_cache:
                full_jpg_cache.append(jpeg(frame, cfg.jpeg_quality))
            return full_jpg_cache[0]

        if cfg.record_clip:
            self.feed_clip(full_jpg())
        boxes_info = []
        self.prune_tracks(ts)

        if boxes is None or len(boxes) == 0:
            self.publish_live(frame, boxes_info)
            return

        for i in range(len(boxes)):
            xyxy = boxes.xyxy[i].tolist()
            if not box_in_roi(cfg, xyxy):
                continue
            with self.stats_lock:
                self.stats["detections"] += 1
            conf = float(boxes.conf[i])
            cls_name = names.get(int(boxes.cls[i]), str(int(boxes.cls[i])))
            tid = int(boxes.id[i]) if boxes.id is not None else -1
            boxes_info.append((cls_name, tid, conf, xyxy))

            st = self.track_state.setdefault(tid, {"first": ts, "last_vlm": 0.0, "alarmed": False})
            st["seen"] = ts
            dwell_ok = (cfg.source_type == "image") or (ts - st["first"] >= cfg.dwell_seconds)

            if cfg.mode == "small_only":
                if dwell_ok and not st["alarmed"]:
                    st["alarmed"] = True
                    self.alarm_q.put(AlarmEvent(
                        frame_idx, ts, True, f"目标驻留:{cls_name}", conf,
                        f"目标 {cls_name}(track {tid}) 驻留超过 {cfg.dwell_seconds}s",
                        "", [full_jpg()], full_jpg(), tid, cls_name, conf, tuple(xyxy)))
                continue

            # small_crop / small_full：按 track 限频送 VLM
            if not dwell_ok or ts - st["last_vlm"] < cfg.vlm_interval_seconds:
                continue
            st["last_vlm"] = ts
            if cfg.mode == "small_crop":
                x1, y1, x2, y2 = (max(0, int(v)) for v in xyxy)
                pad_x, pad_y = int((x2 - x1) * 0.15), int((y2 - y1) * 0.15)
                h, w = frame.shape[:2]
                crop = frame[max(0, y1 - pad_y):min(h, y2 + pad_y),
                             max(0, x1 - pad_x):min(w, x2 + pad_x)]
                img = jpeg(crop, cfg.jpeg_quality) if crop.size else full_jpg()
            else:
                img = full_jpg()
            self.submit(VlmTask(frame_idx, ts, [img], full_jpg(),
                                tid, cls_name, conf, tuple(xyxy)))

        self.publish_live(frame, boxes_info)

    TRACK_TTL = 600.0           # 秒：track 超过该时长未再出现即清理状态

    def prune_tracks(self, ts):
        """长时间运行的 RTSP 任务里 track id 单调递增，状态表需定期清理。"""
        if len(self.track_state) < 512:
            return
        stale = [tid for tid, st in self.track_state.items()
                 if ts - st.get("seen", st["first"]) > self.TRACK_TTL]
        for tid in stale:
            self.track_state.pop(tid, None)
        if stale:
            LOG.debug("清理 %d 个过期 track 状态", len(stale))

    # -- Consumer ----------------------------------------------------------

    def consumer(self):
        while not (self.stop.is_set() and self.task_q.empty()):
            try:
                task = self.task_q.get(timeout=0.5)
            except queue.Empty:
                if self.producer_done.is_set():
                    return
                continue
            if self.stop.is_set():
                # 收到停止信号：丢弃积压任务，尽快退出，不再逐条调 VLM
                with self.stats_lock:
                    self.stats["dropped"] += 1
                continue
            try:
                verdict = self.vlm.infer(task)
                with self.stats_lock:
                    self.stats["vlm_calls"] += 1
            except Exception as exc:  # 网络/超时/HTTP错误：记数后继续
                with self.stats_lock:
                    self.stats["vlm_errors"] += 1
                LOG.error("VLM 调用失败 frame=%s: %s", task.frame_idx, exc)
                continue
            hit = verdict["hit"] and verdict["confidence"] >= self.cfg.min_confidence
            if hit or self.cfg.inspection_mode:
                self.alarm_q.put(AlarmEvent(
                    task.frame_idx, task.ts, hit,
                    verdict["action"], verdict["confidence"], verdict["description"],
                    verdict["raw"], task.images_jpeg, task.full_frame_jpeg,
                    task.track_id, task.det_label, task.det_conf, task.box,
                    verdict["latency"]))

    # -- Saver -------------------------------------------------------------

    def saver(self):
        while not (self.stop.is_set() and self.alarm_q.empty()):
            try:
                ev = self.alarm_q.get(timeout=0.5)
            except queue.Empty:
                if self.consumers_done.is_set():
                    return
                continue
            if ev.is_alarm:
                key = ev.action or "alarm"
                last = self.last_alarm_ts.get(key, 0.0)
                if ev.ts - last < self.cfg.cooldown_seconds:
                    continue
                self.last_alarm_ts[key] = ev.ts
            self.persist(ev)

    def persist(self, ev: AlarmEvent):
        # ev.ts 是流内时间（视频文件为媒体时间），展示与文件名用当前时钟
        now = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
        base = f"{stamp}-f{ev.frame_idx}"
        snap_dir = self.out_dir / "snapshots"
        files = []
        for i, jpg in enumerate(ev.images_jpeg):
            p = snap_dir / (f"{base}.jpg" if len(ev.images_jpeg) == 1 else f"{base}-{i}.jpg")
            p.write_bytes(jpg)
            files.append(str(p))
        if self.cfg.save_annotated and ev.box:
            frame = cv2.imdecode(np.frombuffer(ev.full_frame_jpeg, np.uint8), cv2.IMREAD_COLOR)
            x1, y1, x2, y2 = (int(v) for v in ev.box)
            color = (0, 0, 255) if ev.is_alarm else (0, 165, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
            cv2.putText(frame, f"{ev.det_label} id={ev.track_id}", (x1, max(20, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            p = snap_dir / f"{base}-annotated.jpg"
            cv2.imwrite(str(p), frame)
            files.append(str(p))

        clip_path = ""
        if ev.is_alarm and self.cfg.record_clip:
            clip_path = self.request_clip(str(self.out_dir / "clips" / f"{base}.mp4"))

        record = {
            "task_id": self.cfg.db_task_id or self.cfg.task_id,
            "run_id": self.cfg.run_id,
            "mode": self.cfg.mode,
            "alarm_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "alarm_ts": now,
            "stream_ts": round(ev.ts, 3),
            "frame_idx": ev.frame_idx,
            "is_alarm": ev.is_alarm,
            "action": ev.action,
            "confidence": round(ev.confidence, 4),
            "description": ev.description,
            "det_label": ev.det_label,
            "det_conf": round(ev.det_conf, 4),
            "track_id": ev.track_id,
            "box": [round(v, 1) for v in ev.box] if ev.box else [],
            "vlm_latency": round(ev.vlm_latency, 3),
            "vlm_raw": ev.raw_text[:2000],
            "snapshots": files,
            "clip_path": clip_path,
        }
        with self.alarm_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 平台模式：落库 + 推送
        if self.cfg.db_task_id:
            try:
                record["task_id"] = self.cfg.db_task_id
                aid = db.insert_alarm(record)
                push.push_alarm(aid, self.cfg.db_task_id, self.cfg.task_name, record)
            except Exception as exc:
                LOG.error("告警落库/推送失败: %s", exc)

        with self.stats_lock:
            self.stats["records"] += 1
            if ev.is_alarm:
                self.stats["alarms"] += 1
        LOG.info("%s 已落盘: action=%s conf=%.2f frame=%s",
                 "告警" if ev.is_alarm else "巡检记录",
                 ev.action or "-", ev.confidence, ev.frame_idx)

    # -- 运行状态回写 --------------------------------------------------------

    _final_written = False      # 终态（finished/error）一旦写入，就不再接受 running 心跳

    def heartbeat(self, status="running", error=""):
        if not self.cfg.run_id:
            return
        if status == "running":
            if self._final_written:
                return
        else:
            self._final_written = True
        try:
            with self.stats_lock:
                stats = dict(self.stats)
            stats["queue_depth"] = self.task_q.qsize()      # 积压观测：判断 VLM 是否跟得上
            db.execute("UPDATE task_runs SET status=?,heartbeat_at=?,stats_json=?,last_error=? "
                       "WHERE id=?",
                       (status, db.now_str(), json.dumps(stats, ensure_ascii=False),
                        error, self.cfg.run_id))
        except Exception as exc:
            LOG.warning("心跳写入失败: %s", exc)

    # -- 主流程 -------------------------------------------------------------

    def run(self):
        cfg = self.cfg
        LOG.info("任务 %s 启动: mode=%s source=%s(%s)",
                 cfg.task_id, cfg.mode, cfg.source_type, cfg.source_uri)
        if cfg.needs_yolo:
            self.load_yolo()
        self.producer_done = threading.Event()
        self.consumers_done = threading.Event()
        self.heartbeat("running")

        threads = []
        if cfg.needs_vlm:
            for i in range(cfg.consumer_threads):
                t = threading.Thread(target=self.consumer, name=f"consumer-{i}", daemon=True)
                t.start()
                threads.append(t)
        saver_t = threading.Thread(target=self.saver, name="saver", daemon=True)
        saver_t.start()

        def stats_loop():
            while not self.stop.is_set():
                if self.stop.wait(5):
                    break      # 已收到停止信号：不能再写 running，否则会覆盖终态 finished
                with self.stats_lock:
                    LOG.debug("统计: %s", dict(self.stats))
                self.heartbeat("running")
        threading.Thread(target=stats_loop, name="heartbeat", daemon=True).start()

        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        err = ""
        try:
            self.producer()
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            LOG.exception("Producer 异常终止")
        finally:
            self.producer_done.set()
        for t in threads:
            t.join(timeout=cfg.vlm_timeout + 10)
        self.consumers_done.set()
        saver_t.join(timeout=30)
        self.stop.set()
        self.flush_clips()
        if self.cfg.run_id:
            self.heartbeat("error" if err else "finished", err)
            db.execute("UPDATE task_runs SET stopped_at=? WHERE id=?",
                       (db.now_str(), self.cfg.run_id))
        with self.stats_lock:
            LOG.info("任务结束，最终统计: %s", dict(self.stats))
            print(json.dumps({"ok": not err, "stats": self.stats,
                              "alarm_file": str(self.alarm_file)}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest(cfg: Config):
    report = {"config": "ok"}
    if cfg.needs_yolo:
        try:
            from ultralytics import YOLO
            m = YOLO(cfg.yolo_weights)
            img = np.zeros((320, 320, 3), dtype=np.uint8)
            m.predict(img, device=cfg.yolo_device, verbose=False)
            report["yolo"] = f"ok ({len(m.names)} classes)"
        except Exception as exc:
            report["yolo"] = f"FAIL: {exc}"
    if cfg.needs_vlm:
        for ep in cfg.vlm_endpoints:
            key = f"vlm:{ep['base_url']}"
            try:
                r = requests.get(f"{ep['base_url']}/models",
                                 headers={"Authorization": f"Bearer {ep['api_key']}"}, timeout=10)
                r.raise_for_status()
                report[key] = f"ok, models={[m['id'] for m in r.json().get('data', [])]}"
            except Exception as exc:
                report[key] = f"FAIL: {exc}"
        try:
            img = np.full((64, 64, 3), 128, dtype=np.uint8)
            v = VlmClient(cfg).infer(VlmTask(0, time.time(), [jpeg(img, 85)], b""))
            report["vlm_infer"] = f"ok, latency={v['latency']:.2f}s hit={v['hit']}"
        except Exception as exc:
            report["vlm_infer"] = f"FAIL: {exc}"
    ok = all(not str(x).startswith("FAIL") for x in report.values())
    print(json.dumps({"ok": ok, "checks": report}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


def resolve_config(args) -> Config:
    if getattr(args, "task", None):
        return load_config_from_db(args.task)
    if getattr(args, "config", None):
        return load_config(args.config)
    raise SystemExit("需要 --config <yaml> 或 --task <id>")


def main():
    parser = argparse.ArgumentParser(description="YOLO+VLM 协同视频分析引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("run", "运行分析任务"), ("selftest", "检查 YOLO 权重与 VLM 端点")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", help="YAML 配置路径（独立模式）")
        p.add_argument("--task", type=int, help="平台任务 ID（从数据库读配置）")
        p.add_argument("--run-id", type=int, default=0, help="平台运行记录 ID")
        p.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")

    cfg = resolve_config(args)
    if args.max_frames is not None:
        cfg.max_frames = args.max_frames
    if args.cmd == "selftest":
        selftest(cfg)
    cfg.run_id = args.run_id
    Pipeline(cfg).run()


if __name__ == "__main__":
    main()
