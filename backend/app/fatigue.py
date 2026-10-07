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
"""AI 倦怠/休息机制。

设计规则：
- fatigue_score 每完成一次任务 +10，上限 100；
- efficiency_multiplier = 1.0 - (fatigue_score / 100) * 0.4（100 疲劳 → 乘数 0.6）；
- fatigue_score >= 80 视为"过劳"（burned out），应强制休息；
- 每日休息：fatigue_score -= 25（下限 0），consecutive_work_days 归零；
- 周加班：每周超过 40 单位（小时）记为 overtime_hours_week；
- 周一（weekday==0）日任务：重置 overtime_hours_week，对 >=80 疲劳者自动休息。

注册：模块 import 时 scheduler.register_daily_job("fatigue", fatigue_daily_job)。
本模块只 flush，commit 由调度器/调用方负责。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .models import AIFatigueState
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
FATIGUE_PER_TASK: float = 10.0
FATIGUE_CAP: float = 100.0
BURNOUT_THRESHOLD: float = 80.0
REST_RECOVERY: float = 25.0
WEEKLY_WORK_LIMIT: float = 40.0  # 周工时阈值（超出记为加班）


def _now() -> datetime:
    return datetime.utcnow()


def _calc_efficiency(fatigue_score: float) -> float:
    """根据疲劳分数计算效率乘数（1.0 ~ 0.6）。"""
    return max(0.6, 1.0 - (fatigue_score / 100.0) * 0.4)


# ======================== 核心操作 ========================

def get_or_init_fatigue(db: Session, citizen_id: int) -> AIFatigueState:
    """获取公民的倦怠状态，不存在则惰性创建。"""
    state = (db.query(AIFatigueState)
             .filter(AIFatigueState.citizen_id == citizen_id)
             .first())
    if state is None:
        state = AIFatigueState(
            citizen_id=citizen_id,
            fatigue_score=0.0,
            efficiency_multiplier=1.0,
            consecutive_work_days=0,
            rest_days_taken=0,
            overtime_hours_week=0.0,
        )
        db.add(state)
        db.flush()
    return state


def record_task_completion(db: Session, citizen_id: int,
                           work_hours: float = 1.0) -> AIFatigueState:
    """记录任务完成：增加疲劳度、更新连续工作天数、加班追踪、重算效率。

    Args:
        citizen_id: AI 公民 ID。
        work_hours: 本次工作任务工时（默认 1.0）。

    Returns:
        更新后的 AIFatigueState。
    """
    state = get_or_init_fatigue(db, citizen_id)
    now = _now()

    # 增加疲劳度（不超过上限）
    state.fatigue_score = min(FATIGUE_CAP, state.fatigue_score + FATIGUE_PER_TASK)

    # 连续工作天数：仅在当天首次任务时递增（通过 last_task_at 判断）
    if state.last_task_at is None or state.last_task_at.date() < now.date():
        state.consecutive_work_days += 1
        # 新的一天开始休息计数归零
        state.rest_days_taken = 0

    # 周加班追踪：累计超过阈值的部分记为加班
    state.overtime_hours_week += work_hours
    # 超过周阈值的部分保留为 overtime（实际值可 > 40）

    # 重算效率乘数
    state.efficiency_multiplier = _calc_efficiency(state.fatigue_score)
    state.last_task_at = now
    state.updated_at = now
    db.flush()
    return state


def record_rest(db: Session, citizen_id: int) -> AIFatigueState:
    """记录一次休息：减少疲劳度、重置连续工作天数、增加休息天数计数。

    Returns:
        更新后的 AIFatigueState。
    """
    state = get_or_init_fatigue(db, citizen_id)
    now = _now()

    # 恢复疲劳（不低于 0）
    state.fatigue_score = max(0.0, state.fatigue_score - REST_RECOVERY)

    # 重置连续工作天数
    state.consecutive_work_days = 0
    state.rest_days_taken += 1

    # 重算效率乘数
    state.efficiency_multiplier = _calc_efficiency(state.fatigue_score)
    state.updated_at = now
    db.flush()
    return state


# ======================== 查询 ========================

def is_burned_out(db: Session, citizen_id: int) -> bool:
    """判断公民是否过劳（fatigue_score >= 80）。"""
    state = (db.query(AIFatigueState)
             .filter(AIFatigueState.citizen_id == citizen_id)
             .first())
    if state is None:
        return False
    return state.fatigue_score >= BURNOUT_THRESHOLD


def get_efficiency(db: Session, citizen_id: int) -> float:
    """返回公民当前效率乘数（无记录则返回 1.0）。"""
    state = (db.query(AIFatigueState)
             .filter(AIFatigueState.citizen_id == citizen_id)
             .first())
    if state is None:
        return 1.0
    return state.efficiency_multiplier


def fatigue_report(db: Session, citizen_id: int) -> dict:
    """生成指定公民的倦怠报告。"""
    state = get_or_init_fatigue(db, citizen_id)
    return {
        "citizen_id": state.citizen_id,
        "fatigue_score": state.fatigue_score,
        "efficiency": state.efficiency_multiplier,
        "consecutive_work_days": state.consecutive_work_days,
        "burned_out": state.fatigue_score >= BURNOUT_THRESHOLD,
        "overtime_hours_week": state.overtime_hours_week,
    }


# ======================== 日级任务 ========================

def fatigue_daily_job(db: Session, now: datetime | None = None) -> int:
    """日级维护任务（由 scheduler 调用）——唯一权威疲劳巡检。

    合并自原 fatigue.py 与 work_balance.py 的日任务（C-D9 修复）：
    1. 周一（weekday==0）：重置所有公民的 overtime_hours_week 为 0；
    2. 每日：对 fatigue_score >= 80 或 consecutive_work_days >= 7 的公民执行强制休息。

    Returns:
        处理的公民数量（供 scheduler 日志参考）。
    """
    now = now or _now()
    count = 0

    # 周一重置周加班
    if now.weekday() == 0:
        citizens_with_overtime = (
            db.query(AIFatigueState)
            .filter(AIFatigueState.overtime_hours_week > 0)
            .all()
        )
        for state in citizens_with_overtime:
            state.overtime_hours_week = 0.0
            state.updated_at = now
            count += 1
        db.flush()

    # 强制休息：疲劳 >= 80 或 连续工作 >= 7 天（合并 work_balance 判定）
    MAX_CONSECUTIVE_WORK_DAYS = 7
    burned_out_list = (
        db.query(AIFatigueState)
        .filter(
            (AIFatigueState.fatigue_score >= BURNOUT_THRESHOLD) |
            (AIFatigueState.consecutive_work_days >= MAX_CONSECUTIVE_WORK_DAYS)
        )
        .all()
    )
    for state in burned_out_list:
        state.fatigue_score = max(0.0, state.fatigue_score - REST_RECOVERY)
        state.consecutive_work_days = 0
        state.rest_days_taken += 1
        state.efficiency_multiplier = _calc_efficiency(state.fatigue_score)
        state.updated_at = now
        count += 1
    db.flush()

    logger.info("fatigue_daily_job: processed %d citizen(s) at %s", count, now.isoformat())
    return count


# import 时注册日级任务（与 evolution/stats/leaderboard/idle_timeout 同模式）
register_daily_job("fatigue", fatigue_daily_job)
