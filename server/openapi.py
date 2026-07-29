"""第三方开放 API（Token 鉴权，与浏览器会话完全隔离）。

前缀 /openapi/v1，对应参考文档「第三方开放API」章节。
Token 在「用户与权限」页面创建，scopes 取 read / write（逗号分隔）。
调用方式：
    curl -H "Authorization: Bearer vlmp_xxx" http://host:8090/openapi/v1/alarms?limit=10
"""

import json
import time

from flask import Blueprint, jsonify, request, send_file

from server import auth, ops, supervisor
from vlmp import db

bp = Blueprint("openapi", __name__, url_prefix="/openapi/v1")


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _fail(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@bp.route("/ping")
@auth.token_required("read")
def ping():
    return _ok(server_time=db.now_str(), version="1.0.0")


@bp.route("/sources")
@auth.token_required("read")
def sources():
    return _ok(items=db.rows_to_dicts(db.query(
        "SELECT id,name,type,location,enabled,last_check_ok,last_check_at FROM sources "
        "ORDER BY id")))                       # 不返回 uri，避免泄露带凭据的 RTSP 地址


@bp.route("/tasks")
@auth.token_required("read")
def tasks():
    items = db.rows_to_dicts(db.query(
        "SELECT t.id,t.name,t.mode,t.enabled,t.autostart,s.name source_name "
        "FROM tasks t LEFT JOIN sources s ON s.id=t.source_id ORDER BY t.id"))
    for t in items:
        rt = supervisor.task_status(t["id"])
        t["status"] = rt["status"]
        t["stats"] = rt["stats"]
    return _ok(items=items)


@bp.route("/tasks/<int:tid>/<action>", methods=["POST"])
@auth.token_required("write")
def task_action(tid, action):
    if action not in ("start", "stop", "restart"):
        return _fail("action 只支持 start/stop/restart")
    if action == "restart":
        supervisor.stop_task(tid)
        r = supervisor.start_task(tid)
    else:
        r = getattr(supervisor, f"{action}_task")(tid)
    db.audit(f"token:{request.headers.get('X-Client', 'api')}", f"openapi_task_{action}",
             str(tid), request.remote_addr or "")
    return jsonify(r), (200 if r.get("ok") else 400)


@bp.route("/alarms")
@auth.token_required("read")
def alarms():
    limit = min(int(request.args.get("limit", 20)), 200)
    where, params = ["1=1"], []
    if request.args.get("task_id"):
        where.append("a.task_id=?")
        params.append(int(request.args["task_id"]))
    if request.args.get("since_id"):
        where.append("a.id>?")
        params.append(int(request.args["since_id"]))
    if request.args.get("since"):                 # 秒级时间戳
        where.append("a.alarm_ts>=?")
        params.append(float(request.args["since"]))
    # 这是告警订阅接口，默认只给真告警；要连巡检记录一起取，传 kind=all 或 kind=record
    kind = request.args.get("kind", "alarm")
    if kind == "alarm":
        where.append("a.is_alarm=1")
    elif kind == "record":
        where.append("a.is_alarm=0")
    w = " AND ".join(where)
    rows = db.query(
        f"SELECT a.id,a.task_id,a.is_alarm,a.action,a.confidence,a.description,a.alarm_time,"
        f"a.alarm_ts,a.det_label,a.track_id,a.snapshots_json,a.clip_path,a.ack_by,a.ack_at,"
        f"t.name task_name FROM alarms a LEFT JOIN tasks t ON t.id=a.task_id "
        f"WHERE {w} ORDER BY a.id DESC LIMIT ?", tuple(params) + (limit,))
    items = db.rows_to_dicts(rows)
    base = request.host_url.rstrip("/")
    for it in items:
        try:
            snaps = json.loads(it.pop("snapshots_json") or "[]")
        except json.JSONDecodeError:
            snaps = []
        tok = request.args.get("token", "")
        suffix = f"&token={tok}" if tok else ""
        it["snapshot_urls"] = [
            f"{base}/openapi/v1/media?alarm_id={it['id']}&index={i}{suffix}"
            for i in range(len(snaps))]
    return _ok(total=len(items), items=items)


@bp.route("/alarms/<int:aid>")
@auth.token_required("read")
def alarm_detail(aid):
    row = db.query_one("SELECT a.*, t.name task_name FROM alarms a "
                       "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (aid,))
    if not row:
        return _fail("记录不存在", 404)
    item = dict(row)
    item["snapshots"] = json.loads(item.pop("snapshots_json") or "[]")
    item["box"] = json.loads(item.pop("box_json") or "[]")
    return _ok(alarm=item)


@bp.route("/alarms/<int:aid>/ack", methods=["POST"])
@auth.token_required("write")
def alarm_ack(aid):
    db.execute("UPDATE alarms SET ack_by=?,ack_at=? WHERE id=?",
               ("openapi", db.now_str(), aid))
    return _ok()


@bp.route("/media")
@auth.token_required("read")
def media():
    """按告警 ID 取截图/录像，避免暴露服务器绝对路径。"""
    aid = int(request.args.get("alarm_id", 0))
    row = db.query_one("SELECT snapshots_json,clip_path FROM alarms WHERE id=?", (aid,))
    if not row:
        return _fail("记录不存在", 404)
    if request.args.get("clip"):
        path = row["clip_path"]
        mime = "video/mp4"
    else:
        snaps = json.loads(row["snapshots_json"] or "[]")
        idx = int(request.args.get("index", 0))
        path = snaps[idx] if 0 <= idx < len(snaps) else ""
        mime = "image/jpeg"
    from pathlib import Path
    if not path or not Path(path).exists():
        return _fail("文件不存在", 404)
    return send_file(path, mimetype=mime)


@bp.route("/stats")
@auth.token_required("read")
def stats():
    today0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 00:00:00",
                                       "%Y-%m-%d %H:%M:%S"))
    return _ok(
        tasks_total=db.query_one("SELECT COUNT(*) c FROM tasks")["c"],
        tasks_running=sum(1 for t in db.query("SELECT id FROM tasks")
                          if supervisor.is_running(t["id"])),
        alarms_total=db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=1")["c"],
        alarms_today=db.query_one(
            "SELECT COUNT(*) c FROM alarms WHERE is_alarm=1 AND alarm_ts>=?", (today0,))["c"],
        by_action=db.rows_to_dicts(db.query(
            "SELECT action, COUNT(*) c FROM alarms WHERE is_alarm=1 GROUP BY action "
            "ORDER BY c DESC LIMIT 10")),
        host=ops.host_info(), gpus=ops.gpu_info())
