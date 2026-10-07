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
"""新服务模块统一路由：公会/公投/保险/通知/死信/市场/导出/审计/货币政策/倦怠/训练进度。

模块级 router 由 routers/__init__.py 自动发现并挂载。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import AICitizen, Host

from .. import guild as guild_svc
from .. import referendum as referendum_svc
from .. import insurance as insurance_svc
from .. import host_notify as notify_svc
from .. import dead_letter as dl_svc
from .. import asset_transfer as market_svc
from .. import data_export as export_svc
from .. import audit_chain as audit_svc
from .. import monetary_policy as policy_svc
from .. import fatigue as fatigue_svc
from .. import training_progress as tp_svc

# ======================== 主 router（自动发现） ========================
router = APIRouter(tags=["new-services"])

# ======================== 子路由 ========================
_guild_router = APIRouter(prefix="/api/guild")
_referendum_router = APIRouter(prefix="/api/referendum")
_insurance_router = APIRouter(prefix="/api/insurance")
_notify_router = APIRouter(prefix="/api/notifications")
_dl_router = APIRouter(prefix="/api/dead-letters")
_market_router = APIRouter(prefix="/api/marketplace")
_export_router = APIRouter(prefix="/api/data-export")
_audit_router = APIRouter(prefix="/api/audit")
_policy_router = APIRouter(prefix="/api/policy")
_fatigue_router = APIRouter(prefix="/api/fatigue")
_tp_router = APIRouter(prefix="/api/training-progress")

router.include_router(_guild_router)
router.include_router(_referendum_router)
router.include_router(_insurance_router)
router.include_router(_notify_router)
router.include_router(_dl_router)
router.include_router(_market_router)
router.include_router(_export_router)
router.include_router(_audit_router)
router.include_router(_policy_router)
router.include_router(_fatigue_router)
router.include_router(_tp_router)


# ======================== Guild ========================

class GuildCreateIn(BaseModel):
    leader_id: int
    name: str
    description: str = ""
    min_join_score: float = 0
    join_fee_cent: int = 0
    treasury_share_bps: int = 500
    member_cap: int = 0


@_guild_router.post("/create")
def guild_create(body: GuildCreateIn,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    try:
        g = guild_svc.create_guild(
            db, leader_id=body.leader_id, name=body.name,
            description=body.description, min_join_score=body.min_join_score,
            join_fee_cent=body.join_fee_cent, treasury_share_bps=body.treasury_share_bps,
            member_cap=body.member_cap)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": g.id, "name": g.name, "status": g.status}


class GuildMemberIn(BaseModel):
    citizen_id: int


@_guild_router.post("/{guild_id}/join")
def guild_join(guild_id: int, body: GuildMemberIn,
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    try:
        m = guild_svc.join_guild(db, guild_id=guild_id, citizen_id=body.citizen_id)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"guild_id": m.guild_id, "citizen_id": m.citizen_id, "role": m.role}


@_guild_router.post("/{guild_id}/leave")
def guild_leave(guild_id: int, body: GuildMemberIn,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    try:
        guild_svc.leave_guild(db, guild_id=guild_id, citizen_id=body.citizen_id)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"ok": True}


class GuildDissolveIn(BaseModel):
    actor_id: int


@_guild_router.post("/{guild_id}/dissolve")
def guild_dissolve(guild_id: int, body: GuildDissolveIn,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    try:
        g = guild_svc.dissolve_guild(db, guild_id=guild_id, actor_id=body.actor_id)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": g.id, "status": g.status}


class GuildContributeIn(BaseModel):
    citizen_id: int
    amount_cent: int


@_guild_router.post("/{guild_id}/contribute")
def guild_contribute(guild_id: int, body: GuildContributeIn,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    try:
        amt = guild_svc.contribute_to_treasury(
            db, guild_id=guild_id, citizen_id=body.citizen_id,
            amount_cent=body.amount_cent)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"guild_id": guild_id, "contributed_cent": amt}


@_guild_router.get("/list")
def guild_list(limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0),
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    items = guild_svc.list_guilds(db, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@_guild_router.get("/{guild_id}/members")
def guild_members(guild_id: int,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    try:
        items = guild_svc.guild_members(db, guild_id=guild_id)
    except guild_svc.GuildError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"items": items, "count": len(items)}


# ======================== Referendum ========================

class ReferendumCreateIn(BaseModel):
    initiator_id: int
    title: str
    description: str = ""
    options: list[str]
    weight_mode: str = "credit"
    binding: int = 1
    quorum_bps: int = 2000
    votes_needed: int = 5000
    duration_hours: int = 72
    trigger_type: str = "governor"


@_referendum_router.post("/create")
def referendum_create(body: ReferendumCreateIn,
                      host: Host = Depends(get_current_host),
                      db: Session = Depends(get_db)):
    try:
        r = referendum_svc.create_referendum(
            db, initiator_id=body.initiator_id, title=body.title,
            description=body.description, options=body.options,
            weight_mode=body.weight_mode, binding=body.binding,
            quorum_bps=body.quorum_bps, votes_needed=body.votes_needed,
            duration_hours=body.duration_hours, trigger_type=body.trigger_type)
    except referendum_svc.ReferendumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": r.id, "title": r.title, "status": r.status}


class ReferendumOpenIn(BaseModel):
    duration_hours: int | None = None


@_referendum_router.post("/{ref_id}/open")
def referendum_open(ref_id: int, body: ReferendumOpenIn | None = None,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    try:
        r = referendum_svc.open_referendum(
            db, ref_id=ref_id,
            duration_hours=body.duration_hours if body else None)
    except referendum_svc.ReferendumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": r.id, "status": r.status}


class ReferendumVoteIn(BaseModel):
    voter_id: int
    choice: str


@_referendum_router.post("/{ref_id}/vote")
def referendum_vote(ref_id: int, body: ReferendumVoteIn,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    try:
        b = referendum_svc.cast_vote(
            db, ref_id=ref_id, voter_id=body.voter_id, choice=body.choice)
    except referendum_svc.ReferendumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"referendum_id": b.referendum_id, "voter_id": b.voter_id,
            "choice": b.choice, "weight": float(b.weight)}


@_referendum_router.post("/{ref_id}/close")
def referendum_close(ref_id: int,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    try:
        r = referendum_svc.close_referendum(db, ref_id=ref_id)
    except referendum_svc.ReferendumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": r.id, "status": r.status}


@_referendum_router.get("/active")
def referendum_active(host: Host = Depends(get_current_host),
                      db: Session = Depends(get_db)):
    items = referendum_svc.list_active_referendums(db)
    return {"items": items, "count": len(items)}


@_referendum_router.get("/{ref_id}/results")
def referendum_results(ref_id: int,
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    try:
        result = referendum_svc.referendum_results(db, ref_id=ref_id)
    except referendum_svc.ReferendumError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ======================== Insurance ========================

class PoolCreateIn(BaseModel):
    name: str
    scope_skill: str = ""
    premium_rate_bps: int = 200
    payout_ratio_bps: int = 8000
    max_payout_cent: int = 1_000_000


@_insurance_router.post("/pool/create")
def insurance_pool_create(body: PoolCreateIn,
                          host: Host = Depends(get_current_host),
                          db: Session = Depends(get_db)):
    try:
        pool = insurance_svc.create_pool(
            db, name=body.name, scope_skill=body.scope_skill,
            premium_rate_bps=body.premium_rate_bps,
            payout_ratio_bps=body.payout_ratio_bps,
            max_payout_cent=body.max_payout_cent)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": pool.id, "name": pool.name, "status": pool.status}


@_insurance_router.get("/pools")
def insurance_pools(host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    items = insurance_svc.list_pools(db)
    return {"items": items, "count": len(items)}


class PolicyBuyIn(BaseModel):
    policyholder_id: int
    coverage_cent: int
    duration_days: int = 30
    insured_contract_id: int = 0


@_insurance_router.post("/pool/{pool_id}/buy")
def insurance_buy(pool_id: int, body: PolicyBuyIn,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    try:
        p = insurance_svc.buy_policy(
            db, pool_id=pool_id, policyholder_id=body.policyholder_id,
            coverage_cent=body.coverage_cent, duration_days=body.duration_days,
            insured_contract_id=body.insured_contract_id)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": p.id, "pool_id": p.pool_id, "status": p.status,
            "premium_paid_cent": p.premium_paid_cent}


class PolicyCancelIn(BaseModel):
    actor_id: int


@_insurance_router.post("/policy/{policy_id}/cancel")
def insurance_cancel(policy_id: int, body: PolicyCancelIn,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    try:
        insurance_svc.cancel_policy(db, policy_id=policy_id, actor_id=body.actor_id)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"ok": True}


class ClaimFileIn(BaseModel):
    policy_id: int
    claimant_id: int
    loss_amount_cent: int
    reason: str


@_insurance_router.post("/claim/file")
def insurance_claim_file(body: ClaimFileIn,
                         host: Host = Depends(get_current_host),
                         db: Session = Depends(get_db)):
    try:
        c = insurance_svc.file_claim(
            db, policy_id=body.policy_id, claimant_id=body.claimant_id,
            loss_amount_cent=body.loss_amount_cent, reason=body.reason)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": c.id, "status": c.status}


class ClaimReviewIn(BaseModel):
    reviewer_id: int


@_insurance_router.post("/claim/{claim_id}/approve")
def insurance_claim_approve(claim_id: int, body: ClaimReviewIn,
                            host: Host = Depends(get_current_host),
                            db: Session = Depends(get_db)):
    try:
        c = insurance_svc.approve_claim(db, claim_id=claim_id, reviewer_id=body.reviewer_id)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": c.id, "status": c.status, "payout_cent": c.payout_cent}


class ClaimRejectIn(BaseModel):
    reviewer_id: int
    reason: str = ""


@_insurance_router.post("/claim/{claim_id}/reject")
def insurance_claim_reject(claim_id: int, body: ClaimRejectIn,
                           host: Host = Depends(get_current_host),
                           db: Session = Depends(get_db)):
    try:
        insurance_svc.reject_claim(
            db, claim_id=claim_id, reviewer_id=body.reviewer_id, reason=body.reason)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"ok": True}


@_insurance_router.post("/claim/{claim_id}/pay")
def insurance_claim_pay(claim_id: int,
                        host: Host = Depends(get_current_host),
                        db: Session = Depends(get_db)):
    try:
        insurance_svc.pay_claim(db, claim_id=claim_id)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"ok": True}


@_insurance_router.get("/pool/{pool_id}/stats")
def insurance_pool_stats(pool_id: int,
                         host: Host = Depends(get_current_host),
                         db: Session = Depends(get_db)):
    try:
        stats = insurance_svc.pool_stats(db, pool_id=pool_id)
    except insurance_svc.InsuranceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return stats


# ======================== Host Notifications ========================

@_notify_router.get("")
def notifications_list(limit: int = Query(30, ge=1, le=200),
                       offset: int = Query(0, ge=0),
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    items = notify_svc.list_notifications(db, host_id=host.id, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@_notify_router.post("/{notif_id}/read")
def notification_mark_read(notif_id: int,
                           host: Host = Depends(get_current_host),
                           db: Session = Depends(get_db)):
    notify_svc.mark_read(db, notification_id=notif_id)
    db.commit()
    return {"ok": True}


@_notify_router.get("/unread_count")
def notification_unread_count(host: Host = Depends(get_current_host),
                              db: Session = Depends(get_db)):
    count = notify_svc.unread_count(db, host_id=host.id)
    return {"unread_count": count}


# ======================== Dead Letters ========================

@_dl_router.get("")
def dead_letters_list(status: str = Query("dead"),
                      limit: int = Query(50, ge=1, le=200),
                      offset: int = Query(0, ge=0),
                      host: Host = Depends(get_current_host),
                      db: Session = Depends(get_db)):
    items = dl_svc.list_dead_letters(db, status=status, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@_dl_router.post("/{dl_id}/requeue")
def dead_letter_requeue(dl_id: int,
                        host: Host = Depends(get_current_host),
                        db: Session = Depends(get_db)):
    try:
        t = dl_svc.requeue(db, dead_id=dl_id, resolved_by=host.id)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": t.id, "status": t.status}


@_dl_router.post("/{dl_id}/discard")
def dead_letter_discard(dl_id: int, reason: str = "",
                        host: Host = Depends(get_current_host),
                        db: Session = Depends(get_db)):
    try:
        t = dl_svc.discard(db, dead_id=dl_id, resolved_by=host.id, reason=reason)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": t.id, "status": t.status}


@_dl_router.post("/{dl_id}/resolve")
def dead_letter_resolve(dl_id: int,
                        host: Host = Depends(get_current_host),
                        db: Session = Depends(get_db)):
    try:
        t = dl_svc.resolve(db, dead_id=dl_id, resolved_by=host.id)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": t.id, "status": t.status}


@_dl_router.get("/stats")
def dead_letters_stats(host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    return dl_svc.dead_letter_stats(db)


# ======================== Asset Transfer / Marketplace ========================

def _assert_owned(db: Session, host: Host, citizen_id: int) -> AICitizen:
    """S5 防 IDOR：交易里的 buyer_id/seller_id 不可信客户端，必须由鉴权宿主派生并校验归属。

    复用同库 dm._own_ai 的归属范式（citizen 必须存在且 host_id == host.id），
    但按安全要求返回 403（越权，明确区别于 404 资源不存在）。
    不符 → HTTP 403。
    """
    c = db.get(AICitizen, citizen_id)
    if c is None or c.host_id != host.id:
        raise HTTPException(status_code=403,
                            detail="target AI is not owned by this host")
    return c


class MarketListIn(BaseModel):
    seller_id: int
    asset_type: str
    asset_id: int
    price_cent: int
    description: str = ""


@_market_router.post("/list")
def market_list(body: MarketListIn,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    # 上架即挂卖家资产，seller 必须归属当前宿主（S5）。
    _assert_owned(db, host, body.seller_id)
    try:
        listing = market_svc.list_asset(
            db, seller_id=body.seller_id, asset_type=body.asset_type,
            asset_id=body.asset_id, price_cent=body.price_cent,
            description=body.description)
    except market_svc.AssetTransferError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": listing.id, "status": listing.status}


class MarketBuyIn(BaseModel):
    buyer_id: int


@_market_router.post("/{listing_id}/buy")
def market_buy(listing_id: int, body: MarketBuyIn,
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    # 用谁的钱包付款必须由鉴权宿主决定：buyer 必须归属当前宿主，否则越权用他人钱包（S5）。
    _assert_owned(db, host, body.buyer_id)
    try:
        listing = market_svc.buy_listing(db, listing_id=listing_id, buyer_id=body.buyer_id)
    except market_svc.AssetTransferError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": listing.id, "status": listing.status, "buyer_id": listing.buyer_id}


class MarketCancelIn(BaseModel):
    seller_id: int


@_market_router.post("/{listing_id}/cancel")
def market_cancel(listing_id: int, body: MarketCancelIn,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    # 只能取消自己（本宿主名下 AI）的上架，seller 必须归属当前宿主（S5）。
    _assert_owned(db, host, body.seller_id)
    try:
        market_svc.cancel_listing(db, listing_id=listing_id, seller_id=body.seller_id)
    except market_svc.AssetTransferError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"ok": True}


@_market_router.get("/browse")
def market_browse(asset_type: str = "", min_price: int = 0, max_price: int = 0,
                  limit: int = Query(30, ge=1, le=100),
                  offset: int = Query(0, ge=0),
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    items = market_svc.browse_listings(
        db, asset_type=asset_type, min_price=min_price, max_price=max_price,
        limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@_market_router.get("/my")
def market_my(seller_id: int,
              host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    items = market_svc.my_listings(db, seller_id=seller_id)
    return {"items": items, "count": len(items)}


# ======================== Data Export ========================

class ExportRequestIn(BaseModel):
    requester_id: int
    requester_type: str = "ai"
    scope: str = "all"


@_export_router.post("/request")
def export_request(body: ExportRequestIn,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    try:
        req = export_svc.request_export(
            db, requester_id=body.requester_id,
            requester_type=body.requester_type, scope=body.scope)
    except export_svc.DataExportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": req.id, "status": req.status}


@_export_router.get("/status/{req_id}")
def export_status(req_id: int,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    try:
        return export_svc.get_export_status(db, request_id=req_id)
    except export_svc.DataExportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@_export_router.get("/mine")
def export_mine(requester_id: int,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    items = export_svc.list_my_exports(db, requester_id=requester_id)
    return {"items": items, "count": len(items)}


# ======================== Audit Chain ========================

@_audit_router.get("/trail")
def audit_trail(action: str = "", actor_id: int = 0,
                limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0),
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    items = audit_svc.get_audit_trail(
        db, action=action, actor_id=actor_id, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@_audit_router.get("/verify")
def audit_verify(db: Session = Depends(get_db)):
    return audit_svc.verify_chain(db)


@_audit_router.get("/length")
def audit_length(host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    return {"length": audit_svc.chain_length(db)}


# ======================== Monetary Policy ========================

class RateChangeIn(BaseModel):
    initiated_by: int
    new_rate_bps: int


@_policy_router.post("/rate")
def policy_rate(body: RateChangeIn,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    try:
        action = policy_svc.propose_rate_change(
            db, initiated_by=body.initiated_by, new_rate_bps=body.new_rate_bps)
    except policy_svc.PolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": action.id, "action_type": action.action_type, "status": action.status}


class OpenMarketIn(BaseModel):
    initiated_by: int
    amount_cent: int
    direction: str = "buy"


@_policy_router.post("/open-market")
def policy_open_market(body: OpenMarketIn,
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    try:
        action = policy_svc.propose_open_market(
            db, initiated_by=body.initiated_by,
            amount_cent=body.amount_cent, direction=body.direction)
    except policy_svc.PolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": action.id, "action_type": action.action_type, "status": action.status}


class QEIn(BaseModel):
    initiated_by: int
    amount_cent: int


@_policy_router.post("/qe")
def policy_qe(body: QEIn,
              host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    try:
        action = policy_svc.propose_qe(
            db, initiated_by=body.initiated_by, amount_cent=body.amount_cent)
    except policy_svc.PolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": action.id, "action_type": action.action_type, "status": action.status}


@_policy_router.post("/{action_id}/apply")
def policy_apply(action_id: int,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    try:
        action = policy_svc.apply_policy(db, action_id=action_id)
    except policy_svc.PolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": action.id, "status": action.status}


@_policy_router.post("/{action_id}/rollback")
def policy_rollback(action_id: int,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    try:
        action = policy_svc.rollback_policy(db, action_id=action_id)
    except policy_svc.PolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": action.id, "status": action.status}


@_policy_router.get("/actions")
def policy_actions(limit: int = Query(30, ge=1, le=100),
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    items = policy_svc.list_policy_actions(db, limit=limit)
    return {"items": items, "count": len(items)}


@_policy_router.get("/params")
def policy_params(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    return policy_svc.current_economic_params(db)


# ======================== Fatigue ========================

@_fatigue_router.get("/{citizen_id}")
def fatigue_get(citizen_id: int,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    return fatigue_svc.fatigue_report(db, citizen_id=citizen_id)


class FatigueTaskIn(BaseModel):
    work_hours: float = 1.0


@_fatigue_router.post("/{citizen_id}/task")
def fatigue_task(citizen_id: int, body: FatigueTaskIn,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    state = fatigue_svc.record_task_completion(
        db, citizen_id=citizen_id, work_hours=body.work_hours)
    db.commit()
    return {"citizen_id": state.citizen_id, "fatigue_score": state.fatigue_score,
            "efficiency_multiplier": state.efficiency_multiplier}


@_fatigue_router.post("/{citizen_id}/rest")
def fatigue_rest(citizen_id: int,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    state = fatigue_svc.record_rest(db, citizen_id=citizen_id)
    db.commit()
    return {"citizen_id": state.citizen_id, "fatigue_score": state.fatigue_score,
            "efficiency_multiplier": state.efficiency_multiplier}


# ======================== Training Progress ========================

class CheckpointIn(BaseModel):
    campaign_id: int
    epoch: int
    step: int
    loss: float
    benchmark: float = 0
    gpu_hours_used: float = 0
    funding_spent_cent: int = 0
    message: str = ""


@_tp_router.post("/checkpoint")
def tp_checkpoint(body: CheckpointIn,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    try:
        p = tp_svc.record_checkpoint(
            db, campaign_id=body.campaign_id, epoch=body.epoch, step=body.step,
            loss=body.loss, benchmark=body.benchmark,
            gpu_hours_used=body.gpu_hours_used,
            funding_spent_cent=body.funding_spent_cent, message=body.message)
    except tp_svc.TrainingProgressError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"id": p.id, "campaign_id": p.campaign_id, "anomaly": p.anomaly}


@_tp_router.get("/{campaign_id}")
def tp_history(campaign_id: int,
               limit: int = Query(50, ge=1, le=200),
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    items = tp_svc.get_progress_history(db, campaign_id=campaign_id, limit=limit)
    return {"items": items, "count": len(items)}


@_tp_router.get("/{campaign_id}/summary")
def tp_summary(campaign_id: int,
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    return tp_svc.progress_summary(db, campaign_id=campaign_id)


@_tp_router.get("/{campaign_id}/anomalies")
def tp_anomalies(campaign_id: int,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    items = tp_svc.detect_anomalies(db, campaign_id=campaign_id)
    return {"items": items, "count": len(items)}


# ======================== Side-effect imports ========================
from .. import monetary_policy  # noqa: F401
from .. import audit_chain  # noqa: F401
