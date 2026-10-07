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
"""经济周期检测器（P1 经济增强）。

功能：
- 根据经济指标判断当前周期阶段（衰退/过热/滞胀/复苏）；
- 根据周期阶段推荐政策工具；
- 自动触发稳定器（调用 monetary_policy 接口）— 注册为 daily job；
- 获取历史信号。

依赖模型：EconomicCycleSignal, EconomicIndicator。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import EconomicCycleSignal, EconomicIndicator
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class EconomicCycleDetector:
    """经济周期检测与稳定器触发。"""

    def detect(self, db) -> dict:
        """根据经济指标判断当前周期阶段。

        判定逻辑：
        - 衰退：连续 N 期流通速度下降
        - 过热：通胀率 > threshold + 流通速度激增
        - 滞胀：流通速度停滞 + 通胀上升
        - 复苏：流通速度回升 + 交易量增加

        Returns:
            {"signal_type": str, "confidence": float, "indicators": dict}
        """
        # 获取最近指标
        since = _now() - timedelta(days=30)
        recent = db.query(EconomicIndicator).filter(
            EconomicIndicator.captured_at >= since
        ).order_by(EconomicIndicator.captured_at.desc()).limit(10).all()

        if not recent:
            return {"signal_type": "unknown", "confidence": 0.0, "indicators": {}}

        # 计算趋势
        velocity_values = [r.velocity for r in recent if r.velocity is not None]
        inflation_values = [r.inflation_rate for r in recent if r.inflation_rate is not None]

        velocity_trend = 0.0
        if len(velocity_values) >= 2:
            velocity_trend = velocity_values[0] - velocity_values[-1]

        inflation_avg = sum(inflation_values) / len(inflation_values) if inflation_values else 0.0

        # 判定周期阶段
        inflation_threshold = settings.INFLATION_TARGET_OFFSET * 5
        if velocity_trend > 0.2 and inflation_avg > inflation_threshold:
            signal_type = "overheating"
            confidence = min(1.0, velocity_trend / 0.5)
        elif velocity_trend < -0.2 and inflation_avg < 0:
            signal_type = "recession"
            confidence = min(1.0, abs(velocity_trend) / 0.5)
        elif abs(velocity_trend) < 0.05 and inflation_avg > inflation_threshold:
            signal_type = "stagflation"
            confidence = min(1.0, inflation_avg / (inflation_threshold * 2))
        elif velocity_trend > 0.05 and inflation_avg <= inflation_threshold:
            signal_type = "recovery"
            confidence = min(1.0, velocity_trend / 0.3)
        else:
            signal_type = "expansion"
            confidence = 0.5

        indicators = {
            "velocity_trend": round(velocity_trend, 4),
            "inflation_avg": round(inflation_avg, 4),
            "data_points": len(recent),
        }

        recommended = self._get_recommendations(signal_type)

        signal = EconomicCycleSignal(
            signal_type=signal_type,
            confidence=round(confidence, 4),
            indicators=json.dumps(indicators),
            recommended_actions=json.dumps(recommended),
        )
        db.add(signal)
        db.commit()
        return {"signal_type": signal_type, "confidence": round(confidence, 4),
                "indicators": indicators}

    def _get_recommendations(self, signal_type: str) -> list:
        """根据周期阶段推荐政策工具。"""
        mapping = {
            "recession": ["lower_fee_rate", "increase_ubi", "tax_relief"],
            "overheating": ["raise_fee_rate", "reduce_ubi", "tighten_credit"],
            "stagflation": ["targeted_tax", "supply_incentive", "wage_control"],
            "recovery": ["maintain_stable", "gradual_taper", "investment_incentive"],
            "expansion": ["build_reserve", "maintain_neutral"],
            "unknown": [],
        }
        return mapping.get(signal_type, [])

    def recommend(self, db, signal_type: str) -> list:
        """根据周期阶段推荐政策工具。"""
        return self._get_recommendations(signal_type)

    def auto_stabilize(self, db):
        """自动触发稳定器：检测到过热/衰退→生成货币政策提案→告警宿主审批（S2 全链路闭环）。

        monetary_policy 是「提案-审批」制（propose_* 仅生成待批提案，apply_policy 才生效，
        生效须宿主签字），故稳定器的正确落点是：生成提案 + 触达宿主，而非直接改参数。
        原实现 `from .monetary_policy import monetary_policy`（该符号不存在）+ auto_adjust
        注释调用，导致"亏损→调参→宿主告警"链路断裂，现接回真实原语。
        """
        if not settings.CYCLE_DETECT_ENABLED:
            return {"triggered": False, "reason": "disabled"}
        result = self.detect(db)
        signal_type = result.get("signal_type", "unknown")
        if signal_type not in ("overheating", "recession"):
            return {"triggered": False, "reason": "no stabilization needed"}
        try:
            from . import monetary_policy as mp  # 惰性 import 防循环
            from .host_notify import notify as _notify
            cur = int(mp.current_economic_params(db).get("interest_rate_bps") or 0)
            if signal_type == "overheating":
                new_rate = cur + 25
                act = mp.propose_rate_change(db, initiated_by=0, new_rate_bps=new_rate)
                label = f"加息 +25bps → {new_rate}bps"
            else:  # recession：降准/扩表刺激
                act = mp.propose_qe(db, initiated_by=0, amount_cent=100000)
                label = "QE 扩表 +100000cent"
            # 重大宏观事件必须触达宿主（提案待其审批签字）。
            _notify(db, 0, title=f"[经济稳定器] {signal_type}",
                    body=f"检测到 {signal_type}，已生成货币政策提案 #{act.id}（{label}），待宿主审批。",
                    severity="warning", category="economic", link="/host/approvals")
            db.commit()
            logger.info("auto_stabilize: %s → proposal#%s", signal_type, act.id)
            return {"triggered": True, "signal_type": signal_type, "action_id": act.id}
        except Exception:  # noqa: BLE001  稳定器异常不得冒泡打断日任务
            db.rollback()
            logger.exception("auto_stabilize 失败")
            return {"triggered": False, "reason": "stabilization failed"}

    def get_history(self, db, periods: int = 10) -> list:
        """获取历史周期信号。"""
        signals = db.query(EconomicCycleSignal).order_by(
            EconomicCycleSignal.detected_at.desc()
        ).limit(periods).all()
        return [
            {
                "id": s.id,
                "signal_type": s.signal_type,
                "confidence": s.confidence,
                "indicators": s.indicators,
                "detected_at": s.detected_at.isoformat() if s.detected_at else None,
            }
            for s in signals
        ]


cycle_detector = EconomicCycleDetector()


def cycle_stabilize_daily_job(db, now=None) -> int:
    """日任务薄包装（契约：fn(db)->int）：触发经济稳定器，命中返回 1，否则 0。

    scheduler 每日调用；模块 import 时注册（与 fatigue/evolution 等同模式）。
    """
    res = cycle_detector.auto_stabilize(db)
    return 1 if res.get("triggered") else 0


# S2：把"检测→稳定→告警宿主"接入调度日任务（此前 auto_stabilize 从未被调度 = 死代码）。
register_daily_job("cycle_stabilize", cycle_stabilize_daily_job)
