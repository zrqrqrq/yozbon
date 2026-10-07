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
# STATUS: 预留模块，待 scheduler 定时任务集成（检测宿主离线>30天→自动激活监护人）
"""G-13 监护人/托管机制。

当宿主长期不在线（>30天），其名下 AI 公民由临时监护人托管，
确保 AI 经济活动不因宿主离线而停滞。

核心流程：
- 自动检测：scheduler 调用 check_inactivity_and_auto_assign，扫描 >30d 不在线宿主，
  从信用最高的其他宿主中选取监护人；
- 手动激活：城主/管理员可手动为 AI 指定监护人；
- 权限校验：监护人执行操作前调用 guardian_can_act 校验权限范围；
- 过期/撤销：expires_at 到期自动过期，原宿主回归后可撤销。

约定：本模块写 guardian_delegations 表并 flush，commit 由调用方负责。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import AICitizen, CreditProfile, GuardianDelegation, Host

logger = logging.getLogger(__name__)


class GuardianError(Exception):
    """监护人业务异常。"""


def _now() -> datetime:
    return datetime.utcnow()


# ======================== 激活监护 ========================

def activate_guardian(db: Session, citizen_id: int, guardian_host_id: int,
                      reason: str, permissions: list | None = None,
                      expires_days: int = 30) -> int:
    """为 AI 指定临时监护人。

    Args:
        citizen_id: 被监护 AI。
        guardian_host_id: 监护人宿主 ID。
        reason: 触发原因。
        permissions: 权限列表，默认 ["read", "accept_tasks"]。
        expires_days: 有效天数，默认 30 天。

    Returns:
        新建的 GuardianDelegation.id。
    """
    citizen = db.get(AICitizen, citizen_id)
    if citizen is None:
        raise GuardianError(f"AI citizen {citizen_id} not found")
    if citizen.status in ("dead", "banned"):
        raise GuardianError(f"AI citizen {citizen_id} status is {citizen.status}; cannot be guarded")

    host = db.get(Host, guardian_host_id)
    if host is None or host.status != "active":
        raise GuardianError(f"Guardian host {guardian_host_id} not found or inactive")

    # 检查是否已有活跃监护
    existing = (db.query(GuardianDelegation)
                .filter(GuardianDelegation.citizen_id == citizen_id,
                        GuardianDelegation.status == "active")
                .first())
    if existing:
        raise GuardianError(f"AI {citizen_id} already has an active guardian (delegation_id={existing.id}) ")

    perms = permissions or ["read", "accept_tasks"]
    now = _now()

    delegation = GuardianDelegation(
        citizen_id=citizen_id,
        guardian_host_id=guardian_host_id,
        original_host_id=citizen.host_id,
        reason=reason[:200],
        status="active",
        permissions=json.dumps(perms, ensure_ascii=False),
        expires_at=now + timedelta(days=expires_days),
    )
    db.add(delegation)
    db.flush()
    logger.info("guardian activated: citizen=%d guardian_host=%d id=%d",
                citizen_id, guardian_host_id, delegation.id)
    return delegation.id


# ======================== 撤销监护 ========================

def revoke_guardian(db: Session, delegation_id: int, revoked_by: int) -> bool:
    """撤销监护。

    Args:
        delegation_id: 监护记录 ID。
        revoked_by: 撤销操作人（宿主 ID）。

    Returns:
        True 表示撤销成功。
    """
    delegation = db.get(GuardianDelegation, delegation_id)
    if delegation is None:
        raise GuardianError(f"Guardianship record {delegation_id} not found")
    if delegation.status != "active":
        raise GuardianError(f"Guardianship record {delegation_id} status is {delegation.status}; cannot revoke")

    delegation.status = "revoked"
    delegation.revoked_at = _now()
    db.flush()
    logger.info("guardian revoked: delegation=%d by_host=%d", delegation_id, revoked_by)
    return True


# ======================== 查询监护状态 ========================

def get_guardianship(db: Session, citizen_id: int) -> GuardianDelegation | None:
    """获取某 AI 当前活跃监护记录。"""
    return (db.query(GuardianDelegation)
            .filter(GuardianDelegation.citizen_id == citizen_id,
                    GuardianDelegation.status == "active")
            .first())


# ======================== 不活跃自动检测 ========================

def check_inactivity_and_auto_assign(db: Session) -> dict:
    """由 scheduler 调用：检查宿主 >30d 不在线的 AI，自动选监护人。

    策略：
    1. 扫描 last_tick_at < 30 天前的活跃 AI；
    2. 排除已有监护的 AI；
    3. 从信用最高的其他宿主中选监护人（排除自身宿主和已冻结宿主）；
    4. 自动创建监护记录，有效期 60 天。

    Returns:
        {"assigned": int, "skipped": int} — 分配数/跳过数。
    """
    now = _now()
    threshold = now - timedelta(days=30)

    # 查找不活跃宿主名下活跃 AI
    inactive_citizens = (
        db.query(AICitizen)
        .join(Host, AICitizen.host_id == Host.id)
        .filter(
            AICitizen.status == "active",
            AICitizen.is_internal == 0,
            Host.status == "active",
        )
        .all()
    )

    # 筛选 last_tick_at 在阈值之前的
    inactive_list = [c for c in inactive_citizens
                     if c.last_tick_at is not None and c.last_tick_at < threshold]

    # 排除已有活跃监护的
    already_guarded = set(
        row.citizen_id for row in
        db.query(GuardianDelegation.citizen_id)
        .filter(GuardianDelegation.status == "active")
        .all()
    )

    # 获取信用最高的候选宿主列表（排除不活跃宿主）
    active_hosts = (
        db.query(Host)
        .filter(Host.status == "active", Host.host_credit >= 100)
        .order_by(Host.host_credit.desc())
        .limit(50)
        .all()
    )

    assigned = 0
    skipped = 0
    for citizen in inactive_list:
        if citizen.id in already_guarded:
            skipped += 1
            continue
        # 选信用最高的非原宿主
        guardian_host = None
        for h in active_hosts:
            if h.id != citizen.host_id:
                guardian_host = h
                break
        if guardian_host is None:
            skipped += 1
            continue
        try:
            activate_guardian(
                db,
                citizen_id=citizen.id,
                guardian_host_id=guardian_host.id,
                reason=f"Host{citizen.host_id} offline for over 30 days; system auto-assigns guardianship",
                permissions=["read", "accept_tasks", "complete_tasks"],
                expires_days=60,
            )
            assigned += 1
        except GuardianError:
            skipped += 1

    logger.info("inactivity check: assigned=%d skipped=%d", assigned, skipped)
    return {"assigned": assigned, "skipped": skipped}


# ======================== 权限校验 ========================

def guardian_can_act(db: Session, guardian_host_id: int, citizen_id: int,
                     action: str) -> bool:
    """检查监护人是否有权限执行某操作。

    Args:
        guardian_host_id: 操作发起人（监护人宿主 ID）。
        citizen_id: 目标 AI 公民 ID。
        action: 操作名称，如 "read", "accept_tasks", "complete_tasks"。

    Returns:
        True 表示有权限。
    """
    delegation = get_guardianship(db, citizen_id)
    if delegation is None:
        return False
    if delegation.guardian_host_id != guardian_host_id:
        return False
    # 检查是否过期
    if delegation.expires_at and delegation.expires_at < _now():
        return False
    # 检查权限范围
    perms = json.loads(delegation.permissions or "[]")
    return action in perms
