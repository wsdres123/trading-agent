"""知识库加载与 RAG 检索。

扫描 knowledge/ 与 docs/，分块后用 Qwen text-embedding-v3 向量化，
缓存到 .knowledge_index/。search() 返回与查询最相关的 top-k 文本块。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import List

import numpy as np
from openai import OpenAI

from config import settings as cfg

logger = logging.getLogger("knowledge")

CHUNK_SIZE = 400      # 每块约 400 字
CHUNK_OVERLAP = 60
BATCH = 10            # Qwen text-embedding-v3 单批上限 10
EMBED_DIM = 1024

_client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL) if cfg.QWEN_API_KEY else None


# ── 文件读取与分块 ──────────────────────────────────────────────────────────
def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning("读取失败 %s: %s", path, e)
        return ""


def _split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = text.strip()
    if not text:
        return []
    # 优先按段落/换行切，再按长度补齐
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paragraphs:
        if len(p) >= size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), size - overlap):
                chunks.append(p[i:i + size])
        elif len(buf) + len(p) + 1 <= size:
            buf = (buf + "\n" + p) if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c]


def _csv_to_text(path: Path) -> str:
    """把 CSV 行转成可检索的描述文本。"""
    import csv
    rows = list(csv.reader(path.open(encoding="utf-8", errors="ignore")))
    if not rows:
        return ""
    header = [h.strip() for h in rows[0]]
    lines = [f"表 {path.stem}，字段：{'、'.join(header)}"]
    for r in rows[1:]:
        cells = [f"{h}={v}" for h, v in zip(header, r) if v and v.strip()]
        if cells:
            lines.append("；".join(cells))
    return "\n".join(lines)


def collect_chunks() -> List[str]:
    """从 knowledge/ 与 docs/ 收集所有文本块。"""
    chunks: List[str] = []
    for d in (cfg.KNOWLEDGE_DIR, cfg.DOCS_DIR):
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.name.startswith("."):
                continue
            if p.suffix.lower() == ".csv":
                txt = _csv_to_text(p)
            else:
                txt = _read_file(p)
            chunks.extend(_split_text(txt))
    # 去重保序
    seen = set()
    uniq = []
    for c in chunks:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


# ── 向量化（带缓存）─────────────────────────────────────────────────────────
def _embed(texts: List[str]) -> np.ndarray:
    if not _client:
        raise RuntimeError("未配置 QWEN_API_KEY，无法向量化。")
    cache_path = cfg.INDEX_CACHE_DIR / "_embed_cache.json"
    cache: dict[str, list[float]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    vecs = []
    new_items: dict[str, list[float]] = {}
    todo = [t for t in texts if _hash(t) not in cache]
    # 批量请求新文本
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            resp = _client.embeddings.create(model=cfg.QWEN_EMBEDDING_MODEL, input=batch)
            for j, item in enumerate(resp.data):
                new_items[_hash(batch[j])] = item.embedding
        except Exception as e:
            logger.warning("embedding 批次失败：%s", e)
            for b in batch:
                new_items[_hash(b)] = [0.0] * EMBED_DIM
    cache.update(new_items)
    try:
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    except Exception as e:
        logger.warning("写 embed 缓存失败：%s", e)
    for t in texts:
        vecs.append(cache.get(_hash(t), [0.0] * EMBED_DIM))
    return np.array(vecs, dtype="float32")


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ── 索引构建/加载 ──────────────────────────────────────────────────────────
_INDEX: dict | None = None


def _build_index() -> dict:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    pkl = cfg.INDEX_CACHE_DIR / "text_vectors.json"
    chunks = collect_chunks()
    # 若缓存块数与当前一致则复用向量
    if pkl.exists():
        try:
            cached = json.loads(pkl.read_text(encoding="utf-8"))
            if cached.get("chunks") == chunks and "vecs" in cached:
                _INDEX = {"chunks": chunks, "vecs": np.array(cached["vecs"], dtype="float32")}
                logger.info("知识索引命中缓存：%d 块", len(chunks))
                return _INDEX
        except Exception:
            pass
    logger.info("重建知识索引：%d 块", len(chunks))
    vecs = _embed(chunks) if chunks else np.zeros((0, EMBED_DIM), dtype="float32")
    _INDEX = {"chunks": chunks, "vecs": vecs}
    try:
        pkl.write_text(json.dumps({"chunks": chunks, "vecs": vecs.tolist()}), encoding="utf-8")
    except Exception as e:
        logger.warning("写索引缓存失败：%s", e)
    return _INDEX


def search(query: str, k: int = 5) -> List[str]:
    """检索与 query 最相关的 top-k 知识块。"""
    idx = _build_index()
    chunks, vecs = idx["chunks"], idx["vecs"]
    if not chunks or _client is None:
        return []
    qv = _embed([query])[0]
    norms = np.linalg.norm(vecs, axis=1) * (np.linalg.norm(qv) + 1e-9)
    sims = (vecs @ qv) / (norms + 1e-9)
    k = min(k, len(chunks))
    top = np.argsort(sims)[-k:][::-1]
    return [chunks[i] for i in top if sims[i] > 0.1]


def status() -> dict:
    """知识库状态（不触发重建）。"""
    n = 0
    for d in (cfg.KNOWLEDGE_DIR, cfg.DOCS_DIR):
        if d.exists():
            n += sum(1 for p in d.iterdir() if not p.name.startswith(".") and p.is_file())
    return {"files": n, "indexed": _INDEX is not None, "qwen_key": bool(cfg.QWEN_API_KEY)}
