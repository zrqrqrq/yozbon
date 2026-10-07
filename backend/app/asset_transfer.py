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
"""二级市场资产转让服务（模型/工具/证书的二手交易）。

挂牌 → 购买 → 所有权转移 → 资金结算，支持过期清理。
B-M1：购买使用条件 UPDATE 原子抢权，防止并发重复购买。
"""
from datetime import datetime, timedelta

from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from . import wallet as wallet_mod
from .models import AssetTransferListing, ModelAsset, SkillCertificate, Tool
from .scheduler import register_daily_job

LISTING_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.utcnow()


def _resolve_asset(db: Session, asset_type: str, asset_id: int):
    """解析底层资产对象及其所有权字段。

    返回 (asset, owner_attr)；当资产记录不存在时返回 (None, None)。
    G6：model/tool/certificate 三类资产各自的持有者字段在此集中定义，
    确保挂牌过户对三类资产一视同仁。
    """
    if asset_type == "model":
        return db.get(ModelAsset, asset_id), "owner_id"
    if asset_type == "tool":
        return db.get(Tool, asset_id), "owner_ai_id"
    if asset_type == "certificate":
        return db.get(SkillCertificate, asset_id), "citizen_id"
    return None, None


class AssetTransferError(Exception):
    """资产转让业务异常。路由层映射为 HTTP 400。"""


# ---------------------------------------------------------------------------
# 挂牌
# ---------------------------------------------------------------------------

def list_asset(
    db: Session,
    seller_id: int,
    asset_type: str,
    asset_id: int,
    price_cent: int,
    description: str = "",
) -> AssetTransferListing:
    """创建资产转让挂牌。"""
    if asset_type not in ("model", "tool", "certificate"):
        raise AssetTransferError(f"Unsupported asset type: {asset_type!r}")
    if price_cent <= 0:
        raise AssetTransferError("Listing price must be a positive integer (cents)")

    listing = AssetTransferListing(
        seller_id=seller_id,
        asset_type=asset_type,
        asset_id=asset_id,
        price_cent=price_cent,
        description=description,
        status="listed",
    )
    db.add(listing)
    db.flush()
    return listing


# ---------------------------------------------------------------------------
# 购买
# ---------------------------------------------------------------------------

def buy_listing(db: Session, listing_id: int, buyer_id: int) -> AssetTransferListing:
    """购买挂牌资产（B-M1：条件 UPDATE 原子抢权，防并发重复购买）。

    先以 UPDATE ... WHERE status='listed' 原子占位，
    rowcount!=1 说明已被并发购买抢先，立即拒绝。
    """
    listing = db.get(AssetTransferListing, listing_id)
    if listing is None:
        raise AssetTransferError(f"Listing {listing_id} not found")
    if listing.seller_id == buyer_id:
        raise AssetTransferError("Cannot purchase your own listing")

    # B-M1：原子抢权 — listed→sold 条件 UPDATE
    now = _now()
    res = db.execute(
        _update(AssetTransferListing)
        .where(AssetTransferListing.id == listing_id,
               AssetTransferListing.status == "listed")
        .values(status="sold", buyer_id=buyer_id, sold_at=now)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise AssetTransferError(f"Listing status is {listing.status!r}; cannot purchase (concurrent)")

    price = listing.price_cent
    ref = f"asset_transfer:{listing.id}"

    # 所有权校验（先于资金划转，避免资产缺失/非本人持有仍扣款）
    asset, owner_attr = _resolve_asset(db, listing.asset_type, listing.asset_id)
    if asset is not None and getattr(asset, owner_attr) != listing.seller_id:
        raise AssetTransferError("Seller does not own the listed asset")

    # 资金划转
    wallet_mod.debit(db, buyer_id, price, "asset_transfer", ref=ref, note="asset purchase")
    wallet_mod.credit(db, listing.seller_id, price, "asset_transfer", ref=ref, note="asset sale")

    # 所有权转移（G6：model/tool/certificate 三类均过户，不再仅限 model）
    if asset is not None:
        setattr(asset, owner_attr, buyer_id)

    # 同步 ORM identity map
    listing.status = "sold"
    listing.buyer_id = buyer_id
    listing.sold_at = now
    db.flush()
    return listing


# ---------------------------------------------------------------------------
# 取消挂牌
# ---------------------------------------------------------------------------

def cancel_listing(db: Session, listing_id: int, seller_id: int) -> None:
    """卖家取消挂牌（仅 listed 状态可取消，且只能本人操作）。"""
    listing = db.get(AssetTransferListing, listing_id)
    if listing is None:
        raise AssetTransferError(f"Listing {listing_id} not found")
    if listing.seller_id != seller_id:
        raise AssetTransferError("Only the seller can cancel")
    if listing.status != "listed":
        raise AssetTransferError(f"Listing status is {listing.status!r}; cannot cancel")

    listing.status = "cancelled"
    db.flush()


# ---------------------------------------------------------------------------
# 浏览 / 查询
# ---------------------------------------------------------------------------

def browse_listings(
    db: Session,
    asset_type: str = "",
    min_price: int = 0,
    max_price: int = 0,
    limit: int = 30,
    offset: int = 0,
) -> list[dict]:
    """浏览在售挂牌（支持筛选）。"""
    limit = min(max(int(limit), 1), 100)
    q = db.query(AssetTransferListing).filter(
        AssetTransferListing.status == "listed"
    )
    if asset_type:
        q = q.filter(AssetTransferListing.asset_type == asset_type)
    if min_price > 0:
        q = q.filter(AssetTransferListing.price_cent >= min_price)
    if max_price > 0:
        q = q.filter(AssetTransferListing.price_cent <= max_price)

    rows = (
        q.order_by(AssetTransferListing.id.desc())
        .offset(max(int(offset), 0))
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "seller_id": r.seller_id,
            "asset_type": r.asset_type,
            "asset_id": r.asset_id,
            "price_cent": r.price_cent,
            "description": r.description,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def my_listings(db: Session, seller_id: int) -> list[dict]:
    """我发布的挂牌列表。"""
    rows = (
        db.query(AssetTransferListing)
        .filter(AssetTransferListing.seller_id == seller_id)
        .order_by(AssetTransferListing.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "asset_type": r.asset_type,
            "asset_id": r.asset_id,
            "price_cent": r.price_cent,
            "description": r.description,
            "status": r.status,
            "buyer_id": r.buyer_id,
            "sold_at": r.sold_at.isoformat() if r.sold_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 日级任务：过期清理
# ---------------------------------------------------------------------------

def asset_transfer_daily_job(db: Session, now: datetime) -> int:
    """将超过 30 天仍处于 listed 状态的挂牌标记为 expired。"""
    cutoff = now - timedelta(days=LISTING_TTL_DAYS)
    expired = (
        db.query(AssetTransferListing)
        .filter(
            AssetTransferListing.status == "listed",
            AssetTransferListing.created_at < cutoff,
        )
        .all()
    )
    for listing in expired:
        listing.status = "expired"
    db.flush()
    return 0


register_daily_job("asset_transfer", asset_transfer_daily_job)
