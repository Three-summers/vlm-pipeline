#!/usr/bin/env python
"""端到端验证：对着已启动的 Web 平台跑完整链路。

验证 建源 → 建算法 → 建文档 → 建任务 → 启动 → 实时预览
→ 告警落库 → 截图服务 → 导出 → 推送 → 智能对话 → 开放 API。

用法：
    VLMP_ADMIN_PW=xxx python scripts/e2e_check.py

可选环境变量：
    VLMP_BASE          默认 http://127.0.0.1:8090
    VLMP_E2E_IMGDIR    图片序列源路径
    VLMP_E2E_CLIP      视频文件源路径
"""

import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("VLMP_BASE", "http://127.0.0.1:8090")
PW = os.environ.get("VLMP_ADMIN_PW", "")
PASS, FAIL = [], []

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + str(detail)[:300] if detail and not cond else ''}",
          flush=True)


def req(path, data=None, method=None, form=False, raw=False, headers=None):
    # 路径里可能带中文查询参数，统一做百分号编码（urllib 只接受 ASCII 请求行）
    url = BASE + urllib.parse.quote(path, safe="/?&=%+")
    body, hdr = None, dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data, ensure_ascii=False).encode()
            hdr["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=hdr, method=method)
    try:
        with _opener.open(r, timeout=120) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload or b"{}")), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], e.headers
    except json.JSONDecodeError:
        return 200, {}, {}


def api(path, data=None, method=None, expect_ok=True, headers=None):
    st, d, _ = req(path, data, method, headers=headers)
    good = st == 200 and (not expect_ok or (isinstance(d, dict) and d.get("ok")))
    check(f"{method or ('POST' if data is not None else 'GET')} {path}", good, f"{st} {d}")
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- 推送接收端
RECEIVED = []


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(self.rfile.read(n).decode("utf-8", "replace"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def start_hook(port=9099):
    srv = socketserver.TCPServer(("127.0.0.1", port), Hook)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_task(tid, timeout=300):
    """等任务跑到结束或超时，返回最后一次状态。"""
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        st, d, _ = req(f"/api/tasks/{tid}/status")
        last = d if isinstance(d, dict) else {}
        if last.get("status") in ("stopped", "finished", "error", "dead", "stale", "idle"):
            return last
        time.sleep(3)
    return last


def main():
    if not PW:
        print("请通过 VLMP_ADMIN_PW 传入管理员密码", file=sys.stderr)
        return 2

    print("== 登录 ==")
    st, _, _ = req("/login", {"username": "admin", "password": PW}, form=True)
    check("登录", st == 200, st)
    st, d, _ = req("/api/dashboard")
    check("会话可用", st == 200 and d.get("ok"), f"{st} {d}")

    print("\n== 算法登记 ==")
    r = api("/api/algorithms/scan", {})
    algos = api("/api/algorithms").get("items", [])
    check("已登记算法", len(algos) > 0, algos)
    pick = next((a for a in algos if "yolo11m" in a["name"]), algos[0] if algos else None)
    check("选到 yolo11m", pick is not None and "yolo11m" in pick["name"], pick)
    aid = pick["id"]
    cls = api(f"/api/algorithms/{aid}/classes", {})
    check("读取类别表", len(cls.get("classes", [])) >= 80, len(cls.get("classes", [])))

    print("\n== 视频源 ==")
    IMGDIR = os.environ.get(
        "VLMP_E2E_IMGDIR",
        # 默认值需按实际环境修改，或通过环境变量覆盖
        "./output/test-images",
    )
    CLIP = os.environ.get(
        "VLMP_E2E_CLIP",
        "./output/test-clip.mp4",
    )
    s1 = api("/api/sources", {"name": "E2E 图片序列", "type": "image",
                              "uri": IMGDIR, "location": "车间"}).get("id")
    s2 = api("/api/sources", {"name": "E2E 视频文件", "type": "video",
                              "uri": CLIP, "location": "测试"}).get("id")
    t1 = api(f"/api/sources/{s1}/test", {})
    check("图片源可达并出缩略图", t1.get("check_ok") and t1.get("thumb"), t1)
    t2 = api(f"/api/sources/{s2}/test", {})
    check("视频源可达并出缩略图", t2.get("check_ok") and t2.get("thumb"), t2)

    print("\n== 规范文档与检索 ==")
    did = api("/api/docs", {
        "title": "E2E 车间安全规范", "tags": "PPE",
        "content": "第一条 进入生产车间的所有人员必须全程佩戴安全帽，安全帽应系紧下颚带。\n"
                   "第二条 未佩戴安全帽进入作业区域的，判定为违规，应立即告警并通知班组长。\n"
                   "第三条 车间内严禁吸烟与使用明火。\n"
                   "第四条 高空作业必须佩戴安全带并挂在牢固位置。"}).get("id")
    check("文档入库", bool(did), did)
    hits = api(f"/api/docs/search?q=安全帽&doc_ids={did}").get("results", [])
    check("检索命中安全帽条款", any("安全帽" in h.get("text", "") for h in hits), hits)

    print("\n== 任务 A：small_crop 巡检（YOLO→裁剪→VLM，逐条落库）==")
    ta = api("/api/tasks", {
        "name": "E2E-A-安全帽巡检", "source_id": s1, "algorithm_id": aid,
        "mode": "small_crop", "rule_doc_ids": str(did),
        "config": {"conf": 0.35, "imgsz": 960, "classes": [0], "frame_stride": 1,
                   "dwell_seconds": 0, "vlm_interval_seconds": 0, "max_frames": 4,
                   "target_actions": [], "consumer_threads": 2,
                   "live_preview": True, "save_annotated": True},
    }).get("id")
    check("任务 A 创建", bool(ta), ta)
    api(f"/api/tasks/{ta}/start", {})
    time.sleep(8)
    st, img, hdr = req(f"/live/{ta}.jpg", raw=True)
    check("实时预览返回 JPEG", st == 200 and isinstance(img, bytes) and img[:2] == b"\xff\xd8",
          f"{st} {len(img) if isinstance(img, bytes) else img}")
    ra = wait_task(ta)
    check("任务 A 正常结束", ra.get("status") in ("finished", "stopped"), ra)

    print("\n== 任务 B：large_only 巡检（不依赖 YOLO，定时整帧送审）==")
    tb = api("/api/tasks", {
        "name": "E2E-B-整帧巡检", "source_id": s2, "mode": "large_only",
        "rule_doc_ids": str(did),
        "config": {"frame_stride": 5, "large_interval_seconds": 0.5, "max_frames": 30,
                   "target_actions": [], "live_preview": True},
    }).get("id")
    check("任务 B 创建", bool(tb), tb)
    api(f"/api/tasks/{tb}/start", {})
    rb = wait_task(tb)
    check("任务 B 正常结束", rb.get("status") in ("finished", "stopped"), rb)

    print("\n== 记录落库 ==")
    alarms = api("/api/alarms?limit=50").get("items", [])
    check("已产生记录", len(alarms) > 0, len(alarms))
    from_a = [a for a in alarms if a.get("task_id") == ta]
    from_b = [a for a in alarms if a.get("task_id") == tb]
    check("任务 A 有记录（YOLO+VLM 链路）", len(from_a) > 0, len(from_a))
    check("任务 B 有记录（纯 VLM 链路）", len(from_b) > 0, len(from_b))
    check("存在真告警（is_alarm=1）", any(a.get("is_alarm") for a in alarms), 0)
    check("存在巡检记录（is_alarm=0）", any(not a.get("is_alarm") for a in alarms), 0)
    if alarms:
        a0 = alarms[0]
        check("记录含 VLM 描述", bool(a0.get("description")), a0)
        check("记录含时间戳", a0.get("alarm_time") is not None, a0)
        snaps = a0.get("snapshots") or []
        check("记录含截图路径", len(snaps) > 0, a0)
        if snaps:
            st, img, _ = req("/media?path=" + urllib.parse.quote(snaps[0]), raw=True)
            check("截图可下载", st == 200 and img[:2] == b"\xff\xd8", st)
        st, page, _ = req(f"/alarms/{a0['id']}", raw=True)
        check("告警详情页可渲染", st == 200 and b"</html>" in page, st)
        api(f"/api/alarms/{a0['id']}/ack", {})

    print("\n== 导出 ==")
    st, csv, hdr = req("/api/alarms/export", raw=True)
    check("CSV 导出含数据行", st == 200 and csv.startswith(b"\xef\xbb\xbf")
          and len(csv.splitlines()) > 1, f"{st} {len(csv.splitlines()) if st == 200 else csv}")

    print("\n== 告警推送 ==")
    srv = start_hook()
    pid = api("/api/push-targets", {
        "name": "E2E 本地回调", "url": "http://127.0.0.1:9099/hook", "method": "POST",
        "headers_json": '{"X-From":"vlmp"}',
        "body_template": '{"task":"{{task_name}}","action":"{{action}}","desc":"{{description}}"}',
        "only_alarm": 0}).get("id")
    check("推送目标创建", bool(pid), pid)
    api(f"/api/push-targets/{pid}/test", {})
    time.sleep(1.5)
    check("回调已收到推送", len(RECEIVED) > 0, RECEIVED)
    if RECEIVED:
        try:
            check("推送体是合法 JSON 且已渲染模板",
                  "task" in json.loads(RECEIVED[0]), RECEIVED[0])
        except json.JSONDecodeError as e:
            check("推送体是合法 JSON 且已渲染模板", False, f"{e} {RECEIVED[0]}")
    logs = api("/api/push-targets").get("items", [])
    check("推送目标可列出", len(logs) > 0, logs)
    srv.shutdown()

    print("\n== 智能对话（VLM 问答 + 数据注入）==")
    t0 = time.time()
    ans = api("/api/chat/ask", {"question": "现在系统里一共有几个分析任务？分别叫什么名字？"})
    txt = ans.get("answer", "")
    check("对话有回答", len(txt) > 5, txt[:200])
    check("回答引用了真实任务名", "E2E" in txt, txt[:300])
    print(f"     （耗时 {time.time() - t0:.1f}s）回答：{txt[:160]}")
    ans2 = api("/api/chat/ask", {"question": "车间对安全帽有什么规定？",
                                 "session_id": ans.get("session_id")})
    check("多轮对话可用", len(ans2.get("answer", "")) > 5, ans2)

    print("\n== 开放 API ==")
    tok = api("/api/tokens", {"name": "E2E 令牌", "scopes": "read,write"}).get("token")
    h = {"Authorization": f"Bearer {tok}"}
    st, d, _ = req("/openapi/v1/alarms?limit=5", headers=h)
    check("令牌拉取告警", st == 200 and d.get("ok") and len(d.get("items", [])) > 0, f"{st} {d}")
    st, d, _ = req("/openapi/v1/stats", headers=h)
    check("令牌拉取统计", st == 200 and d.get("ok"), f"{st} {d}")
    if d.get("ok"):
        first = api("/openapi/v1/alarms?limit=1", headers=h).get("items", [])
        st, d2, _ = req(f"/openapi/v1/alarms?since_id={first[0]['id'] if first else 0}",
                        headers=h)
        check("增量拉取可用", st == 200, st)
    st, img, _ = req("/openapi/v1/media?alarm_id=%d&index=0" % alarms[0]["id"],
                     raw=True, headers=h)
    check("令牌下载截图", st == 200 and img[:2] == b"\xff\xd8", st)

    print("\n== 页面 ==")
    for p in ["/", "/wall", "/tasks", "/alarms", "/chat", "/docs", "/ops", "/settings"]:
        st, page, _ = req(p, raw=True)
        check(f"页面 {p}", st == 200 and b"</html>" in page, st)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
