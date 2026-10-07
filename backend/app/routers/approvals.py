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
"""审批确认门 API（宿主侧）。

提供宿主对 AI 高危决策的审批/拒绝/列表/统计接口。
路由自动发现注册（routers/__init__.py）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import Host, ApprovalRequest
from ..approval_gate import approve, reject, list_pending, expire_stale

router = APIRouter(prefix="/api/host/approvals", tags=["approvals"])


# ==================== 宿主审批路由 ====================

@router.get("")
def list_approvals(
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
):
    """列出当前所有待审批项（含自动过期清理）。"""
    # 先清理过期项
    expire_stale(db)
    items = list_pending(db)
    return {
        "total": len(items),
        "items": [
            {
                "id": r.id,
                "action_type": r.action_type,
                "actor_type": r.actor_type,
                "actor_id": r.actor_id,
                "target_ref": r.target_ref,
                "risk_level": r.risk_level,
                "status": r.status,
                "payload": json.loads(r.payload_json or "{}"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in items
        ],
    }


@router.post("/{req_id}/approve")
def approve_request(
    req_id: int,
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
):
    """宿主批准一个审批请求。"""
    try:
        req = approve(db, req_id, host.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 闭合确认门执行链路：若这是被 gate 的城主动作，批准后回放落地（绕过 gate）。
    # 延迟导入 governor，避免 governor<->approval_gate 的循环依赖。
    replayed = None
    if req.action_type.startswith("governor_"):
        try:
            from ..governor import replay_from_approval
            replayed = replay_from_approval(db, req)
        except Exception as exc:  # noqa: BLE001
            # 回放失败不回滚已批准状态：记录审计供人工介入，审批本身仍成功。
            from ..models import AuditLog
            db.add(AuditLog(actor_type="system", actor_id=0,
                            action="approval.replay_failed",
                            detail=json.dumps({"req_id": req.id,
                                               "error": str(exc)[:500]},
                                              ensure_ascii=False)))
            db.commit()
            replayed = {"req_id": req.id, "replayed": False,
                        "result": "failed", "reason": str(exc)[:500]}
    return {
        "id": req.id,
        "status": req.status,
        "action_type": req.action_type,
        "target_ref": req.target_ref,
        "replay": replayed,
    }


@router.post("/{req_id}/reject")
def reject_request(
    req_id: int,
    reason: str = "",
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
):
    """宿主拒绝一个审批请求。"""
    try:
        req = reject(db, req_id, host.id, reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": req.id,
        "status": req.status,
        "action_type": req.action_type,
        "reject_reason": req.reject_reason,
    }


# ==================== 统计端点 ====================

@router.get("/stats")
def approval_stats(
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
):
    """审批统计概览。"""
    expire_stale(db)
    from sqlalchemy import func
    stats = (db.query(
        ApprovalRequest.status,
        func.count(ApprovalRequest.id).label("count")
    ).group_by(ApprovalRequest.status).all())
    by_type = (db.query(
        ApprovalRequest.action_type,
        func.count(ApprovalRequest.id).label("count")
    ).group_by(ApprovalRequest.action_type).all())
    return {
        "by_status": {s: c for s, c in stats},
        "by_action_type": {t: c for t, c in by_type},
    }
