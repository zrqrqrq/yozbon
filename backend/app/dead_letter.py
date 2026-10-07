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
"""死信队列服务：重试耗尽后的任务落死信，等待人工/城主介入。

来源（source_type）：worker_task（后台任务重试耗尽）/ webhook（webhook 投递重试耗尽）。
状态机：dead → {requeued（重新入队，实际入队由调用方负责）| discarded（人工丢弃）|
resolved（人工标记已处理）}。一旦离开 dead 即不可逆（只允许对 dead 执行处置）。

dead_letter_daily_job 只汇总日志，不做自动处置——死信必须人工/城主介入。

纪律：服务层只 add/flush，commit 由路由/调用方负责；json 仅用于 payload 校验/透传。
"""
import json
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import DeadLetterTask
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# 死信可处置状态
_STATUS_DEAD = "dead"
_STATUS_REQUEUED = "requeued"
_STATUS_DISCARDED = "discarded"
_STATUS_RESOLVED = "resolved"
_ALL_STATUSES = (_STATUS_DEAD, _STATUS_REQUEUED, _STATUS_DISCARDED, _STATUS_RESOLVED)


def _now() -> datetime:
    return datetime.utcnow()


def _normalize_payload(payload_json: str) -> str:
    """保证 payload_json 为合法 JSON 字符串，非法即回退 "{}"。"""
    try:
        json.loads(payload_json or "{}")
    except (ValueError, TypeError):
        return "{}"
    return payload_json or "{}"


def _require_dead(db: Session, dead_id: int) -> DeadLetterTask:
    """取一条仍处于 dead 状态的死信；否则抛错（非法处置）。"""
    task = db.get(DeadLetterTask, dead_id)
    if task is None:
        raise ValueError(f"dead letter {dead_id} not found")
    if task.status != _STATUS_DEAD:
        raise ValueError(
            f"dead letter {dead_id} not in 'dead' state (current: {task.status})"
        )
    return task


# ==================== 落死信 ====================
def push_dead_letter(
    db: Session,
    source_type: str,
    source_id: int,
    citizen_id: int = 0,
    retries_exhausted: int = 0,
    last_error: str = "",
    payload_json: str = "{}",
) -> DeadLetterTask:
    """把一条重试耗尽的任务落入死信队列（status=dead）并返回。"""
    task = DeadLetterTask(
        source_type=source_type,
        source_id=source_id,
        citizen_id=citizen_id,
        retries_exhausted=retries_exhausted,
        last_error=last_error,
        payload_json=_normalize_payload(payload_json),
        status=_STATUS_DEAD,
    )
    db.add(task)
    db.flush()
    logger.warning(
        "dead letter pushed: source_type=%s source_id=%s retries=%s",
        source_type, source_id, retries_exhausted,
    )
    return task


# ==================== 查询 ====================
def list_dead_letters(
    db: Session, status: str = "dead", limit: int = 50, offset: int = 0
) -> list[dict]:
    """按 created_at 倒序列出死信（字典列表，可直接 JSON 序列化）。

    status="dead" 默认仅列待处理死信；可传其它状态或对应过滤。
    """
    q = db.query(DeadLetterTask)
    if status:
        q = q.filter(DeadLetterTask.status == status)
    rows = (
        q.order_by(DeadLetterTask.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": t.id,
            "source_type": t.source_type,
            "source_id": t.source_id,
            "citizen_id": t.citizen_id,
            "retries_exhausted": t.retries_exhausted,
            "last_error": t.last_error,
            "payload": json.loads(t.payload_json) if t.payload_json else {},
            "status": t.status,
            "resolved_by": t.resolved_by,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


# ==================== 处置 ====================
def requeue(db: Session, dead_id: int, resolved_by: int) -> DeadLetterTask:
    """重新入队（dead → requeued）。

    本函数只标记状态；实际把任务重新投递到 worker/webhook 队列由调用方负责。
    """
    task = _require_dead(db, dead_id)
    task.status = _STATUS_REQUEUED
    task.resolved_by = resolved_by
    db.flush()
    return task


def discard(
    db: Session, dead_id: int, resolved_by: int, reason: str = ""
) -> DeadLetterTask:
    """人工丢弃（dead → discarded），reason 追加进 last_error 留痕。"""
    task = _require_dead(db, dead_id)
    task.status = _STATUS_DISCARDED
    task.resolved_by = resolved_by
    if reason:
        task.last_error = (
            f"{task.last_error}\n[discarded] {reason}".strip() if task.last_error
            else f"[discarded] {reason}"
        )
    db.flush()
    return task


def resolve(db: Session, dead_id: int, resolved_by: int) -> DeadLetterTask:
    """人工标记已处理（dead → resolved）。"""
    task = _require_dead(db, dead_id)
    task.status = _STATUS_RESOLVED
    task.resolved_by = resolved_by
    db.flush()
    return task


# ==================== 统计 ====================
def dead_letter_stats(db: Session) -> dict:
    """死信分状态计数：{"total", "dead", "requeued", "discarded", "resolved"}。"""
    rows = (
        db.query(DeadLetterTask.status, func.count(DeadLetterTask.id))
        .group_by(DeadLetterTask.status)
        .all()
    )
    counts = {status: 0 for status in _ALL_STATUSES}
    total = 0
    for status, cnt in rows:
        total += cnt
        if status in counts:
            counts[status] = cnt
    return {"total": total, **counts}


# ==================== 日级任务 ====================
def dead_letter_daily_job(db: Session, now: datetime) -> None:
    """日级汇总死信队列健康度——仅日志告警，不做自动处置（须人工/城主介入）。"""
    stats = dead_letter_stats(db)
    if stats["dead"] > 0:
        logger.warning(
            "dead_letter daily report: total=%s pending(dead)=%s "
            "requeued=%s discarded=%s resolved=%s — manual intervention required",
            stats["total"], stats["dead"], stats["requeued"],
            stats["discarded"], stats["resolved"],
        )
    else:
        logger.info(
            "dead_letter daily report: total=%s no pending dead letters",
            stats["total"],
        )


register_daily_job("dead_letter", dead_letter_daily_job)
