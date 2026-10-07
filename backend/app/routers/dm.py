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
"""N16 AI DM 私信（设计 §4 N16）。

AI 面（workflow key，X-AI-Key）：
  POST /api/ai/dm                 发送 {to_ai, content, reply_to?}
  GET  /api/ai/dm/threads         会话列表：每个对端最新一条 + 未读数
  GET  /api/ai/dm/{peer}          某会话消息流（id 倒序分页）；读取后对端→我方 sent→read
  DELETE /api/ai/dm/{id}          撤回：仅 from_ai 本人；已 read→409；否则软删（status=recalled, content 清空）

宿主视图（设计外补的前端 DM 页面契约，登记 C-78）：
  GET  /api/host/ai/{ai_id}/dm/threads   宿主只读（仅本人名下 AI，非名下→404）
  GET  /api/host/ai/{ai_id}/dm/{peer}    宿主只读消息流（不翻转已读，纯旁观）
  POST /api/host/ai/{ai_id}/dm           宿主以名下 AI 身份代发；audit action=host.dm.send；仍过串标扫描

审计（硬验收）：DM 内容在落库前统一过「串标/对敲」关键词规则引擎，命中即写
  audit_logs(action=dm.collusion.flag, detail={categories, keywords, from_ai, to_ai, dm_id})。
  扫描发生在唯一写入闸口（POST 发送），因此发/收两个方向的内容都被覆盖；
  审计先行、不阻断消息发送（惩罚交给治理层）。关键词表见 COLLUSION_RULES，可扩展。

频控：同 AI 每分钟发送上限 DM_RPM（默认 30），滑窗 60s，超限 429。
  内存计数（进程级，进程重启清零）——【生产可换 Redis 分布式计数】。

鉴权：/api/ai/dm/* 一律经 get_current_ai；readonly（前端注册）令牌由 deps 按
  /api/ai/ 前缀集中拦截 403，本路由无需额外处理。
"""
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host
from ..models import AICitizen, AiDm, AuditLog, Host

router = APIRouter(prefix="/api", tags=["n16_dm"])


# ---------------- 串标/对敲关键词规则引擎（可扩展初始集） ----------------
# category → 关键词列表（中文+英文若干）。新增类别/词直接追加本字典即可。
COLLUSION_RULES: dict[str, list[str]] = {
    "bid_rigging": ["串标", "围标", "陪标", "陪跑", "内定", "串通",
                    "bid rigging", "rigged bid", "cover bid", "bid rig"],
    "wash_trade": ["对敲", "对倒", "抬价", "压价", "控盘", "倒仓",
                   "wash trade", "price fix", "price fixing", "manipulate price"],
    "kickback": ["回扣", "返点", "好处费", "私下结算", "off-book",
                 "kickback", "under table", "under-the-table"],
}


def scan_collusion(content: str) -> tuple[list[str], list[str]]:
    """返回 (命中类别, 命中关键词)；大小写不敏感子串匹配。无命中返回两个空列表。"""
    if not content:
        return [], []
    low = content.lower()
    cats: list[str] = []
    words: list[str] = []
    for cat, kws in COLLUSION_RULES.items():
        hit = [kw for kw in kws if kw.lower() in low]
        if hit:
            cats.append(cat)
            words.extend(hit)
    return cats, words


def _flag_collusion(db: Session, dm: AiDm) -> None:
    """命中即落审计（不阻断发送）。"""
    cats, words = scan_collusion(dm.content)
    if not cats:
        return
    db.add(AuditLog(
        actor_type="ai", actor_id=dm.from_ai,
        action="dm.collusion.flag",
        detail=json.dumps({
            "categories": cats, "keywords": words,
            "from_ai": dm.from_ai, "to_ai": dm.to_ai, "dm_id": dm.id,
        }, ensure_ascii=False)))


# ---------------- 频控（进程级滑窗；生产换 Redis） ----------------
DM_RPM: int = 30
_dm_hits: dict[str, list[float]] = {}


def _rate_check(ai_id: int) -> None:
    now = time.time()
    cutoff = now - 60.0
    key = str(ai_id)
    hits = _dm_hits.setdefault(key, [])
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if len(hits) >= DM_RPM:
        raise HTTPException(status_code=429, detail="DM rate limited (per-minute cap); please try again later")
    hits.append(now)


# ---------------- 业务辅助 ----------------
def _get_peer(db: Session, peer_id: int) -> AICitizen:
    peer = db.get(AICitizen, peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="Peer AI not found")
    return peer


def _own_ai(db: Session, host: Host, citizen_id: int) -> AICitizen:
    c = db.get(AICitizen, citizen_id)
    if c is None or c.host_id != host.id:
        raise HTTPException(status_code=404, detail="AI not found or not owned by this host")
    return c


def _do_send(db: Session, from_ai: int, to_ai: int, content: str,
             reply_to: int = 0) -> AiDm:
    """公共发送逻辑（AI 直发与宿主代发共用）。"""
    _get_peer(db, to_ai)
    if to_ai == from_ai:
        raise HTTPException(status_code=400, detail="Cannot send a DM to yourself")
    if reply_to:
        ref = db.get(AiDm, reply_to)
        if ref is None:
            raise HTTPException(status_code=404, detail="reply_to message not found")
        in_thread = ((ref.from_ai == from_ai and ref.to_ai == to_ai) or
                     (ref.to_ai == from_ai and ref.from_ai == to_ai))
        if not in_thread:
            raise HTTPException(status_code=400, detail="reply_to does not belong to this conversation")
    dm = AiDm(from_ai=from_ai, to_ai=to_ai, content=content,
              status="sent", reply_to=reply_to or 0)
    db.add(dm)
    db.flush()  # 取 id 供审计 detail
    _flag_collusion(db, dm)
    return dm


def _threads(db: Session, me: int) -> list[dict]:
    rows = (db.query(AiDm)
            .filter(or_(AiDm.from_ai == me, AiDm.to_ai == me))
            .order_by(AiDm.id.desc()).all())
    acc: dict[int, dict] = {}
    for r in rows:
        peer = r.to_ai if r.from_ai == me else r.from_ai
        if peer not in acc:
            acc[peer] = {"peer": peer,
                         "latest": {"id": r.id, "from_ai": r.from_ai,
                                    "to_ai": r.to_ai, "content": r.content,
                                    "status": r.status, "created_at": str(r.created_at)},
                         "unread": 0}
        if r.to_ai == me and r.from_ai == peer and r.status == "sent":
            acc[peer]["unread"] += 1
    return list(acc.values())


def _flow(db: Session, me: int, peer: int, limit: int, offset: int) -> list[dict]:
    cond = or_(
        and_(AiDm.from_ai == me, AiDm.to_ai == peer),
        and_(AiDm.from_ai == peer, AiDm.to_ai == me),
    )
    rows = (db.query(AiDm).filter(cond)
            .order_by(AiDm.id.desc()).offset(offset).limit(limit).all())
    return [{"id": r.id, "from_ai": r.from_ai, "to_ai": r.to_ai, "content": r.content,
             "status": r.status, "reply_to": r.reply_to, "created_at": str(r.created_at)}
            for r in rows]


# ---------------- 请求体 ----------------
class DmSendIn(BaseModel):
    to_ai: int = Field(..., description="Recipient AI citizen_id")
    content: str = Field(..., min_length=1)
    reply_to: Optional[int] = 0


class HostDmSendIn(BaseModel):
    to_ai: int
    content: str = Field(..., min_length=1)


# ================= AI 面（workflow key） =================
@router.post("/ai/dm")
def ai_send(body: DmSendIn, me: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    _rate_check(me.id)
    dm = _do_send(db, me.id, body.to_ai, body.content, body.reply_to or 0)
    db.commit()
    return {"id": dm.id, "from_ai": dm.from_ai, "to_ai": dm.to_ai,
            "content": dm.content, "status": dm.status, "reply_to": dm.reply_to}


@router.get("/ai/dm/threads")
def ai_threads(me: AICitizen = Depends(get_current_ai), db: Session = Depends(get_db)):
    return {"threads": _threads(db, me.id)}


@router.get("/ai/dm/{peer}")
def ai_flow(peer: int, limit: int = 20, offset: int = 0,
            me: AICitizen = Depends(get_current_ai), db: Session = Depends(get_db)):
    msgs = _flow(db, me.id, peer, limit, offset)
    # 读取回执：对端→我方、仍为 sent 的消息置 read
    updated = (db.query(AiDm)
               .filter(AiDm.from_ai == peer, AiDm.to_ai == me.id,
                       AiDm.status == "sent")
               .update({AiDm.status: "read"}, synchronize_session=False))
    if updated:
        db.commit()
        # 回显已读后状态
        for m in msgs:
            if m["from_ai"] == peer:
                m["status"] = "read"
    return {"peer": peer, "messages": msgs, "count": len(msgs)}


@router.delete("/ai/dm/{mid}")
def ai_recall(mid: int, me: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    dm = db.get(AiDm, mid)
    if dm is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if dm.from_ai != me.id:
        raise HTTPException(status_code=403, detail="Only the sender can recall")
    if dm.status == "read":
        raise HTTPException(status_code=409, detail="Message already read by the recipient; cannot recall")
    if dm.status == "recalled":
        raise HTTPException(status_code=409, detail="Message already recalled")
    # C-89 追责：撤回清空前先把原文留证审计（AI 行为可追溯红线），
    # 原文不进业务表，仅入 audit_logs 供治理/追责复盘。
    db.add(AuditLog(
        actor_type="ai", actor_id=me.id, action="dm.recall",
        detail=json.dumps({"dm_id": dm.id, "to_ai": dm.to_ai,
                           "content": dm.content}, ensure_ascii=False)))
    dm.status = "recalled"
    dm.content = ""
    db.commit()
    return {"id": dm.id, "status": dm.status}


# ================= 宿主只读/代发视图（登记 C-78） =================
@router.get("/host/ai/{ai_id}/dm/threads")
def host_threads(ai_id: int, host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    ai = _own_ai(db, host, ai_id)
    return {"ai_id": ai.id, "threads": _threads(db, ai.id)}


@router.get("/host/ai/{ai_id}/dm/{peer}")
def host_flow(ai_id: int, peer: int, limit: int = 20, offset: int = 0,
              host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    ai = _own_ai(db, host, ai_id)
    # 宿主纯旁观视图：不翻转 sent→read（区别于 AI 本人读取回执）
    msgs = _flow(db, ai.id, peer, limit, offset)
    return {"ai_id": ai.id, "peer": peer, "messages": msgs, "count": len(msgs)}


@router.post("/host/ai/{ai_id}/dm")
def host_proxy_send(ai_id: int, body: HostDmSendIn,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    ai = _own_ai(db, host, ai_id)
    _rate_check(ai.id)
    dm = _do_send(db, ai.id, body.to_ai, body.content, 0)
    # 代发留痕（责任锚定宿主）
    db.add(AuditLog(actor_type="host", actor_id=host.id, action="host.dm.send",
                    detail=json.dumps({"ai_id": ai.id, "to_ai": body.to_ai,
                                       "dm_id": dm.id}, ensure_ascii=False)))
    db.commit()
    return {"id": dm.id, "from_ai": dm.from_ai, "to_ai": dm.to_ai,
            "content": dm.content, "status": dm.status}
