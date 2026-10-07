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
"""P2 Push 通知/IM 集成服务。

统一管理 Push Subscription：支持 host/citizen 按事件类型订阅，
渠道涵盖 IM（webhook 转发）、email、SMS、web-push 等。

核心特性：
- 订阅管理：subscribe / unsubscribe；
- 事件分发：send 单播 / broadcast 广播；
- 限流：同一订阅每分钟最多 N 条推送（rate_limit_check）；
- 测试推送：验证端点可达。
"""
import json
import logging
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import PushSubscription

logger = logging.getLogger(__name__)

RATE_LIMIT_RPM = 60  # 每订阅每分钟最大推送数
_rate_counters: dict = defaultdict(list)  # subscription_id -> [timestamps]


def _now() -> datetime:
    return datetime.utcnow()


class PushService:
    """Push 通知服务。"""

    def subscribe(self, subscriber_type: str, subscriber_id: int,
                  channel: str, endpoint: str, event_types: list) -> dict:
        """创建推送订阅。

        Args:
            subscriber_type: "host" 或 "citizen"。
            subscriber_id: 订阅者 ID。
            channel: 渠道标识 (im/email/sms/webpush/webhook)。
            endpoint: 推送端点 URL 或地址。
            event_types: 订阅事件类型列表，["*"] 表示全部。

        Returns:
            {"subscription_id", "subscriber_type", "channel", "event_types"}
        """
        if subscriber_type not in ("host", "citizen"):
            raise ValueError("subscriber_type must be host or citizen")

        valid_channels = ("im", "email", "sms", "webpush", "webhook")
        if channel not in valid_channels:
            raise ValueError(f"channel must be one of {valid_channels}")

        db: Session = SessionLocal()
        try:
            sub = PushSubscription(
                subscriber_type=subscriber_type,
                subscriber_id=subscriber_id,
                channel=channel,
                endpoint=endpoint,
                event_types=json.dumps(event_types),
                is_active=1,
            )
            db.add(sub)
            db.commit()
            logger.info("push_notify: subscribed %s %d channel=%s events=%s",
                        subscriber_type, subscriber_id, channel, event_types)
            return {
                "subscription_id": sub.id,
                "subscriber_type": subscriber_type,
                "subscriber_id": subscriber_id,
                "channel": channel,
                "event_types": event_types,
            }
        finally:
            db.close()

    def unsubscribe(self, subscription_id: int) -> bool:
        """取消订阅（软删除：设 is_active=0）。"""
        db: Session = SessionLocal()
        try:
            sub = db.get(PushSubscription, subscription_id)
            if sub is None:
                return False
            sub.is_active = 0
            db.commit()
            logger.info("push_notify: unsubscribed %d", subscription_id)
            return True
        finally:
            db.close()

    def send(self, subscriber_type: str, subscriber_id: int,
             event_type: str, payload: dict) -> dict:
        """向特定订阅者发送推送。

        查找该订阅者的活跃订阅，匹配事件类型后投递。

        Returns:
            {"sent_count", "skipped_count", "details": [...]}
        """
        db: Session = SessionLocal()
        try:
            subs = (db.query(PushSubscription)
                    .filter(PushSubscription.subscriber_type == subscriber_type,
                            PushSubscription.subscriber_id == subscriber_id,
                            PushSubscription.is_active == 1)
                    .all())

            sent = 0
            skipped = 0
            details = []
            for sub in subs:
                events = json.loads(sub.event_types or "[]")
                if "*" not in events and event_type not in events:
                    skipped += 1
                    continue
                # 限流检查
                if not self.rate_limit_check(sub.id):
                    skipped += 1
                    details.append({"subscription_id": sub.id, "status": "rate_limited"})
                    continue
                # 模拟投递
                self._deliver(sub, event_type, payload)
                sent += 1
                details.append({"subscription_id": sub.id, "channel": sub.channel, "status": "sent"})

            return {"sent_count": sent, "skipped_count": skipped, "details": details}
        finally:
            db.close()

    def broadcast(self, channel: str, event_type: str, payload: dict) -> dict:
        """向某渠道所有活跃订阅广播推送。

        Args:
            channel: 目标渠道（None 或 "*" 表示全部渠道）。
            event_type: 事件类型。
            payload: 推送内容。

        Returns:
            {"total_matched", "sent_count", "failed_count"}
        """
        db: Session = SessionLocal()
        try:
            q = db.query(PushSubscription).filter(PushSubscription.is_active == 1)
            if channel and channel != "*":
                q = q.filter(PushSubscription.channel == channel)
            subs = q.all()

            sent = 0
            failed = 0
            for sub in subs:
                events = json.loads(sub.event_types or "[]")
                if "*" not in events and event_type not in events:
                    continue
                if not self.rate_limit_check(sub.id):
                    continue
                ok = self._deliver(sub, event_type, payload)
                if ok:
                    sent += 1
                else:
                    failed += 1

            return {"total_matched": len(subs), "sent_count": sent, "failed_count": failed}
        finally:
            db.close()

    def get_subscriptions(self, subscriber_type: str, subscriber_id: int) -> list:
        """查询订阅者的所有活跃订阅。"""
        db: Session = SessionLocal()
        try:
            subs = (db.query(PushSubscription)
                    .filter(PushSubscription.subscriber_type == subscriber_type,
                            PushSubscription.subscriber_id == subscriber_id,
                            PushSubscription.is_active == 1)
                    .all())
            return [
                {
                    "subscription_id": s.id,
                    "channel": s.channel,
                    "endpoint": s.endpoint[:50] + "..." if len(s.endpoint) > 50 else s.endpoint,
                    "event_types": json.loads(s.event_types or "[]"),
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in subs
            ]
        finally:
            db.close()

    def test_push(self, subscription_id: int) -> dict:
        """发送测试推送验证端点可达。"""
        db: Session = SessionLocal()
        try:
            sub = db.get(PushSubscription, subscription_id)
            if sub is None:
                raise ValueError(f"Subscription {subscription_id} not found")
            if not sub.is_active:
                raise ValueError(f"Subscription {subscription_id} is disabled")

            test_payload = {"message": "Push test from yozbon", "timestamp": _now().isoformat()}
            ok = self._deliver(sub, "test.push", test_payload)
            return {
                "subscription_id": subscription_id,
                "channel": sub.channel,
                "success": ok,
                "timestamp": _now().isoformat(),
            }
        finally:
            db.close()

    def rate_limit_check(self, subscription_id: int) -> bool:
        """检查订阅是否超过每分钟推送限制。

        滑动窗口算法：保留最近 60s 的推送时间戳。
        """
        now_ts = time.time()
        window = _rate_counters[subscription_id]
        # 清除窗口外记录
        cutoff = now_ts - 60.0
        while window and window[0] < cutoff:
            window.pop(0)

        if len(window) >= RATE_LIMIT_RPM:
            return False
        window.append(now_ts)
        return True

    # ---- 内部方法 ----

    @staticmethod
    def _deliver(sub: PushSubscription, event_type: str, payload: dict) -> bool:
        """模拟投递（生产按 channel 分派到对应适配器：SMTP/HTTP POST/WebSocket 等）。"""
        logger.info("push_notify: delivering to %s %s event=%s channel=%s",
                    sub.subscriber_type, sub.subscriber_id, event_type, sub.channel)
        # 此处返回 True 模拟成功；实际应 httpx.post(sub.endpoint, ...)
        return True


instance = PushService()
