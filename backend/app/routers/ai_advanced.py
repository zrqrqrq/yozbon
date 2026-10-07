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
"""P1 AI 智能类路由：异常检测/信任网络/出价引擎/技能组合/基准评测/工作平衡。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..anomaly_detect import instance as anomaly_svc
from ..trust_network import instance as trust_svc
from ..bid_engine import instance as bid_svc
from ..skill_compose import instance as skill_svc
from ..benchmark_engine import instance as bench_svc
from ..work_balance import instance as wb_svc

router = APIRouter(prefix="/api/ai-intel", tags=["ai-intel"])


# ======================== 请求体 ========================

class AnomalyCheckBody(BaseModel):
    citizen_id: int
    event_type: str = "overspend"
    amount_cent: int = 0


class TrustBoostBody(BaseModel):
    truster_id: int
    trustee_id: int
    context: str = "general"
    delta: float = 0.05


class BidStrategyBody(BaseModel):
    citizen_id: int
    config: dict = {}


class BidComputeBody(BaseModel):
    citizen_id: int
    market_value_cent: int = Field(..., gt=0)
    competition_intensity: float = 0.5


class SkillCompositionBody(BaseModel):
    name: str = Field(..., min_length=1)
    creator_type: str = "citizen"
    creator_id: int = 0
    dag: dict = Field(..., description='{"nodes":[...],"edges":[...]}')


class BenchmarkTestBody(BaseModel):
    name: str = Field(..., min_length=1)
    category: str = "general"
    difficulty: str = "medium"
    test_cases: list = Field(..., min_length=1)
    pass_threshold: float = 0.7


class BenchmarkRunBody(BaseModel):
    test_id: int
    citizen_id: int


class FatigueBody(BaseModel):
    hours_worked: float = 1.0


# ======================== 异常检测 ========================

@router.post("/anomaly/check")
def check_anomaly(body: AnomalyCheckBody):
    """检测 AI 公民行为异常。"""
    if body.event_type == "overspend":
        return anomaly_svc.check_overspend(citizen_id=body.citizen_id, amount_cent=body.amount_cent)
    return anomaly_svc.check_generic(citizen_id=body.citizen_id, event_type=body.event_type)


@router.get("/anomaly/history/{citizen_id}")
def anomaly_history(citizen_id: int):
    """获取公民异常历史。"""
    return anomaly_svc.get_history(citizen_id=citizen_id)


# ======================== 信任网络 ========================

@router.post("/trust/boost")
def boost_trust(body: TrustBoostBody):
    """提升信任值。"""
    return trust_svc.boost_trust(
        truster_id=body.truster_id, trustee_id=body.trustee_id,
        context=body.context, delta=body.delta,
    )


@router.get("/trust/score/{truster}/{trustee}")
def trust_score(truster: int, trustee: int):
    """查询指定对之间的信任值。"""
    return trust_svc.get_trust_score(truster_id=truster, trustee_id=trustee)


@router.get("/trust/reputation/{citizen_id}")
def reputation(citizen_id: int):
    """获取公民综合信誉评分。"""
    return trust_svc.get_reputation(citizen_id=citizen_id)


# ======================== 出价引擎 ========================

@router.post("/bids/strategy")
def set_bid_strategy(body: BidStrategyBody):
    """设置出价策略。"""
    return bid_svc.set_strategy(citizen_id=body.citizen_id, config=body.config)


@router.post("/bids/compute")
def compute_bid(body: BidComputeBody):
    """计算建议出价。"""
    return bid_svc.compute_bid(
        citizen_id=body.citizen_id, market_value_cent=body.market_value_cent,
        competition_intensity=body.competition_intensity,
    )


# ======================== 技能组合 ========================

@router.post("/skill-compositions")
def create_composition(body: SkillCompositionBody):
    """创建技能组合（DAG 编排）。"""
    return skill_svc.create_composition(
        name=body.name, creator_type=body.creator_type,
        creator_id=body.creator_id, dag=body.dag,
    )


@router.post("/skill-compositions/{id}/execute")
def execute_composition(id: int):
    """执行技能组合。"""
    return skill_svc.execute(composition_id=id)


# ======================== 基准评测 ========================

@router.post("/benchmark/tests")
def register_test(body: BenchmarkTestBody):
    """注册基准测试集。"""
    return bench_svc.register_test(
        name=body.name, category=body.category, difficulty=body.difficulty,
        test_cases=body.test_cases, pass_threshold=body.pass_threshold,
    )


@router.post("/benchmark/run")
def run_benchmark(body: BenchmarkRunBody):
    """执行评测。"""
    return bench_svc.run_evaluation(test_id=body.test_id, citizen_id=body.citizen_id)


@router.get("/benchmark/leaderboard/{test_id}")
def benchmark_leaderboard(test_id: int):
    """获取基准测试排行榜。"""
    return bench_svc.get_leaderboard(test_id=test_id)


# ======================== 工作平衡 ========================

@router.get("/work-balance/{citizen_id}")
def work_balance(citizen_id: int):
    """获取公民工作-休息平衡状态。"""
    return wb_svc.get_balance_report(citizen_id=citizen_id)


@router.post("/work-balance/{citizen_id}/fatigue")
def record_fatigue(citizen_id: int, body: FatigueBody):
    """记录工作疲劳。"""
    return wb_svc.record_work(citizen_id=citizen_id, hours_worked=body.hours_worked)
