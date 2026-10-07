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
"""宿主（人类）侧通知服务 + Webhook 投递重试（指数退避）。

两部分职责：
  ① 宿主通知：notify 落库 / mark_read / unread_count / list_notifications，
     并提供 host_notify_daily_job（30 天以上通知自动置已读，避免无限堆积）。
  ② Webhook 投递重试：schedule_webhook_retry 落一条 pending 尝试，
     next_retry_at = now + 2**attempt_num 分钟（2/4/8/16/32 分钟指数退避）；
     attempt_num > 5 视为放弃（不再排重试）。get_pending_retries 供后台轮询取到期任务，
     mark_delivery_success / mark_delivery_failed 收敛单次投递结果，失败自动排下一次重试。

纪律：服务层只 add/flush，commit 由路由/调用方负责；json 仅用于 payload 校验/透传。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import HostNotification, WebhookDeliveryAttempt
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# 指数退避上限：attempt_num 超过该值即放弃（2/4/8/16/32 分钟 → 第 6 次起不再排）
_MAX_RETRY_ATTEMPTS = 5
# 通知自动置已读的保留窗口（天）：超过则日级任务批量置已读
_NOTIFY_RETENTION_DAYS = 30


def _now() -> datetime:
    return datetime.utcnow()


# ==================== ① 宿主通知 ====================
def notify(
    db: Session,
    host_id: int,
    title: str,
    body: str = "",
    severity: str = "info",
    category: str = "general",
    link: str = "",
) -> HostNotification:
    """落一条宿主通知并返回。commit 由调用方负责。"""
    n = HostNotification(
        host_id=host_id,
        title=title,
        body=body,
        severity=severity,
        category=category,
        link=link,
    )
    db.add(n)
    db.flush()
    return n


def mark_read(db: Session, notification_id: int) -> None:
    """置单条通知为已读（幂等：已读则覆盖 read_at 时间戳）。"""
    n = db.get(HostNotification, notification_id)
    if n is not None:
        n.read_at = _now()
        db.flush()


def unread_count(db: Session, host_id: int) -> int:
    """某宿主未读通知数。"""
    return (
        db.query(func.count(HostNotification.id))
        .filter(HostNotification.host_id == host_id)
        .filter(HostNotification.read_at.is_(None))
        .scalar()
        or 0
    )


def list_notifications(
    db: Session, host_id: int, limit: int = 30, offset: int = 0
) -> list[dict]:
    """按 created_at 倒序列出某宿主通知（字典列表，可直接 JSON 序列化）。"""
    rows = (
        db.query(HostNotification)
        .filter(HostNotification.host_id == host_id)
        .order_by(HostNotification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": n.id,
            "host_id": n.host_id,
            "title": n.title,
            "body": n.body,
            "severity": n.severity,
            "category": n.category,
            "link": n.link,
            "read": n.read_at is not None,
            "read_at": n.read_at.isoformat() if n.read_at else None,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]


# ==================== ② Webhook 投递重试 ====================
def _retry_delay(attempt_num: int) -> timedelta:
    """指数退避：第 n 次尝试失败后，下一次在 now + 2**n 分钟。"""
    return timedelta(minutes=2 ** attempt_num)


def schedule_webhook_retry(
    db: Session,
    subscription_id: int,
    event_type: str,
    payload_json: str,
    attempt_num: int = 1,
    error_msg: str = "",
    http_status: int = 0,
) -> WebhookDeliveryAttempt:
    """落一条 webhook 投递尝试（pending）。

    next_retry_at = now + 2**attempt_num 分钟；若 attempt_num > _MAX_RETRY_ATTEMPTS
    则不排重试（放弃，等待人工/死信流程介入）。payload_json 必须是合法 JSON 字符串。
    """
    try:
        json.loads(payload_json or "{}")
    except (ValueError, TypeError):
        payload_json = "{}"

    now = _now()
    attempt = WebhookDeliveryAttempt(
        subscription_id=subscription_id,
        event_type=event_type,
        payload_json=payload_json,
        attempt_num=attempt_num,
        status="pending",
        http_status=http_status,
        error_msg=error_msg,
        next_retry_at=(now + _retry_delay(attempt_num))
        if attempt_num <= _MAX_RETRY_ATTEMPTS
        else None,
    )
    db.add(attempt)
    db.flush()
    return attempt


def get_pending_retries(
    db: Session, now: datetime
) -> list[WebhookDeliveryAttempt]:
    """取出已到期的待重试投递（status=pending 且 next_retry_at <= now）。"""
    return (
        db.query(WebhookDeliveryAttempt)
        .filter(WebhookDeliveryAttempt.status == "pending")
        .filter(WebhookDeliveryAttempt.next_retry_at.isnot(None))
        .filter(WebhookDeliveryAttempt.next_retry_at <= now)
        .order_by(WebhookDeliveryAttempt.next_retry_at.asc())
        .all()
    )


def mark_delivery_success(db: Session, attempt_id: int, http_status: int) -> None:
    """单次投递成功 → status=success，清理 next_retry_at。"""
    attempt = db.get(WebhookDeliveryAttempt, attempt_id)
    if attempt is None:
        return
    attempt.status = "success"
    attempt.http_status = http_status
    attempt.next_retry_at = None
    db.flush()


def mark_delivery_failed(
    db: Session,
    attempt_id: int,
    http_status: int = 0,
    error_msg: str = "",
) -> WebhookDeliveryAttempt | None:
    """单次投递失败 → 收敛本次记录并排下一次重试。

    - 本次记录置 failed；
    - 若 attempt_num < _MAX_RETRY_ATTEMPTS：新建一条 attempt_num+1 的 pending 尝试并返回；
    - 否则（重试耗尽）：返回 None，交由死信队列处理。
    """
    attempt = db.get(WebhookDeliveryAttempt, attempt_id)
    if attempt is None:
        return None

    attempt.status = "failed"
    attempt.http_status = http_status
    attempt.error_msg = error_msg
    attempt.next_retry_at = None
    db.flush()

    next_num = attempt.attempt_num + 1
    if attempt.attempt_num >= _MAX_RETRY_ATTEMPTS:
        logger.warning(
            "webhook delivery exhausted: subscription=%s event=%s attempt=%s",
            attempt.subscription_id, attempt.event_type, attempt.attempt_num,
        )
        return None

    return schedule_webhook_retry(
        db,
        subscription_id=attempt.subscription_id,
        event_type=attempt.event_type,
        payload_json=attempt.payload_json,
        attempt_num=next_num,
        error_msg=error_msg,
        http_status=http_status,
    )


# ==================== ③ 日级任务 ====================
def host_notify_daily_job(db: Session, now: datetime) -> None:
    """日级清理：把超过保留窗口（30 天）的通知批量置已读。

    人工干预无所需，属可自动化的清理动作。commit 由调度器负责。
    """
    cutoff = (now or _now()) - timedelta(days=_NOTIFY_RETENTION_DAYS)
    updated = (
        db.query(HostNotification)
        .filter(HostNotification.created_at < cutoff)
        .filter(HostNotification.read_at.is_(None))
        .update({HostNotification.read_at: now or _now()}, synchronize_session=False)
    )
    db.flush()
    logger.info("host_notify daily cleanup: %s notifications auto-read", updated)


register_daily_job("host_notify", host_notify_daily_job)
