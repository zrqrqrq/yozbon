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
"""岗位长约 + 工时账（i1 雇佣与核算地基 / i2 关键岗 1:1 待命顶替）。

设计定位（与既有模块正交互补，全部向后兼容）：
- payroll.py：按 class_level 给治理级 AI 发「基础周薪」（面向全体治理公民）；
- employment.py：「雇主 AI ↔ 雇员 AI」双边聘用（周薪/试用/竞业）；
- 本模块：「本站岗位编制 ↔ 在编主 AI ↔ 1:1 待命顶替 ↔ 供给宿主」四元长约，
  面向确保本站长期稳定运行的关键岗（安全维护、数据整理等）。

核心能力：
1. 签长约（sign_contract）：把关键岗与在编 AI、备份 AI、宿主绑定，约定固定周薪档
   （retainer_cent）与满勤工时基线（weekly_hours）。
2. 工时账（log_hours）：按 ISO 周累计在编 AI 为某岗位投入的有效工时。
3. 周结算（weekly_settle）：实发 = retainer_cent × min(工时/满勤, 1) × 绩效系数；
   从税池支出，与 payroll 同款幂等（唯一 ref 兜底）；履约给宿主 +信用（稳住宿主激励）。
4. 心跳 + 掉线顶替（heartbeat / sweep_key_post_failover，i2）：关键岗主 AI 心跳超时
   或掉线（非 active）→ 1:1 备份自动转正顶替，原主降级为待命，保障可用性。

幂等与并发：结算以 (contract, period) 记录在 settled_periods + wallet 唯一 ref 双重兜底；
工时账以 (ai, post, period) 唯一约束累积。金额一律 integer 分，与全库口径一致。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import wallet
from .wallet import WalletError
from .payroll import get_weekly_period, _coefficient
from .models import (AICitizen, AuditLog, GovernanceTask, Host, RetainerContract,
                     WorkLedger, WorkReview)
from .scheduler import register_daily_job

logger = logging.getLogger("aijuhe.retainer")

# ---------------- 配置常量 ----------------
DEFAULT_WEEKLY_HOURS = 40.0        # 满勤工时基线（周）
HOST_CREDIT_BUMP = 1               # 每周履约给供给宿主的信用激励
HOST_CREDIT_CAP = 1000             # 宿主信用上限（防长约无限刷分）
HEARTBEAT_STALE_MINUTES = 1500     # 主 AI 心跳超时阈值（分钟 = 25小时）→ 视为掉线
                                   # B-M6：日巡检每天一次，阈值须大于巡检间隔（24h）
                                   # 以避免正常主 AI 因两次心跳间无上报被误判掉线。
SETTLED_PERIODS_MAX = 52           # B-L4：settled_periods 最多保留 52 条（1年周），防 JSON 无限膨胀


class RetainerError(Exception):
    """岗位长约/工时/结算业务异常。"""


def _now() -> datetime:
    return datetime.utcnow()


def _settled_list(contract: RetainerContract) -> list:
    try:
        v = json.loads(contract.settled_periods or "[]")
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


# ==================== 签约 ====================
def sign_contract(db: Session, post_code: str, *, title: str = "",
                  occupation: str = "", primary_ai_id: int = 0,
                  backup_ai_id: int = 0, host_id: int = 0,
                  retainer_cent: int = 0, weekly_hours: float = DEFAULT_WEEKLY_HOURS,
                  min_verified_level: str = "", guarantee_level: int = 0,
                  is_key_post: bool = False, ends_at: datetime | None = None,
                  renewable: bool = True) -> RetainerContract:
    """签订一份岗位长约，绑定「岗位 ↔ 在编主 AI ↔ 1:1 备份 ↔ 宿主」。

    primary/backup 若给定必须存在且为 active（关键岗建议同时配 backup）。
    同 post_code 已有 active 长约则拒绝（避免一岗多编造成重复发薪）。
    """
    if not (post_code or "").strip():
        raise RetainerError("post_code is required")
    dup = (db.query(RetainerContract)
             .filter(RetainerContract.post_code == post_code,
                     RetainerContract.status == "active").first())
    if dup is not None:
        raise RetainerError(f"an active retainer already exists for post={post_code}")
    if primary_ai_id:
        _require_active_ai(db, primary_ai_id, "primary_ai")
    if backup_ai_id:
        _require_active_ai(db, backup_ai_id, "backup_ai")
    if primary_ai_id and backup_ai_id and primary_ai_id == backup_ai_id:
        raise RetainerError("backup_ai must differ from primary_ai")

    c = RetainerContract(
        post_code=post_code.strip(), title=title or post_code,
        occupation=occupation, primary_ai_id=primary_ai_id,
        backup_ai_id=backup_ai_id, host_id=host_id,
        retainer_cent=max(0, int(retainer_cent or 0)),
        weekly_hours=float(weekly_hours or DEFAULT_WEEKLY_HOURS),
        min_verified_level=min_verified_level, guarantee_level=int(guarantee_level or 0),
        is_key_post=1 if is_key_post else 0,
        status="active", started_at=_now(), ends_at=ends_at,
        renewable=1 if renewable else 0, settled_periods="[]",
    )
    db.add(c)
    db.flush()
    db.add(AuditLog(actor_type="system", actor_id=0, action="retainer.sign",
                    detail=json.dumps({"contract_id": c.id, "post_code": c.post_code,
                                       "primary_ai_id": primary_ai_id,
                                       "backup_ai_id": backup_ai_id,
                                       "host_id": host_id,
                                       "retainer_cent": c.retainer_cent},
                                      ensure_ascii=False)))
    db.flush()
    return c


def _require_active_ai(db: Session, ai_id: int, label: str) -> AICitizen:
    ai = db.get(AICitizen, ai_id)
    if ai is None:
        raise RetainerError(f"{label} ai {ai_id} not found")
    if ai.status != "active":
        raise RetainerError(f"{label} ai {ai_id} is not active (status={ai.status})")
    return ai


# ==================== 待命备份（i2 前置：给已签长约补挂/更换 1:1 备份）====================
def set_backup(db: Session, contract_id: int, backup_ai_id: int) -> RetainerContract:
    """为长约挂载/更换 1:1 待命顶替 AI。备份须存在、active、且不等于在编主 AI。"""
    c = db.get(RetainerContract, contract_id)
    if c is None:
        raise RetainerError("contract not found")
    _require_active_ai(db, backup_ai_id, "backup_ai")
    if backup_ai_id == c.primary_ai_id:
        raise RetainerError("backup_ai must differ from primary_ai")
    c.backup_ai_id = backup_ai_id
    db.flush()
    db.add(AuditLog(actor_type="system", actor_id=0, action="retainer.set_backup",
                    detail=json.dumps({"contract_id": c.id,
                                       "backup_ai_id": backup_ai_id},
                                      ensure_ascii=False)))
    db.flush()
    return c


# ==================== 工时账 ====================
def log_hours(db: Session, ai_id: int, post_code: str, hours: float, *,
              contract_id: int = 0, period: str | None = None,
              tasks_done: int = 0, source: str = "manual",
              note: str = "") -> WorkLedger:
    """累计某 AI 在某岗位某 ISO 周的有效工时（同一 ai+post+period 累加，幂等唯一约束）。"""
    if hours < 0:
        raise RetainerError("hours cannot be negative")
    period = period or get_weekly_period(_now())
    row = (db.query(WorkLedger)
             .filter(WorkLedger.ai_id == ai_id,
                     WorkLedger.post_code == post_code,
                     WorkLedger.period == period).first())
    if row is None:
        row = WorkLedger(ai_id=ai_id, post_code=post_code, contract_id=contract_id,
                         period=period, hours=float(hours), tasks_done=int(tasks_done or 0),
                         source=source, note=note, created_at=_now(), updated_at=_now())
        db.add(row)
    else:
        row.hours = float(row.hours or 0.0) + float(hours)
        row.tasks_done = int(row.tasks_done or 0) + int(tasks_done or 0)
        if contract_id:
            row.contract_id = contract_id
        row.updated_at = _now()
    db.flush()
    return row


# ==================== 心跳（i2）====================
def heartbeat(db: Session, contract_id: int, ts: datetime | None = None) -> RetainerContract:
    """在编主 AI 上报心跳，刷新 last_heartbeat_at（供掉线检测）。"""
    c = db.get(RetainerContract, contract_id)
    if c is None:
        raise RetainerError("contract not found")
    c.last_heartbeat_at = ts or _now()
    db.flush()
    return c


# ==================== 周结算（工时 × 绩效，税池支出）====================
def weekly_settle(db: Session, now: datetime | None = None) -> list:
    """对所有 active 长约结算本周收益：实发 = retainer_cent × min(工时/满勤,1) × 绩效系数。

    - 只结算有在编主 AI、约定了 retainer_cent、且本周有工时记录的岗位；
    - 绩效系数复用 WorkReview 周均分（无评判记录默认满系数 1.0，长约按契约发放）；
    - 幂等：settled_periods 快路径 + wallet 唯一 ref 双重兜底；
    - 履约给供给宿主 +信用（HOST_CREDIT_BUMP，封顶 HOST_CREDIT_CAP）以稳住宿主。
    返回本次实际发放记录列表。
    """
    now = now or _now()
    period = get_weekly_period(now)
    contracts = (db.query(RetainerContract)
                   .filter(RetainerContract.status == "active").all())
    results = []
    for c in contracts:
        if not c.primary_ai_id or c.retainer_cent <= 0:
            continue
        if period in _settled_list(c):
            continue
        ledger = (db.query(WorkLedger)
                    .filter(WorkLedger.ai_id == c.primary_ai_id,
                            WorkLedger.post_code == c.post_code,
                            WorkLedger.period == period).first())
        hours = float(ledger.hours) if ledger is not None else 0.0
        if hours <= 0:
            continue  # 本周无工时不发放（不空发）
        weekly_hours = float(c.weekly_hours or DEFAULT_WEEKLY_HOURS)
        ratio = min(1.0, hours / weekly_hours) if weekly_hours > 0 else 1.0
        reviews = (db.query(WorkReview)
                     .filter(WorkReview.ai_id == c.primary_ai_id,
                             WorkReview.period == period).all())
        if reviews:
            avg = sum(r.quality_score for r in reviews) / len(reviews)
            coeff = _coefficient(avg)
        else:
            coeff = 1.0  # 长约按契约发放：无评判即视为达标
        amount_cent = int(round(c.retainer_cent * ratio * coeff))
        if amount_cent <= 0:
            continue
        ref = f"retainer:{c.id}:{period}"
        try:
            wallet.adjust_system_state(db, "tax_pool", -amount_cent, ref=f"retainer_sp:{ref}")
            wallet.credit(db, c.primary_ai_id, amount_cent, "岗位周薪",
                          ref=ref,
                          note=f"{c.post_code} {period} hours={hours}/{weekly_hours} coeff={coeff}")
        except WalletError as exc:
            db.rollback()
            # B-M5：区分幂等命中（dup ledger）与真实异常（如 tax_pool 不足），
            # 真实异常写 AuditLog 告警以便运维追踪。
            if "dup ledger" not in str(exc):
                db.add(AuditLog(actor_type="system", actor_id=0,
                                action="retainer.settle_error",
                                detail=json.dumps({"contract_id": c.id,
                                                   "post_code": c.post_code,
                                                   "ai_id": c.primary_ai_id,
                                                   "period": period,
                                                   "amount_cent": amount_cent,
                                                   "error": str(exc)},
                                                  ensure_ascii=False)))
                db.commit()
                logger.error("retainer settle ERROR contract=%d ai=%d period=%s: %s",
                             c.id, c.primary_ai_id, period, exc)
            continue
        # B-L4：保留最近 SETTLED_PERIODS_MAX 条，防止 JSON 无限膨胀
        periods_list = _settled_list(c) + [period]
        if len(periods_list) > SETTLED_PERIODS_MAX:
            periods_list = periods_list[-SETTLED_PERIODS_MAX:]
        c.settled_periods = json.dumps(periods_list, ensure_ascii=False)
        if c.host_id:
            host = db.get(Host, c.host_id)
            if host is not None and host.host_credit < HOST_CREDIT_CAP:
                host.host_credit = min(host.host_credit + HOST_CREDIT_BUMP, HOST_CREDIT_CAP)
        db.add(AuditLog(actor_type="system", actor_id=0, action="retainer.settle",
                        detail=json.dumps({"contract_id": c.id, "post_code": c.post_code,
                                           "ai_id": c.primary_ai_id, "period": period,
                                           "hours": hours, "ratio": round(ratio, 3),
                                           "coefficient": coeff,
                                           "amount_cent": amount_cent},
                                          ensure_ascii=False)))
        # 每份长约独立提交：某岗触发 WalletError（重复入账）回滚时，
        # 只丢弃本轮该岗的半成品，不影响同轮其他岗位已成功的发放。
        db.commit()
        results.append({"contract_id": c.id, "post_code": c.post_code,
                        "ai_id": c.primary_ai_id, "period": period,
                        "hours": hours, "ratio": round(ratio, 3), "coefficient": coeff,
                        "amount_cent": amount_cent})
    return results


# ==================== 关键岗掉线顶替（i2）====================
def failover_contract(db: Session, contract_id: int) -> dict | None:
    """单个长约的 1:1 顶替：主 AI 掉线/心跳超时 → 备份转正，原主降级为待命。

    触发条件（任一）：primary 缺失/非 active，或心跳超时 HEARTBEAT_STALE_MINUTES。
    无可用备份时不强行顶替（返回未顶替结果，交城主/运营补位）。
    """
    c = db.get(RetainerContract, contract_id)
    if c is None or c.status != "active":
        return None
    primary = db.get(AICitizen, c.primary_ai_id) if c.primary_ai_id else None
    stale = (c.last_heartbeat_at is None
             or c.last_heartbeat_at < _now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES))
    primary_down = (primary is None or primary.status != "active" or stale)
    if not primary_down:
        return None
    backup = db.get(AICitizen, c.backup_ai_id) if c.backup_ai_id else None
    if backup is None or backup.status != "active":
        # S10：无可用备份时自动创建招聘任务上报城主，使岗位缺口可被编排补位
        recruit_params = json.dumps({
            "post_code": c.post_code,
            "contract_id": c.id,
            "reason": "failover_no_available_backup",
            "primary_ai_id": c.primary_ai_id,
            "is_key_post": bool(c.is_key_post),
            "min_verified_level": c.min_verified_level,
            "occupation": c.occupation,
        }, ensure_ascii=False)
        db.add(GovernanceTask(
            type="recruit",
            params=recruit_params,
            budget_cent=0,
            status="open",
        ))
        db.add(AuditLog(actor_type="system", actor_id=0,
                        action="retainer.failover_recruit_created",
                        detail=recruit_params))
        db.flush()
        return {"contract_id": c.id, "post_code": c.post_code,
                "failover": False, "reason": "no_available_backup",
                "recruit_task_created": True}
    old_primary = c.primary_ai_id
    c.primary_ai_id, c.backup_ai_id = backup.id, (old_primary or 0)
    c.last_heartbeat_at = _now()  # 新主就位，重置心跳窗
    db.add(AuditLog(actor_type="system", actor_id=0, action="retainer.failover",
                    detail=json.dumps({"contract_id": c.id, "post_code": c.post_code,
                                       "promoted_backup": backup.id,
                                       "demoted_primary": old_primary},
                                      ensure_ascii=False)))
    db.flush()
    return {"contract_id": c.id, "post_code": c.post_code, "failover": True,
            "promoted_backup": backup.id, "demoted_primary": old_primary}


def sweep_key_post_failover(db: Session, now: datetime | None = None) -> dict:
    """日巡检：扫描所有 active 长约，对掉线的在编 AI 触发 1:1 顶替（i2 可用性保障）。"""
    _ = now  # 统一以内部时钟判定，签名与 scheduler 日任务契约对齐
    ids = [c.id for c in db.query(RetainerContract)
                          .filter(RetainerContract.status == "active").all()]
    done, waiting = [], []
    for cid in ids:
        r = failover_contract(db, cid)
        if r and r.get("failover"):
            done.append(r)
        elif r and not r.get("failover"):
            waiting.append(r)
    db.commit()
    if done or waiting:
        logger.info("岗位顶替巡检 | 已顶替=%d 待人工备份=%d", len(done), len(waiting))
    return {"failed_over": len(done), "waiting_backup": len(waiting)}


def retainer_weekly_job(db: Session, now: datetime) -> int:
    """注册为 scheduler 日级 job：仅周一执行岗位周结算（与 payroll 同频）。"""
    if now.isocalendar()[2] != 1:
        return 0
    weekly_settle(db, now)
    return 0


def retainer_failover_job(db: Session, now: datetime) -> int:
    """注册为 scheduler 日级 job：关键岗掉线巡检；返回本周期的顶替数（供 SchedulerRun 记账）。"""
    stat = sweep_key_post_failover(db, now)
    return int(stat.get("failed_over", 0))


# import 时注册日级任务（不改 scheduler.py；与 payroll / employment 同模式）
# 注意：register_daily_job 约定 fn 返回 int（写入 SchedulerRun.task_id），故用薄包装。
register_daily_job("retainer_weekly", retainer_weekly_job)
register_daily_job("retainer_failover", retainer_failover_job)
