#!/usr/bin/env python
"""Web 平台冒烟测试：在临时数据库上跑一遍页面渲染与主要 API。

不联网、不启动分析子进程、不调用 VLM，只验证导入、SQL、模板渲染与权限。
用法： <VLMP_ENV>/bin/python scripts/smoke_web.py
"""

import os
import shutil
import sys
import tempfile
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="vlmp-smoke-"))
os.environ["VLMP_DB"] = str(TMP / "smoke.db")

from server.app import app, init_app     # noqa: E402
from vlmp import db                      # noqa: E402
from server import auth                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + detail if detail and not cond else ''}")


def get(c, url, expect=200):
    r = c.get(url)
    check(f"GET {url}", r.status_code == expect,
          f"got {r.status_code}: {r.data[:200].decode('utf-8', 'replace')}")
    return r


def api(c, url, method="POST", **kw):
    r = getattr(c, method.lower())(url, **kw)
    try:
        data = r.get_json() or {}
    except Exception:
        data = {}
    check(f"{method} {url}", r.status_code == 200 and data.get("ok"),
          f"{r.status_code} {str(data)[:200]}")
    return data


def main():
    print("== 初始化 ==")
    init_app()
    pw = "smoke-admin-pw"
    db.execute("UPDATE users SET password_hash=? WHERE username='admin'",
               (auth.hash_password(pw),))
    check("建库完成", db.query_one("SELECT id FROM users WHERE username='admin'") is not None)

    app.config["TESTING"] = True
    c = app.test_client()

    print("\n== 未登录访问应跳转 ==")
    r = c.get("/")
    check("未登录跳登录页", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          str(r.status_code))
    get(c, "/login")

    print("\n== 登录 ==")
    r = c.post("/login", data={"username": "admin", "password": pw})
    check("登录成功", r.status_code == 302, f"{r.status_code}")

    print("\n== 页面渲染 ==")
    for url in ["/", "/sources", "/algorithms", "/tasks", "/tasks/new", "/alarms",
                "/wall", "/chat", "/docs", "/endpoints", "/push", "/users",
                "/settings", "/ops"]:
        get(c, url)
    get(c, "/not-exist", 404)

    print("\n== 只读 API ==")
    for url in ["/api/dashboard", "/api/sources", "/api/algorithms", "/api/tasks",
                "/api/alarms", "/api/endpoints", "/api/docs", "/api/push-targets",
                "/api/ops/stats", "/api/docs/search?q=安全帽"]:
        api(c, url, "GET")

    print("\n== 增改删 ==")
    # 视频源（用一段本地生成的视频文件，避免依赖摄像头）
    vid = TMP / "clip.mp4"
    make_video(vid)
    sid = api(c, "/api/sources", json={"name": "冒烟视频", "type": "video",
                                       "uri": str(vid), "location": "测试"}).get("id")
    check("创建视频源", bool(sid))
    api(c, f"/api/sources/{sid}", "PUT", json={"name": "冒烟视频2", "type": "video",
                                               "uri": str(vid)})
    api(c, f"/api/sources/{sid}/test")

    # 算法（找一个真实权重；找不到就造个假文件只验流程）
    weights = find_weights() or make_fake_weights(TMP)
    aid = api(c, "/api/algorithms", json={"name": "冒烟算法",
                                          "weights_path": str(weights)}).get("id")
    check("创建算法", bool(aid))
    api(c, "/api/algorithms/scan")

    # 规范文档 + RAG
    did = api(c, "/api/docs", json={
        "title": "冒烟规范", "tags": "测试",
        "content": "作业人员进入现场必须佩戴安全帽。未佩戴安全帽属于违规行为，应当立即告警。"
                   "配电室内严禁吸烟，发现吸烟立即上报。"}).get("id")
    check("创建文档", bool(did))
    hits = api(c, "/api/docs/search?q=安全帽", "GET").get("results", [])
    check("RAG 能检索到片段", len(hits) > 0, str(hits)[:120])

    # 任务
    tid = api(c, "/api/tasks", json={
        "name": "冒烟任务", "source_id": sid, "algorithm_id": aid, "mode": "small_crop",
        "rule_doc_ids": str(did),
        "config": {"conf": 0.35, "target_actions": ["未佩戴安全帽"], "max_frames": 30,
                   "roi_include": [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]],
                   "live_preview": True, "record_clip": False, "bogus_key": "应被过滤"},
    }).get("id")
    check("创建任务", bool(tid))
    t = api(c, f"/api/tasks/{tid}", "GET").get("task", {})
    check("非法配置项被过滤", "bogus_key" not in t.get("config", {}), str(t.get("config")))
    check("ROI 已保存", len(t.get("config", {}).get("roi_include", [])) == 1)
    check("模式已保存", t.get("mode") == "small_crop")
    get(c, f"/tasks/{tid}")
    api(c, f"/api/tasks/{tid}/status", "GET")
    api(c, f"/api/tasks/{tid}/log", "GET")

    # 端点：密钥不得回显
    eid = api(c, "/api/endpoints", json={"name": "冒烟端点",
                                         "base_url": "http://127.0.0.1:9/v1",
                                         "api_key": "SECRET-SHOULD-NOT-LEAK",
                                         "model": "test"}).get("id")
    check("创建端点", bool(eid))
    eps = api(c, "/api/endpoints", "GET").get("items", [])
    leaked = any(e.get("api_key") == "SECRET-SHOULD-NOT-LEAK" for e in eps)
    check("端点密钥未回显", not leaked)
    page = c.get("/endpoints").data.decode("utf-8", "replace")
    check("端点页面不含明文密钥", "SECRET-SHOULD-NOT-LEAK" not in page)
    api(c, f"/api/endpoints/{eid}", "PUT", json={"name": "冒烟端点改",
                                                 "base_url": "http://127.0.0.1:9/v1",
                                                 "model": "test"})
    row = db.query_one("SELECT api_key FROM vlm_endpoints WHERE id=?", (eid,))
    check("留空不覆盖原密钥", row["api_key"] == "SECRET-SHOULD-NOT-LEAK")

    # 推送目标：部分字段更新不得清空模板
    pid = api(c, "/api/push-targets", json={"name": "冒烟推送",
                                            "url": "http://127.0.0.1:9/hook",
                                            "body_template": '{"m":"{{message}}"}'}).get("id")
    api(c, f"/api/push-targets/{pid}", "PUT", json={"name": "冒烟推送改", "enabled": 0})
    row = db.query_one("SELECT * FROM push_targets WHERE id=?", (pid,))
    check("未提交字段保持原值", row["body_template"] == '{"m":"{{message}}"}',
          repr(row["body_template"]))

    # 用户与令牌
    uid = api(c, "/api/users", json={"username": "smoke_op", "password": "op12345",
                                     "role": "operator"}).get("id")
    check("创建用户", bool(uid))
    tok = api(c, "/api/tokens", json={"name": "冒烟令牌", "scopes": "read,write"}).get("token")
    check("生成令牌", bool(tok) and tok.startswith("vlmp_"))

    # 设置
    api(c, "/api/settings", json={"site_name": "冒烟平台", "retention_days": "7"})
    check("设置已写入", db.get_setting("site_name") == "冒烟平台")

    print("\n== 开放 API（令牌鉴权）==")
    h = {"Authorization": f"Bearer {tok}"}
    for url in ["/openapi/v1/ping", "/openapi/v1/sources", "/openapi/v1/tasks",
                "/openapi/v1/alarms", "/openapi/v1/stats"]:
        api(c, url, "GET", headers=h)
    r = c.get("/openapi/v1/tasks")
    check("无令牌应 401", r.status_code == 401, str(r.status_code))
    srcs = api(c, "/openapi/v1/sources", "GET", headers=h).get("items", [])
    check("开放 API 不返回视频源地址", all("uri" not in s for s in srcs))

    print("\n== 权限隔离（operator 不能进用户管理）==")
    c2 = app.test_client()
    c2.post("/login", data={"username": "smoke_op", "password": "op12345"})
    r = c2.get("/users")
    check("operator 访问 /users 被拒", r.status_code in (302, 403), str(r.status_code))
    r = c2.post("/api/users", json={"username": "x", "password": "123456", "role": "admin"})
    check("operator 创建用户被拒", r.status_code in (302, 403), str(r.status_code))

    print("\n== 导出 ==")
    r = c.get("/api/alarms/export")
    check("导出 CSV", r.status_code == 200 and r.data.startswith("﻿".encode()),
          str(r.status_code))

    print("\n== 目录穿越防护 ==")
    r = c.get("/media?path=/etc/passwd")
    check("越权路径被拒", r.status_code in (400, 403, 404), str(r.status_code))
    r = c.get("/api/ops/log?name=/etc/passwd")
    check("日志接口绝对路径被拒", (r.get_json() or {}).get("ok") is False, str(r.data[:120]))
    r = c.get("/api/ops/log?name=../data/vlmp.db")
    check("日志接口 .. 穿越被拒", (r.get_json() or {}).get("ok") is False, str(r.data[:120]))

    print("\n== 新增功能 ==")
    r = c.get("/healthz")
    check("healthz 免登录可用", r.status_code == 200 and (r.get_json() or {}).get("ok"),
          str(r.data[:120]))
    db.execute("INSERT INTO alarms(task_id,is_alarm,action,alarm_time,alarm_ts) "
               "VALUES(1,1,'冒烟批量',?,?)", (db.now_str(), _time.time()))
    a1 = db.query_one("SELECT id FROM alarms ORDER BY id DESC LIMIT 1")["id"]
    r = api(c, "/api/alarms/batch", json={"op": "ack", "ids": [a1]})
    check("批量确认生效", r.get("affected") == 1, str(r))
    api(c, "/api/alarms/batch", json={"op": "delete", "ids": [a1]})
    check("批量删除生效",
          db.query_one("SELECT id FROM alarms WHERE id=?", (a1,)) is None)

    print("\n== 安全回归 ==")
    c.get("/logout")
    r = c.post("/login?next=//evil.example.com/", data={"username": "admin", "password": pw})
    loc = r.headers.get("Location", "")
    check("开放重定向被拒", "evil.example.com" not in loc, loc)
    c.post("/login", data={"username": "admin", "password": pw})
    old_key = db.get_setting("secret_key")
    api(c, "/api/settings", json={"secret_key": "hacked"})
    check("secret_key 不可经接口改写", db.get_setting("secret_key") == old_key)

    from vlmp import push as pushmod
    import json as _json
    body_json = pushmod.render_template('{"d":"{{description}}"}',
                                        {"description": '含"引号"与\n换行'})
    try:
        parsed = _json.loads(body_json)
        check("推送模板转义特殊字符", parsed["d"] == '含"引号"与\n换行', body_json)
    except Exception as exc:
        check("推送模板转义特殊字符", False, f"{exc} {body_json}")

    print("\n== 清理 ==")
    api(c, f"/api/tasks/{tid}", "DELETE")
    api(c, f"/api/sources/{sid}", "DELETE")
    api(c, f"/api/users/{uid}", "DELETE")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


def make_video(path: Path):
    import cv2
    import numpy as np
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240))
    for i in range(20):
        f = np.full((240, 320, 3), 30, dtype=np.uint8)
        cv2.rectangle(f, (i * 5, 60), (i * 5 + 60, 180), (0, 200, 0), -1)
        w.write(f)
    w.release()


def find_weights():
    for d in (str(Path.home() / "models" / "yolo"), "./weights", "./models/yolo"):
        p = Path(d)
        if p.is_dir():
            for w in sorted(p.glob("*.pt")):
                return w
    return None


def make_fake_weights(tmp: Path):
    p = tmp / "fake.pt"
    p.write_bytes(b"not a real checkpoint")
    return p


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        db.close_all() if hasattr(db, "close_all") else None
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
