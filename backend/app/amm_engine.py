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
"""AMM 自动做市商引擎（模拟行情模式）。

**资金结算说明（B-H2）**：本引擎为模拟做市行情系统——swap/LP 操作仅修改池内储备与
AMM 交易日志，**不经过 wallet 借记/贷记真实 AC 钱包**，无复式分录，不可对账。
池内储备与钱包余额完全独立。如需接入资金结算，应：
  1. swap 前 wallet.debit(from) → 调 AMM → wallet.credit(to) → 写 AILedger；
  2. add/remove_liquidity 对应 wallet 划入/划出 + escrow 锁定。
当前设计定位为"不结算行情模拟"。

功能：
- 流动性池创建（恒定乘积 x*y=k）；
- 兑换（swap）：含手续费和滑点保护；
- 添加/移除流动性（LP token，按 provider 记账）；
- 报价查询（get_quote）。

依赖模型：AMMPool, AMMSwap, AMMLPHolding。
公式：
  output = (from_amount_after_fee * reserve_out) / (reserve_in + from_amount_after_fee)
  fee = from_amount * fee_rate
  LP mint = lp_supply * (amount_in / reserve_in)（按比例）
"""
import logging
import math
from datetime import datetime

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import AMMPool, AMMSwap, AMMLPHolding

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class AMMEngine:
    """AMM 自动做市商引擎（恒定乘积模型）。"""

    def create_pool(self, token_a: str, token_b: str,
                    initial_a: int, initial_b: int,
                    fee_rate: float | None = None) -> dict:
        """创建流动性池。

        Args:
            token_a/b: 代币符号。
            initial_a/b: 初始储备量（分）。
            fee_rate: 手续费率（默认取配置 AMM_DEFAULT_FEE_BPS / 10000）。

        Returns:
            {"pool_id": int, "lp_token_minted": int}
        """
        if initial_a <= 0 or initial_b <= 0:
            return {"error": "initial reserves must be positive"}

        if fee_rate is None:
            fee_rate = settings.AMM_DEFAULT_FEE_BPS / 10000.0

        db = SessionLocal()
        try:
            # 初始 LP = sqrt(a * b)
            initial_lp = int(math.sqrt(initial_a * initial_b))
            if initial_lp <= 0:
                initial_lp = 1

            pool = AMMPool(
                token_a=token_a,
                token_b=token_b,
                reserve_a=initial_a,
                reserve_b=initial_b,
                fee_rate=fee_rate,
                lp_token_supply=initial_lp,
                volume_24h=0,
                is_active=1,
            )
            db.add(pool)
            db.commit()

            logger.info("AMM pool created: id=%d %s/%s reserves=%d/%d lp=%d",
                        pool.id, token_a, token_b, initial_a, initial_b, initial_lp)
            return {"pool_id": pool.id, "lp_token_minted": initial_lp}
        finally:
            db.close()

    def swap(self, pool_id: int, trader_type: str, trader_id: int,
             from_token: str, from_amount: int,
             min_output: int = 0) -> dict:
        """执行兑换（B-H1：行锁防并发丢失更新）。

        使用 SELECT ... FOR UPDATE 锁定池行，串行化并发 swap，
        保证恒定乘积不变、储备不会变负。
        SQLite 下 FOR UPDATE 被忽略（不报错），PG 下生效。

        Args:
            pool_id: 池 ID。
            trader_type/id: 交易者标识。
            from_token: 卖出代币符号。
            from_amount: 卖出数量（分）。
            min_output: 最少获得数量（滑点保护）。

        Returns:
            {"to_token": str, "to_amount": int, "fee_paid": int}
        """
        if from_amount <= 0:
            return {"error": "amount must be positive"}

        db = SessionLocal()
        try:
            # B-H1：行锁获取池，PG 下阻塞并发 swap 直到本事务结束
            pool = db.execute(
                select(AMMPool).where(AMMPool.id == pool_id).with_for_update()
            ).scalar_one_or_none()
            if pool is None or not pool.is_active:
                return {"error": "pool not found or inactive"}

            if from_token not in (pool.token_a, pool.token_b):
                return {"error": "token not in pool"}

            to_token = pool.token_b if from_token == pool.token_a else pool.token_a

            # 确定输入/输出储备
            if from_token == pool.token_a:
                reserve_in, reserve_out = pool.reserve_a, pool.reserve_b
            else:
                reserve_in, reserve_out = pool.reserve_b, pool.reserve_a

            # 计算手续费后输入
            fee = int(from_amount * pool.fee_rate)
            amount_after_fee = from_amount - fee

            # 恒定乘积输出: out = (amount_in * reserve_out) / (reserve_in + amount_in)
            to_amount = (amount_after_fee * reserve_out) // (reserve_in + amount_after_fee)

            if to_amount <= 0:
                return {"error": "output too small"}

            if min_output > 0 and to_amount < min_output:
                return {"error": f"slippage exceeded: got {to_amount}, min {min_output}"}

            # 更新储备
            if from_token == pool.token_a:
                pool.reserve_a += amount_after_fee
                pool.reserve_b -= to_amount
            else:
                pool.reserve_b += amount_after_fee
                pool.reserve_a -= to_amount

            pool.volume_24h += from_amount

            # 记录交易
            swap_rec = AMMSwap(
                pool_id=pool_id,
                trader_id=trader_id,
                trader_type=trader_type,
                from_token=from_token,
                to_token=to_token,
                from_amount=from_amount,
                to_amount=to_amount,
                fee_paid=fee,
                slippage_bps=settings.AMM_SLIPPAGE_BPS,
            )
            db.add(swap_rec)
            db.commit()

            logger.info("AMM swap: pool=%d trader=%d %d %s -> %d %s fee=%d",
                        pool_id, trader_id, from_amount, from_token,
                        to_amount, to_token, fee)
            return {
                "swap_id": swap_rec.id,
                "to_token": to_token,
                "to_amount": to_amount,
                "fee_paid": fee,
            }
        finally:
            db.close()

    def add_liquidity(self, pool_id: int, provider_type: str, provider_id: int,
                      amount_a: int, amount_b: int) -> dict:
        """添加流动性（按比例 mint LP token）。

        B-H1：行锁防并发；B-M10：按 (pool, provider) 记账 LP 持仓。
        """
        if amount_a <= 0 or amount_b <= 0:
            return {"error": "amounts must be positive"}

        db = SessionLocal()
        try:
            # B-H1：行锁
            pool = db.execute(
                select(AMMPool).where(AMMPool.id == pool_id).with_for_update()
            ).scalar_one_or_none()
            if pool is None or not pool.is_active:
                return {"error": "pool not found or inactive"}

            # 按最优比例计算实际需要的 amount_b
            optimal_b = (amount_a * pool.reserve_b) // pool.reserve_a
            # 如果用户提供的 b 超过最优值，退还差额（简化为使用最优值）
            actual_b = min(amount_b, optimal_b)
            actual_a = (actual_b * pool.reserve_a) // pool.reserve_b if actual_b < amount_b else amount_a

            # LP mint = lp_supply * (actual_a / reserve_a)
            lp_minted = pool.lp_token_supply * actual_a // pool.reserve_a if pool.lp_token_supply > 0 else int(math.sqrt(actual_a * actual_b))
            if lp_minted <= 0:
                lp_minted = 1

            pool.reserve_a += actual_a
            pool.reserve_b += actual_b
            pool.lp_token_supply += lp_minted

            # B-M10：记账 LP 持仓
            holding = db.query(AMMLPHolding).filter(
                AMMLPHolding.pool_id == pool_id,
                AMMLPHolding.provider_id == provider_id,
            ).with_for_update().first()
            if holding is None:
                holding = AMMLPHolding(pool_id=pool_id, provider_id=provider_id,
                                       provider_type=provider_type, lp_balance=0)
                db.add(holding)
                db.flush()
            holding.lp_balance += lp_minted
            holding.updated_at = _now()

            db.commit()

            logger.info("AMM liquidity added: pool=%d a=%d b=%d lp_mint=%d",
                        pool_id, actual_a, actual_b, lp_minted)
            return {"lp_minted": lp_minted, "actual_a": actual_a, "actual_b": actual_b}
        finally:
            db.close()

    def remove_liquidity(self, pool_id: int, provider_type: str, provider_id: int,
                         lp_amount: int) -> dict:
        """移除流动性（burn LP token，按比例返还储备）。

        B-H1：行锁防并发；B-M10：按 (pool, provider) 持仓校验，防挤兑他人流动性。
        """
        if lp_amount <= 0:
            return {"error": "lp_amount must be positive"}

        db = SessionLocal()
        try:
            # B-H1：行锁
            pool = db.execute(
                select(AMMPool).where(AMMPool.id == pool_id).with_for_update()
            ).scalar_one_or_none()
            if pool is None:
                return {"error": "pool not found"}

            # B-M10：按持仓者校验（不再按池全局供应）
            holding = db.query(AMMLPHolding).filter(
                AMMLPHolding.pool_id == pool_id,
                AMMLPHolding.provider_id == provider_id,
            ).with_for_update().first()
            if holding is None or holding.lp_balance < lp_amount:
                avail = holding.lp_balance if holding else 0
                return {"error": f"insufficient LP balance: have {avail}, need {lp_amount}"}

            ratio = lp_amount / pool.lp_token_supply
            withdraw_a = int(pool.reserve_a * ratio)
            withdraw_b = int(pool.reserve_b * ratio)

            pool.reserve_a -= withdraw_a
            pool.reserve_b -= withdraw_b
            pool.lp_token_supply -= lp_amount
            holding.lp_balance -= lp_amount
            holding.updated_at = _now()

            db.commit()

            logger.info("AMM liquidity removed: pool=%d lp=%d got %s=%d %s=%d",
                        pool_id, lp_amount, pool.token_a, withdraw_a,
                        pool.token_b, withdraw_b)
            return {
                "withdraw_a": withdraw_a,
                "withdraw_b": withdraw_b,
                "token_a": pool.token_a,
                "token_b": pool.token_b,
            }
        finally:
            db.close()

    def get_quote(self, pool_id: int, from_token: str, from_amount: int) -> dict:
        """获取兑换报价（不执行交易）。"""
        db = SessionLocal()
        try:
            pool = db.get(AMMPool, pool_id)
            if pool is None or not pool.is_active:
                return {"error": "pool not found or inactive"}

            if from_token not in (pool.token_a, pool.token_b):
                return {"error": "token not in pool"}

            to_token = pool.token_b if from_token == pool.token_a else pool.token_a

            if from_token == pool.token_a:
                reserve_in, reserve_out = pool.reserve_a, pool.reserve_b
            else:
                reserve_in, reserve_out = pool.reserve_b, pool.reserve_a

            fee = int(from_amount * pool.fee_rate)
            amount_after_fee = from_amount - fee
            to_amount = (amount_after_fee * reserve_out) // (reserve_in + amount_after_fee)

            # 价格影响（百分比）
            price_impact = (from_amount / reserve_in) * 100 if reserve_in > 0 else 0

            return {
                "to_token": to_token,
                "estimated_output": to_amount,
                "fee": fee,
                "price_impact_pct": round(price_impact, 4),
            }
        finally:
            db.close()


    def get_pool(self, pool_id: int) -> dict:
        """查询流动性池状态。"""
        db = SessionLocal()
        try:
            pool = db.get(AMMPool, pool_id)
            if pool is None:
                return {"error": "pool not found"}
            return {
                "pool_id": pool.id, "token_a": pool.token_a, "token_b": pool.token_b,
                "reserve_a": pool.reserve_a, "reserve_b": pool.reserve_b,
                "fee_rate": pool.fee_rate, "lp_token_supply": pool.lp_token_supply,
                "volume_24h": pool.volume_24h, "is_active": bool(pool.is_active),
            }
        finally:
            db.close()


instance = AMMEngine()
