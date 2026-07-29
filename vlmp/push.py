"""告警推送：向配置的 HTTP 目标投递告警，带重试与投递日志。

对应参考文档「报警推送」章节。企业微信/钉钉/飞书机器人以及自建接口都是
HTTP POST JSON，这里用可编辑的 body 模板统一支持。
"""

import json
import logging
import threading
import time

import requests

from . import db

LOG = logging.getLogger("vlmp.push")

RETRIES = 2
TIMEOUT = 8


def render_template(template: str, ctx: dict) -> str:
    """{{key}} 占位替换；模板为空时直接发原始 JSON。

    替换值做 JSON 字符串转义（不含外层引号），保证 description 里的
    引号/换行不会破坏 JSON 模板；中文保持可读。
    """
    if not template.strip():
        return json.dumps(ctx, ensure_ascii=False)
    out = template
    for k, v in ctx.items():
        if isinstance(v, str):
            rep = json.dumps(v, ensure_ascii=False)[1:-1]
        elif isinstance(v, bool):
            rep = "true" if v else "false"
        else:
            rep = str(v)
        out = out.replace("{{" + k + "}}", rep)
    return out


def targets_for(task_id: int, is_alarm: bool, path=None) -> list:
    rows = db.query("SELECT * FROM push_targets WHERE enabled=1", (), path)
    picked = []
    for r in rows:
        if r["only_alarm"] and not is_alarm:
            continue
        flt = (r["task_filter"] or "").strip()
        if flt:
            ids = {x.strip() for x in flt.split(",") if x.strip()}
            if str(task_id) not in ids:
                continue
        picked.append(dict(r))
    return picked


def deliver(target: dict, ctx: dict, alarm_id: int, path=None):
    try:
        headers = json.loads(target.get("headers_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        headers = {}
    headers.setdefault("Content-Type", "application/json; charset=utf-8")
    body = render_template(target.get("body_template") or "", ctx)
    method = (target.get("method") or "POST").upper()

    last_err, code, ok = "", 0, False
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.request(method, target["url"], headers=headers,
                                    data=body.encode("utf-8"), timeout=TIMEOUT)
            code = resp.status_code
            ok = 200 <= code < 300
            last_err = "" if ok else resp.text[:200]
            if ok:
                break
        except Exception as exc:                      # 网络异常：退避重试
            last_err = f"{type(exc).__name__}: {exc}"[:200]
        if attempt < RETRIES:
            time.sleep(1 + attempt)
    db.execute("INSERT INTO push_logs(alarm_id,target_id,ok,status_code,error,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (alarm_id, target["id"], 1 if ok else 0, code, last_err, db.now_str()), path)
    if not ok:
        LOG.warning("推送失败 target=%s code=%s err=%s", target["name"], code, last_err)


def push_alarm(alarm_id: int, task_id: int, task_name: str, rec: dict, path=None):
    """异步投递，避免阻塞 Saver 线程。"""
    tgts = targets_for(task_id, bool(rec.get("is_alarm")), path)
    if not tgts:
        return
    ctx = {
        "alarm_id": alarm_id,
        "task_id": task_id,
        "task_name": task_name,
        "is_alarm": bool(rec.get("is_alarm")),
        "action": rec.get("action", ""),
        "confidence": round(float(rec.get("confidence", 0.0)), 3),
        "description": rec.get("description", ""),
        "alarm_time": rec.get("alarm_time", ""),
        "det_label": rec.get("det_label", ""),
        "snapshot": (rec.get("snapshots") or [""])[0],
    }

    def _run():
        for t in tgts:
            deliver(t, ctx, alarm_id, path)

    threading.Thread(target=_run, name="push", daemon=True).start()
