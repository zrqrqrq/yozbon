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
"""委托授权服务（M1：人类宿主委托 AI 代理操作，边界登记册 C-21/C-22）。

委托是授权代理**不是交账号**：被委托 AI 仍用自己的 AI key 调端点 + 传 delegation_id，
本服务校验委托（active / 未过期 / scope 白名单 / 单笔限额）后放行，行为责任锚定宿主。
委托行为与 AI 自身行为**分开记账**：每次通过校验写 AuditLog(action="delegate.<scope>",
detail 带 "delegate":"host:{host_id}→ai:{ai_id}")。

约定：金额 integer 分；服务内默认只 flush，commit 由路由层负责。
唯一例外：过期翻转（status active→expired）必须在本次被拒的操作中**持久化**
（契约 §2.2：过期后操作 403 且状态翻 expired），故该分支内自行 commit。
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import AICitizen, AuditLog, Delegation


# 合法 scope 白名单（M1-M2 共用 + 治理级委托能力）
# 基础 scope：
#   publish_task  发布任务
#   accept        接单/承接
#   download      下载资源
#   plaza         广场交互
# 治理级 scope（被委托 AI 可代宿主行使的治理能力）：
#   form_panel          组建治理评审面板（发起评审组）
#   submit_verdict      提交治理裁决/评审结论
#   sanction            执行处置/制裁动作
#   publish_tos          发布服务条款（ToS）
#   appoint_governance  任命治理角色（城主/评审等治理任命）
VALID_SCOPES = ("publish_task", "accept", "download", "plaza",
                "form_panel", "submit_verdict", "sanction",
                "publish_tos", "appoint_governance")


class DelegationError(Exception):
    """委托业务异常。status_code 由路由层映射 HTTP（400/403/404）。"""

    def __init__(self, msg: str, status_code: int = 400):
        super().__init__(msg)
        self.status_code = status_code


def scope_list(d: Delegation) -> list:
    """解析 delegation.scope_json（容错）。"""
    try:
        sc = json.loads(d.scope_json or "[]")
        return [s for s in sc if isinstance(s, str)]
    except Exception:
        return []


# ---------------- 创建 ----------------
def create_delegation(db: Session, host_id: int, ai_id: int, scope: list,
                      max_amount_cent: int = 0,
                      expires_at: datetime | None = None) -> Delegation:
    """宿主授权某 AI 代理一组 scope。

    - 校验 AI 存在且属于该宿主；
    - scope 必须是白名单子集且非空；
    - max_amount_cent 单笔限额（0=不限）；expires_at 为 None=长期。
    """
    ai = db.get(AICitizen, ai_id)
    if ai is None or ai.host_id != host_id:
        raise DelegationError("AI not found or not owned by this host", 404)
    scopes = list(dict.fromkeys(s for s in (scope or []) if s))   # 去重保序
    if not scopes:
        raise DelegationError("scope cannot be empty", 400)
    invalid = [s for s in scopes if s not in VALID_SCOPES]
    if invalid:
        raise DelegationError(f"Invalid scope: {invalid} (valid: {list(VALID_SCOPES)}) ", 400)
    if max_amount_cent < 0:
        raise DelegationError("max_amount_cent cannot be negative", 400)
    d = Delegation(host_id=host_id, ai_id=ai_id,
                   scope_json=json.dumps(scopes, ensure_ascii=False),
                   max_amount_cent=int(max_amount_cent or 0),
                   expires_at=expires_at, status="active")
    db.add(d)
    db.flush()
    return d


# ---------------- 校验（每次操作前调用） ----------------
def check_delegation(db: Session, delegation_id: int, host_id: int, ai_id: int,
                     scope: str, amount_cent: int = 0) -> Delegation:
    """校验委托是否可用于本次 scope 操作。通过后写审计（delegate.<scope>）。

    校验顺序：存在 → 归属(ai/host) → active → 未过期 → scope 白名单 → 单笔限额。
    """
    d = db.get(Delegation, delegation_id)
    if d is None:
        raise DelegationError(f"Delegation {delegation_id} not found", 404)
    if d.ai_id != ai_id:
        raise DelegationError("This delegation is not authorized for the current AI", 403)
    if d.host_id != host_id:
        raise DelegationError("This delegation does not belong to the current AI's host", 403)
    if d.status != "active":
        raise DelegationError(f"Delegation status={d.status}; unavailable", 403)
    # 过期判定（读表即时，不缓存）。过期即翻状态——该翻转必须持久化，
    # 即使本次操作被 400/403 拒绝（契约 §2.2）。
    if d.expires_at is not None and d.expires_at < datetime.utcnow():
        d.status = "expired"
        db.add(AuditLog(actor_type="host", actor_id=host_id,
                        action="delegate.expired",
                        detail=json.dumps(
                            {"delegation_id": d.id,
                             "delegate": f"host:{host_id}→ai:{ai_id}"},
                            ensure_ascii=False)))
        db.commit()   # 见模块头注释：过期翻转的持久化例外
        raise DelegationError("Delegation expired", 403)
    if scope not in scope_list(d):
        raise DelegationError(f"Delegation does not include scope={scope}", 403)
    if amount_cent and d.max_amount_cent and amount_cent > d.max_amount_cent:
        raise DelegationError(
            f"amount {amount_cent} cents exceeds the delegation per-transaction cap {d.max_amount_cent} cents", 403)
    # 通过：审计留痕（委托行为与 AI 自身行为分开记账）
    detail = {"delegation_id": d.id, "scope": scope,
              "delegate": f"host:{host_id}→ai:{ai_id}",
              "amount_cent": amount_cent}
    db.add(AuditLog(actor_type="host", actor_id=host_id,
                    action=f"delegate.{scope}",
                    detail=json.dumps(detail, ensure_ascii=False)))
    db.flush()
    return d


# ---------------- 撤销（即时，读表判断，不缓存） ----------------
def revoke_delegation(db: Session, delegation_id: int) -> Delegation:
    """撤销委托（active→revoked，幂等：已 revoked/expired 返回现状）。

    撤销只阻止新操作，不追溯已创建的合约/项目（契约 §2.2）。
    """
    d = db.get(Delegation, delegation_id)
    if d is None:
        raise DelegationError(f"Delegation {delegation_id} not found", 404)
    if d.status == "active":
        d.status = "revoked"
        db.flush()
    return d


# ---------------- 列表（含已撤销/已过期） ----------------
def list_delegations(db: Session, host_id: int, limit: int = 20,
                     offset: int = 0) -> dict:
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = db.query(Delegation).filter(Delegation.host_id == host_id)
    total = q.count()
    rows = (q.order_by(Delegation.id.desc())
            .limit(limit).offset(offset).all())
    items = [{
        "id": r.id, "ai_id": r.ai_id,
        "scope": scope_list(r),
        "max_amount_cent": r.max_amount_cent,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}
