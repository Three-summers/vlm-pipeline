"""系统运维：主机指标、GPU、服务健康、日志、数据保留清理。"""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from vlmp import db

LOG = logging.getLogger("vlmp.ops")
ROOT = Path(__file__).resolve().parent.parent


def gpu_info() -> list:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,"
                           "memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=True).stdout
    except Exception as exc:
        LOG.debug("nvidia-smi 不可用: %s", exc)
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        used, total = float(parts[3]), float(parts[4])
        gpus.append({
            "index": parts[0], "name": parts[1], "util": float(parts[2]),
            "mem_used": used, "mem_total": total,
            "mem_pct": round(used / total * 100, 1) if total else 0,
            "temp": float(parts[5]),
        })
    return gpus


def host_info() -> dict:
    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1
    mem_total = mem_avail = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) / 1048576
            elif line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1]) / 1048576
    except OSError:
        pass
    du = shutil.disk_usage(str(ROOT))
    uptime = 0.0
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        pass
    return {
        "cores": cores,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cpu_pct": round(min(load1 / cores * 100, 100), 1),
        "mem_total_gb": round(mem_total, 1),
        "mem_used_gb": round(mem_total - mem_avail, 1),
        "mem_pct": round((mem_total - mem_avail) / mem_total * 100, 1) if mem_total else 0,
        "disk_total_gb": round(du.total / 1073741824, 1),
        "disk_used_gb": round(du.used / 1073741824, 1),
        "disk_pct": round(du.used / du.total * 100, 1),
        "uptime_h": round(uptime / 3600, 1),
    }


def endpoint_health(base_url: str, api_key: str = "EMPTY", timeout=6) -> dict:
    t0 = time.time()
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        r.raise_for_status()
        models = [m["id"] for m in r.json().get("data", [])]
        return {"ok": True, "models": models, "latency_ms": round((time.time() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                "latency_ms": round((time.time() - t0) * 1000)}


def check_all_endpoints() -> list:
    out = []
    for e in db.query("SELECT * FROM vlm_endpoints"):
        h = endpoint_health(e["base_url"], e["api_key"])
        db.execute("UPDATE vlm_endpoints SET last_check_at=?,last_check_ok=?,last_check_msg=? "
                   "WHERE id=?",
                   (db.now_str(), 1 if h["ok"] else 0,
                    (", ".join(h.get("models", [])) if h["ok"] else h.get("error", ""))[:200],
                    e["id"]))
        out.append({**dict(e), "health": h})
    return out


def storage_usage() -> dict:
    def dir_size(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    out = ROOT / "output"
    return {
        "output_mb": round(dir_size(out) / 1048576, 1),
        "models_mb": round(dir_size(ROOT / "models") / 1048576, 1),
        "logs_mb": round(dir_size(ROOT / "logs") / 1048576, 1),
        "db_mb": round(Path(db.db_path()).stat().st_size / 1048576, 2)
        if Path(db.db_path()).exists() else 0,
    }


def tail_file(path, lines=200) -> str:
    p = Path(path)
    if not p.exists():
        return "(文件不存在)"
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 200 * lines))
        data = fh.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def cleanup_expired(dry_run=False) -> dict:
    """按 retention_days 清理过期告警记录与其截图/录像。"""
    days = int(db.get_setting("retention_days", "30") or 0)
    if days <= 0:
        return {"skipped": True, "reason": "retention_days=0（不清理）"}
    cutoff = time.time() - days * 86400
    rows = db.query("SELECT id,snapshots_json,clip_path FROM alarms WHERE alarm_ts < ?", (cutoff,))
    import json
    removed_files = 0
    for r in rows:
        if dry_run:
            continue
        try:
            for f in json.loads(r["snapshots_json"] or "[]"):
                if Path(f).exists():
                    Path(f).unlink()
                    removed_files += 1
        except (json.JSONDecodeError, OSError):
            pass
        if r["clip_path"] and Path(r["clip_path"]).exists():
            try:
                Path(r["clip_path"]).unlink()
                removed_files += 1
            except OSError:
                pass
    if not dry_run and rows:
        db.execute("DELETE FROM alarms WHERE alarm_ts < ?", (cutoff,))
        db.execute("DELETE FROM push_logs WHERE created_at < ?",
                   (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),))
    return {"skipped": False, "days": days, "alarms": len(rows), "files": removed_files,
            "dry_run": dry_run}


def start_cleanup_scheduler(interval_hours: float = 6):
    """后台定时清理：按 retention_days 自动删过期告警，不再依赖手工点击。

    retention_days=0 时每轮直接跳过；间隔宽松（默认 6h），错过一轮无影响。
    """
    def _loop():
        while True:
            time.sleep(interval_hours * 3600)
            try:
                r = cleanup_expired(dry_run=False)
                if not r.get("skipped") and (r.get("alarms") or r.get("files")):
                    LOG.info("自动清理完成: %s", r)
                    db.audit("system", "auto_cleanup", str(r))
            except Exception as exc:
                LOG.error("自动清理失败: %s", exc)
    threading.Thread(target=_loop, name="cleanup", daemon=True).start()
