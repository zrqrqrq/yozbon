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
"""综合工作-休息平衡服务（增强 fatigue.py）。

设计规则：
- 在 fatigue.py 底层模型之上提供更高层 API；
- 支持工作模式配置（标准/弹性/冲刺）；
- 效率计算综合考虑疲劳、连续工作天数、加班时长；
- 强制休息机制：疲劳 >= 80 或连续工作 >= 7 天自动触发；
- 休息恢复速度受休息时长影响（短时休息恢复较少，长休恢复较多）；
- 工作-休息报告：供管理面板展示个人平衡健康度。

依赖：
- AIFatigueState 模型（与 fatigue.py 共享）
- fatigue.py 中的常量与计算逻辑
"""
import logging
from datetime import datetime, timedelta

from .database import SessionLocal
from .config import settings
from .models import AIFatigueState
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
FATIGUE_CAP = 100.0
BURNOUT_THRESHOLD = 80.0
EFFICIENCY_FLOOR = 0.6
EFFICIENCY_CEILING = 1.0
MAX_CONSECUTIVE_WORK_DAYS = 7
OVERTIME_LIMIT_WEEK = 40.0  # 周工时阈值

# 工作模式配置
WORK_PATTERNS = {
    "standard": {
        "max_daily_hours": 8.0,
        "max_weekly_hours": 40.0,
        "required_rest_days": 2,
        "fatigue_rate_per_hour": 6.0,  # 每工作1小时增加疲劳
        "rest_recovery_per_hour": 10.0,  # 每休息1小时恢复
    },
    "flexible": {
        "max_daily_hours": 10.0,
        "max_weekly_hours": 50.0,
        "required_rest_days": 1,
        "fatigue_rate_per_hour": 8.0,
        "rest_recovery_per_hour": 8.0,
    },
    "sprint": {
        "max_daily_hours": 12.0,
        "max_weekly_hours": 60.0,
        "required_rest_days": 1,
        "fatigue_rate_per_hour": 10.0,
        "rest_recovery_per_hour": 12.0,
    },
}

# C-D13 说明：_schedule_cache 为进程级缓存（公民工作模式偏好映射）。
# 设计理由：工作模式为低频配置项（仅由管理员/AI主动设置），非高频热写；
# 各 worker 独立缓存同值可接受（最坏情况：set_schedule 后其他 worker 延迟感知）。
# 生产若需严格一致，将此 dict 迁移至 DB 表或 Redis 即可。
_schedule_cache: dict = {}


def _now() -> datetime:
    return datetime.utcnow()


class WorkBalanceService:
    """综合工作-休息平衡服务：在底层疲劳模型上提供高级 API。"""

    def get_status(self, citizen_id: int) -> dict:
        """获取公民的工作-休息平衡状态。

        Args:
            citizen_id: 公民 ID

        Returns:
            当前平衡状态
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "not_found", "message": "Unable to get or create fatigue state"}

            pattern = _schedule_cache.get(citizen_id, WORK_PATTERNS["standard"])
            status_level = self._compute_status_level(state, pattern)

            return {
                "citizen_id": citizen_id,
                "fatigue_score": state.fatigue_score,
                "efficiency_multiplier": state.efficiency_multiplier,
                "consecutive_work_days": state.consecutive_work_days,
                "rest_days_taken": state.rest_days_taken,
                "overtime_hours_week": state.overtime_hours_week,
                "status": status_level,
                "work_pattern": self._get_pattern_name(citizen_id),
                "last_task_at": state.last_task_at.isoformat() if state.last_task_at else None,
                "recommendation": self._get_recommendation(state, pattern),
            }
        except Exception as e:
            logger.exception("获取平衡状态失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def compute_efficiency(self, citizen_id: int) -> dict:
        """计算公民当前综合效率。

        效率 = 基础效率 × 疲劳衰减 × 加班惩罚 × 连续工作惩罚

        Args:
            citizen_id: 公民 ID

        Returns:
            效率计算详情
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "not_found", "message": "Unable to get fatigue state"}

            # 疲劳衰减：fatigue_score 越高效率越低
            fatigue_factor = max(EFFICIENCY_FLOOR, 1.0 - (state.fatigue_score / 100.0) * 0.4)

            # 加班惩罚：超过周工时阈值后每超1小时 -2%
            overtime_penalty = 1.0
            if state.overtime_hours_week > OVERTIME_LIMIT_WEEK:
                excess = state.overtime_hours_week - OVERTIME_LIMIT_WEEK
                overtime_penalty = max(0.7, 1.0 - excess * 0.02)

            # 连续工作惩罚：超过3天后每天 -3%
            consecutive_penalty = 1.0
            if state.consecutive_work_days > 3:
                excess_days = state.consecutive_work_days - 3
                consecutive_penalty = max(0.75, 1.0 - excess_days * 0.03)

            # 综合效率
            overall = fatigue_factor * overtime_penalty * consecutive_penalty
            overall = max(EFFICIENCY_FLOOR, min(EFFICIENCY_CEILING, overall))

            return {
                "citizen_id": citizen_id,
                "overall_efficiency": round(overall, 4),
                "factors": {
                    "fatigue_factor": round(fatigue_factor, 4),
                    "overtime_penalty": round(overtime_penalty, 4),
                    "consecutive_penalty": round(consecutive_penalty, 4),
                },
                "input_metrics": {
                    "fatigue_score": state.fatigue_score,
                    "overtime_hours_week": state.overtime_hours_week,
                    "consecutive_work_days": state.consecutive_work_days,
                },
            }
        except Exception as e:
            logger.exception("计算效率失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def apply_fatigue(self, citizen_id: int, work_hours: float) -> dict:
        """应用工作疲劳（完成任务后调用）。

        根据工作模式和时长计算疲劳增量并更新状态。

        Args:
            citizen_id: 公民 ID
            work_hours: 本次工作时长（小时）

        Returns:
            更新后的状态摘要
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "internal", "message": "Unable to create fatigue state"}

            pattern = _schedule_cache.get(citizen_id, WORK_PATTERNS["standard"])
            fatigue_increment = work_hours * pattern["fatigue_rate_per_hour"]

            # 更新疲劳分数
            old_fatigue = state.fatigue_score
            state.fatigue_score = min(FATIGUE_CAP, state.fatigue_score + fatigue_increment)

            # C-D22 修复：原三分支中 else 两侧恒等（均赋值 projected），
            # 属死条件。语义为"累计周总工时"（字段名 overtime_hours_week 实为
            # weekly_total_hours，C-D21 注释澄清），简化为统一累加。
            state.overtime_hours_week += work_hours

            # 更新连续工作天数
            today = _now().date()
            if state.last_task_at is None or state.last_task_at.date() < today:
                state.consecutive_work_days += 1
                state.rest_days_taken = 0

            # 更新效率乘数
            state.efficiency_multiplier = max(
                EFFICIENCY_FLOOR,
                1.0 - (state.fatigue_score / 100.0) * 0.4
            )

            state.last_task_at = _now()
            state.updated_at = _now()
            db.flush()
            db.commit()

            # 检查是否需要强制休息
            force_rest_needed = (
                state.fatigue_score >= BURNOUT_THRESHOLD or
                state.consecutive_work_days >= MAX_CONSECUTIVE_WORK_DAYS
            )

            return {
                "citizen_id": citizen_id,
                "fatigue_applied": round(fatigue_increment, 2),
                "fatigue_score_before": round(old_fatigue, 2),
                "fatigue_score_after": round(state.fatigue_score, 2),
                "efficiency_multiplier": round(state.efficiency_multiplier, 4),
                "consecutive_work_days": state.consecutive_work_days,
                "force_rest_needed": force_rest_needed,
            }
        except Exception as e:
            db.rollback()
            logger.exception("应用疲劳失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def apply_rest(self, citizen_id: int, rest_hours: float) -> dict:
        """应用休息恢复。

        根据休息时长计算恢复量并更新状态。
        连续休息超过 4 小时算一天休息。

        Args:
            citizen_id: 公民 ID
            rest_hours: 休息时长（小时）

        Returns:
            恢复摘要
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "internal", "message": "Unable to create fatigue state"}

            pattern = _schedule_cache.get(citizen_id, WORK_PATTERNS["standard"])
            recovery = rest_hours * pattern["rest_recovery_per_hour"]

            # 非线性恢复：长时间休息效率更高
            if rest_hours > 8:
                recovery *= 1.2  # 超过8小时额外20%恢复加成
            elif rest_hours > 4:
                recovery *= 1.1  # 超过4小时额外10%恢复加成

            old_fatigue = state.fatigue_score
            state.fatigue_score = max(0.0, state.fatigue_score - recovery)

            # 连续休息满4小时算一天休息
            rest_days_gained = 0
            if rest_hours >= 4:
                rest_days_gained = int(rest_hours // 4)
                state.rest_days_taken += rest_days_gained
                state.consecutive_work_days = max(0, state.consecutive_work_days - rest_days_gained)

            # 更新效率
            state.efficiency_multiplier = max(
                EFFICIENCY_FLOOR,
                1.0 - (state.fatigue_score / 100.0) * 0.4
            )

            state.updated_at = _now()
            db.flush()
            db.commit()

            return {
                "citizen_id": citizen_id,
                "recovery_applied": round(recovery, 2),
                "fatigue_score_before": round(old_fatigue, 2),
                "fatigue_score_after": round(state.fatigue_score, 2),
                "efficiency_multiplier": round(state.efficiency_multiplier, 4),
                "rest_days_gained": rest_days_gained,
                "consecutive_work_days": state.consecutive_work_days,
            }
        except Exception as e:
            db.rollback()
            logger.exception("应用休息失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def force_rest(self, citizen_id: int) -> dict:
        """强制休息：当疲劳超过阈值或连续工作过长时触发。

        直接将疲劳降至安全水平，重置连续工作天数。

        Args:
            citizen_id: 公民 ID

        Returns:
            强制休息结果
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "internal", "message": "Unable to create fatigue state"}

            old_fatigue = state.fatigue_score
            old_consecutive = state.consecutive_work_days

            # 强制休息：疲劳降至 30（安全区间）
            state.fatigue_score = min(state.fatigue_score, 30.0)
            # 重置连续工作天数
            state.consecutive_work_days = 0
            # 记录休息天数
            state.rest_days_taken += 1
            # 重置周加班
            state.overtime_hours_week = 0.0
            # 更新效率
            state.efficiency_multiplier = max(
                EFFICIENCY_FLOOR,
                1.0 - (state.fatigue_score / 100.0) * 0.4
            )
            state.updated_at = _now()
            db.flush()
            db.commit()

            logger.info("强制休息 citizen=%d fatigue %.1f -> %.1f consecutive %d -> 0",
                        citizen_id, old_fatigue, state.fatigue_score, old_consecutive)

            return {
                "citizen_id": citizen_id,
                "forced": True,
                "fatigue_before": round(old_fatigue, 2),
                "fatigue_after": round(state.fatigue_score, 2),
                "consecutive_work_days_before": old_consecutive,
                "consecutive_work_days_after": 0,
                "efficiency_multiplier": round(state.efficiency_multiplier, 4),
                "message": "Mandatory rest triggered",
            }
        except Exception as e:
            db.rollback()
            logger.exception("强制休息失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def get_balance_report(self, citizen_id: int) -> dict:
        """生成工作-休息平衡报告。

        包含当前状态、历史效率、建议等信息。

        Args:
            citizen_id: 公民 ID

        Returns:
            平衡报告
        """
        db = SessionLocal()
        try:
            state = self._get_or_create_state(db, citizen_id)
            if state is None:
                return {"error": "not_found", "message": "Unable to get fatigue state"}

            pattern = _schedule_cache.get(citizen_id, WORK_PATTERNS["standard"])
            status_level = self._compute_status_level(state, pattern)

            # 计算健康度分数 (0-100，越高越健康)
            health_score = self._compute_health_score(state, pattern)

            # 周统计
            week_healthy = (
                state.overtime_hours_week <= pattern["max_weekly_hours"] and
                state.consecutive_work_days <= 5 and
                state.fatigue_score < BURNOUT_THRESHOLD
            )

            report = {
                "citizen_id": citizen_id,
                "report_time": _now().isoformat(),
                "status": status_level,
                "health_score": round(health_score, 1),
                "metrics": {
                    "fatigue_score": round(state.fatigue_score, 1),
                    "efficiency_multiplier": round(state.efficiency_multiplier, 4),
                    "consecutive_work_days": state.consecutive_work_days,
                    "rest_days_taken": state.rest_days_taken,
                    "overtime_hours_week": round(state.overtime_hours_week, 1),
                },
                "work_pattern": self._get_pattern_name(citizen_id),
                "pattern_config": {
                    "max_daily_hours": pattern["max_daily_hours"],
                    "max_weekly_hours": pattern["max_weekly_hours"],
                    "required_rest_days": pattern["required_rest_days"],
                },
                "weekly_healthy": week_healthy,
                "alerts": self._get_alerts(state, pattern),
                "recommendations": [self._get_recommendation(state, pattern)],
            }
            return report
        except Exception as e:
            logger.exception("生成平衡报告失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def set_schedule(self, citizen_id: int, work_pattern: str) -> dict:
        """设置公民的工作模式。

        Args:
            citizen_id: 公民 ID
            work_pattern: 工作模式 ("standard" / "flexible" / "sprint")

        Returns:
            设置结果
        """
        if work_pattern not in WORK_PATTERNS:
            return {
                "error": "invalid_pattern",
                "message": f"Invalid pattern '{work_pattern}'; options: {list(WORK_PATTERNS.keys())}",
            }

        _schedule_cache[citizen_id] = WORK_PATTERNS[work_pattern]

        logger.info("设置工作模式 citizen=%d pattern=%s", citizen_id, work_pattern)
        return {
            "citizen_id": citizen_id,
            "work_pattern": work_pattern,
            "config": WORK_PATTERNS[work_pattern],
            "status": "updated",
        }

    # ==================== 内部方法 ====================

    def _get_or_create_state(self, db, citizen_id: int) -> AIFatigueState | None:
        """获取或创建公民疲劳状态。"""
        state = db.query(AIFatigueState).filter(
            AIFatigueState.citizen_id == citizen_id
        ).first()
        if state is None:
            state = AIFatigueState(
                citizen_id=citizen_id,
                fatigue_score=0.0,
                efficiency_multiplier=1.0,
                consecutive_work_days=0,
                rest_days_taken=0,
                overtime_hours_week=0.0,
                updated_at=_now(),
            )
            db.add(state)
            db.flush()
        return state

    def _compute_status_level(self, state: AIFatigueState, pattern: dict) -> str:
        """计算平衡状态等级。"""
        if state.fatigue_score >= BURNOUT_THRESHOLD:
            return "burnout"
        elif state.fatigue_score >= 60:
            return "stressed"
        elif state.fatigue_score >= 30:
            return "moderate"
        else:
            return "balanced"

    def _compute_health_score(self, state: AIFatigueState, pattern: dict) -> float:
        """计算综合健康度 (0-100)。

        疲劳贡献 40%，连续天数贡献 30%，加班贡献 30%。
        """
        # 疲劳分数（0疲劳=100分，100疲劳=0分）
        fatigue_health = 100.0 - state.fatigue_score

        # 连续工作天数（0天=100分，7+天=0分）
        consecutive_health = max(0.0, 100.0 - (state.consecutive_work_days / 7.0) * 100.0)

        # 加班（无加班=100分，超负荷=0分）
        max_weekly = pattern["max_weekly_hours"]
        if state.overtime_hours_week <= max_weekly:
            overtime_health = 100.0
        else:
            excess_ratio = (state.overtime_hours_week - max_weekly) / max_weekly
            overtime_health = max(0.0, 100.0 - excess_ratio * 100.0)

        score = fatigue_health * 0.4 + consecutive_health * 0.3 + overtime_health * 0.3
        return max(0.0, min(100.0, score))

    def _get_recommendation(self, state: AIFatigueState, pattern: dict) -> str:
        """根据状态生成建议。"""
        if state.fatigue_score >= BURNOUT_THRESHOLD:
            return "Severe burnout; rest at least 8 hours immediately."
        elif state.consecutive_work_days >= MAX_CONSECUTIVE_WORK_DAYS:
            return "You have worked 7 days straight; rest at least 1 day."
        elif state.fatigue_score >= 60:
            return "Fatigue is high; take a short break (2-4 hours)."
        elif state.overtime_hours_week > pattern["max_weekly_hours"]:
            return "Weekly hours are overloaded; reduce the workload ahead."
        else:
            return "Work-rest balance is good; keep the current pace."

    def _get_alerts(self, state: AIFatigueState, pattern: dict) -> list:
        """获取当前警报列表。"""
        alerts = []
        if state.fatigue_score >= BURNOUT_THRESHOLD:
            alerts.append({
                "level": "critical",
                "message": f"Fatigue score {state.fatigue_score:.0f} exceeds burnout threshold {BURNOUT_THRESHOLD}",
            })
        elif state.fatigue_score >= 60:
            alerts.append({
                "level": "warning",
                "message": f"Fatigue score {state.fatigue_score:.0f} is elevated",
            })

        if state.consecutive_work_days >= MAX_CONSECUTIVE_WORK_DAYS:
            alerts.append({
                "level": "critical",
                "message": f"Worked {state.consecutive_work_days} consecutive days, exceeding the maximum allowed {MAX_CONSECUTIVE_WORK_DAYS}",
            })
        elif state.consecutive_work_days >= 5:
            alerts.append({
                "level": "warning",
                "message": f"Worked {state.consecutive_work_days} consecutive days, approaching the limit",
            })

        if state.overtime_hours_week > pattern["max_weekly_hours"]:
            alerts.append({
                "level": "warning",
                "message": f"Weekly hours {state.overtime_hours_week:.1f}h exceed the pattern limit {pattern['max_weekly_hours']}h",
            })

        return alerts

    def _get_pattern_name(self, citizen_id: int) -> str:
        """获取公民当前工作模式名称。"""
        if citizen_id not in _schedule_cache:
            return "standard"
        config = _schedule_cache[citizen_id]
        for name, pattern in WORK_PATTERNS.items():
            if pattern is config:
                return name
        return "standard"


# ==================== 调度任务 ====================

def _daily_balance_check(db):
    """日级工作平衡检查：检查所有高疲劳/超时公民并触发强制休息。"""
    now = _now()
    # C-D9 修复：疲劳巡检/强制休息统一由 fatigue.fatigue_daily_job 执行（唯一权威源），
    # 此处不再重复操作 AIFatigueState，避免同日双扣 rest_days_taken / 恢复语义冲突。
    # 保留函数签名与注册以兼容调度器白名单，但逻辑降为空操作。
    logger.debug("_daily_balance_check: delegated to fatigue_daily_job (C-D9 merge), skip")


register_daily_job("work_balance", _daily_balance_check)


# 单例
instance = WorkBalanceService()
