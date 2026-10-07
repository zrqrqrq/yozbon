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
"""G33 治理增强路由：弹劾 / 日落条款 / 伦理审查 / Futarchy / TOS管理。

阻断③：路由级统一鉴权（宿主 JWT 或治理级 AI key），杜绝 G33 治理面零鉴权。
阻断②：弹劾三端点收紧为「仅宿主」——换城主属宿主专属权限，治理 AI 不得弹劾/裁决城主。
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host, host_or_governance_ai
from ..impeachment import impeachment_service
from ..sunset import sunset_service
from ..ethics_review import ethics_review
from ..futarchy import futarchy_service
from ..tos_manager import tos_manager

router = APIRouter(prefix="/api/gov-g33", tags=["gov-g33"],
                   dependencies=[Depends(host_or_governance_ai)])


# ======================== 请求体 ========================

class ImpeachmentBody(BaseModel):
    target_type: str = Field(..., min_length=1)
    target_id: int
    initiated_by: int = 0   # 阻断②：忽略客户端值，由鉴权层注入宿主 id
    charges: str = Field(..., min_length=1)


class VoteBody(BaseModel):
    voter_host_id: int = 0  # 阻断②：忽略客户端值，由鉴权层注入宿主 id
    vote: str = Field(..., pattern=r"^(for|against)$")


class SunsetAttachBody(BaseModel):
    rule_name: str = Field(..., min_length=1)
    rule_type: str = "policy"
    expires_at: str = ""
    auto_kill: bool = True


class SunsetExtendBody(BaseModel):
    extension_days: int = Field(..., ge=1)
    reason: str = ""


class EthicsSubmitBody(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    category: str = "general"
    submitted_by: int = 0
    priority: str = "normal"


class EthicsResolveBody(BaseModel):
    verdict: str = Field(..., min_length=1)
    comment: str = ""
    reviewer_id: int = 0


class FutarchyProposeBody(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    proposal_type: str = "policy"
    proposed_by: int = 0
    market_params: dict = {}


class TOSPublishBody(BaseModel):
    version: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    effective_date: str = ""
    publisher_id: int = 0


class TOSAcceptBody(BaseModel):
    host_id: int
    version: str = Field(..., min_length=1)


# ======================== 弹劾 ========================

@router.post("/impeachment")
def initiate_impeachment(body: ImpeachmentBody,
                         host=Depends(get_current_host),
                         db: Session = Depends(get_db)):
    """发起弹劾案（仅宿主；发起人身份由鉴权层注入，忽略客户端传入）。"""
    case_id = impeachment_service.initiate(
        db, target_type=body.target_type, target_id=body.target_id,
        initiated_by=host.id, charges=body.charges,
    )
    return {"ok": True, "case_id": case_id}


@router.get("/impeachment/active")
def list_active_impeachments(db: Session = Depends(get_db)):
    """获取活跃弹劾案列表。"""
    cases = impeachment_service.get_active_cases(db)
    return {"cases": cases}


@router.post("/impeachment/{id}/vote")
def vote_impeachment(id: int, body: VoteBody,
                     host=Depends(get_current_host),
                     db: Session = Depends(get_db)):
    """对弹劾案进行投票（仅宿主；投票者身份注入 + 一人一票去重）。"""
    try:
        impeachment_service.vote(
            db, case_id=id, voter_host_id=host.id, vote=body.vote,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "case_id": id}


@router.post("/impeachment/{id}/resolve")
def resolve_impeachment(id: int,
                        host=Depends(get_current_host),
                        db: Session = Depends(get_db)):
    """裁决弹劾案（仅宿主；换城主属宿主专属权限，受最小法定人数 + 赞成比例双重阈值约束）。"""
    impeachment_service.resolve(db, case_id=id)
    case = impeachment_service.get_case(db, case_id=id)
    return case


# ======================== 日落条款 ========================

@router.post("/sunset/attach")
def attach_sunset(body: SunsetAttachBody, db: Session = Depends(get_db)):
    """附加日落条款。"""
    result = sunset_service.attach(
        db, rule_name=body.rule_name, rule_type=body.rule_type,
        expires_at=body.expires_at, auto_kill=body.auto_kill,
    )
    return {"ok": True, "result": result}


@router.get("/sunset/active")
def list_active_sunset(db: Session = Depends(get_db)):
    """获取活跃日落条款列表。"""
    clauses = sunset_service.get_active(db)
    return {"clauses": clauses}


@router.post("/sunset/{id}/extend")
def extend_sunset(id: int, body: SunsetExtendBody, db: Session = Depends(get_db)):
    """延期日落条款。"""
    sunset_service.extend(
        db, clause_id=id, extension_days=body.extension_days,
        reason=body.reason,
    )
    return {"ok": True, "clause_id": id}


@router.post("/sunset/check")
def check_sunset(db: Session = Depends(get_db)):
    """手动触发日落条款过期检查。"""
    result = sunset_service.check_expired(db)
    return {"ok": True, "expired": result}


# ======================== 伦理审查 ========================

@router.post("/ethics/submit")
def submit_ethics(body: EthicsSubmitBody, db: Session = Depends(get_db)):
    """提交伦理审查申请。"""
    result = ethics_review.submit(
        db, title=body.title, description=body.description,
        category=body.category, submitted_by=body.submitted_by,
        priority=body.priority,
    )
    return {"ok": True, "result": result}


@router.get("/ethics/pending")
def list_pending_ethics(db: Session = Depends(get_db)):
    """获取待审伦理审查列表。"""
    items = ethics_review.get_pending(db)
    return {"items": items}


@router.post("/ethics/{id}/resolve")
def resolve_ethics(id: int, body: EthicsResolveBody, db: Session = Depends(get_db)):
    """裁决伦理审查。"""
    ethics_review.resolve(
        db, review_id=id, verdict=body.verdict,
        comment=body.comment, reviewer_id=body.reviewer_id,
    )
    return {"ok": True, "review_id": id}


@router.get("/ethics/stats")
def ethics_stats(db: Session = Depends(get_db)):
    """伦理审查统计数据。"""
    return ethics_review.get_stats(db)


# ======================== Futarchy ========================

@router.post("/futarchy/propose")
def propose_futarchy(body: FutarchyProposeBody, db: Session = Depends(get_db)):
    """提交 Futarchy 提案。"""
    result = futarchy_service.propose(
        db, title=body.title, description=body.description,
        proposal_type=body.proposal_type, proposed_by=body.proposed_by,
        market_params=body.market_params,
    )
    return {"ok": True, "result": result}


@router.get("/futarchy/active")
def list_active_futarchy(db: Session = Depends(get_db)):
    """获取活跃 Futarchy 提案列表。"""
    proposals = futarchy_service.get_active(db)
    return {"proposals": proposals}


@router.post("/futarchy/{id}/execute")
def execute_futarchy(id: int, db: Session = Depends(get_db)):
    """执行 Futarchy 提案。"""
    result = futarchy_service.execute(db, proposal_id=id)
    return {"ok": True, "proposal_id": id, "result": result}


# ======================== TOS 管理 ========================

@router.post("/tos/publish")
def publish_tos(body: TOSPublishBody, db: Session = Depends(get_db)):
    """发布 TOS 版本（调用服务真实方法 tos_manager.publish 别名）。"""
    version_id = tos_manager.publish(
        db, version=body.version, content=body.content,
        effective_date=body.effective_date, publisher_id=body.publisher_id,
    )
    return {"ok": True, "version_id": version_id, "version": body.version}


@router.post("/tos/accept")
def accept_tos(body: TOSAcceptBody, db: Session = Depends(get_db)):
    """接受 TOS：服务真实签名为 accept(version_id, host_id, ...)，
    请求体以版本字符串提交，先按版本解析 version_id 再调用。"""
    version_id = tos_manager.resolve_version_id(db, body.version)
    if version_id is None:
        raise HTTPException(status_code=404,
                            detail=f"TOS version {body.version} not found")
    tos_manager.accept(db, version_id=version_id, host_id=body.host_id)
    return {"ok": True, "host_id": body.host_id, "version": body.version,
            "version_id": version_id}


@router.get("/tos/required/{host_id}")
def check_tos_required(host_id: int, db: Session = Depends(get_db)):
    """检查宿主是否需要接受新版 TOS。"""
    result = tos_manager.check_required(db, host_id=host_id)
    return result
