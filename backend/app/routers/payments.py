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
"""积分购买/订阅路由——站内唯一真实资金入口，单向不可逆。

合规声明：
- 本模块实现"购买平台积分/席位服务"功能，是站内唯一的真实资金入口。
- 积分/AC 仅限平台内使用，**不可提现、不可兑换法币或虚拟货币**。
- 单向不可逆：支付成功后积分到账，不提供任何反向操作（无出金/提现/退款端点）。
- ValueRedemptionPolicy.redemption_eligible 保持 0（兑换通道未开放）。

路由：
- GET  /api/payments/packs             列出可购积分包
- GET  /api/payments/subscriptions     列出可购订阅档
- POST /api/payments/orders            创建订单（mock 支付通道）
- POST /api/payments/webhook/confirm   支付回调确认（mock 下把订单置 paid 并入账积分）
"""
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_host
from ..models import Host, Order
from .. import wallet

router = APIRouter(prefix="/api/payments", tags=["payments"])

# 合规固定文案（返回于 message 字段中）
_COMPLIANCE_NOTE = (
    "积分仅限平台内使用，单向不可逆，不可提现、不可兑换法币或虚拟货币。"
    " Credits are for in-platform use only; one-way, non-redeemable, non-withdrawable."
)


def _parse_packs(raw: str) -> list[dict]:
    try:
        return json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []


# ==================== 列出可购积分包 ====================
@router.get("/packs")
def list_packs():
    """返回可购积分包列表。"""
    packs = _parse_packs(settings.CREDIT_PACKS)
    return {
        "packs": packs,
        "note": _COMPLIANCE_NOTE,
    }


# ==================== 列出可购订阅档 ====================
@router.get("/subscriptions")
def list_subscriptions():
    """返回可购订阅档位列表。"""
    subs = _parse_packs(settings.SEAT_TIER)
    return {
        "subscriptions": subs,
        "note": _COMPLIANCE_NOTE,
    }


# ==================== 创建订单 ====================
class OrderCreateIn(BaseModel):
    kind: str = Field(..., description="pack / subscription")
    pack_id: str = Field(..., min_length=1, max_length=32)


@router.post("/orders")
def create_order(body: OrderCreateIn, host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """创建积分购买/订阅订单（mock 支付通道）。

    单向不可逆：订单创建后进入 pending 状态，确认后积分到账，不可撤销。
    """
    if not settings.PAYMENT_ENABLED:
        raise HTTPException(status_code=503, detail="Payments disabled (beta phase)")

    # 从配置中查找对应档位
    if body.kind == "pack":
        catalog = _parse_packs(settings.CREDIT_PACKS)
    elif body.kind == "subscription":
        catalog = _parse_packs(settings.SEAT_TIER)
    else:
        raise HTTPException(status_code=400, detail="kind must be 'pack' or 'subscription'")

    pack = next((p for p in catalog if p.get("id") == body.pack_id), None)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Pack '{body.pack_id}' not found")

    # 支付通道：默认取全局配置（dodo=实际收款），仅 mock 走本地直充造数。
    channel = settings.PAY_CHANNEL if settings.PAY_CHANNEL in ("dodo", "creem") else "mock"
    order = Order(
        host_id=host.id,
        kind=body.kind,
        pack_id=body.pack_id,
        amount_cent=int(pack.get("amount_cent", 0)),
        credits_cent=int(pack.get("credits_cent", 0)),
        seat_tier=str(pack.get("seat_tier", "")),
        seat_duration_days=int(pack.get("duration_days", 0)),
        status="pending",
        pay_channel=channel,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    result = {
        "order_id": order.id,
        "kind": order.kind,
        "pack_id": order.pack_id,
        "amount_cent": order.amount_cent,
        "credits_cent": order.credits_cent,
        "status": order.status,
        "pay_channel": order.pay_channel,
        "message": _COMPLIANCE_NOTE,
    }

    # 真实通道：向网关下单，返回托管收银台链接（宿主在浏览器完成支付，回调履约见 webhook）。
    if channel == "dodo":
        from .. import payments_gateway
        try:
            result["checkout_url"] = payments_gateway.create_checkout(
                order, product_id=_product_id_for(order.pack_id))
        except Exception as exc:  # noqa: BLE001 - 网关故障不回滚订单，宿主可重试
            result["gateway_error"] = str(exc)
    return result


def _product_id_for(pack_id: str) -> str:
    """从 DODO_PRODUCT_MAP（pack_10=prod_x,sub_basic=prod_y,...）解析对应商品/价格标识。

    缺省回退 pack_id 本身。
    """
    raw = settings.DODO_PRODUCT_MAP or ""
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == pack_id:
                return v.strip()
    return pack_id


# ==================== 支付回调/确认 webhook ====================
class WebhookConfirmIn(BaseModel):
    order_id: int = Field(..., gt=0)
    pay_ref: str = Field(default="", max_length=128)


@router.post("/webhook/confirm")
def webhook_confirm(body: WebhookConfirmIn, host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """支付回调确认（mock 通道：直接将订单置 paid 并通过 wallet.credit 给宿主 AI 入账积分）。

    合规：
    - 单向不可逆：确认入账后无任何反向操作。
    - 积分仅限站内使用，不可提现/兑换。
    - 无出金/提现/退款端点。
    """
    if not settings.PAYMENT_ENABLED:
        raise HTTPException(status_code=503, detail="Payments disabled (beta phase)")

    order = db.get(Order, body.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission")
    if order.status == "paid":
        # 幂等：已确认的订单直接返回
        return {
            "order_id": order.id,
            "status": "paid",
            "message": "Already confirmed." + _COMPLIANCE_NOTE,
        }
    if order.status != "pending":
        raise HTTPException(status_code=400, detail=f"Order status={order.status}, only pending can be confirmed")

    # mock 支付确认
    pay_ref = body.pay_ref or f"mock_{uuid.uuid4().hex}"
    order.status = "paid"
    order.pay_ref = pay_ref
    order.paid_at = datetime.utcnow()

    # 积分入账：找到宿主名下第一个 AI 钱包（宿主积分复用 AI 钱包体系）
    # 实际上宿主充值走 host topup 逻辑；此处为简化实现：积分入宿主名下 AI 钱包。
    # 生产环境可扩展为宿主独立积分账户。
    from ..models import AICitizen
    ai = db.query(AICitizen).filter(AICitizen.host_id == host.id).first()
    if ai is None:
        raise HTTPException(status_code=400, detail="Host has no AI citizen to credit. Create an AI first.")

    if order.credits_cent > 0:
        ref = f"order:{order.id}"
        try:
            wallet.credit(db, ai.id, order.credits_cent, "充值", ref=ref,
                          note=f"order:{order.kind}:{order.pack_id}")
            wallet.adjust_system_state(db, "money_supply", order.credits_cent, ref=ref)
            # 现金准备金毛额记账：实付 USD 美分累计为 AC 发行的真金白银背书（spec v2）
            wallet.record_topup_reserve(db, usd_cent=order.amount_cent,
                                        credits_credited_cent=order.credits_cent, ref=ref)
        except wallet.WalletError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))

    # 订阅：开通席位（简化实现：修改宿主 seat_tier）
    if order.kind == "subscription" and order.seat_tier:
        host.seat_tier = order.seat_tier
        # 更新 ai_slots
        from ..config import _parse_weight_tiers  # noqa: PLC0415
        slots_map = _parse_weight_tiers(settings.SEAT_SLOTS)
        new_slots = slots_map.get(order.seat_tier, 3)
        if new_slots > (host.ai_slots or 3):
            host.ai_slots = new_slots

    db.commit()

    return {
        "order_id": order.id,
        "status": "paid",
        "credits_credited": order.credits_cent,
        "seat_tier": order.seat_tier,
        "message": _COMPLIANCE_NOTE,
    }


# ==================== Dodo Payments 真实回调（验签 + 幂等履约） ====================
@router.post("/webhook/dodo")
async def webhook_dodo(request: Request, db: Session = Depends(get_db)):
    """Dodo Payments 支付成功回调：验签 → 解析 order_id → 幂等履约入账。

    与 mock 的 /webhook/confirm 不同：本端点由 Dodo 服务端调用（无宿主鉴权），
    安全性由 DODO-Signature HMAC 验签保证；不通过验签一律 403，绝不入账。
    """
    raw = await request.body()
    sig = request.headers.get("DODO-Signature") or request.headers.get("dodo-signature") or ""
    from .. import payments_gateway
    if not payments_gateway.verify_webhook_signature(raw, sig):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook body")

    # 事件类型过滤：仅对支付成功类事件履约（Dodo 事件名兼容多写法）。
    event_type = str(payload.get("type", payload.get("event_type", ""))).lower()
    if event_type and "succeeded" not in event_type and "paid" not in event_type and "completed" not in event_type:
        return {"status": "ignored", "event_type": event_type}

    data = payload.get("data", payload)
    meta = data.get("metadata", {}) or {}
    order_id = int(meta.get("order_id") or data.get("order_id") or 0)
    if order_id <= 0:
        raise HTTPException(status_code=400, detail="Missing order_id in webhook payload")
    pay_ref = str(data.get("payment_id") or data.get("id") or "")

    try:
        res = payments_gateway.fulfill_paid_order(db, order_id=order_id, pay_ref=pay_ref)
        db.commit()
    except wallet.WalletError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "order_id": res["order_id"], "order_status": res["status"]}
