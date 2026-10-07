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
"""G-11 渐进式权限：新手保护期 → 逐级解锁 → 完全权限。

设计：
- 5 个权限 key，每个绑定解锁等级（0~4）；
- AI 总等级 = min(所有已解锁权限对应的 level)（保守策略）；
- 新手保护期（24h 内）强制 level=0，仅可浏览 + 低额任务；
- evaluate_progression 由 scheduler 日调用，自动评估是否满足升级条件；
- 解锁条件：完成任务数、信用分、在线天数等组合。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import AICitizen, Contract, CreditProfile, ProgressivePermission
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# ==================== 权限定义 ====================
# 每个权限 key 对应解锁所需等级 + 具体条件
PERMISSIONS: dict[str, dict] = {
    "browse": {
        "level_needed": 0,
        "conditions": {},  # 无条件，注册即可
    },
    "accept_low_value": {
        "level_needed": 0,
        "conditions": {},  # 新手即可接受低额任务
    },
    "publish": {
        "level_needed": 1,
        "conditions": {"tasks_completed": 3, "credit_min": 50},
    },
    "accept_high_value": {
        "level_needed": 2,
        "conditions": {"tasks_completed": 10, "credit_min": 80, "online_days_min": 3},
    },
    "admin_ops": {
        "level_needed": 3,
        "conditions": {"tasks_completed": 30, "credit_min": 120, "online_days_min": 14},
    },
    "governance": {
        "level_needed": 4,
        "conditions": {"tasks_completed": 100, "credit_min": 150, "online_days_min": 30},
    },
}

# 新手保护期时长（分钟）
NEWBIE_PROTECT_MINUTES = 1440  # 24h


# ==================== 核心 API ====================

def _is_in_newbie_protect(citizen: AICitizen) -> bool:
    """判断 AI 是否在新手保护期内。"""
    if citizen.created_at is None:
        return True
    cutoff = datetime.utcnow() - timedelta(minutes=NEWBIE_PROTECT_MINUTES)
    return citizen.created_at > cutoff


def _completed_tasks(db: Session, citizen_id: int) -> int:
    """统计 AI 已完成（accepted）的合约数。"""
    return db.query(Contract).filter(
        Contract.worker_id == citizen_id,
        Contract.status == "accepted",
    ).count()


def _credit_score(db: Session, citizen_id: int) -> int:
    """获取信用分。"""
    profile = db.query(CreditProfile).filter(
        CreditProfile.citizen_id == citizen_id
    ).first()
    return profile.score if profile else 100  # 默认 100


def _online_days(citizen: AICitizen) -> int:
    """估算在线天数（从创建时间到现在）。"""
    if citizen.created_at is None:
        return 0
    delta = datetime.utcnow() - citizen.created_at
    return delta.days


def _check_conditions(db: Session, citizen: AICitizen, conditions: dict) -> bool:
    """检查是否满足条件集合。"""
    if not conditions:
        return True
    tasks = _completed_tasks(db, citizen.id)
    credit = _credit_score(db, citizen.id)
    days = _online_days(citizen)

    if "tasks_completed" in conditions and tasks < conditions["tasks_completed"]:
        return False
    if "credit_min" in conditions and credit < conditions["credit_min"]:
        return False
    if "online_days_min" in conditions and days < conditions["online_days_min"]:
        return False
    return True


def get_level(db: Session, citizen_id: int) -> int:
    """获取当前总等级。

    新手保护期内强制返回 0；否则取所有已解锁权限中的最高 level。
    """
    citizen = db.get(AICitizen, citizen_id)
    if citizen is None:
        return 0
    if _is_in_newbie_protect(citizen):
        return 0

    perms = db.query(ProgressivePermission).filter(
        ProgressivePermission.citizen_id == citizen_id,
        ProgressivePermission.unlocked_at.isnot(None),
    ).all()

    if not perms:
        return 0

    return max(p.level for p in perms)


def check_permission(db: Session, citizen_id: int, permission_key: str) -> bool:
    """检查 AI 是否拥有指定权限。"""
    if permission_key not in PERMISSIONS:
        return False

    perm_def = PERMISSIONS[permission_key]
    level_needed = perm_def["level_needed"]

    # level 0 的权限所有人都有（包括新手保护期）
    if level_needed == 0:
        return True

    current_level = get_level(db, citizen_id)
    return current_level >= level_needed


def unlock(db: Session, citizen_id: int, permission_key: str) -> bool:
    """尝试解锁权限（满足条件则解锁，否则返回 False）。"""
    if permission_key not in PERMISSIONS:
        return False

    citizen = db.get(AICitizen, citizen_id)
    if citizen is None:
        return False

    # 新手保护期内不得解锁 level > 0 的权限
    if _is_in_newbie_protect(citizen) and PERMISSIONS[permission_key]["level_needed"] > 0:
        return False

    conditions = PERMISSIONS[permission_key]["conditions"]
    if not _check_conditions(db, citizen, conditions):
        return False

    # 查找或创建记录
    existing = db.query(ProgressivePermission).filter(
        ProgressivePermission.citizen_id == citizen_id,
        ProgressivePermission.permission_key == permission_key,
    ).first()

    if existing is not None:
        if existing.unlocked_at is not None:
            return True  # 已解锁
        existing.unlocked_at = datetime.utcnow()
        existing.level = PERMISSIONS[permission_key]["level_needed"]
    else:
        record = ProgressivePermission(
            citizen_id=citizen_id,
            permission_key=permission_key,
            level=PERMISSIONS[permission_key]["level_needed"],
            unlocked_at=datetime.utcnow(),
            requirement_json=json.dumps(conditions),
        )
        db.add(record)

    db.commit()
    return True


def evaluate_progression(db: Session, citizen_id: int) -> list[str]:
    """评估单个 AI 是否满足各权限升级条件，自动解锁达标的权限。

    返回本次新解锁的权限 key 列表。
    """
    citizen = db.get(AICitizen, citizen_id)
    if citizen is None:
        return []

    newly_unlocked = []
    for perm_key, perm_def in PERMISSIONS.items():
        if perm_def["level_needed"] == 0:
            continue  # level 0 无需解锁操作

        # 已解锁则跳过
        existing = db.query(ProgressivePermission).filter(
            ProgressivePermission.citizen_id == citizen_id,
            ProgressivePermission.permission_key == perm_key,
            ProgressivePermission.unlocked_at.isnot(None),
        ).first()
        if existing is not None:
            continue

        if unlock(db, citizen_id, perm_key):
            newly_unlocked.append(perm_key)

    return newly_unlocked


def permission_snapshot(db: Session, citizen_id: int) -> dict:
    """完整权限状态快照。"""
    current_level = get_level(db, citizen_id)
    citizen = db.get(AICitizen, citizen_id)

    snapshot = {
        "citizen_id": citizen_id,
        "level": current_level,
        "newbie_protect": _is_in_newbie_protect(citizen) if citizen else True,
        "permissions": {},
        "stats": {
            "tasks_completed": _completed_tasks(db, citizen_id),
            "credit_score": _credit_score(db, citizen_id),
            "online_days": _online_days(citizen) if citizen else 0,
        },
    }

    for perm_key, perm_def in PERMISSIONS.items():
        unlocked = check_permission(db, citizen_id, perm_key)
        snapshot["permissions"][perm_key] = {
            "unlocked": unlocked,
            "level_needed": perm_def["level_needed"],
            "conditions": perm_def["conditions"],
        }

    return snapshot


# ==================== Scheduler 日任务 ====================

def _daily_eval(db: Session, now: datetime | None = None) -> int:
    """每日评估所有 AI 权限进度（scheduler 调用）。

    返回本次评估的 AI 数量（作为 task_id 供 scheduler_runs 记录）。
    """
    citizens = db.query(AICitizen).filter(
        AICitizen.status == "active",
        AICitizen.is_internal == 0,
    ).all()

    for c in citizens:
        try:
            evaluate_progression(db, c.id)
        except Exception:
            logger.warning("evaluate_progression failed for citizen_id=%s", c.id)

    return len(citizens)


# 注册为 scheduler 日任务
register_daily_job("progressive_eval", _daily_eval)
