"""任务调度器：把分析任务作为独立子进程拉起/停止/巡检。

对应参考文档的多进程解耦——每个分析任务一个进程，崩溃不影响其他任务与 Web。
进程状态通过 task_runs 表的心跳字段观测，Web 侧不持有进程句柄也能判活。
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from vlmp import db

LOG = logging.getLogger("vlmp.supervisor")

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "vlm_pipeline.py"
PYTHON = os.environ.get("VLMP_PYTHON", sys.executable)
HEARTBEAT_STALE = 60          # 秒：超过则认为进程失联
LIVE_STATUSES = ("starting", "running")


def _log_path(task_id: int) -> Path:
    d = ROOT / "logs" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"task-{task_id}.log"


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def latest_run(task_id: int):
    return db.query_one(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))


def task_status(task_id: int) -> dict:
    """综合 pid 存活与心跳新鲜度判断真实状态。"""
    run = latest_run(task_id)
    if not run:
        return {"status": "idle", "run_id": 0, "pid": 0, "stats": {}, "error": ""}
    status = run["status"]
    if status in LIVE_STATUSES and not pid_alive(run["pid"]):
        status = "dead"
    elif status in LIVE_STATUSES and run["heartbeat_at"]:
        try:
            hb = time.mktime(time.strptime(run["heartbeat_at"], "%Y-%m-%d %H:%M:%S"))
            if time.time() - hb > HEARTBEAT_STALE:
                status = "stale"
        except ValueError:
            pass
    import json
    try:
        stats = json.loads(run["stats_json"] or "{}")
    except json.JSONDecodeError:
        stats = {}
    return {"status": status, "run_id": run["id"], "pid": run["pid"],
            "started_at": run["started_at"], "heartbeat_at": run["heartbeat_at"],
            "stats": stats, "error": run["last_error"] or ""}


def is_running(task_id: int) -> bool:
    return task_status(task_id)["status"] in LIVE_STATUSES


def start_task(task_id: int) -> dict:
    if is_running(task_id):
        return {"ok": False, "error": "任务已在运行"}
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return {"ok": False, "error": "任务不存在"}
    if not task["enabled"]:
        return {"ok": False, "error": "任务已禁用"}

    run_id = db.execute(
        "INSERT INTO task_runs(task_id,pid,status,started_at,heartbeat_at) VALUES(?,?,?,?,?)",
        (task_id, 0, "starting", db.now_str(), db.now_str()))
    log_file = _log_path(task_id)
    env = dict(os.environ, VLMP_DB=db.db_path(), PYTHONUNBUFFERED="1")
    cmd = [PYTHON, str(ENGINE), "run", "--task", str(task_id), "--run-id", str(run_id)]
    try:
        with log_file.open("ab") as fh:
            fh.write(f"\n===== {db.now_str()} start run {run_id} =====\n".encode())
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
    except Exception as exc:
        db.execute("UPDATE task_runs SET status='error',last_error=?,stopped_at=? WHERE id=?",
                   (str(exc), db.now_str(), run_id))
        return {"ok": False, "error": str(exc)}
    db.execute("UPDATE task_runs SET pid=? WHERE id=?", (proc.pid, run_id))
    LOG.info("任务 %s 已启动 pid=%s run=%s", task_id, proc.pid, run_id)
    return {"ok": True, "run_id": run_id, "pid": proc.pid}


def stop_task(task_id: int, timeout=15) -> dict:
    run = latest_run(task_id)
    if not run or not pid_alive(run["pid"]):
        if run:
            db.execute("UPDATE task_runs SET status='stopped',stopped_at=? WHERE id=?",
                       (db.now_str(), run["id"]))
        return {"ok": True, "note": "进程已不在运行"}
    pid = run["pid"]
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)     # 引擎自身会优雅收尾
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    deadline = time.time() + timeout
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.3)
    if pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
    db.execute("UPDATE task_runs SET status='stopped',stopped_at=? WHERE id=?",
               (db.now_str(), run["id"]))
    LOG.info("任务 %s 已停止 pid=%s", task_id, pid)
    return {"ok": True}


def tail_log(task_id: int, lines=200) -> str:
    p = _log_path(task_id)
    if not p.exists():
        return "(暂无日志)"
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = min(size, 200 * lines)
        fh.seek(size - block)
        data = fh.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def reconcile():
    """回收：把 pid 已死但状态仍为 running 的运行记录标记为 dead。"""
    rows = db.query("SELECT * FROM task_runs WHERE status IN ('starting','running')")
    for r in rows:
        if not pid_alive(r["pid"]):
            db.execute("UPDATE task_runs SET status='dead',stopped_at=? WHERE id=?",
                       (db.now_str(), r["id"]))
            LOG.warning("运行记录 %s (task %s) 进程已消失，标记 dead", r["id"], r["task_id"])


def autostart_all():
    """Web 启动时拉起标记了 autostart 的任务。"""
    reconcile()
    for t in db.query("SELECT id,name FROM tasks WHERE autostart=1 AND enabled=1"):
        if not is_running(t["id"]):
            LOG.info("自动启动任务: %s", t["name"])
            start_task(t["id"])


def watchdog(interval=30):
    """后台守护：清理僵死记录，并重启配置了 autostart 的掉线任务。"""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                reconcile()
                for t in db.query("SELECT id,name FROM tasks WHERE autostart=1 AND enabled=1"):
                    if not is_running(t["id"]):
                        LOG.warning("autostart 任务 %s 掉线，重新拉起", t["name"])
                        start_task(t["id"])
            except Exception as exc:
                LOG.error("watchdog 异常: %s", exc)
    threading.Thread(target=_loop, name="watchdog", daemon=True).start()
