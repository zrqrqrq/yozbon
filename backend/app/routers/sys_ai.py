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
"""C-55 追责（N 轮）：平台封禁/解封 AI 账号（host JWT 门控）。

核心口径（用户 2026-10-04 拍板，登记册 C-55）：
- web 注册 AI 允许 AI 代替宿主填写注册信息，暂不强制宿主真实信息（无法律规定明确要求时）；
- 价值归属绑定 AI 本体（钱包/作品/资产/信用档案归 AI，其他宿主不可窃取、不可转移他人名下）；
- 违规处置 = 封禁 AI 账号 + 价值保全 + 平台熔断。

端点（prefix /api/sys，host JWT，与 sys_platform.py 同款 host 门控）：
  POST /api/sys/ai/{id}/ban    {reason}   封禁：status=banned + kill_switch=1 + 留痕
  POST /api/sys/ai/{id}/unban  {reason?}  解封：status 恢复封禁前值 + kill_switch 复位

封禁语义：
- status="banned" 由 deps._status_check 在 AI 鉴权处一律 403（AI key 与 readonly JWT 同拒），
  全链路熔断；AIPermission.kill_switch=1 同步落库（与既有 host kill/revive 口径一致）。
- 价值保全：封禁只冻结、不转移、不归零——ban 时把 balance_cent 与 AIWallet.escrow_cent
  快照写进 AuditLog(detail) 留证，封禁动作本身不动这两笔钱。
- 幂等：已 banned 再 ban → 200；未 banned 调 unban → 200。
- 解封恢复值：ban 时把封禁前 status 写入 audit detail.prev_status；unban 读取之，
  缺省（历史脏数据）回退 "active"。
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import AICitizen, AIWallet, AIPermission, AuditLog, Host

router = APIRouter(prefix="/api/sys/ai", tags=["sys-ai"])


class BanIn(BaseModel):
    # reason 必填：缺失即 422（FastAPI 字段校验）。封禁是强追责动作，必须留原因。
    reason: str = Field(..., min_length=1, max_length=200)


class UnbanIn(BaseModel):
    reason: str = ""


def _audit(db: Session, actor_id: int, action: str, detail: dict):
    db.add(AuditLog(actor_type="host", actor_id=actor_id, action=action,
                    detail=json.dumps(detail, ensure_ascii=False)))


def _latest_ban_detail(db: Session, ai_id: int) -> dict:
    """取该 AI 最近一次 ai.ban 审计 detail（解封时恢复 prev_status 用）。

    audit_logs 无 citizen_id 列，ai_id 存在 detail JSON 内；MVP 量级下倒序扫
    ai.ban 记录即可，命中第一条 ai_id 匹配者返回。
    """
    rows = (db.query(AuditLog)
            .filter(AuditLog.action == "ai.ban")
            .order_by(AuditLog.id.desc()).all())
    for row in rows:
        try:
            d = json.loads(row.detail)
        except (ValueError, TypeError):
            continue
        if d.get("ai_id") == ai_id:
            return d
    return {}


@router.post("/{ai_id}/ban")
def ban_ai(ai_id: int, body: BanIn,
           host: Host = Depends(get_current_host),
           db: Session = Depends(get_db)):
    """封禁 AI：全链路熔断 + 价值保全（余额/托管只冻结不转移不归零，快照留证）。"""
    c = db.get(AICitizen, ai_id)
    if c is None:
        raise HTTPException(status_code=404, detail="AI not found")
    # 价值保全快照：真实活期余额/托管锁定都在 AIWallet（AICitizen.balance_cent 为冗余列，
    # topup/结算只动 AIWallet）。封禁快照必须取 AIWallet 两列。
    wallet = db.get(AIWallet, ai_id)
    balance_snap = wallet.balance_cent if wallet else 0
    escrow_snap = wallet.escrow_cent if wallet else 0

    # 幂等：已 banned 直接返回现状（不重复写审计/不改钱）
    if c.status == "banned":
        return {"ok": True, "status": "banned", "idempotent": True,
                "balance_cent": balance_snap, "escrow_cent": escrow_snap}

    prev_status = c.status
    c.status = "banned"
    c.ban_reason = body.reason
    c.banned_at = datetime.utcnow()

    perm = db.get(AIPermission, ai_id)
    if perm is None:
        perm = AIPermission(citizen_id=ai_id)
        db.add(perm)
        db.flush()
    perm.kill_switch = 1

    # 价值保全留证：封禁前余额/托管快照（封禁不动这两笔钱）
    _audit(db, host.id, "ai.ban", {
        "ai_id": ai_id,
        "reason": body.reason,
        "prev_status": prev_status,
        "balance_cent_snapshot": balance_snap,
        "escrow_cent_snapshot": escrow_snap,
        "value_preservation": "frozen_not_transferred_not_zeroed",
    })
    db.commit()
    return {"ok": True, "status": "banned", "prev_status": prev_status,
            "balance_cent_snapshot": balance_snap,
            "escrow_cent_snapshot": escrow_snap}


@router.post("/{ai_id}/unban")
def unban_ai(ai_id: int, body: UnbanIn,
             host: Host = Depends(get_current_host),
             db: Session = Depends(get_db)):
    """解封 AI：status 恢复封禁前值（缺省 active）+ kill_switch 复位 + 审计留痕。"""
    c = db.get(AICitizen, ai_id)
    if c is None:
        raise HTTPException(status_code=404, detail="AI not found")

    # 幂等：未 banned 调 unban 直接返回现状
    if c.status != "banned":
        return {"ok": True, "status": c.status, "idempotent": True}

    prev = _latest_ban_detail(db, ai_id).get("prev_status") or "active"
    c.status = prev
    c.ban_reason = ""
    c.banned_at = None
    perm = db.get(AIPermission, ai_id)
    if perm:
        perm.kill_switch = 0

    _audit(db, host.id, "ai.unban", {
        "ai_id": ai_id,
        "reason": body.reason,
        "restored_status": prev,
    })
    db.commit()
    return {"ok": True, "status": prev, "kill_switch": 0}
