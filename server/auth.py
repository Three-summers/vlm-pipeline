"""认证与权限：会话登录、角色校验、API Token。

角色：admin（全部）> operator（可操作任务/告警）> viewer（只读）
"""

import functools
import secrets

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from vlmp import db

ROLES = ("admin", "operator", "viewer")
ROLE_LEVEL = {"viewer": 0, "operator": 1, "admin": 2}
ROLE_LABEL = {"admin": "管理员", "operator": "操作员", "viewer": "只读用户"}


def hash_password(pw: str) -> str:
    return generate_password_hash(pw)


def verify_password(stored: str, pw: str) -> bool:
    return check_password_hash(stored, pw)


def ensure_default_admin() -> str:
    """首次启动创建 admin 账号，返回随机初始密码（已存在则返回空串）。"""
    if db.query_one("SELECT id FROM users LIMIT 1"):
        return ""
    pw = secrets.token_urlsafe(9)
    db.execute("INSERT INTO users(username,password_hash,role,display_name,enabled,created_at) "
               "VALUES(?,?,?,?,1,?)",
               ("admin", hash_password(pw), "admin", "系统管理员", db.now_str()))
    return pw


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = db.query_one("SELECT * FROM users WHERE id=? AND enabled=1", (uid,))
    return dict(row) if row else None


def load_user():
    """before_request 钩子：把当前用户放进 g。"""
    g.user = current_user()
    g.api_token = None


def _deny(msg="需要登录"):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"ok": False, "error": msg}), 401
    return redirect(url_for("login", next=request.full_path))


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not g.get("user"):
            return _deny()
        return fn(*a, **kw)
    return wrapper


def role_required(min_role: str):
    need = ROLE_LEVEL[min_role]

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = g.get("user")
            if not user:
                return _deny()
            if ROLE_LEVEL.get(user["role"], 0) < need:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"ok": False, "error": "权限不足"}), 403
                return "<h3>权限不足</h3>", 403
            return fn(*a, **kw)
        return wrapper
    return deco


# --- 开放 API Token -----------------------------------------------------

def new_token() -> str:
    return "vlmp_" + secrets.token_urlsafe(24)


def token_required(scope="read"):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            raw = request.headers.get("Authorization", "")
            tok = raw[7:].strip() if raw.lower().startswith("bearer ") else \
                request.args.get("token", "")
            if not tok:
                return jsonify({"ok": False, "error": "缺少 API Token"}), 401
            row = db.query_one("SELECT * FROM api_tokens WHERE token=? AND enabled=1", (tok,))
            if not row:
                return jsonify({"ok": False, "error": "Token 无效"}), 401
            scopes = {s.strip() for s in row["scopes"].split(",")}
            if scope not in scopes:
                return jsonify({"ok": False, "error": f"Token 缺少 {scope} 权限"}), 403
            db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                       (db.now_str(), row["id"]))
            g.api_token = dict(row)
            return fn(*a, **kw)
        return wrapper
    return deco
