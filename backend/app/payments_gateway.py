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
"""Dodo Payments 网关适配（实际收款通道，Merchant of Record）。

费率（标准价，来源 dodopayments.com/pricing）：4% + 40¢/笔（国际卡 +1.5%、订阅 +0.5%）。
因此充值档位最低 $10、并采用预充值钱包，把微额任务与固定手续费解耦。

职责：
- create_checkout(order, product_id)：向 Dodo 创建支付，返回托管收银台链接（宿主浏览器完成付款）。
- verify_webhook_signature(raw_body, signature)：DODO-Signature = HMAC-SHA256(raw_body, DODO_WEBHOOK_SECRET)。
- fulfill_paid_order(db, *, order_id, pay_ref)：回调到账后的幂等履约（订单置 paid + 积分入账 +
  货币供应/现金准备金记账）。幂等：订单已 paid 直接返回，不重复入账。

合规：积分单向不可逆，不可提现/兑换法币或虚拟货币；本模块不提供任何出金/退款端点。

设计取舍：Dodo 的真实字段以官方 API 文档为准；本模块把外部调用集中在 _post_json 一处，
若生产 API 结构有出入，仅需调整此处。无 DODO_API_KEY 时 create_checkout 抛错，订单仍可重试。
"""
import hashlib
import hmac
import json

import httpx

from .config import settings
from .models import Order, AICitizen
from . import wallet

_TIMEOUT = 15.0


# ---------------- 出站 HTTP（Dodo 调用集中此处，便于按官方 schema 调整） ----------------

def _post_json(path: str, payload: dict) -> dict:
    """向 Dodo REST API POST JSON，带 Bearer 鉴权。返回解析后的 JSON。"""
    if not settings.DODO_API_KEY:
        raise RuntimeError("DODO_API_KEY not configured")
    url = settings.DODO_BASE_URL.rstrip("/") + path
    headers = {
        "Authorization": f"Bearer {settings.DODO_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def create_checkout(order: Order, *, product_id: str) -> str:
    """创建 Dodo 支付，返回托管收银台链接（payment_link）。

    product_id 来自 DODO_PRODUCT_MAP（pack_id → Dodo 商品/价格标识）。
    订单 id 写入 metadata，回调据此定位并幂等履约。
    """
    payload = {
        "payment_method": {"type": "test" if settings.DODO_ENV == "test" else "card"},
        "billing_details": {"country": "US"},
        "product_cart": {"items": [{"product_id": product_id}]},
        "metadata": {"order_id": str(order.id), "host_id": str(order.host_id)},
    }
    data = _post_json("/payments", payload)
    # Dodo 不同版本字段名可能为 payment_link / link / url，做兼容取值。
    return (data.get("payment_link") or data.get("link")
            or data.get("url") or data.get("redirect_url") or "")


# ---------------- 入站 webhook 验签 ----------------

def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """校验 DODO-Signature：HMAC-SHA256(raw_body, DODO_WEBHOOK_SECRET)。"""
    if not settings.DODO_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(settings.DODO_WEBHOOK_SECRET.encode(),
                        raw_body, hashlib.sha256).hexdigest()
    # Dodo 签名可能形如 "t=..,v0=hex"，截取十六进制段。
    got = signature.split("=", 1)[-1] if "=" in signature else signature
    return hmac.compare_digest(expected, got.strip())


# ---------------- 幂等履约 ----------------

def fulfill_paid_order(db, *, order_id: int, pay_ref: str = "") -> dict:
    """支付到账履约（幂等）。由验签通过的 Dodo webhook 调用。

    - 订单 pending → paid（已 paid 直接返回，不重复入账）；
    - 积分入宿主名下首个 AI 钱包（充值）；
    - money_supply += 到账 AC 分；cash_reserve_cent += 实付 USD 美分毛额。
    调用方（路由）负责在最外层 commit；本函数内部只 flush + 依赖 wallet 同事务语义。
    """
    order = db.get(Order, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    if order.status == "paid":
        return {"order_id": order.id, "status": "paid", "credited": 0}
    if order.status != "pending":
        raise ValueError(f"order {order.id} status={order.status}, only pending fulfillable")

    ai = db.query(AICitizen).filter(AICitizen.host_id == order.host_id).first()
    if ai is None:
        raise ValueError(f"host {order.host_id} has no AI citizen to credit")

    ref = f"order:{order.id}"
    order.status = "paid"
    order.pay_ref = pay_ref or f"dodo_{order.id}"
    order.paid_at = wallet._now()
    if order.credits_cent > 0:
        wallet.credit(db, ai.id, order.credits_cent, "充值", ref=ref,
                      note=f"dodo:{order.kind}:{order.pack_id}")
        wallet.adjust_system_state(db, "money_supply", order.credits_cent, ref=ref)
        wallet.record_topup_reserve(db, usd_cent=order.amount_cent,
                                    credits_credited_cent=order.credits_cent, ref=ref)
    db.flush()
    return {"order_id": order.id, "status": "paid", "credited": order.credits_cent}
