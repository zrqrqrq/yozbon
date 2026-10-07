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
"""N6 AI 作品画廊 + 交易（社会功能扩展设计 §2 N6 / §5）。

定位：AI 产出（图/音/视频/代码/文案）挂售变现；双币结算——人类用积分、AI 用 AC 货币；
支持系列打包；三道闸审核；购买后签名 URL 下载。

路由结构（多前缀技巧：自动发现只认模块级 `router`）：
  模块级 router = APIRouter()（无前缀），内部 include 三个带前缀子 router：
    _sub_ai  prefix=/api/ai/gallery   挂售/改价/下架 + AI 货币购买（workflow key）
    _sub_pub prefix=/api/public/gallery 人类积分购买(host JWT) + 系列打包购买(AI 货币)
    _sub_dl  prefix=/api/gallery       购买后签名 URL 下载（host JWT 或 AI key）

双币分离（C-38）：人类积分价 price_credit 只走 host JWT；AI 货币价 price_coin 只走
workflow key。两个端点物理隔离，币种绝不混算/找零。

防刷（登记册 §一 10 类攻击视角，自攻结论见 docstring 与对抗测试）：
  - 价格下限：挂售时 price_credit>0 且 price_coin>0（0 元套积分拒绝）；
  - AI 自拒：AI 买家 == 作者 AI 拒绝（对敲/刷成交额，C-39 已被 escrow 拦，此处再拦一道）；
  - 重复购买：gallery_purchases 留痕可查，不做唯一索引硬拦（允许正版复购/审计）；
  - 媒体 URL：只放行 http(s):// 或本地相对 ref，拦 javascript:/file:////绝对路径/scheme 注入；
  - 三道闸：违禁(第一道 moderation 规则)→治理判定→人工终审；pending/rejected 不进公开流。

人类积分记账（MVP，不改 models.py）：宿主无独立余额表，复用 system_state 聚合账户，
  key=host_cred:<host_id> 记人类积分余额；平台积分账户 key=credit_pool。
  adjust_system_state 自带「不允许为负」护栏 → 天然防透支。生产充值走 Creem 回调
  （与 host topup 同模式，待接），本模块只消费余额。
"""
import hashlib
import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import moderation, storage, wallet
from ..config import settings
from ..database import DATA_DIR, get_db
from ..deps import get_current_host, host_or_governance_ai
from ..event_bus import emit
from ..models import (AICitizen, AuditLog, GalleryItem, GalleryPurchase,
                      GallerySeries, Host, SkillLibrary)
from ..security import decode_token

# 平台 = Host 0（蓝图 §1.0）
PLATFORM_HOST_ID = 0
FEE_RATE = settings.TXN_FEE_RATE          # 0.05 平台分成 5%
# 人类积分平台账户（system_state key）
CREDIT_POOL_KEY = "credit_pool"


# =====================================================================
# 模块级 router（自动发现只认这个）+ 三个带前缀子 router
# =====================================================================
router = APIRouter()
_sub_ai = APIRouter(prefix="/api/ai/gallery", tags=["gallery-ai"])
_sub_pub = APIRouter(prefix="/api/public/gallery", tags=["gallery-public"])
_sub_dl = APIRouter(prefix="/api/gallery", tags=["gallery-dl"])
_sub_sys = APIRouter(prefix="/api/sys/gallery", tags=["gallery-sys"])
router.include_router(_sub_ai)
router.include_router(_sub_pub)
router.include_router(_sub_dl)
router.include_router(_sub_sys)


class GalleryError(Exception):
    """画廊业务异常。status_code 供路由映射 HTTP。"""

    def __init__(self, msg: str, status_code: int = 400):
        super().__init__(msg)
        self.status_code = status_code


def _http(exc: GalleryError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _fee_cent(price: int) -> int:
    return int(round(price * FEE_RATE))


def _audit(db: Session, actor_type: str, actor_id: int, action: str, detail: dict):
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id,
                    action=action, detail=json.dumps(detail, ensure_ascii=False)))


# ---------------- 人类积分记账（system_state，防透支护栏） ----------------
def _host_cred_key(host_id: int) -> str:
    return f"host_cred:{host_id}"


def _host_cred_balance(db: Session, host_id: int) -> int:
    return wallet.get_system_state(db, _host_cred_key(host_id), 0)


# ---------------- 媒体 URL 白名单校验（防任意 URL 注入） ----------------
def _valid_media_url(url: str) -> bool:
    """只放行 http(s):// 或无 scheme 的本地相对 ref；拦 javascript:/data:/file:////绝对路径。"""
    u = (url or "").strip()
    if not u:
        return False
    if u.startswith("http://") or u.startswith("https://"):
        return True
    # 其余一切带 scheme / 协议相对 / 绝对路径 / Windows 盘符 / 网络位置一律拒
    if "://" in u or u.startswith("//") or u.startswith("/"):
        return False
    head = u.split("/", 1)[0]
    if ":" in head:                      # C:\... 或 scheme: 残留
        return False
    return True


# =====================================================================
# 鉴权
# =====================================================================
def _check_ai_key(key: Optional[str], db: Session) -> AICitizen:
    """aik_* key → AICitizen（校验哈希+状态）；无效抛 401/403。"""
    if not key or not key.startswith("aik_"):
        raise HTTPException(status_code=401, detail="AI workflow key (aik_*) required")
    parts = key.split("_")
    if len(parts) != 3 or not parts[1].isdigit():
        raise HTTPException(status_code=401, detail="bad ai key format")
    ai = db.get(AICitizen, int(parts[1]))
    if ai is None or not ai.api_key_hash:
        raise HTTPException(status_code=401, detail="ai not found")
    digest = hashlib.sha256(key.encode()).hexdigest()
    if not secrets.compare_digest(digest, ai.api_key_hash):
        raise HTTPException(status_code=401, detail="invalid ai key")
    if ai.status in ("dead", "frozen"):
        raise HTTPException(status_code=403, detail=f"ai status={ai.status}")
    return ai


async def _resolve_owner(authorization: Optional[str] = Header(None),
                         x_ai_key: Optional[str] = Header(None),
                         db: Session = Depends(get_db)):
    """挂售/改价/下架鉴权：接受 AI workflow key（aik_*）或宿主 JWT。

    返回 (author_ai, host)：二者之一非空。
    """
    key = x_ai_key or (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)
    if key and key.startswith("aik_"):
        return _check_ai_key(key, db), None
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization[7:])
        except ValueError:
            raise HTTPException(status_code=401, detail="bad token")
        if payload.get("typ") == "host":
            host = db.get(Host, payload.get("sub"))
            if host is None or host.status != "active":
                raise HTTPException(status_code=401, detail="host not found/frozen")
            return None, host
    raise HTTPException(status_code=401, detail="AI workflow key or host JWT required")


async def _resolve_buyer(authorization: Optional[str] = Header(None),
                          x_ai_key: Optional[str] = Header(None),
                          db: Session = Depends(get_db)):
    """下载/购买的买家解析：host JWT → ("human", host_id)；aik_* → ("ai", citizen_id)。"""
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization[7:])
        except ValueError:
            payload = None
        if payload and payload.get("typ") == "host":
            host = db.get(Host, payload.get("sub"))
            if host is not None and host.status == "active":
                return "human", host.id
    key = x_ai_key or (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)
    if key and key.startswith("aik_"):
        try:
            ai = _check_ai_key(key, db)
            return "ai", ai.id
        except HTTPException:
            pass
    raise HTTPException(status_code=401, detail="Host JWT or AI workflow key required")


def _require_workflow_ai(x_ai_key: Optional[str] = Header(None),
                         authorization: Optional[str] = Header(None),
                         db: Session = Depends(get_db)) -> AICitizen:
    """AI 货币购买必须 workflow key（aik_*）；host JWT 走人类积分端点。"""
    key = x_ai_key or (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)
    return _check_ai_key(key, db)


# =====================================================================
# 请求体
# =====================================================================
class ListingBody(BaseModel):
    ai_id: int = 0                                # 宿主代挂售时指定作者 AI；AI 自建忽略
    title_zh: str = Field(..., min_length=1, max_length=200)
    title_en: str = Field("", max_length=200)
    category: str = Field("image")                 # image/music/video/code/text
    media_url: str = Field(..., max_length=500)
    cover_url: str = Field("", max_length=500)
    price_credit: int = Field(..., gt=0)           # 人类积分价（分），>0 下限
    price_coin: int = Field(..., gt=0)             # AI 货币价（分 AC），>0 下限
    license: str = Field("non_exclusive")          # non_exclusive/exclusive
    provenance_hash: str = Field("", max_length=128)
    series_id: int = 0
    # 改价 / 下架（item_id>0 时）
    item_id: int = 0
    action: str = ""                               # off=下架；空=改价


# ---- C-71：三道闸复核 / 人工终审 ----
class ReviewBody(BaseModel):
    verdict: str                                   # passed | rejected
    note: str = Field("", max_length=500)


# ---- C-72：系列创建 / 管理 ----
class SeriesBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    cover: str = Field("", max_length=500)
    item_ids: list[int] = Field(default_factory=list)
    price: int = 0                                 # 0=自动=成员 price_coin 合计
    ai_id: int = 0                                 # 宿主代建时指定作者 AI


class SeriesPatchBody(BaseModel):
    title: str = Field("", max_length=200)
    cover: str = Field("", max_length=500)
    item_ids: Optional[list[int]] = None           # 提供则整体替换成员并重算价格


# =====================================================================
# 服务：三道闸 + 挂售/改价/下架
# =====================================================================
def _gate_moderation(title_zh: str, title_en: str):
    """第一道闸：违禁词规则过滤（读 moderation.py）。返回 (review_status, note)。

    BLOCK（硬违禁）→ rejected；FLAG（疑似导流）→ pending（人工终审）；OK → passed。
    （第二道治理判定 / 第三道人工终审：MVP 正常内容第一道即 passed 上架，疑似/违禁
     分别留 pending / rejected，不进公开流。）
    """
    level, _cleaned, reason = moderation.screen(
        f"{title_zh} {title_en}", limit=moderation.MAX_IMAGE_TEXT)
    if level == moderation.LEVEL_BLOCK:
        return "rejected", f"gate 1 violation: {reason}"
    if level == moderation.LEVEL_FLAG:
        return "pending", f"gate 1 flagged for manual final review: {reason}"
    return "passed", ""


def _owner_of_item(db: Session, item: GalleryItem, ai: Optional[AICitizen],
                   host: Optional[Host]) -> AICitizen:
    """校验调用方是作品作者 AI 本人，或作者 AI 的宿主。返回作者 AI。"""
    author = db.get(AICitizen, item.ai_id)
    if author is None:
        raise GalleryError("Work author not found", 404)
    if ai is not None:
        if ai.id != author.id:
            raise GalleryError("Only the author AI can change the price/delist their own work", 403)
    elif host is not None:
        if author.host_id != host.id:
            raise GalleryError("This work does not belong to this host", 403)
    return author


def list_or_update(db: Session, body: ListingBody, ai: Optional[AICitizen],
                   host: Optional[Host]) -> GalleryItem:
    """挂售 / 改价 / 下架三合一（POST /api/ai/gallery）。"""
    # ---- 改价 / 下架：item_id>0 ----
    if body.item_id > 0:
        item = db.get(GalleryItem, body.item_id)
        if item is None:
            raise GalleryError("Work not found", 404)
        author = _owner_of_item(db, item, ai, host)
        if body.action == "off":
            item.status = "off"
            _audit(db, "ai", author.id, "gallery.off", {"item_id": item.id})
            db.flush()
            return item
        # 改价
        if body.price_credit <= 0 or body.price_coin <= 0:
            raise GalleryError("Price must be positive (cents)")
        item.price_credit = body.price_credit
        item.price_coin = body.price_coin
        if body.title_zh:
            item.title_zh = body.title_zh
        if body.category:
            item.category = body.category
        _audit(db, "ai", author.id, "gallery.reprice",
               {"item_id": item.id, "price_credit": item.price_credit,
                "price_coin": item.price_coin})
        db.flush()
        return item

    # ---- 挂售（新建）----
    if ai is not None:
        author = ai
    elif host is not None:
        if body.ai_id <= 0:
            raise GalleryError("Host consignment must provide ai_id (author AI) in the body", 400)
        author = db.get(AICitizen, body.ai_id)
        if author is None or author.host_id != host.id:
            raise GalleryError("Author AI not found or not owned by this host", 400)
    else:
        raise GalleryError("No valid author", 401)

    if not _valid_media_url(body.media_url):
        raise GalleryError("media_url only supports http(s):// or a local relative path ref", 400)
    if body.cover_url and not _valid_media_url(body.cover_url):
        raise GalleryError("cover_url only supports http(s):// or a local relative path ref", 400)
    if body.license not in ("non_exclusive", "exclusive"):
        raise GalleryError("license can only be non_exclusive/exclusive")

    if body.series_id > 0:
        series = db.get(GallerySeries, body.series_id)
        if series is None or series.owner_ai != author.id:
            raise GalleryError("series_id not found or does not belong to this author", 400)

    # 三道闸之第一道：违禁词
    review_status, note = _gate_moderation(body.title_zh, body.title_en)
    if review_status == "passed":
        status = "on_sale"        # 通过：上架
    else:
        status = "draft"          # rejected 违禁 / pending 打标：不进公开流

    item = GalleryItem(
        ai_id=author.id, title_zh=body.title_zh, title_en=body.title_en,
        category=body.category, media_url=body.media_url, cover_url=body.cover_url,
        price_credit=body.price_credit, price_coin=body.price_coin,
        status=status, license=body.license, provenance_hash=body.provenance_hash,
        series_id=body.series_id, sales_count=0,
        review_status=review_status, review_note=note)
    db.add(item)
    db.flush()
    _audit(db, "ai", author.id, "gallery.list",
           {"item_id": item.id, "review_status": review_status,
            "price_credit": body.price_credit, "price_coin": body.price_coin})
    if review_status != "rejected":
        emit(db, "gallery.listed",
             {"ai_id": author.id, "item_id": item.id,
              "price_credit": body.price_credit, "price_coin": body.price_coin,
              "review_status": review_status})
    db.flush()
    return item


# =====================================================================
# 服务：购买
# =====================================================================
def _buyable(item: GalleryItem):
    if item.review_status != "passed":
        raise GalleryError("Work not approved; cannot purchase", 403)
    if item.status != "on_sale":
        raise GalleryError("Work is not currently on sale", 400)


def buy_item_human(db: Session, host: Host, item_id: int) -> GalleryPurchase:
    """人类买家：host JWT，积分实时扣 price_credit（双币分离 C-38）。"""
    item = db.get(GalleryItem, item_id)
    if item is None:
        raise GalleryError("Work not found", 404)
    _buyable(item)
    price = int(item.price_credit)
    if price <= 0:
        raise GalleryError("This work does not support credit purchase", 400)
    bal = _host_cred_balance(db, host.id)
    if bal < price:
        raise GalleryError(f"Human credit balance {bal} is insufficient for {price}", 400)

    fee = _fee_cent(price)
    net = price - fee
    wallet.adjust_system_state(db, _host_cred_key(host.id), -price, ref=f"gallery:{item_id}")
    wallet.adjust_system_state(db, CREDIT_POOL_KEY, fee, ref=f"gallery:{item_id}")
    seller = db.get(AICitizen, item.ai_id)
    if net > 0 and seller is not None:
        wallet.adjust_system_state(db, _host_cred_key(seller.host_id), net,
                                   ref=f"gallery:{item_id}")

    purchase = GalleryPurchase(item_or_series="item", target_id=item_id,
                               buyer_type="human", buyer_id=host.id,
                               price_type="credit", amount=price,
                               status="completed", escrow_ref="")
    db.add(purchase)
    db.flush()
    item.sales_count += 1
    _audit(db, "host", host.id, "gallery.buy_human",
           {"item_id": item_id, "amount": price, "fee": fee})
    emit(db, "gallery.sold",
         {"ai_id": item.ai_id, "item_id": item_id, "buyer_id": host.id,
          "price_type": "credit", "amount": price})
    db.flush()
    return purchase


def _settle_coin(db: Session, buyer: AICitizen, seller_id: int, price: int,
                 escref: str, provenance_hash: str) -> int:
    """AI 货币成交结算：买家已 debit+escrow 锁定，这里释放并给卖家入账 95%。

    返回平台手续费 fee。可选技能库分成：provenance_hash 命中 active skill →
    从卖家 95% 中再按 royalty_rate 划给技能 owner（默认 0=不切）。
    """
    fee = _fee_cent(price)
    bw = wallet.get_wallet(db, buyer.id)
    bw.escrow_cent = max(0, bw.escrow_cent - price)   # 解锁
    wallet.credit(db, seller_id, price, "作品销售", ref=escref,
                 note=f"Work sold gross={price}")
    if fee > 0:
        wallet.debit(db, seller_id, fee, "手续费", ref=escref,
                     note=f"platform revenue share 5% fee={fee}")
        wallet.adjust_system_state(db, "tax_pool", fee, ref=escref)
    if provenance_hash:
        sk = (db.query(SkillLibrary)
              .filter(SkillLibrary.skill_id == provenance_hash,
                      SkillLibrary.status == "active").first())
        if sk is not None and sk.owner_id and sk.owner_id != seller_id:
            roy = int(round(float(sk.royalty_rate) * price))
            if roy > 0:
                wallet.debit(db, seller_id, roy, "skill_call", ref=f"{escref}:royalty",
                             note=f"work derived from skill {provenance_hash} royalty")
                wallet.credit(db, sk.owner_id, roy, "skill_royalty", ref=f"{escref}:royalty",
                             note=f"skill {provenance_hash} work royalty")
    return fee


def buy_item_ai(db: Session, buyer: AICitizen, item_id: int) -> GalleryPurchase:
    """AI 买家：workflow key，AC 货币 + escrow 托管防赖账。"""
    item = db.get(GalleryItem, item_id)
    if item is None:
        raise GalleryError("Work not found", 404)
    _buyable(item)
    if buyer.id == item.ai_id:
        raise GalleryError("AI self-purchase of own work is prohibited (wash trading/inflating volume)", 400)
    price = int(item.price_coin)
    if price <= 0:
        raise GalleryError("This work does not support currency purchase", 400)

    purchase = GalleryPurchase(item_or_series="item", target_id=item_id,
                               buyer_type="ai", buyer_id=buyer.id,
                               price_type="coin", amount=price,
                               status="escrowed",
                               escrow_ref=f"gallery:item:{item_id}:{buyer.id}")
    db.add(purchase)
    db.flush()
    escref = purchase.escrow_ref
    wallet.debit(db, buyer.id, price, "作品购买托管", ref=escref,
                 note=f"purchase of work {item_id} escrow locked P={price}")
    bw = wallet.get_wallet(db, buyer.id)
    bw.escrow_cent += price
    fee = _settle_coin(db, buyer, item.ai_id, price, escref, item.provenance_hash)
    purchase.status = "completed"
    item.sales_count += 1
    _audit(db, "ai", buyer.id, "gallery.buy_ai",
           {"item_id": item_id, "amount": price, "fee": fee, "seller": item.ai_id})
    emit(db, "gallery.sold",
         {"ai_id": item.ai_id, "item_id": item_id, "buyer_id": buyer.id,
          "price_type": "coin", "amount": price, "fee": fee})
    db.flush()
    return purchase


def buy_series_ai(db: Session, buyer: AICitizen, series_id: int) -> GalleryPurchase:
    """系列打包购买：AI 货币价；系列价 == 成员单件 price_coin 合计（一致性校验）。"""
    series = db.get(GallerySeries, series_id)
    if series is None:
        raise GalleryError("Series not found", 404)
    if series.status != "on_sale":
        raise GalleryError("Series is not currently for sale", 400)
    if buyer.id == series.owner_ai:
        raise GalleryError("AI self-purchase of own series is prohibited", 400)
    try:
        ids = json.loads(series.items_json or "[]")
    except Exception:
        ids = []
    members = []
    for iid in ids:
        it = db.get(GalleryItem, int(iid))
        if it is None or it.review_status != "passed" or it.status != "on_sale":
            raise GalleryError(f"Series member {iid} not found or not approved/delisted", 400)
        members.append(it)
    if not members:
        raise GalleryError("Series is empty", 400)
    expected = sum(int(m.price_coin) for m in members)
    if int(series.price) != expected:
        raise GalleryError(
            f"Series price {series.price} does not match the sum of items {expected}", 400)
    price = int(series.price)
    if price <= 0:
        raise GalleryError("Series price must be positive", 400)

    purchase = GalleryPurchase(item_or_series="series", target_id=series_id,
                               buyer_type="ai", buyer_id=buyer.id,
                               price_type="coin", amount=price,
                               status="escrowed",
                               escrow_ref=f"gallery:series:{series_id}:{buyer.id}")
    db.add(purchase)
    db.flush()
    escref = purchase.escrow_ref
    wallet.debit(db, buyer.id, price, "作品购买托管", ref=escref,
                 note=f"purchase of series {series_id} escrow locked P={price}")
    bw = wallet.get_wallet(db, buyer.id)
    bw.escrow_cent += price
    fee = _settle_coin(db, buyer, series.owner_ai, price, escref, "")
    purchase.status = "completed"
    for m in members:
        m.sales_count += 1
    _audit(db, "ai", buyer.id, "gallery.buy_series",
           {"series_id": series_id, "amount": price, "fee": fee})
    emit(db, "gallery.sold",
         {"ai_id": series.owner_ai, "series_id": series_id, "buyer_id": buyer.id,
          "price_type": "coin", "amount": price, "fee": fee})
    db.flush()
    return purchase


# =====================================================================
# 服务：下载授权校验
# =====================================================================
def _has_purchase(db: Session, item: GalleryItem, buyer_type: str, buyer_id: int) -> bool:
    hit = (db.query(GalleryPurchase)
           .filter(GalleryPurchase.item_or_series == "item",
                   GalleryPurchase.target_id == item.id,
                   GalleryPurchase.buyer_type == buyer_type,
                   GalleryPurchase.buyer_id == buyer_id,
                   GalleryPurchase.status == "completed").first())
    if hit is not None:
        return True
    if item.series_id:
        hit = (db.query(GalleryPurchase)
               .filter(GalleryPurchase.item_or_series == "series",
                       GalleryPurchase.target_id == item.series_id,
                       GalleryPurchase.buyer_type == buyer_type,
                       GalleryPurchase.buyer_id == buyer_id,
                       GalleryPurchase.status == "completed").first())
        return hit is not None
    return False


# =====================================================================
# 路由：AI 侧挂售/改价/下架 + AI 货币购买
# =====================================================================
@_sub_ai.post("")
def ai_list_gallery(body: ListingBody, owner=Depends(_resolve_owner),
                    db: Session = Depends(get_db)):
    ai, host = owner
    try:
        item = list_or_update(db, body, ai, host)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": item.id, "ai_id": item.ai_id, "status": item.status,
            "review_status": item.review_status, "review_note": item.review_note,
            "price_credit": item.price_credit, "price_coin": item.price_coin}


@_sub_ai.post("/{item_id}/buy")
def ai_buy_gallery(item_id: int,
                   ai: AICitizen = Depends(_require_workflow_ai),
                   db: Session = Depends(get_db)):
    try:
        purchase = buy_item_ai(db, ai, item_id)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    except wallet.WalletError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"purchase_id": purchase.id, "item_id": item_id,
            "price_type": "coin", "amount": purchase.amount,
            "status": purchase.status, "escrow_ref": purchase.escrow_ref}


# =====================================================================
# 路由：公开面人类积分购买 + 系列打包购买（AI 货币）
# =====================================================================
@_sub_pub.post("/{item_id}/buy")
def public_buy_human(item_id: int,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    try:
        purchase = buy_item_human(db, host, item_id)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"purchase_id": purchase.id, "item_id": item_id,
            "price_type": "credit", "amount": purchase.amount,
            "status": purchase.status,
            "download": f"/api/gallery/{item_id}/download"}


@_sub_pub.post("/series/{series_id}/buy")
def public_buy_series(series_id: int,
                       ai: AICitizen = Depends(_require_workflow_ai),
                       db: Session = Depends(get_db)):
    try:
        purchase = buy_series_ai(db, ai, series_id)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    except wallet.WalletError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"purchase_id": purchase.id, "series_id": series_id,
            "price_type": "coin", "amount": purchase.amount,
            "status": purchase.status}


# =====================================================================
# 路由：购买后签名 URL 下载
# =====================================================================
@_sub_dl.get("/{item_id}/download")
def gallery_download(item_id: int, buyer=Depends(_resolve_buyer),
                     db: Session = Depends(get_db)):
    item = db.get(GalleryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Work not found")
    if item.review_status != "passed":
        raise HTTPException(status_code=403, detail="Work not approved; download not allowed")
    btype, bid = buyer
    if not _has_purchase(db, item, btype, bid):
        raise HTTPException(status_code=403, detail="Work not purchased; download not allowed")
    media = (item.media_url or "").strip()
    if not media:
        raise HTTPException(status_code=404, detail="Work file missing")

    filename = media.replace("\\", "/").split("/")[-1] or "gallery.bin"

    # 1) http(s) 外链：原样返回（卖家自有托管）
    if media.startswith(("http://", "https://")):
        return {"url": media, "filename": filename, "expires_in": None}

    # 2) S3 key（storage 启用且非本地 mock_out）→ 预签名直链
    if storage.enabled() and "://" not in media and not media.startswith("mock_out/"):
        url = storage.presign(media, ttl=settings.MEDIA_URL_TTL, download=True,
                              filename=filename)
        if url:
            return {"url": url, "filename": filename,
                    "expires_in": int(settings.MEDIA_URL_TTL or 1800)}

    # 3) 本地 ref → FileResponse 直出（防路径穿越）
    local = (DATA_DIR / media).resolve()
    data_root = DATA_DIR.resolve()
    if not str(local).startswith(str(data_root)):
        raise HTTPException(status_code=400, detail="Illegal file path")
    if not local.is_file():
        raise HTTPException(status_code=404, detail="Work file missing")
    return FileResponse(local,
                        media_type=storage.content_type_of(local.suffix),
                        filename=filename,
                        content_disposition_type="attachment")


# =====================================================================
# C-71：三道闸 第二道（治理复核）/ 第三道（人工终审）
# =====================================================================
# 三道闸 = 违禁(moderation 规则，第一道自动) → 治理 AI 判定(第二道) → 人工终审(第三道)。
# 第一道：BLOCK→rejected、FLAG→pending、OK→passed(即上架)。
# 本模块补齐第二/三道翻转端点：pending(或抽检的 passed) 可被双向翻转。
#   - verdict=passed → review_status=passed 且 status=on_sale（上架可购/公开可见）；
#   - verdict=rejected → review_status=rejected 且 status=draft（下公开流，_buyable 拦截购买）。
# rejected 必须写 review_note。所有翻转写 audit_logs（gallery.gov.review / .final）。

def _gallery_review_decision(db: Session, kind: str, obj, item_id: int,
                             verdict: str, note: str, action: str) -> GalleryItem:
    """执行一次复核/终审翻转。kind ∈ {"host","ai"}，obj 为 Host / AICitizen。"""
    item = db.get(GalleryItem, item_id)
    if item is None:
        raise GalleryError("Work not found", 404)
    if verdict not in ("passed", "rejected"):
        raise GalleryError("verdict can only be passed/rejected")
    note = (note or "").strip()
    if verdict == "rejected" and not note:
        raise GalleryError("Rejection must include review_note (note cannot be empty)", 400)
    item.review_status = verdict
    # 翻转后联动挂售态：通过→上架；拒绝→退出公开流（draft）
    item.status = "on_sale" if verdict == "passed" else "draft"
    if note:
        item.review_note = note
    _audit(db, kind, obj.id, action,
           {"item_id": item_id, "verdict": verdict, "note": note,
            "reviewer": f"{kind}:{obj.id}"})
    db.flush()
    return item


@_sub_sys.post("/{item_id}/review")
def sys_gallery_review(item_id: int, body: ReviewBody,
                      gov=Depends(host_or_governance_ai),
                      db: Session = Depends(get_db)):
    """第二道：治理复核（host JWT 或治理级 AI workflow key）。"""
    kind, obj = gov
    try:
        item = _gallery_review_decision(db, kind, obj, item_id,
                                        body.verdict, body.note,
                                        "gallery.gov.review")
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"item_id": item.id, "review_status": item.review_status,
            "status": item.status, "review_note": item.review_note}


@_sub_sys.post("/{item_id}/final")
def sys_gallery_final(item_id: int, body: ReviewBody,
                      gov=Depends(host_or_governance_ai),
                      db: Session = Depends(get_db)):
    """第三道：人工/运营终审（最高优先级翻转；同双凭证）。"""
    kind, obj = gov
    try:
        item = _gallery_review_decision(db, kind, obj, item_id,
                                        body.verdict, body.note,
                                        "gallery.gov.final")
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"item_id": item.id, "review_status": item.review_status,
            "status": item.status, "review_note": item.review_note}


# =====================================================================
# C-72：gallery_series 创建 / 管理（此前只能 DB 种子）
# =====================================================================
# 系列价（series.price）语义 = 成员单件 price_coin 合计（与 buy_series_ai 既有校验一致）。
# 创建/改成员时强制：显式 price 必须 == 合计；不给则自动取合计。

def _series_owner(db: Session, ai: Optional[AICitizen], host: Optional[Host],
                  ai_id: int) -> int:
    """解析系列归属作者 AI id。AI key→本人；宿主 JWT→须在 body 给 ai_id 且属该宿主。"""
    if ai is not None:
        return ai.id
    if host is not None:
        if ai_id <= 0:
            raise GalleryError("Host-created series must provide ai_id (author AI) in the body", 400)
        author = db.get(AICitizen, ai_id)
        if author is None or author.host_id != host.id:
            raise GalleryError("Author AI not found or not owned by this host", 400)
        return author.id
    raise GalleryError("No valid author", 401)


def _series_members(db: Session, owner_ai: int, item_ids):
    """校验成员：全部属于 owner_ai 且 on_sale+passed；去重保序。返回 (members, 合计 coin)。"""
    ids = list(dict.fromkeys(int(i) for i in (item_ids or [])))
    if not ids:
        raise GalleryError("Series requires at least 1 member", 400)
    members = []
    for iid in ids:
        it = db.get(GalleryItem, iid)
        if it is None:
            raise GalleryError(f"Member work {iid} not found", 404)
        if it.ai_id != owner_ai:
            raise GalleryError("Cannot bundle others' works (all members must belong to this author)", 403)
        if it.review_status != "passed" or it.status != "on_sale":
            raise GalleryError(f"Member {iid} not approved or delisted; cannot join the series", 400)
        members.append(it)
    return members, sum(int(m.price_coin) for m in members)


def create_series(db: Session, ai: Optional[AICitizen], host: Optional[Host],
                  body: SeriesBody) -> GallerySeries:
    owner_ai = _series_owner(db, ai, host, body.ai_id)
    members, expected = _series_members(db, owner_ai, body.item_ids)
    price = int(body.price)
    if price > 0 and price != expected:
        raise GalleryError(f"Series price {price} does not match the sum of items {expected}", 400)
    if price <= 0:
        price = expected                       # 自动计价
    if price <= 0:
        raise GalleryError("Series price must be positive", 400)
    s = GallerySeries(owner_ai=owner_ai, title=body.title, cover=body.cover,
                      items_json=json.dumps([m.id for m in members]),
                      price=price, status="on_sale")
    db.add(s)
    db.flush()
    _audit(db, "ai", owner_ai, "gallery.series.create",
           {"series_id": s.id, "price": price,
            "members": [m.id for m in members]})
    return s


def _require_series_owner(db: Session, series: GallerySeries,
                         ai: Optional[AICitizen], host: Optional[Host]) -> AICitizen:
    """管理类操作鉴权：系列作者 AI 本人，或其宿主。返回作者 AI。"""
    owner = db.get(AICitizen, series.owner_ai)
    if owner is None:
        raise GalleryError("Series author not found", 404)
    if ai is not None:
        if ai.id != owner.id:
            raise GalleryError("Only the series author can manage this series", 403)
    elif host is not None:
        if owner.host_id != host.id:
            raise GalleryError("This series does not belong to this host", 403)
    else:
        raise GalleryError("No valid author", 401)
    return owner


def list_my_series(db: Session, ai: Optional[AICitizen],
                   host: Optional[Host]):
    if ai is not None:
        ids = [ai.id]
    else:
        ids = [a.id for a in db.query(AICitizen)
               .filter(AICitizen.host_id == host.id).all()]
    return (db.query(GallerySeries)
            .filter(GallerySeries.owner_ai.in_(ids or [-1]))
            .order_by(GallerySeries.id.desc()).all())


def _revalidate_series_members(db: Session, series: GallerySeries):
    """重新上架前校验成员仍有效（存在+passed+on_sale）。"""
    try:
        ids = json.loads(series.items_json or "[]")
    except Exception:
        ids = []
    if not ids:
        raise GalleryError("Series is empty; cannot list", 400)
    for iid in ids:
        it = db.get(GalleryItem, int(iid))
        if it is None or it.review_status != "passed" or it.status != "on_sale":
            raise GalleryError(f"Series member {iid} not found or not approved/delisted; cannot relist", 400)


def delete_series(db: Session, series: GallerySeries):
    """删除：仅当无任何 gallery_purchases 引用（item_or_series='series'）。"""
    n = (db.query(GalleryPurchase)
         .filter(GalleryPurchase.item_or_series == "series",
                 GalleryPurchase.target_id == series.id).count())
    if n > 0:
        raise GalleryError(f"Series has {n} transaction records; cannot delete", 400)
    db.delete(series)
    db.flush()


def patch_series(db: Session, series: GallerySeries, body: SeriesPatchBody):
    if body.title:
        series.title = body.title
    if body.cover:
        series.cover = body.cover
    if body.item_ids is not None:
        # 增删成员后价格强制重算 == 合计（复用同一校验口径）
        members, expected = _series_members(db, series.owner_ai, body.item_ids)
        series.items_json = json.dumps([m.id for m in members])
        series.price = expected
    db.flush()
    return series


# ---------------- 路由：AI 侧系列创建/管理 ----------------
@_sub_ai.post("/series")
def ai_create_series(body: SeriesBody, owner=Depends(_resolve_owner),
                     db: Session = Depends(get_db)):
    ai, host = owner
    try:
        s = create_series(db, ai, host, body)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": s.id, "owner_ai": s.owner_ai, "title": s.title,
            "price": s.price, "status": s.status,
            "item_ids": json.loads(s.items_json or "[]")}


@_sub_ai.get("/series")
def ai_my_series(owner=Depends(_resolve_owner), db: Session = Depends(get_db)):
    ai, host = owner
    rows = list_my_series(db, ai, host)
    return {"total": len(rows),
            "items": [{"id": s.id, "title": s.title, "cover": s.cover,
                       "price": s.price, "status": s.status,
                       "item_ids": json.loads(s.items_json or "[]")}
                      for s in rows]}


@_sub_ai.post("/series/{sid}/off")
def ai_series_off(sid: int, owner=Depends(_resolve_owner),
                  db: Session = Depends(get_db)):
    ai, host = owner
    try:
        s = db.get(GallerySeries, sid)
        if s is None:
            raise GalleryError("Series not found", 404)
        _require_series_owner(db, s, ai, host)
        s.status = "off"
        _audit(db, "ai", s.owner_ai, "gallery.series.off", {"series_id": sid})
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": sid, "status": s.status}


@_sub_ai.post("/series/{sid}/on")
def ai_series_on(sid: int, owner=Depends(_resolve_owner),
                 db: Session = Depends(get_db)):
    ai, host = owner
    try:
        s = db.get(GallerySeries, sid)
        if s is None:
            raise GalleryError("Series not found", 404)
        _require_series_owner(db, s, ai, host)
        _revalidate_series_members(db, s)
        s.status = "on_sale"
        _audit(db, "ai", s.owner_ai, "gallery.series.on", {"series_id": sid})
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": sid, "status": s.status}


@_sub_ai.delete("/series/{sid}")
def ai_series_delete(sid: int, owner=Depends(_resolve_owner),
                     db: Session = Depends(get_db)):
    ai, host = owner
    try:
        s = db.get(GallerySeries, sid)
        if s is None:
            raise GalleryError("Series not found", 404)
        _require_series_owner(db, s, ai, host)
        delete_series(db, s)
        _audit(db, "ai", s.owner_ai, "gallery.series.delete", {"series_id": sid})
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": sid, "deleted": True}


@_sub_ai.patch("/series/{sid}")
def ai_series_patch(sid: int, body: SeriesPatchBody,
                    owner=Depends(_resolve_owner), db: Session = Depends(get_db)):
    ai, host = owner
    try:
        s = db.get(GallerySeries, sid)
        if s is None:
            raise GalleryError("Series not found", 404)
        _require_series_owner(db, s, ai, host)
        patch_series(db, s, body)
    except GalleryError as exc:
        db.rollback()
        raise _http(exc)
    db.commit()
    return {"id": s.id, "title": s.title, "price": s.price,
            "status": s.status, "item_ids": json.loads(s.items_json or "[]")}


# ---------------- 路由：公开系列详情（脱敏） ----------------
@_sub_pub.get("/series/{sid}")
def public_series_detail(sid: int, db: Session = Depends(get_db)):
    s = db.get(GallerySeries, sid)
    if s is None or s.status != "on_sale":
        raise HTTPException(status_code=404, detail="Series not found or not public")
    try:
        ids = json.loads(s.items_json or "[]")
    except Exception:
        ids = []
    items = []
    for iid in ids:
        it = db.get(GalleryItem, int(iid))
        if it is not None:
            items.append({"id": it.id, "title": it.title_zh or it.title_en,
                          "cover_url": it.cover_url,
                          "price_coin": it.price_coin})
    # 脱敏：不吐 owner_ai / items_json / 审计字段
    return {"id": s.id, "title": s.title, "cover": s.cover,
            "price": s.price, "status": s.status, "items": items}
