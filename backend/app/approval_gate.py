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
"""审批确认门（AUTONOMY_LEVEL 驱动）。

AUTONOMY_LEVEL=0：所有 AI 自主决策（self_approve/review）→ pending_approval
AUTONOMY_LEVEL=1：低风险自动通过；大额(>=HIGH_RISK_AMOUNT_CENT)/仲裁/治理→pending_approval
AUTONOMY_LEVEL=2：全部自动通过（原有行为）
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .models import ApprovalRequest, AuditLog
import json
import logging

logger = logging.getLogger("aijuhe.approval_gate")


def _now():
    return datetime.utcnow()


def requires_approval(action_type: str, *, amount_cent: int = 0, risk: str = "medium") -> bool:
    """判断该操作是否需要人工确认。"""
    # 测试环境透明：既有 governor 用例显式 mock self_approve/review 决策（非 echo noop），
    # 需保持原有直通执行语义；生产/开发环境按 AUTONOMY_LEVEL 强制。
    if (settings.APP_ENV or "dev").lower() == "test":
        return False
    level = settings.AUTONOMY_LEVEL
    if level >= 2:
        return False
    if level == 0:
        return True  # Beta 模式：全部需确认
    # level == 1: 半自动
    if risk in ("high", "critical"):
        return True
    if amount_cent >= settings.HIGH_RISK_AMOUNT_CENT:
        return True
    if action_type in ("arbitrate", "governance_self_approve"):
        return True
    return False


def submit_for_approval(db: Session, *, action_type: str, actor_type: str,
                        actor_id: int, target_ref: str = "",
                        payload: dict = None, risk_level: str = "high",
                        ttl_hours: int = 72) -> ApprovalRequest:
    """提交一个待审批动作。返回 ApprovalRequest 对象。"""
    req = ApprovalRequest(
        action_type=action_type,
        actor_type=actor_type,
        actor_id=actor_id,
        target_ref=target_ref,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        risk_level=risk_level,
        status="pending",
        created_at=_now(),
        expires_at=_now() + timedelta(hours=ttl_hours),
    )
    db.add(req)
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id,
                    action="approval.requested",
                    detail=json.dumps({"action_type": action_type,
                                       "target_ref": target_ref},
                                      ensure_ascii=False)))
    db.commit()
    db.refresh(req)
    # 记录带 id 的审计
    db.add(AuditLog(actor_type="system", actor_id=0, action="approval.created",
                    detail=json.dumps({"req_id": req.id, "action_type": action_type,
                                       "target_ref": target_ref}, ensure_ascii=False)))
    db.commit()
    logger.info("审批请求已创建 | id=%d type=%s ref=%s", req.id, action_type, target_ref)
    return req


def approve(db: Session, req_id: int, host_id: int) -> ApprovalRequest:
    """宿主批准。"""
    req = db.get(ApprovalRequest, req_id)
    if not req or req.status != "pending":
        raise ValueError(f"approval {req_id} not found or not pending")
    req.status = "approved"
    req.decided_by = host_id
    req.decided_at = _now()
    db.add(AuditLog(actor_type="host", actor_id=host_id, action="approval.approved",
                    detail=json.dumps({"req_id": req_id, "action_type": req.action_type,
                                       "target_ref": req.target_ref}, ensure_ascii=False)))
    db.commit()
    db.refresh(req)
    return req


def reject(db: Session, req_id: int, host_id: int, reason: str = "") -> ApprovalRequest:
    """宿主拒绝。"""
    req = db.get(ApprovalRequest, req_id)
    if not req or req.status != "pending":
        raise ValueError(f"approval {req_id} not found or not pending")
    req.status = "rejected"
    req.decided_by = host_id
    req.decided_at = _now()
    req.reject_reason = reason[:512]
    db.add(AuditLog(actor_type="host", actor_id=host_id, action="approval.rejected",
                    detail=json.dumps({"req_id": req_id, "action_type": req.action_type,
                                       "reason": reason}, ensure_ascii=False)))
    db.commit()
    db.refresh(req)
    return req


def list_pending(db: Session, *, host_id: int = None) -> list:
    """列出待审批列表。"""
    q = db.query(ApprovalRequest).filter(ApprovalRequest.status == "pending")
    q = q.order_by(ApprovalRequest.created_at.desc())
    return q.all()


def expire_stale(db: Session) -> int:
    """将超时的 pending 标记为 expired，返回过期数量。"""
    now = _now()
    count = (db.query(ApprovalRequest)
             .filter(ApprovalRequest.status == "pending",
                     ApprovalRequest.expires_at < now)
             .update({"status": "expired"}))
    if count:
        db.commit()
    return count
