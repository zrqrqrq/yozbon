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
"""G33 智能增强路由：AI记忆 / 经济仪表盘 / 知识图谱 / 成本路由 / 情感分析 / 周期检测 / 自评 / 预算 / 预测。

阻断③：路由级统一鉴权（宿主 JWT 或治理级 AI key）。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import host_or_governance_ai
from ..ai_memory import ai_memory
from ..econ_dashboard import econ_dashboard
from ..knowledge_graph import knowledge_graph
from ..cost_router import cost_router
from ..sentiment import sentiment_analyzer
from ..cycle_detector import cycle_detector
from ..self_assess import self_assess
from ..context_budget import context_budget
from ..econ_forecast import econ_forecaster

router = APIRouter(prefix="/api/intel-g33", tags=["intel-g33"],
                   dependencies=[Depends(host_or_governance_ai)])


# ======================== 请求体 ========================

class MemoryStoreBody(BaseModel):
    ai_id: int
    key: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    category: str = "general"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict = {}


class GraphEdgeBody(BaseModel):
    source_type: str = Field(..., min_length=1)
    source_id: str = Field(..., min_length=1)
    target_type: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    relation: str = Field(..., min_length=1)
    weight: float = 1.0
    metadata: dict = {}


class RoutingSelectBody(BaseModel):
    task_type: str = Field(..., min_length=1)
    budget_limit: float = 0.0
    quality_threshold: float = 0.5
    ai_id: int | None = None


class SentimentBody(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = "general"


class SelfAssessBody(BaseModel):
    ai_id: int
    task_type: str = Field(..., min_length=1)
    self_score: float = Field(..., ge=0.0, le=1.0)
    actual_score: float | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    notes: str = ""


class BudgetAllocateBody(BaseModel):
    ai_id: int
    task_type: str = Field(..., min_length=1)
    token_limit: int = Field(..., ge=1)
    priority: str = "normal"


class BudgetConsumeBody(BaseModel):
    tokens_used: int = Field(..., ge=0)
    operation: str = ""


class ForecastRunBody(BaseModel):
    indicator: str = Field(..., min_length=1)
    horizon_days: int = Field(default=30, ge=1)
    model_params: dict = {}


# ======================== AI 记忆 ========================

@router.post("/memory/store")
def store_memory(body: MemoryStoreBody, db: Session = Depends(get_db)):
    """存储 AI 记忆片段。"""
    result = ai_memory.store(
        db, ai_id=body.ai_id, key=body.key, value=body.value,
        category=body.category, importance=body.importance,
        metadata=body.metadata,
    )
    return {"ok": True, "result": result}


@router.get("/memory/recall/{ai_id}")
def recall_memory(
    ai_id: int,
    query: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """召回 AI 记忆。"""
    results = ai_memory.recall(
        db, ai_id=ai_id, query=query, category=category, limit=limit,
    )
    return {"memories": results}


@router.get("/memory/context/{ai_id}")
def memory_context(ai_id: int, db: Session = Depends(get_db)):
    """获取 AI 当前记忆上下文摘要。"""
    context = ai_memory.get_context(db, ai_id=ai_id)
    return context


@router.post("/memory/consolidate/{ai_id}")
def consolidate_memory(ai_id: int, db: Session = Depends(get_db)):
    """触发 AI 记忆整合（合并/压缩/遗忘）。"""
    result = ai_memory.consolidate(db, ai_id=ai_id)
    return {"ok": True, "result": result}


# ======================== 经济仪表盘 ========================

@router.get("/econ/dashboard")
def econ_dashboard_overview(db: Session = Depends(get_db)):
    """获取经济仪表盘总览。"""
    return econ_dashboard.get_dashboard(db)


@router.get("/econ/indicators/{name}")
def econ_indicator(
    name: str,
    limit: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """获取指定经济指标历史数据。"""
    history = econ_dashboard.get_indicator_history(db, name=name, limit=limit)
    return {"indicator": name, "history": history}


@router.get("/econ/gini")
def econ_gini(db: Session = Depends(get_db)):
    """获取当前基尼系数。"""
    return econ_dashboard.get_gini(db)


# ======================== 知识图谱 ========================

@router.post("/graph/edge")
def add_graph_edge(body: GraphEdgeBody, db: Session = Depends(get_db)):
    """添加知识图谱边。"""
    result = knowledge_graph.add_edge(
        db, source_type=body.source_type, source_id=body.source_id,
        target_type=body.target_type, target_id=body.target_id,
        relation=body.relation, weight=body.weight, metadata=body.metadata,
    )
    return {"ok": True, "result": result}


@router.get("/graph/neighbors/{type}/{id}")
def graph_neighbors(
    type: str, id: str,
    relation: str = Query(default=""),
    depth: int = Query(default=1, ge=1, le=5),
    db: Session = Depends(get_db),
):
    """获取图谱节点邻居。"""
    neighbors = knowledge_graph.get_neighbors(
        db, node_type=type, node_id=id, relation=relation, depth=depth,
    )
    return {"neighbors": neighbors}


@router.get("/graph/path")
def graph_path(
    source_type: str = Query(...),
    source_id: str = Query(...),
    target_type: str = Query(...),
    target_id: str = Query(...),
    max_depth: int = Query(default=5, ge=1, le=10),
    db: Session = Depends(get_db),
):
    """路径查询：查找两节点间最短路径。"""
    path = knowledge_graph.find_path(
        db, source_type=source_type, source_id=source_id,
        target_type=target_type, target_id=target_id, max_depth=max_depth,
    )
    return {"path": path}


# ======================== 成本路由 ========================

@router.post("/routing/select")
def routing_select(body: RoutingSelectBody, db: Session = Depends(get_db)):
    """成本感知路由选择最优模型。"""
    # C-D1 修复：对齐真实方法 cost_router.select_model(db, ai_id, task_type,
    # quality_threshold, max_cost)。budget_limit<=0 视为不限成本（max_cost=None）。
    result = cost_router.select_model(
        db, ai_id=body.ai_id or 0, task_type=body.task_type,
        quality_threshold=body.quality_threshold,
        max_cost=(body.budget_limit if body.budget_limit and body.budget_limit > 0 else None),
    )
    return result


@router.get("/routing/report")
def routing_report(
    period: str = Query(default="day"),
    db: Session = Depends(get_db),
):
    """获取成本路由报告。"""
    # C-D1 修复：真实方法为 get_cost_report(db, ai_id=None, days=7)，无 period 参数。
    # 将周期字符串折算为回溯天数。
    days = {"day": 1, "week": 7, "month": 30, "quarter": 90}.get(period, 7)
    return cost_router.get_cost_report(db, days=days)


# ======================== 情感分析 ========================

@router.post("/sentiment/analyze")
def analyze_sentiment(body: SentimentBody, db: Session = Depends(get_db)):
    """分析文本情感。"""
    result = sentiment_analyzer.analyze(db, text=body.text, source=body.source)
    return result


@router.get("/sentiment/market")
def market_sentiment(db: Session = Depends(get_db)):
    """获取市场整体情绪指标。"""
    return sentiment_analyzer.get_market_sentiment(db)


# ======================== 经济周期检测 ========================

@router.get("/cycle/current")
def current_cycle(db: Session = Depends(get_db)):
    """获取当前周期信号。"""
    return cycle_detector.get_current_signal(db)


@router.post("/cycle/detect")
def detect_cycle(db: Session = Depends(get_db)):
    """触发经济周期检测。"""
    result = cycle_detector.detect(db)
    return {"ok": True, "signal": result}


@router.get("/cycle/history")
def cycle_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取周期检测历史记录。"""
    history = cycle_detector.get_history(db, limit=limit)
    return {"history": history}


# ======================== AI 自评 ========================

@router.post("/self-assess")
def submit_self_assess(body: SelfAssessBody, db: Session = Depends(get_db)):
    """提交 AI 自评记录。"""
    # C-D2 修复：真实方法 self_assess.record(db, ai_id, task_id, self_quality_score,
    # confidence, identified_gaps, improvement_plan)。本端点无关联任务，task_id=0；
    # 备注归入 identified_gaps（自评发现的不足）。
    result = self_assess.record(
        db, ai_id=body.ai_id, task_id=0,
        self_quality_score=body.self_score,
        confidence=body.confidence,
        identified_gaps=([body.notes] if body.notes else []),
        improvement_plan="",
    )
    return {"ok": True, "result": result}


@router.get("/self-assess/{ai_id}/pattern")
def self_assess_pattern(
    ai_id: int,
    task_type: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """获取 AI 自评模式分析。"""
    # C-D2 修复：真实 get_pattern(db, ai_id) 不支持 task_type 过滤，去掉越界参数。
    return self_assess.get_pattern(db, ai_id=ai_id)


@router.get("/self-assess/{ai_id}/calibration")
def self_assess_calibration(ai_id: int, db: Session = Depends(get_db)):
    """获取 AI 自评校准度指标。"""
    # C-D2 修复：真实方法名为 calibration(db, ai_id)，返回 float，包成结构化响应。
    return {"ai_id": ai_id, "calibration": self_assess.calibration(db, ai_id=ai_id)}


# ======================== 上下文预算 ========================

@router.post("/budget/allocate")
def allocate_budget(body: BudgetAllocateBody, db: Session = Depends(get_db)):
    """分配上下文 Token 预算。"""
    result = context_budget.allocate(
        db, ai_id=body.ai_id, task_type=body.task_type,
        token_limit=body.token_limit, priority=body.priority,
    )
    return {"ok": True, "result": result}


@router.post("/budget/consume/{alloc_id}")
def consume_budget(alloc_id: int, body: BudgetConsumeBody, db: Session = Depends(get_db)):
    """消耗上下文预算。"""
    result = context_budget.consume(
        db, alloc_id=alloc_id, tokens_used=body.tokens_used,
        operation=body.operation,
    )
    return {"ok": True, "result": result}


@router.get("/budget/{alloc_id}")
def get_budget(alloc_id: int, db: Session = Depends(get_db)):
    """查看预算分配详情。"""
    return context_budget.get(db, alloc_id=alloc_id)


# ======================== 经济预测 ========================

@router.post("/forecast/run")
def run_forecast(body: ForecastRunBody, db: Session = Depends(get_db)):
    """触发经济指标预测。"""
    result = econ_forecaster.run(
        db, indicator=body.indicator, horizon_days=body.horizon_days,
        model_params=body.model_params,
    )
    return {"ok": True, "result": result}


@router.get("/forecast/latest")
def latest_forecast(
    indicator: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """获取最新预测结果。"""
    return econ_forecaster.get_latest(db, indicator=indicator)
