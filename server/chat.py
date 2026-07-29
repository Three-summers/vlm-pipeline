"""智能对话：以 VLM 为后端，注入规范文档(RAG)与实时告警数据作为上下文。

对应参考文档「智能对话」章节。因为同一台机器上只有一个多模态模型服务，
这里复用它做纯文本问答，并在 system prompt 中给它两类事实来源：
  1) RAG 检索到的规范条款
  2) 数据库中的任务/告警统计与最近告警明细
"""

import json
import logging
import re
import time

import requests

from vlmp import db, rag

LOG = logging.getLogger("vlmp.chat")

SYSTEM_BASE = (
    "你是本视频分析系统的运行助手。回答必须严格基于下面提供的【系统数据】与【规范条款】，"
    "不要编造数据库中不存在的告警、任务或数字。若资料不足以回答，请直接说明缺少哪些信息。"
    "回答使用简体中文，简洁准确，涉及数字时给出具体值。"
)


def pick_endpoint():
    row = db.query_one("SELECT * FROM vlm_endpoints WHERE enabled=1 ORDER BY weight DESC, id")
    return dict(row) if row else None


def build_context(question: str) -> dict:
    """按问题组织系统数据快照 + RAG 片段。"""
    top_k = int(db.get_setting("rag_top_k", "3"))
    max_alarms = int(db.get_setting("chat_max_alarms", "30"))
    snippets = rag.search(db, question, top_k)

    tasks = db.rows_to_dicts(db.query(
        "SELECT id,name,mode,enabled FROM tasks ORDER BY id"))
    total = db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=1")["c"]
    today0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 00:00:00",
                                       "%Y-%m-%d %H:%M:%S"))
    today = db.query_one("SELECT COUNT(*) c FROM alarms WHERE is_alarm=1 AND alarm_ts>=?",
                         (today0,))["c"]
    by_action = db.rows_to_dicts(db.query(
        "SELECT action, COUNT(*) c FROM alarms WHERE is_alarm=1 "
        "GROUP BY action ORDER BY c DESC LIMIT 10"))
    recent = db.rows_to_dicts(db.query(
        "SELECT a.id,a.alarm_time,a.action,a.confidence,a.description,t.name task_name "
        "FROM alarms a LEFT JOIN tasks t ON t.id=a.task_id "
        "WHERE a.is_alarm=1 ORDER BY a.id DESC LIMIT ?", (max_alarms,)))
    return {
        "snippets": snippets,
        "data": {
            "任务列表": tasks,
            "告警总数": total,
            "今日告警数": today,
            "按行为分布": by_action,
            "最近告警": recent,
        },
    }


def build_messages(question: str, history: list, ctx: dict) -> list:
    parts = [SYSTEM_BASE, "\n【系统数据】\n" +
             json.dumps(ctx["data"], ensure_ascii=False, indent=1)[:6000]]
    if ctx["snippets"]:
        parts.append("\n【规范条款】\n" + rag.build_rule_prompt(ctx["snippets"]))
    msgs = [{"role": "system", "content": "\n".join(parts)}]
    for m in history[-8:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})
    return msgs


def ask(question: str, session_id: int = 0, username: str = "") -> dict:
    ep = pick_endpoint()
    if not ep:
        return {"ok": False, "error": "没有可用的 VLM 端点，请先在「VLM 端点」页面配置"}

    if not session_id:
        session_id = db.execute(
            "INSERT INTO chat_sessions(title,username,created_at) VALUES(?,?,?)",
            (question[:24] or "新会话", username, db.now_str()))
    history = db.rows_to_dicts(db.query(
        "SELECT role,content FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,)))

    ctx = build_context(question)
    payload = {"model": ep["model"], "messages": build_messages(question, history, ctx),
               "max_tokens": 1024, "temperature": 0.3}
    try:
        r = requests.post(f"{ep['base_url'].rstrip('/')}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {ep['api_key']}"}, timeout=120)
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"].get("content") or ""
    except Exception as exc:
        return {"ok": False, "error": f"VLM 调用失败: {exc}", "session_id": session_id}

    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
    refs = [{"title": s["title"], "seq": s["seq"], "score": s["score"]}
            for s in ctx["snippets"]]
    db.execute("INSERT INTO chat_messages(session_id,role,content,refs_json,created_at) "
               "VALUES(?,?,?,?,?)", (session_id, "user", question, "[]", db.now_str()))
    db.execute("INSERT INTO chat_messages(session_id,role,content,refs_json,created_at) "
               "VALUES(?,?,?,?,?)",
               (session_id, "assistant", answer, json.dumps(refs, ensure_ascii=False),
                db.now_str()))
    return {"ok": True, "answer": answer, "refs": refs, "session_id": session_id}
