"""AI 视频数据分析系统 —— Web 平台。

页面 + 内部 JSON API + 第三方开放 API + 媒体/实时预览流。
运行：python3 server/app.py    或    scripts/run-web.sh
"""

import csv
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlmp import db, rag                                   # noqa: E402
from server import auth, chat, ops, supervisor             # noqa: E402
from server.openapi import bp as openapi_bp                # noqa: E402

LOG = logging.getLogger("vlmp.web")
OUTPUT_DIR = (ROOT / "output").resolve()
MODES = ["small_only", "small_crop", "small_full", "large_only", "temporal"]
MODE_LABEL = {
    "small_only": "仅小模型（驻留告警）",
    "small_crop": "小模型裁剪 + 大模型判定",
    "small_full": "小模型触发 + 大模型全帧判定",
    "large_only": "仅大模型（定时全帧）",
    "temporal": "时序分析（多帧综合）",
}

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.register_blueprint(openapi_bp)


@app.template_filter("fromjson")
def _fromjson(s):
    try:
        return json.loads(s or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

@app.before_request
def _before():
    auth.load_user()


@app.context_processor
def _ctx():
    return {
        "user": g.get("user"),
        "site_name": db.get_setting("site_name", "AI 视频数据分析系统"),
        "ROLE_LABEL": auth.ROLE_LABEL,
        "ROLE_LEVEL": auth.ROLE_LEVEL,
        "MODE_LABEL": MODE_LABEL,
        "nav_active": request.path,
    }


def body() -> dict:
    return request.get_json(silent=True) or request.form.to_dict()


def as_bool(v, default=False) -> bool:
    if v in ("", None):
        return default
    return v in (True, 1, "1", "true", "on", "yes")


def as_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 1 if as_bool(v, bool(default)) else 0


def ok(**kw):
    return jsonify({"ok": True, **kw})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def audit(action, detail=""):
    user = g.get("user") or {}
    db.audit(user.get("username", "-"), action, detail, request.remote_addr or "")


def safe_media(path_str: str) -> Path:
    """只允许读取 output/ 下的文件，防目录穿越。"""
    p = Path(path_str)
    if not p.is_absolute():
        p = OUTPUT_DIR / p
    p = p.resolve()
    if not p.is_relative_to(OUTPUT_DIR):
        raise PermissionError("路径越权")
    return p


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    d = body()
    row = db.query_one("SELECT * FROM users WHERE username=?", (d.get("username", "").strip(),))
    if not row or not row["enabled"] or not auth.verify_password(row["password_hash"],
                                                                d.get("password", "")):
        db.audit(d.get("username", ""), "login_failed", "", request.remote_addr or "")
        return render_template("login.html", error="用户名或密码错误"), 401
    session["uid"] = row["id"]
    session.permanent = True
    db.execute("UPDATE users SET last_login=? WHERE id=?", (db.now_str(), row["id"]))
    db.audit(row["username"], "login", "", request.remote_addr or "")
    nxt = request.args.get("next") or url_for("dashboard")
    # 只允许站内相对路径，拒绝 //host 形式的协议相对地址（开放重定向）
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        nxt = url_for("dashboard")
    return redirect(nxt)


@app.route("/logout")
def logout():
    if g.get("user"):
        audit("logout")
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/me/password", methods=["POST"])
@auth.login_required
def change_password():
    d = body()
    row = db.query_one("SELECT * FROM users WHERE id=?", (g.user["id"],))
    if not auth.verify_password(row["password_hash"], d.get("old", "")):
        return fail("原密码错误")
    if len(d.get("new", "")) < 6:
        return fail("新密码至少 6 位")
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (auth.hash_password(d["new"]), g.user["id"]))
    audit("change_password")
    return ok()


# ---------------------------------------------------------------------------
# 仪表板
# ---------------------------------------------------------------------------

def dashboard_data() -> dict:
    tasks = db.rows_to_dicts(db.query("SELECT * FROM tasks ORDER BY id"))
    running = 0
    for t in tasks:
        t["runtime"] = supervisor.task_status(t["id"])
        if t["runtime"]["status"] in supervisor.LIVE_STATUSES:
            running += 1
    today0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 00:00:00",
                                       "%Y-%m-%d %H:%M:%S"))
    trend = []
    for i in range(6, -1, -1):
        d0 = today0 - i * 86400
        c = db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=1 "
                         "AND alarm_ts>=? AND alarm_ts<?", (d0, d0 + 86400))["c"]
        trend.append({"date": time.strftime("%m-%d", time.localtime(d0)), "count": c})
    return {
        "counts": {
            "sources": db.query_one("SELECT COUNT(*) c FROM sources")["c"],
            "algorithms": db.query_one("SELECT COUNT(*) c FROM algorithms")["c"],
            "tasks": len(tasks),
            "running": running,
            "alarms_total": db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=1")["c"],
            "alarms_today": db.query_one(
                "SELECT COUNT(*) c FROM alarms WHERE is_alarm=1 AND alarm_ts>=?", (today0,))["c"],
            "records_total": db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=0")["c"],
            "unacked": db.query_one(
                "SELECT COUNT(*) c FROM alarms WHERE is_alarm=1 AND ack_at=''")["c"],
        },
        "tasks": tasks,
        "trend": trend,
        "by_action": db.rows_to_dicts(db.query(
            "SELECT action, COUNT(*) c FROM alarms WHERE is_alarm=1 "
            "GROUP BY action ORDER BY c DESC LIMIT 8")),
        "recent": db.rows_to_dicts(db.query(
            "SELECT a.id,a.alarm_time,a.action,a.confidence,a.description,a.snapshots_json,"
            "t.name task_name FROM alarms a LEFT JOIN tasks t ON t.id=a.task_id "
            "WHERE a.is_alarm=1 ORDER BY a.id DESC LIMIT 8")),
        "gpus": ops.gpu_info(),
        "host": ops.host_info(),
    }


@app.route("/")
@auth.login_required
def dashboard():
    return render_template("dashboard.html", d=dashboard_data())


@app.route("/api/dashboard")
@auth.login_required
def api_dashboard():
    return ok(**dashboard_data())


# ---------------------------------------------------------------------------
# 视频源
# ---------------------------------------------------------------------------

@app.route("/sources")
@auth.login_required
def page_sources():
    return render_template("sources.html",
                           sources=db.rows_to_dicts(db.query("SELECT * FROM sources ORDER BY id")))


@app.route("/api/sources", methods=["GET", "POST"])
@auth.login_required
def api_sources():
    if request.method == "GET":
        return ok(items=db.rows_to_dicts(db.query("SELECT * FROM sources ORDER BY id")))
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    d = body()
    if not d.get("name") or not d.get("uri"):
        return fail("名称与地址必填")
    sid = db.execute(
        "INSERT INTO sources(name,type,uri,location,note,enabled,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (d["name"], d.get("type", "rtsp"), d["uri"], d.get("location", ""),
         d.get("note", ""), int(d.get("enabled", 1)), db.now_str()))
    audit("source_create", f"{sid} {d['name']}")
    return ok(id=sid)


@app.route("/api/sources/<int:sid>", methods=["PUT", "DELETE"])
@auth.role_required("operator")
def api_source_item(sid):
    if request.method == "DELETE":
        if db.query_one("SELECT id FROM tasks WHERE source_id=? LIMIT 1", (sid,)):
            return fail("该视频源仍被任务引用，请先删除相关任务")
        db.execute("DELETE FROM sources WHERE id=?", (sid,))
        audit("source_delete", str(sid))
        return ok()
    d = body()          # 未提交字段沿用原值
    cur = db.query_one("SELECT * FROM sources WHERE id=?", (sid,))
    if not cur:
        return fail("视频源不存在", 404)
    db.execute("UPDATE sources SET name=?,type=?,uri=?,location=?,note=?,enabled=? WHERE id=?",
               (d.get("name", cur["name"]), d.get("type", cur["type"]),
                d.get("uri", cur["uri"]), d.get("location", cur["location"]),
                d.get("note", cur["note"]),
                1 if as_bool(d.get("enabled"), bool(cur["enabled"])) else 0, sid))
    audit("source_update", str(sid))
    return ok()


@app.route("/api/sources/<int:sid>/test", methods=["POST"])
@auth.role_required("operator")
def api_source_test(sid):
    """抓一帧验证可达性，并生成缩略图。"""
    row = db.query_one("SELECT * FROM sources WHERE id=?", (sid,))
    if not row:
        return fail("视频源不存在", 404)
    import cv2
    msg, okflag, thumb = "", False, ""
    try:
        if row["type"] == "image":
            p = Path(row["uri"])
            files = sorted(p.glob("*")) if p.is_dir() else [p]
            files = [f for f in files if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")]
            if not files:
                raise RuntimeError("目录下没有图片")
            frame = cv2.imread(str(files[0]))
            msg = f"图片目录可读，{len(files)} 张"
        else:
            cap = cv2.VideoCapture(row["uri"])
            if not cap.isOpened():
                raise RuntimeError("无法打开视频源")
            got, frame = cap.read()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = round(cap.get(cv2.CAP_PROP_FPS) or 0, 1)
            cap.release()
            if not got:
                raise RuntimeError("打开成功但读不到帧")
            msg = f"连接成功 {w}x{h} @{fps}fps"
        if frame is not None:
            tdir = OUTPUT_DIR / "_thumbs"
            tdir.mkdir(parents=True, exist_ok=True)
            scale = 320 / max(frame.shape[1], 1)
            small = cv2.resize(frame, (320, max(int(frame.shape[0] * scale), 1)))
            tp = tdir / f"source-{sid}.jpg"
            cv2.imwrite(str(tp), small)
            thumb = str(tp)
        okflag = True
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"[:200]
    db.execute("UPDATE sources SET last_check_at=?,last_check_ok=?,last_check_msg=?,"
               "thumb_path=COALESCE(NULLIF(?,''),thumb_path) WHERE id=?",
               (db.now_str(), 1 if okflag else 0, msg, thumb, sid))
    return ok(check_ok=okflag, message=msg, thumb=thumb)


# ---------------------------------------------------------------------------
# 算法（YOLO 权重）
# ---------------------------------------------------------------------------

WEIGHT_DIRS = [str(ROOT / "models" / "yolo"), str(Path.home() / "models" / "yolo")]


@app.route("/algorithms")
@auth.login_required
def page_algorithms():
    return render_template(
        "algorithms.html",
        items=db.rows_to_dicts(db.query("SELECT * FROM algorithms ORDER BY id")),
        weight_dirs=WEIGHT_DIRS)


@app.route("/api/algorithms", methods=["GET", "POST"])
@auth.login_required
def api_algorithms():
    if request.method == "GET":
        return ok(items=db.rows_to_dicts(db.query("SELECT * FROM algorithms ORDER BY id")))
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    d = body()
    if not Path(d.get("weights_path", "")).exists():
        return fail("权重文件不存在")
    aid = db.execute(
        "INSERT INTO algorithms(name,weights_path,framework,classes_json,note,enabled,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (d.get("name"), d["weights_path"], d.get("framework", "ultralytics"),
         d.get("classes_json", "[]"), d.get("note", ""), 1, db.now_str()))
    audit("algorithm_create", f"{aid} {d.get('name')}")
    return ok(id=aid)


@app.route("/api/algorithms/<int:aid>", methods=["PUT", "DELETE"])
@auth.role_required("operator")
def api_algorithm_item(aid):
    if request.method == "DELETE":
        if db.query_one("SELECT id FROM tasks WHERE algorithm_id=? LIMIT 1", (aid,)):
            return fail("该算法仍被任务引用")
        db.execute("DELETE FROM algorithms WHERE id=?", (aid,))
        audit("algorithm_delete", str(aid))
        return ok()
    d = body()
    db.execute("UPDATE algorithms SET name=?,weights_path=?,note=?,enabled=? WHERE id=?",
               (d.get("name"), d.get("weights_path"), d.get("note", ""),
                1 if as_bool(d.get("enabled"), True) else 0, aid))
    return ok()


@app.route("/api/algorithms/scan", methods=["POST"])
@auth.role_required("operator")
def api_algorithm_scan():
    """扫描权重目录，把未登记的 .pt 自动登记为算法。"""
    added = []
    for d in WEIGHT_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        for w in sorted(p.glob("*.pt")):
            if db.query_one("SELECT id FROM algorithms WHERE weights_path=?", (str(w),)):
                continue
            db.execute("INSERT INTO algorithms(name,weights_path,framework,classes_json,"
                       "note,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
                       (w.stem, str(w), "ultralytics", "[]",
                        f"自动扫描 {round(w.stat().st_size / 1048576, 1)}MB", db.now_str()))
            added.append(w.name)
    audit("algorithm_scan", ",".join(added))
    return ok(added=added)


@app.route("/api/algorithms/<int:aid>/classes", methods=["POST"])
@auth.role_required("operator")
def api_algorithm_classes(aid):
    """加载权重读取类别表（首次较慢，之后缓存在 classes_json）。"""
    row = db.query_one("SELECT * FROM algorithms WHERE id=?", (aid,))
    if not row:
        return fail("算法不存在", 404)
    try:
        from ultralytics import YOLO
        names = YOLO(row["weights_path"]).names
        classes = [{"id": int(k), "name": v} for k, v in sorted(names.items())]
    except Exception as exc:
        return fail(f"读取失败: {exc}")
    db.execute("UPDATE algorithms SET classes_json=? WHERE id=?",
               (json.dumps(classes, ensure_ascii=False), aid))
    return ok(classes=classes)


# ---------------------------------------------------------------------------
# 分析任务
# ---------------------------------------------------------------------------

@app.route("/tasks")
@auth.login_required
def page_tasks():
    tasks = db.rows_to_dicts(db.query(
        "SELECT t.*, s.name source_name, s.type source_type, a.name algo_name "
        "FROM tasks t LEFT JOIN sources s ON s.id=t.source_id "
        "LEFT JOIN algorithms a ON a.id=t.algorithm_id ORDER BY t.id"))
    for t in tasks:
        t["runtime"] = supervisor.task_status(t["id"])
        t["config"] = json.loads(t["config_json"] or "{}")
    return render_template("tasks.html", tasks=tasks, modes=MODES)


@app.route("/tasks/new")
@app.route("/tasks/<int:tid>")
@auth.login_required
def page_task_edit(tid=0):
    task = None
    if tid:
        row = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not row:
            return "任务不存在", 404
        task = dict(row)
        task["config"] = json.loads(task["config_json"] or "{}")
        task["runtime"] = supervisor.task_status(tid)
    return render_template(
        "task_edit.html", task=task, modes=MODES,
        c=task["config"] if task else {},     # 配置字典，content/script 两个 block 都要用
        sources=db.rows_to_dicts(db.query("SELECT * FROM sources WHERE enabled=1 ORDER BY id")),
        algorithms=db.rows_to_dicts(db.query(
            "SELECT * FROM algorithms WHERE enabled=1 ORDER BY id")),
        endpoints=db.rows_to_dicts(db.query(
            "SELECT * FROM vlm_endpoints WHERE enabled=1 ORDER BY id")),
        docs=db.rows_to_dicts(db.query("SELECT id,title FROM rule_docs ORDER BY id")))


TASK_CONFIG_KEYS = {
    "conf": float, "imgsz": int, "classes": list, "frame_stride": int,
    "dwell_seconds": float, "vlm_interval_seconds": float, "vlm_timeout": int,
    "max_tokens": int, "temperature": float, "target_actions": list, "prompt": str,
    "min_confidence": float, "cooldown_seconds": float, "large_interval_seconds": float,
    "temporal_window_seconds": float, "temporal_frames_count": int,
    "consumer_threads": int, "max_frames": int, "save_annotated": bool,
    "live_preview": bool, "record_clip": bool, "clip_pre_frames": int,
    "clip_post_frames": int, "roi_include": list, "roi_exclude": list, "device": str,
}


def clean_config(raw: dict) -> dict:
    out = {}
    for k, caster in TASK_CONFIG_KEYS.items():
        if k not in raw or raw[k] in ("", None):
            continue
        v = raw[k]
        try:
            if caster is bool:
                out[k] = v in (True, 1, "1", "true", "on", "yes")
            elif caster is list:
                out[k] = v if isinstance(v, list) else \
                    [x.strip() for x in str(v).split(",") if x.strip()]
                if k == "classes":
                    out[k] = [int(x) for x in out[k]]
            else:
                out[k] = caster(v)
        except (TypeError, ValueError):
            continue
    return out


@app.route("/api/tasks", methods=["GET", "POST"])
@auth.login_required
def api_tasks():
    if request.method == "GET":
        items = db.rows_to_dicts(db.query("SELECT * FROM tasks ORDER BY id"))
        for t in items:
            t["runtime"] = supervisor.task_status(t["id"])
        return ok(items=items)
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    d = body()
    if not d.get("name") or not d.get("source_id"):
        return fail("名称与视频源必填")
    mode = d.get("mode", "small_crop")
    if mode not in MODES:
        return fail("模式无效")
    if mode in ("small_only", "small_crop", "small_full") and not d.get("algorithm_id"):
        return fail(f"{mode} 模式必须选择 YOLO 算法")
    cfg = clean_config(d.get("config") or d)
    tid = db.execute(
        "INSERT INTO tasks(name,source_id,algorithm_id,endpoint_id,mode,config_json,"
        "rule_doc_ids,autostart,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (d["name"], int(d["source_id"]), int(d.get("algorithm_id") or 0) or None,
         int(d.get("endpoint_id") or 0) or None, mode,
         json.dumps(cfg, ensure_ascii=False), d.get("rule_doc_ids", ""),
         1 if d.get("autostart") in (True, 1, "1", "on", "true") else 0,
         1, db.now_str(), db.now_str()))
    audit("task_create", f"{tid} {d['name']}")
    return ok(id=tid)


@app.route("/api/tasks/<int:tid>", methods=["GET", "PUT", "DELETE"])
@auth.login_required
def api_task_item(tid):
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not row:
        return fail("任务不存在", 404)
    if request.method == "GET":
        t = dict(row)
        t["config"] = json.loads(t["config_json"] or "{}")
        t["runtime"] = supervisor.task_status(tid)
        return ok(task=t)
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    if request.method == "DELETE":
        if supervisor.is_running(tid):
            return fail("任务运行中，请先停止")
        db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        audit("task_delete", str(tid))
        return ok()
    d = body()
    if supervisor.is_running(tid):
        return fail("任务运行中，请先停止再修改")
    cfg = clean_config(d.get("config") or d)
    db.execute("UPDATE tasks SET name=?,source_id=?,algorithm_id=?,endpoint_id=?,mode=?,"
               "config_json=?,rule_doc_ids=?,autostart=?,enabled=?,updated_at=? WHERE id=?",
               (d.get("name", row["name"]), int(d.get("source_id") or row["source_id"]),
                int(d.get("algorithm_id") or 0) or None,
                int(d.get("endpoint_id") or 0) or None,
                d.get("mode", row["mode"]), json.dumps(cfg, ensure_ascii=False),
                d.get("rule_doc_ids", ""),
                1 if d.get("autostart") in (True, 1, "1", "on", "true") else 0,
                1 if d.get("enabled", 1) in (True, 1, "1", "on", "true") else 0,
                db.now_str(), tid))
    audit("task_update", str(tid))
    return ok()


@app.route("/api/tasks/<int:tid>/<action>", methods=["POST"])
@auth.role_required("operator")
def api_task_action(tid, action):
    if action == "start":
        r = supervisor.start_task(tid)
    elif action == "stop":
        r = supervisor.stop_task(tid)
    elif action == "restart":
        supervisor.stop_task(tid)
        r = supervisor.start_task(tid)
    else:
        return fail("未知操作")
    audit(f"task_{action}", str(tid))
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/tasks/<int:tid>/status")
@auth.login_required
def api_task_status(tid):
    return ok(**supervisor.task_status(tid))


@app.route("/api/tasks/<int:tid>/log")
@auth.login_required
def api_task_log(tid):
    return ok(log=supervisor.tail_log(tid, int(request.args.get("lines", 200))))


# ---------------------------------------------------------------------------
# 告警记录
# ---------------------------------------------------------------------------

def alarm_query(args, limit, offset):
    where, params = ["1=1"], []
    if args.get("task_id"):
        where.append("a.task_id=?")
        params.append(int(args["task_id"]))
    if args.get("action"):
        where.append("a.action LIKE ?")
        params.append(f"%{args['action']}%")
    # 默认列全部：巡检模式产出的记录 is_alarm=0，默认只看告警会让页面显得空白
    kind = args.get("kind", "all")
    if kind == "alarm":
        where.append("a.is_alarm=1")
    elif kind == "record":
        where.append("a.is_alarm=0")
    if args.get("acked") == "0":
        where.append("a.ack_at=''")
    elif args.get("acked") == "1":
        where.append("a.ack_at!=''")
    if args.get("start"):
        where.append("a.alarm_time>=?")
        params.append(args["start"])
    if args.get("end"):
        where.append("a.alarm_time<=?")
        params.append(args["end"] + " 23:59:59" if len(args["end"]) == 10 else args["end"])
    if args.get("min_conf"):
        where.append("a.confidence>=?")
        params.append(float(args["min_conf"]))
    w = " AND ".join(where)
    total = db.query_one(f"SELECT COUNT(*) c FROM alarms a WHERE {w}", tuple(params))["c"]
    rows = db.query(
        f"SELECT a.*, t.name task_name FROM alarms a LEFT JOIN tasks t ON t.id=a.task_id "
        f"WHERE {w} ORDER BY a.id DESC LIMIT ? OFFSET ?", tuple(params) + (limit, offset))
    items = db.rows_to_dicts(rows)
    for it in items:
        try:
            it["snapshots"] = json.loads(it["snapshots_json"] or "[]")
            it["box"] = json.loads(it["box_json"] or "[]")
        except json.JSONDecodeError:
            it["snapshots"], it["box"] = [], []
    return total, items


@app.route("/alarms")
@auth.login_required
def page_alarms():
    page = max(int(request.args.get("page", 1)), 1)
    size = int(db.get_setting("alarm_page_size", "20"))
    total, items = alarm_query(request.args, size, (page - 1) * size)
    qs_base = "".join(f"{k}={quote(v)}&" for k, v in request.args.items() if k != "page")
    return render_template("alarms.html", items=items, total=total, page=page, size=size,
                           pages=max((total + size - 1) // size, 1), qs_base=qs_base,
                           tasks=db.rows_to_dicts(db.query("SELECT id,name FROM tasks ORDER BY id")),
                           q=request.args)


@app.route("/alarms/<int:aid>")
@auth.login_required
def page_alarm_detail(aid):
    row = db.query_one("SELECT a.*, t.name task_name FROM alarms a "
                       "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (aid,))
    if not row:
        return "记录不存在", 404
    item = dict(row)
    item["snapshots"] = json.loads(item["snapshots_json"] or "[]")
    item["box"] = json.loads(item["box_json"] or "[]")
    pushes = db.rows_to_dicts(db.query(
        "SELECT p.*, g.name target_name FROM push_logs p "
        "LEFT JOIN push_targets g ON g.id=p.target_id WHERE p.alarm_id=? ORDER BY p.id", (aid,)))
    return render_template("alarm_detail.html", a=item, pushes=pushes)


@app.route("/api/alarms")
@auth.login_required
def api_alarms():
    limit = min(int(request.args.get("limit", 20)), 200)
    offset = int(request.args.get("offset", 0))
    total, items = alarm_query(request.args, limit, offset)
    return ok(total=total, items=items)


@app.route("/api/alarms/<int:aid>/ack", methods=["POST"])
@auth.role_required("operator")
def api_alarm_ack(aid):
    db.execute("UPDATE alarms SET ack_by=?,ack_at=? WHERE id=?",
               (g.user["username"], db.now_str(), aid))
    audit("alarm_ack", str(aid))
    return ok()


@app.route("/api/alarms/<int:aid>", methods=["DELETE"])
@auth.role_required("operator")
def api_alarm_delete(aid):
    row = db.query_one("SELECT snapshots_json,clip_path FROM alarms WHERE id=?", (aid,))
    if row:
        files = json.loads(row["snapshots_json"] or "[]")
        if row["clip_path"]:
            files.append(row["clip_path"])
        for f in files:
            try:
                safe_media(f).unlink(missing_ok=True)
            except (PermissionError, OSError):
                pass
    db.execute("DELETE FROM alarms WHERE id=?", (aid,))
    audit("alarm_delete", str(aid))
    return ok()


@app.route("/api/alarms/batch", methods=["POST"])
@auth.role_required("operator")
def api_alarms_batch():
    """批量确认/删除。body: {"op": "ack"|"delete", "ids": [1,2,...]}"""
    d = body()
    op = d.get("op")
    ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()][:500]
    if op not in ("ack", "delete") or not ids:
        return fail("op 取 ack/delete，ids 不能为空")
    marks = ",".join("?" * len(ids))
    if op == "ack":
        n = len(db.query(f"SELECT id FROM alarms WHERE id IN ({marks}) AND ack_at=''",
                         tuple(ids)))
        db.execute(f"UPDATE alarms SET ack_by=?,ack_at=? WHERE id IN ({marks}) AND ack_at=''",
                   (g.user["username"], db.now_str(), *ids))
    else:
        rows = db.query(f"SELECT snapshots_json,clip_path FROM alarms WHERE id IN ({marks})",
                        tuple(ids))
        for r in rows:
            for f in json.loads(r["snapshots_json"] or "[]"):
                try:
                    safe_media(f).unlink(missing_ok=True)
                except (PermissionError, OSError):
                    pass
            if r["clip_path"]:
                try:
                    safe_media(r["clip_path"]).unlink(missing_ok=True)
                except (PermissionError, OSError):
                    pass
        n = len(rows)
        db.execute(f"DELETE FROM alarms WHERE id IN ({marks})", tuple(ids))
    audit(f"alarm_batch_{op}", f"{len(ids)} ids")
    return ok(affected=n)


@app.route("/api/alarms/export")
@auth.login_required
def api_alarms_export():
    _, items = alarm_query(request.args, 5000, 0)

    def cell(v):
        """描述可能来自 VLM 输出：以 =+-@ 开头会被 Excel 当公式执行，前缀单引号。"""
        s = str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "任务", "时间", "类型", "行为", "置信度", "描述",
                "检测类别", "跟踪ID", "确认人", "确认时间"])
    for it in items:
        w.writerow([it["id"], cell(it.get("task_name", "")), it["alarm_time"],
                    "告警" if it["is_alarm"] else "巡检", cell(it["action"]),
                    round(it["confidence"], 3), cell(it["description"]),
                    cell(it["det_label"]), it["track_id"], cell(it["ack_by"]), it["ack_at"]])
    data = "﻿" + buf.getvalue()          # BOM 以便 Excel 正确识别中文
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=alarms-{time.strftime('%Y%m%d%H%M')}.csv"})


# ---------------------------------------------------------------------------
# 监控墙 / 实时预览 / 媒体
# ---------------------------------------------------------------------------

@app.route("/wall")
@auth.login_required
def page_wall():
    tasks = db.rows_to_dicts(db.query(
        "SELECT t.*, s.name source_name FROM tasks t LEFT JOIN sources s ON s.id=t.source_id "
        "WHERE t.enabled=1 ORDER BY t.id"))
    for t in tasks:
        t["runtime"] = supervisor.task_status(t["id"])
    return render_template("wall.html", tasks=tasks,
                           cols=int(db.get_setting("wall_columns", "2")))


def live_frame_path(tid: int) -> Path:
    return OUTPUT_DIR / f"task-{tid}" / "live.jpg"


@app.route("/live/<int:tid>.jpg")
@auth.login_required
def live_jpg(tid):
    p = live_frame_path(tid)
    if not p.exists():
        return send_file(str(Path(app.static_folder) / "no-signal.jpg"), mimetype="image/jpeg")
    return send_file(str(p), mimetype="image/jpeg", max_age=0)


@app.route("/live/<int:tid>.mjpg")
@auth.login_required
def live_mjpg(tid):
    """把引擎写出的 live.jpg 转成 MJPEG 流——替代参考文档里的 MediaMTX 推流。"""
    path = live_frame_path(tid)
    placeholder = Path(app.static_folder) / "no-signal.jpg"

    def gen():
        last_mtime, idle = 0.0, 0
        while idle < 600:                       # 无更新 ~60s 后结束，避免连接泄漏
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            if mtime and mtime != last_mtime:
                last_mtime, idle = mtime, 0
                try:
                    data = path.read_bytes()
                except OSError:
                    data = placeholder.read_bytes() if placeholder.exists() else b""
                if data:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                           + data + b"\r\n")
            else:
                idle += 1
            time.sleep(0.1)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/media")
@auth.login_required
def media():
    try:
        p = safe_media(request.args.get("path", ""))
    except PermissionError:
        return "越权访问", 403
    if not p.exists():
        return "文件不存在", 404
    mt = "video/mp4" if p.suffix == ".mp4" else "image/jpeg"
    return send_file(str(p), mimetype=mt, max_age=3600)


# ---------------------------------------------------------------------------
# VLM 端点
# ---------------------------------------------------------------------------

@app.route("/endpoints")
@auth.role_required("operator")
def page_endpoints():
    return render_template("endpoints.html", items=db.rows_to_dicts(
        db.query("SELECT * FROM vlm_endpoints ORDER BY id")))


@app.route("/api/endpoints", methods=["GET", "POST"])
@auth.login_required
def api_endpoints():
    if request.method == "GET":
        items = db.rows_to_dicts(db.query("SELECT * FROM vlm_endpoints ORDER BY id"))
        for it in items:
            it["api_key"] = "***" if it.get("api_key") else ""      # 不回显密钥
        return ok(items=items)
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    d = body()
    eid = db.execute(
        "INSERT INTO vlm_endpoints(name,base_url,api_key,model,weight,enabled,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (d.get("name"), d.get("base_url", "").rstrip("/"), d.get("api_key", "EMPTY"),
         d.get("model"), int(d.get("weight", 1)), int(d.get("enabled", 1)), db.now_str()))
    audit("endpoint_create", str(eid))
    return ok(id=eid)


@app.route("/api/endpoints/<int:eid>", methods=["PUT", "DELETE"])
@auth.role_required("operator")
def api_endpoint_item(eid):
    if request.method == "DELETE":
        db.execute("DELETE FROM vlm_endpoints WHERE id=?", (eid,))
        return ok()
    d = body()
    db.execute("UPDATE vlm_endpoints SET name=?,base_url=?,model=?,weight=?,enabled=? WHERE id=?",
               (d.get("name"), d.get("base_url", "").rstrip("/"), d.get("model"),
                as_int(d.get("weight"), 1) or 1,
                1 if as_bool(d.get("enabled"), True) else 0, eid))
    if d.get("api_key"):          # 留空表示保留原密钥，页面永不回显
        db.execute("UPDATE vlm_endpoints SET api_key=? WHERE id=?", (d["api_key"], eid))
    return ok()


@app.route("/api/endpoints/check", methods=["POST"])
@auth.login_required
def api_endpoints_check():
    return ok(items=[{"id": e["id"], "name": e["name"], "health": e["health"]}
                     for e in ops.check_all_endpoints()])


# ---------------------------------------------------------------------------
# 规范文档 / RAG
# ---------------------------------------------------------------------------

@app.route("/docs")
@auth.login_required
def page_docs():
    return render_template("docs.html", items=db.rows_to_dicts(db.query(
        "SELECT id,title,filename,tags,chunk_count,created_at,length(content) size "
        "FROM rule_docs ORDER BY id")))


@app.route("/api/docs", methods=["GET", "POST"])
@auth.login_required
def api_docs():
    if request.method == "GET":
        return ok(items=db.rows_to_dicts(db.query(
            "SELECT id,title,filename,tags,chunk_count,created_at FROM rule_docs ORDER BY id")))
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    f = request.files.get("file")
    if f:
        content = f.read().decode("utf-8", "replace")
        title = request.form.get("title") or Path(f.filename).stem
        filename = f.filename
        tags = request.form.get("tags", "")
    else:
        d = body()
        content, title = d.get("content", ""), d.get("title", "")
        filename, tags = d.get("filename", ""), d.get("tags", "")
    if not content.strip() or not title:
        return fail("标题与内容必填")
    did = db.execute("INSERT INTO rule_docs(title,filename,tags,content,created_at) "
                     "VALUES(?,?,?,?,?)", (title, filename, tags, content, db.now_str()))
    n = rag.index_doc(db, did, content)
    audit("doc_create", f"{did} {title} chunks={n}")
    return ok(id=did, chunks=n)


@app.route("/api/docs/<int:did>", methods=["GET", "PUT", "DELETE"])
@auth.login_required
def api_doc_item(did):
    row = db.query_one("SELECT * FROM rule_docs WHERE id=?", (did,))
    if not row:
        return fail("文档不存在", 404)
    if request.method == "GET":
        return ok(doc=dict(row))
    if auth.ROLE_LEVEL[g.user["role"]] < 1:
        return fail("权限不足", 403)
    if request.method == "DELETE":
        db.execute("DELETE FROM rule_docs WHERE id=?", (did,))
        audit("doc_delete", str(did))
        return ok()
    d = body()
    content = d.get("content", row["content"])
    db.execute("UPDATE rule_docs SET title=?,tags=?,content=? WHERE id=?",
               (d.get("title", row["title"]), d.get("tags", row["tags"]), content, did))
    n = rag.index_doc(db, did, content)
    return ok(chunks=n)


@app.route("/api/docs/search")
@auth.login_required
def api_doc_search():
    q = request.args.get("q", "")
    ids = [int(x) for x in request.args.get("doc_ids", "").split(",") if x.strip().isdigit()]
    return ok(results=rag.search(db, q, int(request.args.get("top_k", 5)), ids or None))


# ---------------------------------------------------------------------------
# 告警推送
# ---------------------------------------------------------------------------

@app.route("/push")
@auth.role_required("operator")
def page_push():
    return render_template("push.html",
                           items=db.rows_to_dicts(db.query("SELECT * FROM push_targets ORDER BY id")),
                           logs=db.rows_to_dicts(db.query(
                               "SELECT p.*, g.name target_name FROM push_logs p "
                               "LEFT JOIN push_targets g ON g.id=p.target_id "
                               "ORDER BY p.id DESC LIMIT 50")),
                           tasks=db.rows_to_dicts(db.query("SELECT id,name FROM tasks ORDER BY id")))


@app.route("/api/push-targets", methods=["GET", "POST"])
@auth.role_required("operator")
def api_push_targets():
    if request.method == "GET":
        return ok(items=db.rows_to_dicts(db.query("SELECT * FROM push_targets ORDER BY id")))
    d = body()
    pid = db.execute(
        "INSERT INTO push_targets(name,url,method,headers_json,body_template,only_alarm,"
        "task_filter,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (d.get("name"), d.get("url"), d.get("method", "POST"),
         d.get("headers_json", "{}"), d.get("body_template", ""),
         int(d.get("only_alarm", 1)), d.get("task_filter", ""),
         int(d.get("enabled", 1)), db.now_str()))
    audit("push_target_create", str(pid))
    return ok(id=pid)


@app.route("/api/push-targets/<int:pid>", methods=["PUT", "DELETE"])
@auth.role_required("operator")
def api_push_target_item(pid):
    if request.method == "DELETE":
        db.execute("DELETE FROM push_targets WHERE id=?", (pid,))
        return ok()
    cur = db.query_one("SELECT * FROM push_targets WHERE id=?", (pid,))
    if not cur:
        return fail("推送目标不存在", 404)
    d = body()          # 只更新提交了的字段，未提交的沿用原值（列表页不回传模板）
    db.execute("UPDATE push_targets SET name=?,url=?,method=?,headers_json=?,body_template=?,"
               "only_alarm=?,task_filter=?,enabled=? WHERE id=?",
               (d.get("name", cur["name"]), d.get("url", cur["url"]),
                d.get("method", cur["method"]),
                d.get("headers_json", cur["headers_json"]),
                d.get("body_template", cur["body_template"]),
                int(d.get("only_alarm", cur["only_alarm"])),
                d.get("task_filter", cur["task_filter"]),
                int(d.get("enabled", cur["enabled"])), pid))
    return ok()


@app.route("/api/push-targets/<int:pid>/test", methods=["POST"])
@auth.role_required("operator")
def api_push_test(pid):
    from vlmp import push as pushmod
    row = db.query_one("SELECT * FROM push_targets WHERE id=?", (pid,))
    if not row:
        return fail("推送目标不存在", 404)
    ctx = {"alarm_id": 0, "task_id": 0, "task_name": "测试任务", "is_alarm": True,
           "action": "测试告警", "confidence": 0.99, "description": "这是一条推送连通性测试",
           "alarm_time": db.now_str(), "det_label": "person", "snapshot": ""}
    pushmod.deliver(dict(row), ctx, 0)
    last = db.query_one("SELECT * FROM push_logs WHERE target_id=? ORDER BY id DESC LIMIT 1",
                        (pid,))
    return ok(result=dict(last) if last else {})


# ---------------------------------------------------------------------------
# 智能对话
# ---------------------------------------------------------------------------

@app.route("/chat")
@auth.login_required
def page_chat():
    return render_template("chat.html", sessions=db.rows_to_dicts(db.query(
        "SELECT * FROM chat_sessions ORDER BY id DESC LIMIT 30")))


@app.route("/api/chat/ask", methods=["POST"])
@auth.login_required
def api_chat_ask():
    d = body()
    q = (d.get("question") or "").strip()
    if not q:
        return fail("问题不能为空")
    r = chat.ask(q, int(d.get("session_id") or 0), g.user["username"])
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/chat/sessions/<int:sid>")
@auth.login_required
def api_chat_session(sid):
    return ok(messages=db.rows_to_dicts(db.query(
        "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id", (sid,))))


@app.route("/api/chat/sessions/<int:sid>", methods=["DELETE"])
@auth.login_required
def api_chat_session_delete(sid):
    db.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
    return ok()


# ---------------------------------------------------------------------------
# 用户与权限
# ---------------------------------------------------------------------------

@app.route("/users")
@auth.role_required("admin")
def page_users():
    return render_template("users.html",
                           users=db.rows_to_dicts(db.query(
                               "SELECT id,username,role,display_name,enabled,created_at,last_login"
                               " FROM users ORDER BY id")),
                           tokens=db.rows_to_dicts(db.query("SELECT * FROM api_tokens ORDER BY id")),
                           roles=auth.ROLES)


@app.route("/api/users", methods=["POST"])
@auth.role_required("admin")
def api_user_create():
    d = body()
    if not d.get("username") or len(d.get("password", "")) < 6:
        return fail("用户名必填，密码至少 6 位")
    if d.get("role") not in auth.ROLES:
        return fail("角色无效")
    if db.query_one("SELECT id FROM users WHERE username=?", (d["username"],)):
        return fail("用户名已存在")
    uid = db.execute("INSERT INTO users(username,password_hash,role,display_name,enabled,"
                     "created_at) VALUES(?,?,?,?,1,?)",
                     (d["username"], auth.hash_password(d["password"]), d["role"],
                      d.get("display_name", ""), db.now_str()))
    audit("user_create", f"{uid} {d['username']}")
    return ok(id=uid)


@app.route("/api/users/<int:uid>", methods=["PUT", "DELETE"])
@auth.role_required("admin")
def api_user_item(uid):
    if request.method == "DELETE":
        if uid == g.user["id"]:
            return fail("不能删除当前登录账号")
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        audit("user_delete", str(uid))
        return ok()
    cur = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not cur:
        return fail("用户不存在", 404)
    d = body()          # 未提交的字段沿用原值，避免部分更新清空资料
    if d.get("password"):
        if len(d["password"]) < 6:
            return fail("密码至少 6 位")
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (auth.hash_password(d["password"]), uid))
    if d.get("role") in auth.ROLES:
        db.execute("UPDATE users SET role=? WHERE id=?", (d["role"], uid))
    db.execute("UPDATE users SET display_name=?,enabled=? WHERE id=?",
               (d.get("display_name", cur["display_name"]),
                1 if as_bool(d.get("enabled"), bool(cur["enabled"])) else 0, uid))
    audit("user_update", str(uid))
    return ok()


@app.route("/api/tokens", methods=["POST"])
@auth.role_required("admin")
def api_token_create():
    d = body()
    tok = auth.new_token()
    tid = db.execute("INSERT INTO api_tokens(name,token,scopes,enabled,created_at) "
                     "VALUES(?,?,?,1,?)",
                     (d.get("name", "token"), tok, d.get("scopes", "read"), db.now_str()))
    audit("token_create", str(tid))
    return ok(id=tid, token=tok)


@app.route("/api/tokens/<int:tid>", methods=["DELETE"])
@auth.role_required("admin")
def api_token_delete(tid):
    db.execute("DELETE FROM api_tokens WHERE id=?", (tid,))
    audit("token_delete", str(tid))
    return ok()


# ---------------------------------------------------------------------------
# 系统配置 / 运维
# ---------------------------------------------------------------------------

@app.route("/settings")
@auth.role_required("admin")
def page_settings():
    rows = db.query("SELECT * FROM settings ORDER BY key")
    return render_template("settings.html", settings=db.rows_to_dicts(rows))


@app.route("/api/settings", methods=["POST"])
@auth.role_required("admin")
def api_settings_save():
    for k, v in body().items():
        if k == "secret_key":          # 会话签名密钥不允许经接口改写
            continue
        db.set_setting(k, v)
    audit("settings_update")
    return ok()


@app.route("/ops")
@auth.role_required("operator")
def page_ops():
    return render_template("ops.html",
                           host=ops.host_info(), gpus=ops.gpu_info(),
                           storage=ops.storage_usage(),
                           endpoints=db.rows_to_dicts(db.query(
                               "SELECT * FROM vlm_endpoints ORDER BY id")),
                           audits=db.rows_to_dicts(db.query(
                               "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50")))


@app.route("/api/ops/stats")
@auth.login_required
def api_ops_stats():
    return ok(host=ops.host_info(), gpus=ops.gpu_info(), storage=ops.storage_usage())


@app.route("/api/ops/cleanup", methods=["POST"])
@auth.role_required("admin")
def api_ops_cleanup():
    r = ops.cleanup_expired(dry_run=bool(body().get("dry_run")))
    audit("ops_cleanup", json.dumps(r, ensure_ascii=False))
    return ok(**r)


@app.route("/api/ops/log")
@auth.role_required("operator")
def api_ops_log():
    name = request.args.get("name", "vlm-server.log")
    logs_dir = (ROOT / "logs").resolve()
    p = (logs_dir / name).resolve()
    # resolve 后必须仍在 logs/ 内：拦截 ../ 穿越与 /etc/passwd 这类绝对路径
    if not re.fullmatch(r"[\w.\-/]+", name) or not p.is_relative_to(logs_dir):
        return fail("非法日志名")
    return ok(log=ops.tail_file(p, int(request.args.get("lines", 200))))


@app.errorhandler(404)
def _404(_):
    if request.path.startswith("/api/"):
        return fail("接口不存在", 404)
    return render_template("404.html"), 404


@app.route("/healthz")
def healthz():
    """免登录健康检查（systemd/监控探活用），不暴露业务数据。"""
    try:
        db.query_one("SELECT 1")
        running = sum(1 for t in db.query("SELECT id FROM tasks WHERE enabled=1")
                      if supervisor.is_running(t["id"]))
        return jsonify({"ok": True, "db": "ok", "tasks_running": running,
                        "time": db.now_str()})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}), 500


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def init_app():
    """建库、密钥、初始管理员、默认端点——不启动后台线程或子进程。

    `run-web.sh` 的初始化步骤只调用它，避免短生命周期进程拉起看门狗。
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    db.init_db()
    key = db.get_setting("secret_key", "")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting("secret_key", key)
    app.secret_key = key
    app.permanent_session_lifetime = 86400 * 7
    # 会话 cookie 加固：禁 JS 读取、跨站请求不携带（缓解 CSRF）
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    pw = auth.ensure_default_admin()
    if pw:
        LOG.warning("=" * 60)
        LOG.warning("已创建初始管理员账号  admin  密码: %s", pw)
        LOG.warning("请登录后立即在「用户与权限」中修改密码")
        LOG.warning("=" * 60)
        (ROOT / "logs").mkdir(exist_ok=True)
        (ROOT / "logs" / "initial-admin-password.txt").write_text(
            f"admin / {pw}\n生成时间: {db.now_str()}\n请修改密码后删除本文件\n", encoding="utf-8")

    # 默认登记本机 vLLM 端点，省去首次手工配置
    if not db.query_one("SELECT id FROM vlm_endpoints LIMIT 1"):
        db.execute("INSERT INTO vlm_endpoints(name,base_url,api_key,model,weight,enabled,"
                   "created_at) VALUES(?,?,?,?,1,1,?)",
                   ("本机 vLLM", "http://127.0.0.1:8001/v1", "EMPTY",
                    "Qwen2.5-VL-7B-Instruct-AWQ", db.now_str()))
    return app


def bootstrap():
    """Web 进程入口：初始化 + 恢复自启动任务 + 看门狗 + 自动清理。"""
    init_app()
    supervisor.autostart_all()
    supervisor.watchdog()
    ops.start_cleanup_scheduler()
    return app


if __name__ == "__main__":
    bootstrap()
    port = int(os.environ.get("VLMP_PORT", "8090"))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
