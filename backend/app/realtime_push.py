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
"""WebSocket/SSE 实时推送服务。

功能：
- 事件发布（publish）：写入事件队列；
- 订阅管理（subscribe/unsubscribe）：按 channel + event_types 过滤；
- 待投递事件拉取（long-polling / SSE stream 消费）；
- 投递确认（mark_delivered）。

依赖模型：RealtimeSubscription, RealtimeEvent。
"""
import json
import logging
from datetime import datetime

from .config import settings
from .database import SessionLocal
from .models import RealtimeSubscription, RealtimeEvent

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class RealtimeService:
    """实时推送核心服务。"""

    def publish(self, channel: str, event_type: str, payload: dict,
                priority: int = 5) -> dict:
        """发布事件到指定频道。

        Args:
            channel: 频道标识（task/negotiation/market/governance）。
            event_type: 事件类型（created/updated/deleted/custom）。
            payload: 事件负载（字典，将序列化为 JSON 存储）。
            priority: 优先级 1(最高)-10(最低)，用于拉取排序。

        Returns:
            {"event_id": int, "channel": str}
        """
        db = SessionLocal()
        try:
            event = RealtimeEvent(
                channel=channel,
                event_type=event_type,
                payload=json.dumps(payload, ensure_ascii=False),
                priority=max(1, min(10, priority)),
                delivered=0,
            )
            db.add(event)
            db.commit()
            logger.debug("Event published: channel=%s type=%s id=%d",
                         channel, event_type, event.id)
            return {"event_id": event.id, "channel": channel}
        finally:
            db.close()

    def subscribe(self, citizen_id: int = 0, host_id: int = 0,
                  channel: str = "", event_types: list[str] | None = None,
                  delivery: str = "sse") -> dict:
        """创建订阅。

        Args:
            citizen_id: AI 公民 ID（与 host_id 二选一）。
            host_id: 宿主 ID。
            channel: 订阅频道。
            event_types: 订阅的事件类型列表，["*"] 表示全部。
            delivery: 投递方式 sse/websocket/push。

        Returns:
            {"subscription_id": int}
        """
        db = SessionLocal()
        try:
            # 检查是否已有相同订阅
            existing = (db.query(RealtimeSubscription)
                        .filter(RealtimeSubscription.citizen_id == citizen_id,
                                RealtimeSubscription.host_id == host_id,
                                RealtimeSubscription.channel == channel,
                                RealtimeSubscription.is_active == 1)
                        .first())
            if existing:
                # 更新 event_types
                existing.event_types = json.dumps(event_types or ["*"])
                existing.delivery = delivery
                db.commit()
                return {"subscription_id": existing.id, "updated": True}

            sub = RealtimeSubscription(
                citizen_id=citizen_id,
                host_id=host_id,
                channel=channel,
                event_types=json.dumps(event_types or ["*"]),
                delivery=delivery,
                is_active=1,
            )
            db.add(sub)
            db.commit()
            logger.info("Subscription created: sub=%d channel=%s", sub.id, channel)
            return {"subscription_id": sub.id}
        finally:
            db.close()

    def unsubscribe(self, subscription_id: int) -> dict:
        """取消订阅（软删除）。"""
        db = SessionLocal()
        try:
            sub = db.get(RealtimeSubscription, subscription_id)
            if sub is None:
                return {"error": "subscription not found"}
            sub.is_active = 0
            db.commit()
            logger.info("Subscription cancelled: id=%d", subscription_id)
            return {"ok": True}
        finally:
            db.close()

    def get_pending_events(self, channel: str, since_id: int = 0,
                           citizen_id: int = 0, host_id: int = 0,
                           limit: int = 50) -> list[dict]:
        """拉取指定频道的待投递事件（按优先级排序）。

        Args:
            channel: 频道。
            since_id: 上次拉取的最后一个 event_id（增量拉取）。
            citizen_id: 用于 event_types 过滤。
            host_id: 用于 event_types 过滤。
            limit: 返回数量上限。

        Returns:
            事件列表 [{"id", "event_type", "payload", "priority", "created_at"}]
        """
        db = SessionLocal()
        try:
            q = (db.query(RealtimeEvent)
                 .filter(RealtimeEvent.channel == channel,
                         RealtimeEvent.id > since_id,
                         RealtimeEvent.delivered == 0))

            # 按订阅者 event_types 过滤
            if citizen_id or host_id:
                sub = (db.query(RealtimeSubscription)
                       .filter(RealtimeSubscription.channel == channel,
                               RealtimeSubscription.is_active == 1)
                       .filter(
                           (RealtimeSubscription.citizen_id == citizen_id) |
                           (RealtimeSubscription.host_id == host_id)
                       )
                       .first())
                if sub:
                    types = json.loads(sub.event_types)
                    if "*" not in types:
                        q = q.filter(RealtimeEvent.event_type.in_(types))

            events = (q.order_by(RealtimeEvent.priority.asc(), RealtimeEvent.id.asc())
                      .limit(min(limit, 100))
                      .all())

            return [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "payload": json.loads(e.payload) if e.payload else {},
                    "priority": e.priority,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ]
        finally:
            db.close()

    def mark_delivered(self, event_ids: list[int]) -> dict:
        """标记事件已投递。"""
        if not event_ids:
            return {"ok": True, "count": 0}
        db = SessionLocal()
        try:
            updated = (db.query(RealtimeEvent)
                       .filter(RealtimeEvent.id.in_(event_ids))
                       .update({"delivered": 1}, synchronize_session=False))
            db.commit()
            logger.debug("Events marked delivered: %d", updated)
            return {"ok": True, "count": updated}
        finally:
            db.close()


instance = RealtimeService()
