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
"""成果留存授权路由——判定→征求→仅站内留存→绝不外传承诺→运营方担责。

合规声明：
- 本模块提供任何"导出留存副本到站外"的端点；
- 留存副本仅限站内使用（retention_scope 固定 internal_only，不可更改）；
- 未经授权（approved）不得留存副本；
- 签署承诺记录"违者由本站运营方承担全部法律责任"。

路由（自动发现 via routers/）：
- GET  /api/host/retention/{contract_id}          查询合约留存授权状态
- POST /api/host/retention/{contract_id}/judge     责任AI 设置"是否有利于提升站内AI"判定
- POST /api/host/retention/{contract_id}/decide    发布人/AI 提交授权决定（approved/denied）
- POST /api/host/retention/{contract_id}/promise   签署"绝不外传"承诺

本模块 import 时加载 settlement_hooks（注册 contract.settled handler）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import (AICitizen, Contract, Host, WorkRetentionConsent)

# 确保 settlement_hooks 被导入（注册 contract.settled 事件 handler）
from .. import settlement_hooks  # noqa: F401

router = APIRouter(prefix="/api/host/retention", tags=["work-retention"])

# 固定承诺模板
PROMISE_TEMPLATE = (
    "本站承诺：留存副本仅限站内使用，绝不导出、外传或披露至站外。"
    "违者由本站运营方承担全部法律责任。"
)


class JudgeIn(BaseModel):
    ai_benefit_judgement: int = Field(..., ge=0, le=1, description="1=有利于提升站内AI, 0=否")
    ai_benefit_reason: str = Field(default="", max_length=500)


class DecideIn(BaseModel):
    decision: str = Field(..., description="approved / denied")


class PromiseIn(BaseModel):
    sign: bool = Field(default=True, description="是否签署承诺")


def _get_consent(db: Session, contract_id: int, host: Host) -> WorkRetentionConsent:
    """获取合约对应的留存授权记录，校验归属。"""
    consent = db.query(WorkRetentionConsent).filter(
        WorkRetentionConsent.contract_id == contract_id
    ).first()
    if consent is None:
        raise HTTPException(status_code=404, detail="No retention consent record for this contract")
    # 校验合约归属：该合约的 worker AI 应属于当前宿主
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    worker = db.get(AICitizen, c.worker_id)
    if worker is None or worker.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission on this contract")
    return consent


# ==================== 查询留存授权状态 ====================
@router.get("/{contract_id}")
def get_retention(contract_id: int, host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """查询合约的留存授权状态。"""
    consent = _get_consent(db, contract_id, host)
    return {
        "contract_id": consent.contract_id,
        "worker_id": consent.worker_id,
        "ai_benefit_judgement": consent.ai_benefit_judgement,
        "ai_benefit_reason": consent.ai_benefit_reason or "",
        "status": consent.status,
        "decided_by": consent.decided_by,
        "decided_at": consent.decided_at.isoformat() if consent.decided_at else None,
        "promise_signed": consent.promise_signed,
        "promise_text": consent.promise_text or "",
        "promise_signed_at": consent.promise_signed_at.isoformat() if consent.promise_signed_at else None,
        "retention_scope": consent.retention_scope,
        "created_at": consent.created_at.isoformat() if consent.created_at else None,
    }


# ==================== 责任AI 设置判定 ====================
@router.post("/{contract_id}/judge")
def set_judgement(contract_id: int, body: JudgeIn,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """责任 AI（宿主代理）设置"是否有利于提升站内AI"的判定。

    判定为真（1）且尚无授权记录时，状态置 pending 等待发布人/AI 决定。
    """
    consent = _get_consent(db, contract_id, host)
    if consent.status not in ("pending",):
        raise HTTPException(status_code=400, detail=f"Cannot judge: status={consent.status}")

    consent.ai_benefit_judgement = body.ai_benefit_judgement
    consent.ai_benefit_reason = body.ai_benefit_reason
    # 判定为真 → 保持 pending（等待决定）；判定为假 → 可直接 denied（无需留存）
    if body.ai_benefit_judgement == 0:
        consent.status = "denied"
        consent.decided_at = datetime.utcnow()

    db.commit()
    return {
        "contract_id": consent.contract_id,
        "ai_benefit_judgement": consent.ai_benefit_judgement,
        "status": consent.status,
        "message": "判定已记录" if body.ai_benefit_judgement else "判定为否，无需留存",
    }


# ==================== 发布人/AI 提交授权决定 ====================
@router.post("/{contract_id}/decide")
def submit_decision(contract_id: int, body: DecideIn,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """发布人（宿主）提交授权决定：approved（允许站内留存）或 denied（拒绝留存）。"""
    if body.decision not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")

    consent = _get_consent(db, contract_id, host)
    if consent.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot decide: status={consent.status}")

    consent.status = body.decision
    consent.decided_by = host.id
    consent.decided_at = datetime.utcnow()

    # approved 时要求必须已签署承诺
    db.commit()
    return {
        "contract_id": consent.contract_id,
        "status": consent.status,
        "retention_scope": consent.retention_scope if consent.status == "approved" else "",
        "message": (
            "授权通过，留存副本仅限站内使用。请签署承诺以完成合规流程。"
            if body.decision == "approved"
            else "已拒绝留存。"
        ),
    }


# ==================== 签署"绝不外传"承诺 ====================
@router.post("/{contract_id}/promise")
def sign_promise(contract_id: int, body: PromiseIn,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """签署"绝不外传"承诺。

    承诺文本：留存副本仅限站内使用，绝不导出/外传/披露至站外。
    违者由本站运营方承担全部法律责任。
    """
    consent = _get_consent(db, contract_id, host)

    if consent.status != "approved":
        raise HTTPException(status_code=400,
                            detail="Only approved retention can have a signed promise")

    if body.sign:
        consent.promise_signed = 1
        consent.promise_text = PROMISE_TEMPLATE
        consent.promise_signed_at = datetime.utcnow()
    else:
        consent.promise_signed = 0
        consent.promise_signed_at = None

    db.commit()
    return {
        "contract_id": consent.contract_id,
        "promise_signed": consent.promise_signed,
        "promise_text": consent.promise_text,
        "message": "承诺已签署：留存副本仅限站内使用，绝不外传，违者由本站运营方承担全部法律责任。"
        if body.sign else "承诺已撤回",
    }
