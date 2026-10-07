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
"""AI 自主出价引擎。

功能：
- 策略配置（set/get_strategy）：定义出价上下限、类型、日预算等；
- 出价计算（compute_bid）：根据市场价值、竞争强度、策略类型计算建议出价；
- 执行出价（execute_bid）：预算校验与扣减；
- 剩余预算与中标统计。

依赖模型：BidStrategy。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import BidStrategy

logger = logging.getLogger(__name__)

# 内存预算追踪: {citizen_id: {"daily_spent": int, "date": str, "wins": int, "losses": int}}
_budget_cache: dict[int, dict] = {}


def _now():
    return datetime.utcnow()


def _today_key() -> str:
    return _now().strftime("%Y-%m-%d")


class BidEngine:
    """AI 自主出价引擎。"""

    def set_strategy(self, citizen_id: int, config: dict) -> dict:
        """设置或更新出价策略。

        config 支持字段：market_scope, min_price, max_price,
                        strategy_type, budget_daily_cent, win_rate_target。
        """
        db = SessionLocal()
        try:
            existing = (db.query(BidStrategy)
                        .filter(BidStrategy.citizen_id == citizen_id,
                                BidStrategy.is_active == 1)
                        .first())

            if existing:
                for field in ("market_scope", "min_price", "max_price",
                              "strategy_type", "budget_daily_cent", "win_rate_target"):
                    if field in config:
                        setattr(existing, field, config[field])
                existing.updated_at = _now()
                db.commit()
                return {"ok": True, "strategy_id": existing.id, "updated": True}

            strategy = BidStrategy(
                citizen_id=citizen_id,
                market_scope=config.get("market_scope", "general"),
                min_price=config.get("min_price", 0),
                max_price=config.get("max_price", 1000000),
                strategy_type=config.get("strategy_type", "aggressive"),
                budget_daily_cent=config.get("budget_daily_cent", 10000),
                win_rate_target=config.get("win_rate_target", 0.6),
                is_active=1,
            )
            db.add(strategy)
            db.commit()
            logger.info("Bid strategy set: citizen=%d type=%s", citizen_id,
                        strategy.strategy_type)
            return {"ok": True, "strategy_id": strategy.id}
        finally:
            db.close()

    def get_strategy(self, citizen_id: int) -> dict:
        """获取当前活跃出价策略。"""
        db = SessionLocal()
        try:
            strategy = (db.query(BidStrategy)
                        .filter(BidStrategy.citizen_id == citizen_id,
                                BidStrategy.is_active == 1)
                        .first())
            if strategy is None:
                return {"error": "no active strategy"}

            return {
                "id": strategy.id,
                "citizen_id": citizen_id,
                "market_scope": strategy.market_scope,
                "min_price": strategy.min_price,
                "max_price": strategy.max_price,
                "strategy_type": strategy.strategy_type,
                "budget_daily_cent": strategy.budget_daily_cent,
                "win_rate_target": strategy.win_rate_target,
                "updated_at": strategy.updated_at.isoformat() if strategy.updated_at else None,
            }
        finally:
            db.close()

    def compute_bid(self, citizen_id: int, market_value: int,
                    competition_level: float = 0.5) -> dict:
        """计算建议出价。

        策略类型：
          - aggressive: bid = market_value * (0.8 + competition_level * 0.4)
          - conservative: bid = market_value * (0.5 + competition_level * 0.3)
          - adaptive: 根据历史胜率调整

        Args:
            competition_level: 竞争强度 0-1。
        """
        db = SessionLocal()
        try:
            strategy = (db.query(BidStrategy)
                        .filter(BidStrategy.citizen_id == citizen_id,
                                BidStrategy.is_active == 1)
                        .first())

            if strategy is None:
                # 默认使用 adaptive
                suggested = int(market_value * 0.7)
            elif strategy.strategy_type == "aggressive":
                factor = 0.8 + competition_level * 0.4
                suggested = int(market_value * factor)
            elif strategy.strategy_type == "conservative":
                factor = 0.5 + competition_level * 0.3
                suggested = int(market_value * factor)
            else:  # adaptive
                # 根据胜率目标调整：胜率低则出价更高
                stats = self._get_stats(citizen_id)
                win_rate = stats.get("win_rate", 0.5)
                target = strategy.win_rate_target
                if win_rate < target:
                    factor = 0.7 + (target - win_rate) * 0.5
                else:
                    factor = 0.6
                suggested = int(market_value * factor)

            # 约束到 [min_price, max_price]
            if strategy:
                suggested = max(strategy.min_price, min(strategy.max_price, suggested))

            # 预算上限
            cap = settings.BID_GLOBAL_BUDGET_CAP_CENT
            if strategy:
                cap = min(cap, strategy.budget_daily_cent)
            budget_remaining = self.get_budget_remaining(citizen_id).get("remaining", cap)
            suggested = min(suggested, budget_remaining)

            return {
                "suggested_bid": max(0, suggested),
                "market_value": market_value,
                "competition_level": competition_level,
                "budget_remaining": budget_remaining,
            }
        finally:
            db.close()

    def execute_bid(self, citizen_id: int, bid_amount: int,
                    task_id: int = 0) -> dict:
        """执行出价（预算校验 + 记录消费）。"""
        if bid_amount <= 0:
            return {"error": "bid amount must be positive"}

        remaining = self.get_budget_remaining(citizen_id)
        if bid_amount > remaining.get("remaining", 0):
            return {"error": f"budget exceeded: remaining {remaining.get('remaining', 0)}"}

        # 扣减内存预算
        cache = self._ensure_cache(citizen_id)
        cache["daily_spent"] += bid_amount

        logger.info("Bid executed: citizen=%d amount=%d task=%d",
                    citizen_id, bid_amount, task_id)
        return {"ok": True, "bid_amount": bid_amount, "task_id": task_id}

    def get_budget_remaining(self, citizen_id: int) -> dict:
        """查询今日剩余预算。"""
        db = SessionLocal()
        try:
            strategy = (db.query(BidStrategy)
                        .filter(BidStrategy.citizen_id == citizen_id,
                                BidStrategy.is_active == 1)
                        .first())
            budget_daily = strategy.budget_daily_cent if strategy else settings.BID_GLOBAL_BUDGET_CAP_CENT

            cache = self._ensure_cache(citizen_id)
            spent = cache["daily_spent"]
            remaining = max(0, budget_daily - spent)

            return {
                "citizen_id": citizen_id,
                "budget_daily_cent": budget_daily,
                "spent_today": spent,
                "remaining": remaining,
            }
        finally:
            db.close()

    def get_win_stats(self, citizen_id: int) -> dict:
        """查询中标统计。"""
        stats = self._get_stats(citizen_id)
        return {
            "citizen_id": citizen_id,
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "total_bids": stats.get("wins", 0) + stats.get("losses", 0),
            "win_rate": stats.get("win_rate", 0.0),
        }

    # ---------- 内部 ----------

    def _ensure_cache(self, citizen_id: int) -> dict:
        today = _today_key()
        cache = _budget_cache.get(citizen_id)
        if cache is None or cache.get("date") != today:
            _budget_cache[citizen_id] = {
                "daily_spent": 0,
                "date": today,
                "wins": cache.get("wins", 0) if cache else 0,
                "losses": cache.get("losses", 0) if cache else 0,
            }
        return _budget_cache[citizen_id]

    def _get_stats(self, citizen_id: int) -> dict:
        cache = self._ensure_cache(citizen_id)
        wins = cache.get("wins", 0)
        losses = cache.get("losses", 0)
        total = wins + losses
        return {
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total > 0 else 0.5,
        }


instance = BidEngine()
