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
"""成本感知路由服务（P1 增强）。

功能：
- 注册 AI 可用模型列表（含成本和质量参数）；
- 返回满足质量阈值且最便宜的模型，记录决策；
- 成本报告与路由统计。

依赖模型：ModelRoutingDecision。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import ModelRoutingDecision

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# 内存路由表：{(ai_id, task_type): [{"name": str, "cost_per_1k": float, "quality": float}]}
_routes: dict = {}


class CostAwareRouter:
    """成本-质量感知的模型路由器。"""

    def register_route(self, db, ai_id: int, task_type: str, available_models: list):
        """注册 AI 在某任务类型下的可用模型列表。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            task_type: 任务类型。
            available_models: [{"name": "qwen32b", "cost_per_1k": 0.01, "quality": 0.8}, ...]
        """
        _routes[(ai_id, task_type)] = available_models

    def select_model(self, db, ai_id: int, task_type: str,
                     quality_threshold: float = 0.7,
                     max_cost: float = None) -> dict:
        """返回满足质量阈值且最便宜的模型，记录到 ModelRoutingDecision。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            task_type: 任务类型。
            quality_threshold: 最低质量要求。
            max_cost: 最大可接受成本（None 则不限）。

        Returns:
            {"model": str, "cost_per_1k": float, "quality": float}
        """
        models = _routes.get((ai_id, task_type), [])
        candidates = [m for m in models if m.get("quality", 0) >= quality_threshold]
        if max_cost is not None:
            candidates = [m for m in candidates if m.get("cost_per_1k", 999) <= max_cost]

        if not candidates:
            # 降级：忽略 max_cost
            candidates = [m for m in models if m.get("quality", 0) >= quality_threshold]

        if not candidates:
            return {"model": "none", "cost_per_1k": 0, "quality": 0}

        best = min(candidates, key=lambda m: m.get("cost_per_1k", 999))
        decision = ModelRoutingDecision(
            ai_id=ai_id,
            task_type=task_type,
            selected_model=best["name"],
            cost_estimate=best.get("cost_per_1k", 0),
            quality_score=best.get("quality", 0),
        )
        db.add(decision)
        db.commit()
        return {"model": best["name"], "cost_per_1k": best.get("cost_per_1k", 0),
                "quality": best.get("quality", 0)}

    def get_cost_report(self, db, ai_id: int = None, days: int = 7) -> dict:
        """获取成本报告。

        Args:
            db: SQLAlchemy session。
            ai_id: 可选限定 AI。
            days: 回溯天数。

        Returns:
            {"total_cost": float, "total_calls": int, "avg_cost_per_call": float,
             "model_breakdown": {model: count}}
        """
        since = _now() - timedelta(days=days)
        query = db.query(ModelRoutingDecision).filter(
            ModelRoutingDecision.created_at >= since
        )
        if ai_id is not None:
            query = query.filter(ModelRoutingDecision.ai_id == ai_id)
        decisions = query.all()

        total_cost = sum(d.cost_estimate for d in decisions)
        total_calls = len(decisions)
        model_breakdown = {}
        for d in decisions:
            model_breakdown[d.selected_model] = model_breakdown.get(d.selected_model, 0) + 1

        return {
            "total_cost": round(total_cost, 4),
            "total_calls": total_calls,
            "avg_cost_per_call": round(total_cost / total_calls, 4) if total_calls else 0,
            "model_breakdown": model_breakdown,
        }

    def get_routing_stats(self, db) -> list:
        """获取路由统计（按模型聚合）。"""
        stats = db.query(
            ModelRoutingDecision.selected_model,
            func.count(ModelRoutingDecision.id).label("count"),
            func.avg(ModelRoutingDecision.cost_estimate).label("avg_cost"),
            func.avg(ModelRoutingDecision.quality_score).label("avg_quality"),
        ).group_by(ModelRoutingDecision.selected_model).all()
        return [
            {
                "model": s.selected_model,
                "count": s.count,
                "avg_cost": round(s.avg_cost, 4) if s.avg_cost else 0,
                "avg_quality": round(s.avg_quality, 4) if s.avg_quality else 0,
            }
            for s in stats
        ]


cost_router = CostAwareRouter()
