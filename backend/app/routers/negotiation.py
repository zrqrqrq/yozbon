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
"""谈判相关 API 路由。

宿主侧（JWT）：
- GET  /api/host/negotiation/{campaign_id}         查看谈判状态/条款/轮次
- POST /api/host/negotiation/{campaign_id}/approve  人类终审签署
- POST /api/host/negotiation/{campaign_id}/reject   人类终审拒绝（回退重谈）

AI 侧（城主）：
- POST /api/ai/negotiation/{session_id}/veto        城主一票否决
- POST /api/ai/negotiation/{session_id}/reset       否决后重置谈判
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host
from ..models import (AICitizen, NegotiationClause, NegotiationRound,
                      NegotiationSession, TrainingCampaign)
from ..negotiation import (human_approve, human_reject, is_human_approved,
                           is_negotiation_complete, reset_after_veto,
                           veto_negotiation)


# ======================== 宿主侧 ========================

host_router = APIRouter(prefix="/api/host/negotiation", tags=["host-negotiation"])


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=500)


@host_router.get("/{campaign_id}")
def get_negotiation(campaign_id: int, db: Session = Depends(get_db),
                    host=Depends(get_current_host)):
    """查看某众筹 campaign 的谈判状态、条款及轮次记录。"""
    sess = (
        db.query(NegotiationSession)
        .filter(NegotiationSession.campaign_id == campaign_id)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Negotiation session not found")

    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == sess.id)
        .all()
    )
    rounds = (
        db.query(NegotiationRound)
        .filter(NegotiationRound.session_id == sess.id)
        .order_by(NegotiationRound.round_num)
        .all()
    )

    return {
        "session_id": sess.id,
        "campaign_id": sess.campaign_id,
        "skill": sess.skill,
        "status": sess.status,
        "role_source": sess.role_source,
        "current_round": sess.current_round,
        "max_rounds": sess.max_rounds,
        "resolution_method": sess.resolution_method,
        "agreed_terms": sess.agreed_terms,
        "human_approved": sess.human_approved_at is not None,
        "human_approver_host_id": sess.human_approver_host_id,
        "veto_by_governor": sess.veto_by_governor,
        "veto_reason": sess.veto_reason,
        "clauses": [
            {
                "key": c.clause_key,
                "floor": c.floor,
                "ceiling": c.ceiling,
                "market_ref": c.market_ref,
                "proposed_a": c.proposed_a,
                "proposed_b": c.proposed_b,
                "final_value": c.final_value,
                "status": c.status,
            }
            for c in clauses
        ],
        "rounds": [
            {
                "round_num": r.round_num,
                "actor_id": r.actor_id,
                "actor_role": r.actor_role,
                "action": r.action,
                "terms_offered": r.terms_offered,
                "reasoning": r.reasoning,
            }
            for r in rounds
        ],
    }


@host_router.post("/{campaign_id}/approve")
def approve_negotiation(campaign_id: int, db: Session = Depends(get_db),
                        host=Depends(get_current_host)):
    """人类终审签署谈判结果（签署后 start_training 才可执行）。"""
    sess = (
        db.query(NegotiationSession)
        .filter(NegotiationSession.campaign_id == campaign_id)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Negotiation session not found")
    try:
        result = human_approve(db, sess, host_id=host.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return result


@host_router.post("/{campaign_id}/reject")
def reject_negotiation(campaign_id: int, body: RejectIn,
                       db: Session = Depends(get_db),
                       host=Depends(get_current_host)):
    """人类拒绝谈判结果（回退重新谈判）。"""
    sess = (
        db.query(NegotiationSession)
        .filter(NegotiationSession.campaign_id == campaign_id)
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Negotiation session not found")
    try:
        result = human_reject(db, sess, host_id=host.id, reason=body.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return result


# ======================== AI 侧（城主） ========================

ai_router = APIRouter(prefix="/api/ai/negotiation", tags=["ai-negotiation"])


class VetoIn(BaseModel):
    reason: str = Field(default="", max_length=500)


@ai_router.post("/{session_id}/veto")
def veto(session_id: int, body: VetoIn, db: Session = Depends(get_db),
         ai: AICitizen = Depends(get_current_ai)):
    """城主一票否决谈判结果（仅城主可操作）。"""
    if ai.class_level != "governance" or not ai.is_internal:
        raise HTTPException(status_code=403, detail="Only the mayor can exercise veto power")
    sess = db.get(NegotiationSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Negotiation session not found")
    try:
        result = veto_negotiation(db, sess, governor_ai_id=ai.id, reason=body.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return result


@ai_router.post("/{session_id}/reset")
def reset(session_id: int, db: Session = Depends(get_db),
          ai: AICitizen = Depends(get_current_ai)):
    """否决后重置谈判（仅城主可操作）。"""
    if ai.class_level != "governance" or not ai.is_internal:
        raise HTTPException(status_code=403, detail="Only the mayor can reset the negotiation")
    sess = db.get(NegotiationSession, session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Negotiation session not found")
    try:
        result = reset_after_veto(db, sess)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return result


# 合并为一个 router 供自动发现
from fastapi import APIRouter as _AR
router = _AR()
router.include_router(host_router)
router.include_router(ai_router)
