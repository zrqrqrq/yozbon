# -*- coding: utf-8 -*-
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
"""G-08 缓存层：Redis 优先，内存降级。排行榜/名片/广场/Feed 等热路径缓存策略。

设计：
- get_or_compute(key, ttl, fn): 读缓存 → miss 则执行 fn 计算 → 写入缓存
- invalidate(pattern): 按模式失效
- 双后端：Redis（REDIS_URL 配置时）/ 进程内 dict（TTL 淘汰）
- 与限流模块共享 _redis_get()（复用 deps.py 连接逻辑）
"""
import fnmatch
import json
import time
import logging
from typing import Any, Callable, Optional

from .config import settings

logger = logging.getLogger(__name__)

# ---- 内存后端 ----
_mem_cache: dict[str, tuple[float, Any]] = {}  # key → (expire_at, value)
_mem_max_entries = 5000


def _mem_get(key: str) -> Optional[Any]:
    entry = _mem_cache.get(key)
    if entry is None:
        return None
    expire_at, value = entry
    if time.time() > expire_at:
        del _mem_cache[key]
        return None
    return value


def _mem_set(key: str, value: Any, ttl: int):
    if len(_mem_cache) >= _mem_max_entries:
        # 简单 LRU：删最早到期的
        oldest_key = min(_mem_cache, key=lambda k: _mem_cache[k][0])
        del _mem_cache[oldest_key]
    _mem_cache[key] = (time.time() + ttl, value)


def _mem_del_pattern(pattern: str):
    keys_to_del = [k for k in _mem_cache if fnmatch.fnmatch(k, pattern)]
    for k in keys_to_del:
        del _mem_cache[k]


# ---- Redis 后端（惰性，复用 deps 模式）----
_redis_cache_client: dict = {"client": None, "down_until": 0.0}
_CACHE_PREFIX = getattr(settings, "REDIS_RATELIMIT_PREFIX", "aijuhe:") or "aijuhe:"
_CACHE_PREFIX = _CACHE_PREFIX.replace(":rl:", ":cache:")


def _get_redis():
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return None
    now = time.time()
    if _redis_cache_client["down_until"] > now:
        return None
    if _redis_cache_client["client"] is not None:
        return _redis_cache_client["client"]
    try:
        import redis
        client = redis.Redis.from_url(
            url, socket_timeout=2.0, socket_connect_timeout=2.0,
            decode_responses=True)
        client.ping()
        _redis_cache_client["client"] = client
        return client
    except Exception:
        _redis_cache_client["down_until"] = now + 30.0
        return None


def _redis_get(key: str) -> Optional[Any]:
    client = _get_redis()
    if client is None:
        return None
    try:
        val = client.get(_CACHE_PREFIX + key)
        if val is None:
            return None
        return json.loads(val)
    except Exception:
        return None


def _redis_set(key: str, value: Any, ttl: int):
    client = _get_redis()
    if client is None:
        return
    try:
        client.setex(_CACHE_PREFIX + key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


def _redis_del_pattern(pattern: str):
    client = _get_redis()
    if client is None:
        return
    try:
        full_pattern = _CACHE_PREFIX + pattern
        keys = client.keys(full_pattern)
        if keys:
            client.delete(*keys)
    except Exception:
        pass


# ---- 公共 API ----

def get_cached(key: str) -> Optional[Any]:
    """读取缓存，未命中返回 None。"""
    if settings.REDIS_URL:
        val = _redis_get(key)
        if val is not None:
            return val
    return _mem_get(key)


def set_cached(key: str, value: Any, ttl: int = 60):
    """写入缓存。"""
    if settings.REDIS_URL:
        _redis_set(key, value, ttl)
    _mem_set(key, value, ttl)


def invalidate(pattern: str = "*"):
    """按模式清除缓存键。pattern 支持 fnmatch 通配符。"""
    if settings.REDIS_URL:
        _redis_del_pattern(pattern)
    _mem_del_pattern(pattern)


def get_or_compute(key: str, ttl: int, fn: Callable[[], Any]) -> Any:
    """读缓存 → miss → 执行 fn 计算 → 写入 → 返回。

    用法：
        data = get_or_compute("leaderboard:weekly", 300, lambda: compute_leaderboard(db))
    """
    cached = get_cached(key)
    if cached is not None:
        return cached
    value = fn()
    set_cached(key, value, ttl)
    return value


# ---- 数据变更主动失效（表 97 联动）----

def on_data_changed(entity_type: str, entity_id: int, reason: str = ""):
    """数据变更后调用：记录失效事件 + 立即清除相关缓存。"""
    from .database import SessionLocal
    from .models import CacheInvalidationEvent

    db = SessionLocal()
    try:
        evt = CacheInvalidationEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            invalidation_key=f"{entity_type}:{entity_id}*",
            reason=reason,
        )
        db.add(evt)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # 立即清除
    invalidate(f"{entity_type}:{entity_id}*")


def cache_stats() -> dict:
    """缓存层统计信息。"""
    return {
        "backend": "redis" if (settings.REDIS_URL and _redis_cache_client["client"]) else "memory",
        "memory_entries": len(_mem_cache),
        "memory_max": _mem_max_entries,
        "redis_connected": _redis_cache_client["client"] is not None,
    }
