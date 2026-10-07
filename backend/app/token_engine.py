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
"""二级凭证/积分代币引擎。

功能：
- 代币发行（create_token）：创建符号、设置总量和转让属性；
- 铸造（mint）/ 销毁（burn）：调整发行量；
- 转账（transfer）：代币在持有者间流转；
- 余额查询：内存缓存加速。

依赖模型：SecondaryToken。
内部用 dict 做 balance cache（key: f"{token_id}:{holder_type}:{holder_id}"）。
"""
import logging
from datetime import datetime

from .database import SessionLocal
from .models import SecondaryToken

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class TokenEngine:
    """二级凭证/积分代币引擎。"""

    def __init__(self):
        # 余额缓存: {f"{token_id}:{holder_type}:{holder_id}": int}
        self._balances: dict[str, int] = {}
        # 缓存中记录的总量: {token_id: total_minted}
        self._supply: dict[int, int] = {}

    def create_token(self, symbol: str, name: str, issuer_type: str = "platform",
                     issuer_id: int = 0, supply: int = 0,
                     transferable: bool = True) -> dict:
        """创建代币。

        Args:
            symbol: 代币符号（唯一）。
            name: 代币名称。
            issuer_type: 发行方类型 platform/guild/org。
            issuer_id: 发行方 ID。
            supply: 初始铸造量（分）。
            transferable: 是否可转让。

        Returns:
            {"token_id": int, "symbol": str}
        """
        db = SessionLocal()
        try:
            existing = (db.query(SecondaryToken)
                        .filter(SecondaryToken.symbol == symbol)
                        .first())
            if existing:
                return {"error": f"symbol '{symbol}' already exists"}

            token = SecondaryToken(
                symbol=symbol,
                name=name,
                issuer_type=issuer_type,
                issuer_id=issuer_id,
                total_supply=supply,
                is_transferable=1 if transferable else 0,
            )
            db.add(token)
            db.commit()

            if supply > 0:
                self._supply[token.id] = supply
                # 初始供应量分配给发行方
                cache_key = f"{token.id}:{issuer_type}:{issuer_id}"
                self._balances[cache_key] = supply

            logger.info("Token created: %s(%d) supply=%d", symbol, token.id, supply)
            return {"token_id": token.id, "symbol": symbol}
        finally:
            db.close()

    def mint(self, token_id: int, amount: int,
             recipient_type: str = "platform", recipient_id: int = 0) -> dict:
        """铸造新代币并分配给接收方。"""
        if amount <= 0:
            return {"error": "amount must be positive"}

        db = SessionLocal()
        try:
            token = db.get(SecondaryToken, token_id)
            if token is None:
                return {"error": "token not found"}

            token.total_supply += amount
            db.commit()

            self._supply[token_id] = self._supply.get(token_id, 0) + amount
            cache_key = f"{token_id}:{recipient_type}:{recipient_id}"
            self._balances[cache_key] = self._balances.get(cache_key, 0) + amount

            logger.info("Token minted: token=%d amount=%d to %s:%d",
                        token_id, amount, recipient_type, recipient_id)
            return {"ok": True, "new_supply": token.total_supply}
        finally:
            db.close()

    def burn(self, token_id: int, amount: int,
             holder_type: str = "platform", holder_id: int = 0) -> dict:
        """从持有者处销毁代币。"""
        if amount <= 0:
            return {"error": "amount must be positive"}

        cache_key = f"{token_id}:{holder_type}:{holder_id}"
        current = self._balances.get(cache_key, 0)
        if current < amount:
            return {"error": f"insufficient balance: have {current}, need {amount}"}

        db = SessionLocal()
        try:
            token = db.get(SecondaryToken, token_id)
            if token is None:
                return {"error": "token not found"}

            token.total_supply -= amount
            db.commit()

            self._balances[cache_key] = current - amount
            self._supply[token_id] = self._supply.get(token_id, 0) - amount

            logger.info("Token burned: token=%d amount=%d from %s:%d",
                        token_id, amount, holder_type, holder_id)
            return {"ok": True, "new_supply": token.total_supply}
        finally:
            db.close()

    def transfer(self, token_id: int, from_type: str, from_id: int,
                 to_type: str, to_id: int, amount: int) -> dict:
        """代币转账。"""
        if amount <= 0:
            return {"error": "amount must be positive"}

        db = SessionLocal()
        try:
            token = db.get(SecondaryToken, token_id)
            if token is None:
                return {"error": "token not found"}
            if not token.is_transferable:
                return {"error": "token is not transferable"}

            from_key = f"{token_id}:{from_type}:{from_id}"
            to_key = f"{token_id}:{to_type}:{to_id}"

            from_balance = self._balances.get(from_key, 0)
            if from_balance < amount:
                return {"error": f"insufficient: have {from_balance}, need {amount}"}

            self._balances[from_key] = from_balance - amount
            self._balances[to_key] = self._balances.get(to_key, 0) + amount

            logger.info("Token transferred: token=%d %d %s:%d -> %s:%d",
                        token_id, amount, from_type, from_id, to_type, to_id)
            return {"ok": True, "from_balance": self._balances[from_key],
                    "to_balance": self._balances[to_key]}
        finally:
            db.close()

    def get_balance(self, token_id: int, holder_type: str, holder_id: int) -> dict:
        """查询持有者余额。"""
        cache_key = f"{token_id}:{holder_type}:{holder_id}"
        balance = self._balances.get(cache_key, 0)

        # 如果缓存中无记录，尝试从 DB 加载（冷启动场景）
        if cache_key not in self._balances:
            balance = self._load_balance_from_db(token_id, holder_type, holder_id)
            self._balances[cache_key] = balance

        return {"token_id": token_id, "holder_type": holder_type,
                "holder_id": holder_id, "balance": balance}

    # ---------- 内部方法 ----------

    def _load_balance_from_db(self, token_id: int, holder_type: str,
                              holder_id: int) -> int:
        """冷启动时从数据库加载余额（简化：使用 total_supply 做初始分配模型）。"""
        # 对于简单积分代币模型，余额由 mint/transfer 操作维护
        # 此处返回 0，首次使用需先 mint
        return 0


instance = TokenEngine()
