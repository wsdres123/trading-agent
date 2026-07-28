"""API Key 保护：机器绑定加密存储，磁盘不落明文。

- 加密密钥由 本机 machine-id + 当前用户 + 盐 派生（SHA256），不写盘。
- 密文文件拷贝到其他机器/其他用户下无法解密，防止随项目打包泄露。
- 首次启动自动把 .env 中的明文 key 迁移进密文库并抹除明文。
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

_SALT = b"jiecai-ai-trading-keystore-v1"
PLACEHOLDER = "__PROTECTED__"  # 迁移后 .env 中的占位值


def _machine_secret() -> bytes:
    parts = [_SALT]
    try:
        parts.append(Path("/etc/machine-id").read_bytes().strip())
    except Exception:
        pass
    parts.append(os.environ.get("USER", os.environ.get("USERNAME", "")).encode())
    return hashlib.sha256(b"|".join(parts)).digest()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_machine_secret()))


def save_key(store: Path, plain: str) -> bool:
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(_fernet().encrypt(plain.encode()))
        os.chmod(store, 0o600)
        return load_key(store) == plain  # 回读校验后才允许抹除明文
    except Exception:
        return False


def load_key(store: Path) -> str:
    try:
        return _fernet().decrypt(store.read_bytes()).decode()
    except Exception:
        return ""


def mask(key: str) -> str:
    """日志/界面展示用脱敏形式，绝不输出完整 key。"""
    if not key:
        return "(未配置)"
    return key[:4] + "****" + key[-4:] if len(key) > 12 else "****"


def scrub_env_file(env_path: Path, key_names: tuple, raw: str) -> None:
    """把 .env 中的明文 key 替换为占位符，并收紧文件权限。"""
    try:
        if not env_path.exists():
            return
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out, changed = [], False
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                if k.strip() in key_names and v.strip().strip("'\"") == raw:
                    line = f"{k.strip()}={PLACEHOLDER}"
                    changed = True
            out.append(line)
        if changed:
            env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
    except Exception:
        pass
