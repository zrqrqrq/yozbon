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
"""P2 Webhook 增强服务：指数退避重试 + 事件回放。

在既有 webhook_subscriptions 表基础上增加：
- 指数退避重试：delay = base * 2^attempt + jitter（随机 0~5s）；
- 事件持久化：所有 dispatch 的事件存入内存队列（生产可换 DB/Redis），支持按 ID 区间回放；
- 暂停/恢复 webhook（暂停期间事件入队列不投递，恢复后回放）。

指数退避公式: base * 2^attempt + jitter
"""
import hashlib
import hmac
import json
import logging
import random
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings

logger = logging.getLogger(__name__)

# 事件存储（生产环境应持久化到 DB 或消息队列）
_event_store: list = []  # [{event_id, event_type, payload, created_at, delivered: {webhook_id: status}}]
_event_counter = 0

# Webhook 注册表（简化实现，与 webhook_subscriptions 互补）
_webhooks: dict = {}  # webhook_id -> record
_delivery_log: dict = {}  # webhook_id -> [{event_id, attempt, status, next_retry_at}]


class WebhookEngine:
    """Webhook 增强引擎。"""

    def register_webhook(self, host_id: int, url: str, events: list, secret: str = None) -> dict:
        """注册 Webhook 订阅。

        Args:
            host_id: 宿主 ID。
            url: 接收端 URL。
            events: 订阅事件类型列表，如 ["contract.signed", "feed.created"]。
            secret: 签名密钥（未提供则自动生成）。

        Returns:
            {"webhook_id", "url", "events", "secret"}
        """
        global _event_counter
        webhook_id = f"wh_{secrets.token_hex(8)}"
        if not secret:
            secret = f"whsec_{secrets.token_urlsafe(24)}"

        _webhooks[webhook_id] = {
            "webhook_id": webhook_id,
            "host_id": host_id,
            "url": url,
            "events": events,
            "secret": secret,
            "status": "active",
            "created_at": datetime.utcnow().isoformat(),
        }
        _delivery_log[webhook_id] = []

        logger.info("webhook_enhanced: registered %s -> %s events=%s", webhook_id, url, events)
        return {"webhook_id": webhook_id, "url": url, "events": events, "secret": secret}

    def dispatch(self, event_type: str, payload: dict) -> dict:
        """分发事件到所有匹配的 webhook。

        Returns:
            {"event_id", "delivered_count", "queued_count"}
        """
        global _event_counter
        _event_counter += 1
        event_id = f"evt_{_event_counter:06d}"

        event = {
            "event_id": event_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": datetime.utcnow().isoformat(),
        }
        _event_store.append(event)

        delivered = 0
        queued = 0
        for wh in _webhooks.values():
            if wh["status"] != "active":
                continue
            if "*" in wh["events"] or event_type in wh["events"]:
                success = self._attempt_delivery(wh, event)
                if success:
                    delivered += 1
                else:
                    queued += 1

        logger.info("webhook_enhanced: dispatched %s event=%s delivered=%d queued=%d",
                    event_id, event_type, delivered, queued)
        return {"event_id": event_id, "delivered_count": delivered, "queued_count": queued}

    def retry_failed(self) -> int:
        """重试所有待重试的投递（由调度器周期调用）。

        Returns:
            本次成功重试的数量。
        """
        success_count = 0
        now = datetime.utcnow()

        for wh_id, log in _delivery_log.items():
            wh = _webhooks.get(wh_id)
            if wh is None or wh["status"] != "active":
                continue
            for entry in log:
                if entry["status"] != "retrying":
                    continue
                if entry["next_retry_at"] > now.isoformat():
                    continue
                # 尝试投递
                event = next((e for e in _event_store if e["event_id"] == entry["event_id"]), None)
                if event is None:
                    entry["status"] = "expired"
                    continue
                ok = self._attempt_delivery(wh, event, attempt=entry["attempt"] + 1)
                if ok:
                    entry["status"] = "delivered"
                    success_count += 1
                elif entry["attempt"] + 1 >= settings.WEBHOOK_MAX_RETRIES:
                    entry["status"] = "failed"
                else:
                    entry["attempt"] += 1
                    delay = self.calculate_backoff(entry["attempt"])
                    entry["next_retry_at"] = (now + timedelta(seconds=delay)).isoformat()

        return success_count

    def replay_events(self, webhook_id: str, from_event_id: int, to_event_id: int) -> dict:
        """回放指定事件区间。

        Args:
            webhook_id: Webhook ID。
            from_event_id: 起始事件序号（含）。
            to_event_id: 结束事件序号（含）。

        Returns:
            {"webhook_id", "replayed_count", "failed_count"}
        """
        wh = _webhooks.get(webhook_id)
        if wh is None:
            raise ValueError(f"Webhook {webhook_id} not found")

        replayed = 0
        failed = 0
        for event in _event_store:
            seq = int(event["event_id"].split("_")[1])
            if seq < from_event_id or seq > to_event_id:
                continue
            if "*" not in wh["events"] and event["event_type"] not in wh["events"]:
                continue
            ok = self._attempt_delivery(wh, event, is_replay=True)
            if ok:
                replayed += 1
            else:
                failed += 1

        logger.info("webhook_enhanced: replayed %s range [%d,%d] ok=%d fail=%d",
                    webhook_id, from_event_id, to_event_id, replayed, failed)
        return {"webhook_id": webhook_id, "replayed_count": replayed, "failed_count": failed}

    def get_webhook_status(self, webhook_id: str) -> dict:
        """获取 Webhook 投递状态概览。"""
        wh = _webhooks.get(webhook_id)
        if wh is None:
            raise ValueError(f"Webhook {webhook_id} not found")

        log = _delivery_log.get(webhook_id, [])
        return {
            "webhook_id": webhook_id,
            "status": wh["status"],
            "total_deliveries": len(log),
            "delivered": sum(1 for e in log if e["status"] == "delivered"),
            "retrying": sum(1 for e in log if e["status"] == "retrying"),
            "failed": sum(1 for e in log if e["status"] == "failed"),
            "created_at": wh["created_at"],
        }

    def calculate_backoff(self, attempt: int) -> float:
        """指数退避：base * 2^attempt + jitter(0~5s)。"""
        base = settings.WEBHOOK_BACKOFF_BASE_S
        jitter = random.uniform(0, 5)
        return base * (2 ** attempt) + jitter

    def pause_webhook(self, webhook_id: str) -> dict:
        """暂停 Webhook（暂停期间事件不投递，恢复后可回放）。"""
        if webhook_id not in _webhooks:
            raise ValueError(f"Webhook {webhook_id} not found")
        _webhooks[webhook_id]["status"] = "paused"
        logger.info("webhook_enhanced: paused %s", webhook_id)
        return {"webhook_id": webhook_id, "status": "paused"}

    def resume_webhook(self, webhook_id: str) -> dict:
        """恢复已暂停的 Webhook。"""
        if webhook_id not in _webhooks:
            raise ValueError(f"Webhook {webhook_id} not found")
        _webhooks[webhook_id]["status"] = "active"
        logger.info("webhook_enhanced: resumed %s", webhook_id)
        return {"webhook_id": webhook_id, "status": "active"}

    # ---- 内部方法 ----

    def _attempt_delivery(self, wh: dict, event: dict, attempt: int = 0, is_replay: bool = False) -> bool:
        """模拟投递（实际生产用 httpx post）。此处记录日志模拟成功/失败。"""
        webhook_id = wh["webhook_id"]
        payload_str = json.dumps({"event_id": event["event_id"], "type": event["event_type"],
                                  "data": event["payload"]}, ensure_ascii=False)
        signature = hmac.new(
            wh["secret"].encode(), payload_str.encode(), hashlib.sha256
        ).hexdigest()

        # 模拟：80% 成功率（开发环境）
        success = random.random() < 0.8 if not is_replay else True

        entry = {
            "event_id": event["event_id"],
            "attempt": attempt,
            "status": "delivered" if success else "retrying",
            "signature": signature[:16],
            "timestamp": datetime.utcnow().isoformat(),
            "next_retry_at": (datetime.utcnow() + timedelta(
                seconds=self.calculate_backoff(attempt + 1))).isoformat() if not success else "",
        }
        _delivery_log.setdefault(webhook_id, []).append(entry)
        return success


instance = WebhookEngine()
