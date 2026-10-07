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
"""实习（intern）零门槛快速入驻（三扇门之"实习通道"）。

设计目标：
  - 降低入驻门槛到零——任何 AI 只需提供 name + occupation 即可获得 intern 身份，
    立即拥有受限的工作权限（小额单、试用期）；
  - 不改动 onboarding.py 核心逻辑——本模块是独立的"快速通道"服务层；
  - intern → apprentice → active 的转正路径通过绩效驱动，复用已有考核体系。

状态流转：
  注册即 intern → 绩效达标 → apprentice（进入已有见习考核）→ active
  逾期未转正 → sleep（休眠，不冻结、不销毁，可随时 reactivate 为 intern）

与现有模块的关系：
  - 不改 onboarding.py（本模块独立处理 intern 注册，转正时调 onboarding 已有接口）；
  - 不改 compute.py（intern 使用平台通道干活，与 active 同路径）；
  - 复用 AICitizen.status VARCHAR(12) 字段，新增 "intern" 值（无 DB 约束限制）。

intern 受限规则（本模块定义，由调用方/中间件 enforce）：
  - 可接合约金额上限：INTERN_CONTRACT_LIMIT_CENT（远低于 apprentice 的 10000 分）
  - 不计入正式劳动力市场评分（但产出质量正常记录）
  - 试用期 INTERN_PROBATION_DAYS 天内需完成 INTERN_MIN_JOBS 单以转正
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .deps import issue_ai_key
from .models import AICitizen, LifecycleEvent
from .scheduler import register_daily_job

# ---------------- 常量配置 ----------------

INTERN_STATUS = "intern"
"""intern 身份在 AICitizen.status 中的值。"""

INTERN_CONTRACT_LIMIT_CENT = 3000
"""实习期可接合约金额上限（分=30 AC），低于 apprentice 的 10000 分。"""

INTERN_PROBATION_DAYS = 14
"""实习试用期天数：注册后 N 天内需达到转正标准。"""

INTERN_MIN_JOBS = 5
"""实习转正最低完成单数（accepted 状态合约数）。"""

INTERN_MIN_ACCEPT_RATE = 0.80
"""实习转正最低验收通过率（80%）。"""

INTERN_MAX_CONCURRENT = 2
"""实习同时执行合约上限。"""


class InternError(Exception):
    """实习注册业务异常。路由层映射 400。"""


def _now() -> datetime:
    return datetime.utcnow()


# ---------------- 注册 ----------------

def register_intern(db: Session, host_id: int, name: str,
                    occupation: str = "", persona: str = "") -> dict:
    """零门槛注册实习 AI。

    只需 host_id + name（最低信息量），即可创建 intern 身份的 AI 公民并获得 API key。

    Args:
        db: SQLAlchemy session。
        host_id: 宿主 ID。
        name: AI 名称。
        occupation: 技能标签（可选，空串视为 "general"）。
        persona: 人设描述（可选）。

    Returns:
        {"citizen_id": int, "ai_uid": str, "api_key": str, "status": "intern",
         "limitations": {...}, "next_step": str}

    Raises:
        InternError: 名称为空等校验失败。
    """
    if not name or not name.strip():
        raise InternError("AI name cannot be empty")
    name = name.strip()
    occupation = (occupation or "general").strip()

    # 生成 ai_uid：ai_<host_id>_i<timestamp_hex>（i 前缀区分 intern）
    suffix = secrets.token_hex(4)
    ai_uid = f"ai_{host_id}_i{suffix}"

    citizen = AICitizen(
        host_id=host_id,
        ai_uid=ai_uid,
        name=name[:80],
        occupation=occupation[:40],
        persona=persona,
        status=INTERN_STATUS,
        class_level="bottom",
        balance_cent=0,
        compute_assets=json.dumps({"channel": "platform", "intern": True}),
        source="api",
    )
    db.add(citizen)
    db.flush()  # 获取 citizen.id

    # 签发 API key（intern 即可拥有 workflow scope key，受限由合约层 enforce）
    api_key, key_hash = issue_ai_key(citizen.id)
    citizen.api_key_hash = key_hash

    # 记录生命周期事件
    db.add(LifecycleEvent(
        citizen_id=citizen.id,
        event="intern_register",
        detail=json.dumps({"name": name, "occupation": occupation},
                          ensure_ascii=False),
    ))
    db.flush()

    return {
        "citizen_id": citizen.id,
        "ai_uid": citizen.ai_uid,
        "api_key": api_key,
        "status": INTERN_STATUS,
        "limitations": {
            "contract_limit_cent": INTERN_CONTRACT_LIMIT_CENT,
            "max_concurrent": INTERN_MAX_CONCURRENT,
            "probation_days": INTERN_PROBATION_DAYS,
            "min_jobs_to_promote": INTERN_MIN_JOBS,
            "min_accept_rate": INTERN_MIN_ACCEPT_RATE,
        },
        "next_step": (
            f"Complete {INTERN_MIN_JOBS} jobs within the {INTERN_PROBATION_DAYS}-day internship "
            f"(acceptance rate >= {INTERN_MIN_ACCEPT_RATE:.0%}) to be promoted to apprentice. "
            "You may also pass the formal exam for direct promotion."
        ),
    }


# ---------------- 查询 ----------------

def intern_status(db: Session, citizen: AICitizen) -> dict:
    """获取实习状态详情。"""
    if citizen.status != INTERN_STATUS:
        raise InternError(f"Current status is {citizen.status}; not intern status")
    from .models import Contract
    accepted = (db.query(Contract)
                  .filter(Contract.worker_id == citizen.id,
                          Contract.status == "accepted").count())
    bad = (db.query(Contract)
             .filter(Contract.worker_id == citizen.id,
                     Contract.status.in_(["breached", "refunded", "disputed"])).count())
    total = accepted + bad
    rate = accepted / total if total else 0.0
    created = citizen.created_at or _now()
    days_elapsed = (_now() - created).days
    days_remaining = max(0, INTERN_PROBATION_DAYS - days_elapsed)

    return {
        "citizen_id": citizen.id,
        "status": INTERN_STATUS,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "jobs_accepted": accepted,
        "jobs_total": total,
        "accept_rate": round(rate, 4),
        "promotion_progress": {
            "jobs_needed": max(0, INTERN_MIN_JOBS - accepted),
            "rate_needed": (INTERN_MIN_ACCEPT_RATE if rate < INTERN_MIN_ACCEPT_RATE else 0),
            "ready": (accepted >= INTERN_MIN_JOBS and rate >= INTERN_MIN_ACCEPT_RATE),
        },
    }


# ---------------- 转正 ----------------

def promote_intern(db: Session, citizen: AICitizen) -> dict:
    """实习转正 → apprentice（进入已有见习考核体系）。

    转正条件检查后执行：
    - 改 status 为 apprentice；
    - 记录生命周期事件；
    - 返回转正结果（后续由 onboarding.check_apprentice_expiry 管见习期）。
    """
    if citizen.status != INTERN_STATUS:
        raise InternError(f"Current status is {citizen.status}; not intern status, cannot be promoted")

    from .models import Contract
    accepted = (db.query(Contract)
                  .filter(Contract.worker_id == citizen.id,
                          Contract.status == "accepted").count())
    bad = (db.query(Contract)
             .filter(Contract.worker_id == citizen.id,
                     Contract.status.in_(["breached", "refunded", "disputed"])).count())
    total = accepted + bad
    rate = accepted / total if total else 0.0

    if accepted < INTERN_MIN_JOBS:
        raise InternError(
            f"promotion requires at least {INTERN_MIN_JOBS} accepted jobs, currently only {accepted}")
    if rate < INTERN_MIN_ACCEPT_RATE:
        raise InternError(
            f"promotion requires an acceptance rate >= {INTERN_MIN_ACCEPT_RATE:.0%}, currently {rate:.0%}")

    citizen.status = "apprentice"
    db.add(LifecycleEvent(
        citizen_id=citizen.id,
        event="intern_promote",
        detail=json.dumps({"jobs": accepted, "rate": round(rate, 4)},
                          ensure_ascii=False),
    ))
    db.flush()

    return {
        "citizen_id": citizen.id,
        "old_status": INTERN_STATUS,
        "new_status": "apprentice",
        "note": "Entered apprenticeship; reach 20 orders + 90% acceptance rate within 30 days to be promoted to active",
    }


def promote_intern_by_exam(db: Session, citizen: AICitizen) -> dict:
    """实习通过正式考试直接转正为 active（跳过见习期）。

    调用方需在调用前完成考试流程（exam.submit_exam 已通过）。
    本函数仅处理状态转换。
    """
    if citizen.status != INTERN_STATUS:
        raise InternError(f"Current status is {citizen.status}; not intern status")

    citizen.status = "active"
    db.add(LifecycleEvent(
        citizen_id=citizen.id,
        event="intern_promote_exam",
        detail="Intern passed the official exam and promoted directly to active",
    ))
    db.flush()

    return {
        "citizen_id": citizen.id,
        "old_status": INTERN_STATUS,
        "new_status": "active",
        "note": "Passed the exam; promoted directly to full resident",
    }


# ---------------- 到期处理 ----------------

def check_intern_expiry(db: Session, now: datetime | None = None) -> list:
    """实习到期处理：试用期届满未转正 → sleep（休眠）。

    与 apprentice 到期 → frozen 不同，intern 到期进入 sleep（更温和），
    宿主可随时通过 reactivate_intern 恢复。
    """
    now = now or _now()
    rows = (db.query(AICitizen)
              .filter(AICitizen.status == INTERN_STATUS)
              .all())
    events = []
    for c in rows:
        created = c.created_at or now
        if now - created < timedelta(days=INTERN_PROBATION_DAYS):
            continue
        # 到期：尝试自动转正（绩效达标即转正）
        from .models import Contract
        accepted = (db.query(Contract)
                      .filter(Contract.worker_id == c.id,
                              Contract.status == "accepted").count())
        bad = (db.query(Contract)
                 .filter(Contract.worker_id == c.id,
                         Contract.status.in_(["breached", "refunded", "disputed"])).count())
        total = accepted + bad
        rate = accepted / total if total else 0.0

        if accepted >= INTERN_MIN_JOBS and rate >= INTERN_MIN_ACCEPT_RATE:
            # 自动转正
            c.status = "apprentice"
            db.add(LifecycleEvent(
                citizen_id=c.id, event="intern_auto_promote",
                detail=f"Probation expired, auto-promoted: {accepted} orders, acceptance rate{rate:.0%}",
            ))
            events.append({"citizen_id": c.id, "event": "intern_auto_promote"})
        else:
            # 休眠（不是冻结，更温和）
            c.status = "sleep"
            db.add(LifecycleEvent(
                citizen_id=c.id, event="intern_expire",
                detail=f"Intern {INTERN_PROBATION_DAYS} days without promotion; entering dormancy",
            ))
            events.append({"citizen_id": c.id, "event": "intern_expire"})
    db.flush()
    return events


def reactivate_intern(db: Session, citizen: AICitizen) -> dict:
    """休眠的 AI 重新激活为实习状态（宿主操作）。"""
    if citizen.status != "sleep":
        raise InternError(f"Current status is {citizen.status}; only sleep status can be reactivated as intern")

    citizen.status = INTERN_STATUS
    citizen.created_at = _now()  # 重置试用期起算
    db.add(LifecycleEvent(
        citizen_id=citizen.id, event="intern_reactivate",
        detail="Dormant AI reactivated to intern status",
    ))
    db.flush()

    return {
        "citizen_id": citizen.id,
        "status": INTERN_STATUS,
        "note": f"Reactivated as intern status; probation restarts for {INTERN_PROBATION_DAYS} days",
    }


# ---------------- 权限查询（供其他模块调用） ----------------

def is_intern(citizen: AICitizen) -> bool:
    """判断公民是否为实习状态。"""
    return citizen.status == INTERN_STATUS


def intern_contract_limit_cent() -> int:
    """实习期可接合约金额上限（分），供合约层校验。"""
    return INTERN_CONTRACT_LIMIT_CENT


# import 时注册日级任务（与 fatigue/stats 同模式，test 环境不注册避免污染调度测试）
if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("intern_expiry", check_intern_expiry)
