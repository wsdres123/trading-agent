"""多用户登录：SQLite 用户表 + bcrypt 哈希 + Redis 会话 token。

- 用户存 .data/users.db（username/password_hash/role）；第一个注册的用户为 admin。
- 会话存 Redis（key: jc:auth:session:<token>，TTL 7 天，滑动续期）；
  Redis 不可用时降级为进程内存会话（进程重启后需重新登录）。
- 防暴力破解：同一用户名连续失败 5 次锁定 15 分钟。

CLI：
    python -m src.auth register <用户名>            # 交互式设置密码
    python -m src.auth register <用户名> -p <密码>
    python -m src.auth passwd <用户名>              # 改密码
    python -m src.auth list                         # 列出用户
    python -m src.auth rm <用户名>                  # 删除用户
"""
from __future__ import annotations

import secrets
import sqlite3
import sys
import time

import bcrypt

from config import settings as cfg

DB_PATH = cfg.DATA_DIR / "users.db"
SESSION_TTL = 7 * 86400          # 会话 7 天
MAX_FAILS = 5                    # 连续失败次数上限
LOCK_SECONDS = 15 * 60           # 锁定 15 分钟
SESSION_PREFIX = "jc:auth:session:"
FAIL_PREFIX = "jc:auth:fail:"
LOCK_PREFIX = "jc:auth:lock:"


# ── SQLite 用户表 ──────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL)"
    )
    return conn


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def register_user(username: str, password: str) -> str:
    """注册用户，返回角色。用户名 2-20 位字母数字下划线。"""
    import re
    if not re.fullmatch(r"[\w一-龥]{2,20}", username):
        raise ValueError("用户名须为 2-20 位字母/数字/下划线/中文")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    role = "admin" if user_count() == 0 else "user"
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (username, _hash_password(password), role,
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"用户 {username} 已存在")
    return role


def _get_user(username: str) -> tuple[str, str] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT password_hash, role FROM users WHERE username=?", (username,)
        ).fetchone()
    return row


def change_password(username: str, password: str) -> None:
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    with _conn() as c:
        n = c.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (_hash_password(password), username),
        ).rowcount
    if n == 0:
        raise ValueError(f"用户 {username} 不存在")


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [{"username": r[0], "role": r[1], "created_at": r[2]} for r in rows]


def remove_user(username: str) -> None:
    with _conn() as c:
        if c.execute("DELETE FROM users WHERE username=?", (username,)).rowcount == 0:
            raise ValueError(f"用户 {username} 不存在")


# ── Redis 会话（Redis 不可用时降级内存）────────────────────────────────────
_rclient = None
_MEM_SESSIONS: dict[str, tuple[str, float]] = {}


def _redis():
    global _rclient
    if _rclient is not None:
        return _rclient or None
    try:
        import redis as _redis
        _rclient = _redis.Redis(
            host=cfg.REDIS_HOST, port=cfg.REDIS_PORT, db=cfg.REDIS_DB,
            password=cfg.REDIS_PASSWORD or None,
            socket_connect_timeout=1, socket_timeout=1,
        )
        _rclient.ping()
    except Exception:
        _rclient = False  # 记住不可用，避免每次重试
    return _rclient or None


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    r = _redis()
    if r:
        r.setex(SESSION_PREFIX + token, SESSION_TTL, username)
    else:
        _MEM_SESSIONS[token] = (username, time.time() + SESSION_TTL)
    return token


def get_session_user(token: str | None) -> str | None:
    if not token:
        return None
    r = _redis()
    if r:
        try:
            v = r.get(SESSION_PREFIX + token)
            return v.decode("utf-8") if v else None
        except Exception:
            return None
    ent = _MEM_SESSIONS.get(token)
    if not ent:
        return None
    if ent[1] < time.time():
        _MEM_SESSIONS.pop(token, None)
        return None
    return ent[0]


def touch_session(token: str | None) -> None:
    """滑动续期。"""
    if not token:
        return
    r = _redis()
    if r:
        try:
            r.expire(SESSION_PREFIX + token, SESSION_TTL)
        except Exception:
            pass
        return
    ent = _MEM_SESSIONS.get(token)
    if ent:
        _MEM_SESSIONS[token] = (ent[0], time.time() + SESSION_TTL)


def delete_session(token: str | None) -> None:
    if not token:
        return
    r = _redis()
    if r:
        try:
            r.delete(SESSION_PREFIX + token)
        except Exception:
            pass
    _MEM_SESSIONS.pop(token, None)


# ── 登录校验（带失败锁定）──────────────────────────────────────────────────
def verify(username: str, password: str) -> str | None:
    """校验成功返回用户名，失败返回 None。连续失败 5 次锁定 15 分钟。"""
    username = username.strip()
    r = _redis()
    if r:
        try:
            if r.exists(LOCK_PREFIX + username):
                return None
        except Exception:
            r = None
    row = _get_user(username)
    ok = False
    if row:
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), row[0].encode("ascii"))
        except Exception:
            ok = False
    if ok:
        if r:
            try:
                r.delete(FAIL_PREFIX + username)
            except Exception:
                pass
        return username
    if r:
        try:
            n = r.incr(FAIL_PREFIX + username)
            r.expire(FAIL_PREFIX + username, 10 * 60)
            if n >= MAX_FAILS:
                r.setex(LOCK_PREFIX + username, LOCK_SECONDS, "1")
                r.delete(FAIL_PREFIX + username)
        except Exception:
            pass
    return None


def bootstrap_admin_password(username: str = "admin") -> str | None:
    """零用户时自动生成管理员账号并返回初始密码（仅首启调用一次）。"""
    if user_count() > 0:
        return None
    pw = secrets.token_urlsafe(9)
    register_user(username, pw)
    return pw


# ── CLI ────────────────────────────────────────────────────────────────────
def _main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]
    try:
        if cmd == "register":
            username = args[1]
            pw = None
            if "-p" in args:
                pw = args[args.index("-p") + 1]
            else:
                import getpass
                pw = getpass.getpass("设置密码（至少6位）: ")
                pw2 = getpass.getpass("再输入一次: ")
                if pw != pw2:
                    print("两次密码不一致"); return 1
            role = register_user(username, pw)
            print(f"✓ 已创建用户 {username}（角色: {role}）")
        elif cmd == "passwd":
            import getpass
            username = args[1]
            pw = getpass.getpass("新密码（至少6位）: ")
            pw2 = getpass.getpass("再输入一次: ")
            if pw != pw2:
                print("两次密码不一致"); return 1
            change_password(username, pw)
            print(f"✓ {username} 密码已更新")
        elif cmd == "list":
            users = list_users()
            if not users:
                print("（无用户）")
            for u in users:
                print(f"{u['username']}\t{u['role']}\t{u['created_at']}")
        elif cmd == "rm":
            remove_user(args[1])
            print(f"✓ 已删除用户 {args[1]}")
        else:
            print(__doc__)
            return 1
    except (ValueError, IndexError) as e:
        print(f"✗ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
