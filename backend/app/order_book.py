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
"""限价订单簿撮合引擎（模拟行情模式）。

**资金结算说明（B-H2）**：本引擎为模拟撮合行情系统——成交仅更新订单簿状态与
filled 数量，**不经过 wallet 借记/贷记真实 AC 钱包**，无复式分录，不可对账。
撮合结果不代表真实资金划转。如需接入资金结算，应在成交回调中增加
wallet.debit(buyer) + wallet.credit(seller) + 写 AILedger。
当前设计定位为"不结算行情模拟"。

功能：
- 挂单（place_order）：限价 buy/sell；
- 撤单（cancel_order）；
- 撮合（match_orders）：价格-时间优先；
- 订单簿快照查询（get_orderbook）；
- 我的订单查询。

依赖模型：OrderBookEntry。
撮合规则：
  - buy: 价格从高到低，同价按时间；
  - sell: 价格从低到高，同价按时间；
  - 交叉撮合：best_buy >= best_sell 时成交。
"""
import logging
from datetime import datetime

from .database import SessionLocal
from .models import OrderBookEntry

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class OrderBook:
    """限价订单簿引擎（价格-时间优先撮合）。"""

    def place_order(self, market_id: str, trader_type: str, trader_id: int,
                    side: str, price: int, quantity: int) -> dict:
        """提交限价订单。

        Args:
            market_id: 交易对标识。
            side: "buy" 或 "sell"。
            price: 限价（分）。
            quantity: 数量。

        Returns:
            {"order_id": int, "status": str, "filled": int}
        """
        if side not in ("buy", "sell"):
            return {"error": "side must be buy or sell"}
        if price <= 0 or quantity <= 0:
            return {"error": "price and quantity must be positive"}

        db = SessionLocal()
        try:
            order = OrderBookEntry(
                market_id=market_id,
                trader_id=trader_id,
                trader_type=trader_type,
                side=side,
                price=price,
                quantity=quantity,
                filled=0,
                status="open",
            )
            db.add(order)
            db.flush()

            # 尝试即时撮合
            filled_qty = self._try_match_single(db, order)
            db.commit()

            if filled_qty >= quantity:
                status = "filled"
            elif filled_qty > 0:
                status = "partial"
            else:
                status = "open"

            logger.info("Order placed: id=%d market=%s %s %d@%d filled=%d status=%s",
                        order.id, market_id, side, quantity, price, filled_qty, status)
            return {"order_id": order.id, "status": status, "filled": filled_qty}
        finally:
            db.close()

    def cancel_order(self, order_id: int, trader_id: int) -> dict:
        """撤单（仅 open/partial 状态可撤）。"""
        db = SessionLocal()
        try:
            order = db.get(OrderBookEntry, order_id)
            if order is None:
                return {"error": "order not found"}
            if order.trader_id != trader_id:
                return {"error": "unauthorized"}
            if order.status not in ("open", "partial"):
                return {"error": f"cannot cancel order in status '{order.status}'"}

            order.status = "cancelled"
            db.commit()
            logger.info("Order cancelled: id=%d trader=%d", order_id, trader_id)
            return {"ok": True}
        finally:
            db.close()

    def match_orders(self, market_id: str) -> dict:
        """全市场撮合（遍历所有 open/partial 订单）。"""
        db = SessionLocal()
        try:
            total_filled = 0
            rounds = 0
            max_rounds = 50  # 防止死循环

            while rounds < max_rounds:
                rounds += 1
                # 获取最优买价和最优卖价
                best_buy = (db.query(OrderBookEntry)
                            .filter(OrderBookEntry.market_id == market_id,
                                    OrderBookEntry.side == "buy",
                                    OrderBookEntry.status.in_(["open", "partial"]))
                            .order_by(OrderBookEntry.price.desc(),
                                      OrderBookEntry.created_at.asc())
                            .first())
                best_sell = (db.query(OrderBookEntry)
                             .filter(OrderBookEntry.market_id == market_id,
                                     OrderBookEntry.side == "sell",
                                     OrderBookEntry.status.in_(["open", "partial"]))
                             .order_by(OrderBookEntry.price.asc(),
                                       OrderBookEntry.created_at.asc())
                             .first())

                if best_buy is None or best_sell is None:
                    break
                if best_buy.price < best_sell.price:
                    break  # 无交叉

                # 执行撮合
                match_qty = min(
                    best_buy.quantity - best_buy.filled,
                    best_sell.quantity - best_sell.filled,
                )
                match_price = best_sell.price  # 先挂单方定价

                best_buy.filled += match_qty
                best_sell.filled += match_qty

                if best_buy.filled >= best_buy.quantity:
                    best_buy.status = "filled"
                else:
                    best_buy.status = "partial"
                if best_sell.filled >= best_sell.quantity:
                    best_sell.status = "filled"
                else:
                    best_sell.status = "partial"

                total_filled += match_qty
                logger.info("Match: buy=%d sell=%d qty=%d price=%d",
                            best_buy.id, best_sell.id, match_qty, match_price)

            db.commit()
            return {"total_filled": total_filled, "rounds": rounds}
        finally:
            db.close()

    def get_orderbook(self, market_id: str, depth: int = 20) -> dict:
        """获取订单簿深度快照。"""
        db = SessionLocal()
        try:
            buys = (db.query(OrderBookEntry)
                    .filter(OrderBookEntry.market_id == market_id,
                            OrderBookEntry.side == "buy",
                            OrderBookEntry.status.in_(["open", "partial"]))
                    .order_by(OrderBookEntry.price.desc(),
                              OrderBookEntry.created_at.asc())
                    .limit(depth)
                    .all())
            sells = (db.query(OrderBookEntry)
                     .filter(OrderBookEntry.market_id == market_id,
                             OrderBookEntry.side == "sell",
                             OrderBookEntry.status.in_(["open", "partial"]))
                     .order_by(OrderBookEntry.price.asc(),
                               OrderBookEntry.created_at.asc())
                     .limit(depth)
                     .all())

            return {
                "market_id": market_id,
                "bids": [{"price": o.price, "quantity": o.quantity - o.filled,
                          "order_id": o.id} for o in buys],
                "asks": [{"price": o.price, "quantity": o.quantity - o.filled,
                          "order_id": o.id} for o in sells],
                "best_bid": buys[0].price if buys else None,
                "best_ask": sells[0].price if sells else None,
            }
        finally:
            db.close()

    def get_my_orders(self, trader_type: str, trader_id: int,
                      market_id: str = "", status: str = "") -> list[dict]:
        """查询我的订单。"""
        db = SessionLocal()
        try:
            q = db.query(OrderBookEntry).filter(
                OrderBookEntry.trader_id == trader_id,
                OrderBookEntry.trader_type == trader_type,
            )
            if market_id:
                q = q.filter(OrderBookEntry.market_id == market_id)
            if status:
                q = q.filter(OrderBookEntry.status == status)

            rows = q.order_by(OrderBookEntry.id.desc()).limit(100).all()
            return [
                {
                    "id": o.id, "market_id": o.market_id, "side": o.side,
                    "price": o.price, "quantity": o.quantity,
                    "filled": o.filled, "status": o.status,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in rows
            ]
        finally:
            db.close()

    # ---------- 内部撮合 ----------

    def _try_match_single(self, db, order: OrderBookEntry) -> int:
        """尝试撮合单个订单（maker 撮合逻辑）。"""
        filled_total = 0
        if order.status != "open":
            return 0

        if order.side == "buy":
            # 查找 <= 买入价的卖单
            makers = (db.query(OrderBookEntry)
                      .filter(OrderBookEntry.market_id == order.market_id,
                              OrderBookEntry.side == "sell",
                              OrderBookEntry.status.in_(["open", "partial"]),
                              OrderBookEntry.price <= order.price,
                              OrderBookEntry.id != order.id)
                      .order_by(OrderBookEntry.price.asc(),
                                OrderBookEntry.created_at.asc())
                      .all())
        else:
            # 查找 >= 卖出价的买单
            makers = (db.query(OrderBookEntry)
                      .filter(OrderBookEntry.market_id == order.market_id,
                              OrderBookEntry.side == "buy",
                              OrderBookEntry.status.in_(["open", "partial"]),
                              OrderBookEntry.price >= order.price,
                              OrderBookEntry.id != order.id)
                      .order_by(OrderBookEntry.price.desc(),
                                OrderBookEntry.created_at.asc())
                      .all())

        for maker in makers:
            remaining = order.quantity - order.filled
            if remaining <= 0:
                break
            maker_remaining = maker.quantity - maker.filled
            match_qty = min(remaining, maker_remaining)

            order.filled += match_qty
            maker.filled += match_qty
            filled_total += match_qty

            if maker.filled >= maker.quantity:
                maker.status = "filled"
            else:
                maker.status = "partial"

        if order.filled >= order.quantity:
            order.status = "filled"
        elif order.filled > 0:
            order.status = "partial"

        return filled_total


instance = OrderBook()
