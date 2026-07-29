"""规范文档 RAG：分块、倒排索引、检索。

参考文档用 ChromaDB + 向量检索；离线主机没有 embedding 服务，这里用
纯标准库的 TF-IDF（中文按 2-gram + 英文按词）实现关键词检索。
对"安全规范/作业标准"这类术语密集的短文档，关键词召回已足够可用。
"""

import json
import math
import re

_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_CJK = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list:
    """中文取连续 2-gram（并保留单字），英文/数字取词，统一小写。"""
    toks = [w.lower() for w in _ASCII.findall(text)]
    for seg in _CJK.findall(text):
        if len(seg) == 1:
            toks.append(seg)
            continue
        toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def term_freq(text: str) -> dict:
    tf = {}
    for t in tokenize(text):
        tf[t] = tf.get(t, 0) + 1
    return tf


def split_chunks(content: str, target: int = 320) -> list:
    """按段落聚合到约 target 字，保证语义块不被从句中间切断。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(p) > target * 2:                       # 超长段落再按句号切
            for sent in re.split(r"(?<=[。；！？\n])", p):
                if not sent.strip():
                    continue
                if len(buf) + len(sent) > target and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += sent
            continue
        if len(buf) + len(p) > target and buf:
            chunks.append(buf.strip())
            buf = ""
        buf += ("\n" if buf else "") + p
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [content.strip()[:target]]


def index_doc(db, doc_id: int, content: str, path=None) -> int:
    """重建某文档的分块索引，返回块数。"""
    db.execute("DELETE FROM rule_chunks WHERE doc_id=?", (doc_id,), path)
    chunks = split_chunks(content)
    db.executemany(
        "INSERT INTO rule_chunks(doc_id,seq,text,terms_json) VALUES(?,?,?,?)",
        [(doc_id, seq, text, json.dumps(term_freq(text), ensure_ascii=False))
         for seq, text in enumerate(chunks)], path)
    db.execute("UPDATE rule_docs SET chunk_count=? WHERE id=?", (len(chunks), doc_id), path)
    return len(chunks)


def search(db, question: str, top_k: int = 3, doc_ids=None, path=None) -> list:
    """返回 [{doc_id, title, seq, text, score}]，按 TF-IDF 余弦近似排序。"""
    q_tf = term_freq(question)
    if not q_tf:
        return []
    sql = ("SELECT c.id,c.doc_id,c.seq,c.text,c.terms_json,d.title "
           "FROM rule_chunks c JOIN rule_docs d ON d.id=c.doc_id")
    args = ()
    if doc_ids:
        marks = ",".join("?" * len(doc_ids))
        sql += f" WHERE c.doc_id IN ({marks})"
        args = tuple(doc_ids)
    rows = db.query(sql, args, path)
    if not rows:
        return []

    n_docs = len(rows)
    df = {}
    parsed = []
    for r in rows:
        try:
            tf = json.loads(r["terms_json"])
        except (json.JSONDecodeError, TypeError):
            tf = {}
        parsed.append((r, tf))
        for t in tf:
            df[t] = df.get(t, 0) + 1

    results = []
    for r, tf in parsed:
        score, norm = 0.0, 0.0
        for t, c in tf.items():
            idf = math.log(1 + n_docs / (1 + df.get(t, 0)))
            w = (1 + math.log(c)) * idf
            norm += w * w
            if t in q_tf:
                score += w * (1 + math.log(q_tf[t])) * idf
        if score <= 0:
            continue
        results.append({
            "chunk_id": r["id"], "doc_id": r["doc_id"], "title": r["title"],
            "seq": r["seq"], "text": r["text"],
            "score": round(score / math.sqrt(norm or 1.0), 4),
        })
    results.sort(key=lambda x: -x["score"])
    return results[:top_k]


def build_rule_prompt(snippets: list) -> str:
    """把检索到的规范片段拼成可注入 system prompt 的段落。"""
    if not snippets:
        return ""
    lines = ["以下是本场景适用的规范条款，判定时必须以其为依据："]
    for i, s in enumerate(snippets, 1):
        lines.append(f"[{i}] 《{s['title']}》 {s['text']}")
    return "\n".join(lines)
