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
"""全局限流服务。

功能：
- 滑动窗口限流算法（基于 RateLimitCounter 表）；
- 多维度限流策略（global/endpoint/user）；
- 策略 CRUD 和动态加载；
- 请求计数与用量查询。

依赖模型：RateLimitPolicy, RateLimitCounter。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import RateLimitPolicy, RateLimitCounter

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class RateLimiter:
    """全局限流业务逻辑（滑动窗口算法）。"""

    def check_limit(self, subject: str, endpoint: str = "*") -> dict:
        """检查请求是否超出限流阈值。

        Args:
            subject: 限流主体标识（如 "host:123:192.168.1.1" 或 "ai:ai_1_1"）。
            endpoint: 请求端点路径。

        Returns:
            {"allowed": bool, "remaining": int, "reset_at": str, "limit": int}
        """
        if not settings.RATE_LIMIT_ENABLED:
            return {"allowed": True, "remaining": 999999, "reset_at": "", "limit": 999999}

        db = SessionLocal()
        try:
            # 匹配适用策略：精确 endpoint > 通配 endpoint
            policies = self._match_policies(db, endpoint)
            if not policies:
                # 无策略匹配时使用全局默认
                return {"allowed": True, "remaining": settings.RATE_LIMIT_DEFAULT_RPM,
                        "reset_at": "", "limit": settings.RATE_LIMIT_DEFAULT_RPM}

            now = _now()
            # 对每个匹配策略检查
            min_remaining = 999999
            earliest_reset = None

            for policy in policies:
                window_start = now - timedelta(seconds=policy.window_seconds)

                # 查找当前窗口计数器
                counter = (db.query(RateLimitCounter)
                           .filter(RateLimitCounter.policy_id == policy.id,
                                   RateLimitCounter.subject == subject,
                                   RateLimitCounter.window_start >= window_start)
                           .first())

                if counter is None:
                    # 新建窗口
                    counter = RateLimitCounter(
                        policy_id=policy.id,
                        subject=subject,
                        window_start=now,
                        request_count=1,
                    )
                    db.add(counter)
                    db.flush()
                    current_count = 1
                else:
                    counter.request_count += 1
                    current_count = counter.request_count
                    db.flush()

                remaining = max(0, policy.max_requests - current_count)
                if remaining < min_remaining:
                    min_remaining = remaining
                    earliest_reset = counter.window_start + timedelta(
                        seconds=policy.window_seconds)

                if current_count > policy.max_requests:
                    db.commit()
                    return {
                        "allowed": False,
                        "remaining": 0,
                        "reset_at": earliest_reset.isoformat() if earliest_reset else "",
                        "limit": policy.max_requests,
                        "policy": policy.name,
                    }

            db.commit()
            return {
                "allowed": True,
                "remaining": min_remaining,
                "reset_at": earliest_reset.isoformat() if earliest_reset else "",
                "limit": min_remaining,
            }
        except Exception as exc:
            db.rollback()
            logger.error("Rate limit check error: %s", exc)
            return {"allowed": True, "remaining": 0, "reset_at": "", "limit": 0}
        finally:
            db.close()

    def get_policies(self, tier: str = "") -> list[dict]:
        """获取限流策略列表（可按席位过滤）。"""
        db = SessionLocal()
        try:
            q = db.query(RateLimitPolicy).filter(RateLimitPolicy.is_active == 1)
            if tier:
                q = q.filter(RateLimitPolicy.tier.in_([tier, "*"]))
            rows = q.all()
            return [
                {
                    "id": p.id, "name": p.name, "scope": p.scope,
                    "endpoint_pattern": p.endpoint_pattern,
                    "max_requests": p.max_requests,
                    "window_seconds": p.window_seconds,
                    "tier": p.tier,
                }
                for p in rows
            ]
        finally:
            db.close()

    def create_policy(self, name: str, scope: str, max_requests: int,
                      window_seconds: int, tier: str = "free",
                      endpoint_pattern: str = "*") -> dict:
        """创建限流策略。"""
        db = SessionLocal()
        try:
            policy = RateLimitPolicy(
                name=name,
                scope=scope,
                endpoint_pattern=endpoint_pattern,
                max_requests=max_requests,
                window_seconds=window_seconds,
                tier=tier,
                is_active=1,
            )
            db.add(policy)
            db.commit()
            logger.info("Rate limit policy created: %s (max=%d window=%ds tier=%s)",
                        name, max_requests, window_seconds, tier)
            return {"ok": True, "policy_id": policy.id}
        finally:
            db.close()

    def reset_counter(self, subject: str) -> dict:
        """重置指定主体的所有限流计数器。"""
        db = SessionLocal()
        try:
            deleted = (db.query(RateLimitCounter)
                       .filter(RateLimitCounter.subject == subject)
                       .delete())
            db.commit()
            logger.info("Rate limit counters reset for subject=%s count=%d", subject, deleted)
            return {"ok": True, "reset_count": deleted}
        finally:
            db.close()

    def get_usage(self, subject: str) -> list[dict]:
        """查询指定主体的当前限流用量。"""
        db = SessionLocal()
        try:
            now = _now()
            counters = (db.query(RateLimitCounter)
                        .filter(RateLimitCounter.subject == subject)
                        .all())
            result = []
            for c in counters:
                policy = db.get(RateLimitPolicy, c.policy_id)
                if policy is None:
                    continue
                window_end = c.window_start + timedelta(seconds=policy.window_seconds)
                if window_end <= now:
                    continue  # 窗口已过期
                result.append({
                    "policy": policy.name,
                    "current": c.request_count,
                    "limit": policy.max_requests,
                    "remaining": max(0, policy.max_requests - c.request_count),
                    "window_end": window_end.isoformat(),
                })
            return result
        finally:
            db.close()

    # ---------- 内部 ----------

    def _match_policies(self, db, endpoint: str) -> list[RateLimitPolicy]:
        """匹配适用策略（精确优先 > 通配）。"""
        all_policies = (db.query(RateLimitPolicy)
                        .filter(RateLimitPolicy.is_active == 1)
                        .all())
        matched = []
        for p in all_policies:
            if p.endpoint_pattern == "*":
                matched.append(p)
            elif endpoint and p.endpoint_pattern and endpoint.startswith(
                    p.endpoint_pattern.rstrip("*")):
                matched.append(p)
        return matched


instance = RateLimiter()
