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
"""基础设施运维端点：缓存/队列/FTS/熔断器管理。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..cache import cache_stats, invalidate
from ..async_queue import enqueue as queue_enqueue, dequeue_and_execute, queue_stats
from ..database import get_db
from ..deps import get_current_host
from ..fts import rebuild_all
from ..middleware import _BREAKERS

router = APIRouter(prefix="/api/sys/ops", tags=["sys_ops"])


# ==================== 缓存管理 ====================

@router.post("/cache/invalidate")
def op_cache_invalidate(
    pattern: str = Query(..., description="Cache key match pattern (fnmatch wildcard)"),
    host=Depends(get_current_host),
):
    """手动清除匹配模式的缓存。"""
    invalidate(pattern)
    return {"status": "ok", "pattern": pattern}


@router.get("/cache/stats")
def op_cache_stats(host=Depends(get_current_host)):
    """缓存层统计信息。"""
    return cache_stats()


# ==================== 队列管理 ====================

@router.get("/queue/stats")
def op_queue_stats(host=Depends(get_current_host)):
    """异步队列统计。"""
    return queue_stats()


@router.post("/queue/enqueue")
def op_queue_enqueue(
    task_type: str = Query(...),
    payload: str = Query(default="{}"),
    priority: int = Query(default=5, ge=1, le=10),
    host=Depends(get_current_host),
):
    """手动入队异步任务。"""
    import json as _json
    try:
        p = _json.loads(payload)
    except Exception:
        p = {}
    task_id = queue_enqueue(task_type, p, priority=priority)
    return {"task_id": task_id, "status": "pending"}


@router.post("/queue/dequeue")
def op_queue_dequeue(host=Depends(get_current_host)):
    """手动触发一次队列消费。"""
    executed = dequeue_and_execute()
    return {"executed": executed}


# ==================== FTS 管理 ====================

@router.get("/fts/reindex")
def op_fts_reindex(db: Session = Depends(get_db), host=Depends(get_current_host)):
    """触发 FTS 全量重建。"""
    counts = rebuild_all(db)
    return {"status": "ok", **counts}


# ==================== 熔断器状态 ====================

@router.get("/breaker/status")
def op_breaker_status(host=Depends(get_current_host)):
    """列出所有注册熔断器状态。"""
    result = {}
    for name, breaker in _BREAKERS.items():
        result[name] = {
            "state": breaker.state,
            "failure_count": breaker.failure_count,
            "failure_threshold": breaker.failure_threshold,
            "recovery_seconds": breaker.recovery_seconds,
        }
    return {"breakers": result}
