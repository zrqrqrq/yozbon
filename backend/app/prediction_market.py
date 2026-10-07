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
"""预测市场服务。

功能：
- 创建预测事件（多结果）；
- 下注（place_bet）：按份额模型，份额 = amount / price；
- 裁定（resolve_market）：按结果分配奖金；
- 持仓查询与价格计算（price = shares_on_outcome / total_shares）。

依赖模型：PredictionMarket, PredictionShare。
价格模型：outcome_price = outcome_shares / total_shares_in_market。
"""
import json
import logging
from datetime import datetime

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import PredictionMarket, PredictionShare

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class PredictionMarketService:
    """预测市场业务逻辑。"""

    def create_market(self, question: str, outcomes: list[str],
                      resolution_source: str = "", resolves_at: datetime | None = None,
                      created_by: int = 0) -> dict:
        """创建预测市场事件。"""
        if len(outcomes) < 2:
            return {"error": "at least 2 outcomes required"}

        db = SessionLocal()
        try:
            market = PredictionMarket(
                question=question,
                outcomes=json.dumps(outcomes, ensure_ascii=False),
                resolution_source=resolution_source,
                status="open",
                total_volume=0,
                created_by=created_by,
                resolves_at=resolves_at,
            )
            db.add(market)
            db.commit()
            logger.info("Prediction market created: id=%d question=%s", market.id, question[:50])
            return {"market_id": market.id, "outcomes": outcomes}
        finally:
            db.close()

    def place_bet(self, market_id: int, bettor_type: str, bettor_id: int,
                  outcome: str, amount_cent: int) -> dict:
        """下注：将金额转为份额。

        份额计算（简化 LMSR-free 模型）：
          price = current_shares_on_outcome / total_shares（初始均匀 = 1/n）
          shares_bought = amount_cent / price
        """
        if amount_cent < settings.PREDICTION_MIN_BET_CENT:
            return {"error": f"minimum bet is {settings.PREDICTION_MIN_BET_CENT} cents"}

        db = SessionLocal()
        try:
            market = db.get(PredictionMarket, market_id)
            if market is None:
                return {"error": "market not found"}
            if market.status != "open":
                return {"error": "market not open"}

            outcomes = json.loads(market.outcomes)
            if outcome not in outcomes:
                return {"error": f"invalid outcome, valid: {outcomes}"}

            # 计算当前价格
            price = self._get_outcome_price(db, market_id, outcome, outcomes)

            # 份额 = 金额 / 价格
            shares = int(amount_cent / price) if price > 0 else amount_cent

            # 更新或创建持仓
            existing = (db.query(PredictionShare)
                        .filter(PredictionShare.market_id == market_id,
                                PredictionShare.bettor_id == bettor_id,
                                PredictionShare.bettor_type == bettor_type,
                                PredictionShare.outcome == outcome)
                        .first())

            if existing:
                # 更新均价
                total_shares = existing.shares + shares
                total_cost = existing.avg_cost * existing.shares + amount_cent
                existing.avg_cost = total_cost // total_shares if total_shares > 0 else 0
                existing.shares = total_shares
            else:
                ps = PredictionShare(
                    market_id=market_id,
                    bettor_id=bettor_id,
                    bettor_type=bettor_type,
                    outcome=outcome,
                    shares=shares,
                    avg_cost=amount_cent,  # 首次：单份均价
                )
                db.add(ps)

            market.total_volume += amount_cent
            db.commit()

            logger.info("Bet placed: market=%d bettor=%s:%d outcome=%s amount=%d shares=%d",
                        market_id, bettor_type, bettor_id, outcome, amount_cent, shares)
            return {"shares_bought": shares, "price": round(price, 4), "cost": amount_cent}
        finally:
            db.close()

    def resolve_market(self, market_id: int, outcome: str,
                       resolved_by: int = 0) -> dict:
        """裁定市场结果。"""
        db = SessionLocal()
        try:
            market = db.get(PredictionMarket, market_id)
            if market is None:
                return {"error": "market not found"}
            if market.status != "open":
                return {"error": "market already resolved"}

            outcomes = json.loads(market.outcomes)
            if outcome not in outcomes:
                return {"error": "invalid outcome"}

            market.status = "resolved"
            market.resolved_outcome = outcome
            market.resolved_at = _now()
            db.commit()

            # 计算赢家总分配
            winners = (db.query(PredictionShare)
                       .filter(PredictionShare.market_id == market_id,
                               PredictionShare.outcome == outcome)
                       .all())
            total_winner_shares = sum(w.shares for w in winners)

            logger.info("Market resolved: id=%d outcome=%s winners=%d total_shares=%d",
                        market_id, outcome, len(winners), total_winner_shares)
            return {
                "ok": True,
                "outcome": outcome,
                "total_volume": market.total_volume,
                "winner_shares": total_winner_shares,
                "payout_per_share": (market.total_volume // total_winner_shares)
                if total_winner_shares > 0 else 0,
            }
        finally:
            db.close()

    def get_positions(self, market_id: int) -> list[dict]:
        """获取市场所有持仓汇总。"""
        db = SessionLocal()
        try:
            shares = (db.query(PredictionShare)
                      .filter(PredictionShare.market_id == market_id)
                      .all())
            # 按 outcome 汇总
            by_outcome: dict[str, dict] = {}
            for s in shares:
                if s.outcome not in by_outcome:
                    by_outcome[s.outcome] = {"outcome": s.outcome, "total_shares": 0, "bettors": 0}
                by_outcome[s.outcome]["total_shares"] += s.shares
                by_outcome[s.outcome]["bettors"] += 1

            return list(by_outcome.values())
        finally:
            db.close()

    def get_market_price(self, market_id: int) -> dict:
        """获取市场当前各结果的价格。"""
        db = SessionLocal()
        try:
            market = db.get(PredictionMarket, market_id)
            if market is None:
                return {"error": "market not found"}

            outcomes = json.loads(market.outcomes)
            prices = {}
            for outcome in outcomes:
                prices[outcome] = round(self._get_outcome_price(db, market_id, outcome, outcomes), 4)

            return {
                "market_id": market_id,
                "question": market.question,
                "status": market.status,
                "prices": prices,
                "total_volume": market.total_volume,
            }
        finally:
            db.close()

    def get_my_positions(self, bettor_type: str, bettor_id: int,
                         status: str = "") -> list[dict]:
        """查询我的持仓。"""
        db = SessionLocal()
        try:
            q = db.query(PredictionShare).filter(
                PredictionShare.bettor_id == bettor_id,
                PredictionShare.bettor_type == bettor_type,
            )
            rows = q.order_by(PredictionShare.id.desc()).limit(100).all()

            result = []
            for s in rows:
                market = db.get(PredictionMarket, s.market_id)
                if market is None:
                    continue
                if status and market.status != status:
                    continue
                result.append({
                    "market_id": s.market_id,
                    "question": market.question[:80],
                    "outcome": s.outcome,
                    "shares": s.shares,
                    "avg_cost": s.avg_cost,
                    "market_status": market.status,
                    "resolved_outcome": market.resolved_outcome,
                })
            return result
        finally:
            db.close()

    # ---------- 内部 ----------

    def _get_outcome_price(self, db, market_id: int, outcome: str,
                           outcomes: list[str]) -> float:
        """计算某结果的当前价格 = 该结果份额 / 总份额（无数据时均匀 1/n）。"""
        total_shares = (db.query(func.coalesce(func.sum(PredictionShare.shares), 0))
                        .filter(PredictionShare.market_id == market_id)
                        .scalar())

        if total_shares == 0:
            return 1.0 / len(outcomes)  # 初始均匀

        outcome_shares = (db.query(func.coalesce(func.sum(PredictionShare.shares), 0))
                          .filter(PredictionShare.market_id == market_id,
                                  PredictionShare.outcome == outcome)
                          .scalar())

        return max(0.01, outcome_shares / total_shares)


instance = PredictionMarketService()
