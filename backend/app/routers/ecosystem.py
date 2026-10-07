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
"""P3 生态成熟路由：DID/社交图谱/游戏化/知识共享/SLA。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..deps import host_or_any_ai
from ..did_vc import instance as did_svc
from ..social_graph import instance as sg_svc
from ..gamification import instance as game_svc
from ..knowledge_sharing import instance as kb_svc
from ..sla_dashboard import instance as sla_svc

router = APIRouter(prefix="/api/ecosystem", tags=["ecosystem"],
                   dependencies=[Depends(host_or_any_ai)])


# ======================== 请求体 ========================

class DIDRegisterBody(BaseModel):
    holder_type: str = "citizen"
    holder_id: int


class VCIssueBody(BaseModel):
    issuer_did: str = Field(..., min_length=1)
    subject_did: str = Field(..., min_length=1)
    credential_type: str = Field(..., min_length=1)
    claims: dict = {}


class VCVerifyBody(BaseModel):
    credential: dict = Field(..., description="Full VC JSON object")


class SocialEdgeBody(BaseModel):
    from_id: int
    to_id: int
    relation_type: str = Field(..., min_length=1)
    weight: float = 1.0


class AchievementBody(BaseModel):
    key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = "general"
    xp_reward: int = 0
    condition_expr: dict | None = None
    rarity: str = "common"


class GamificationCheckBody(BaseModel):
    citizen_id: int
    event_type: str = Field(..., min_length=1)
    metrics: dict = {}


class ArticleBody(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    author_type: str = "citizen"
    author_id: int = 0
    category: str = "tutorial"
    tags: list[str] | None = None


class SLAMetricBody(BaseModel):
    service: str = Field(..., min_length=1)
    metric_name: str = Field(..., min_length=1)
    value: float


# ======================== DID / VC ========================

@router.post("/did/register")
def register_did(body: DIDRegisterBody):
    """注册去中心化身份 DID。"""
    return did_svc.register(holder_type=body.holder_type, holder_id=body.holder_id)


@router.post("/did/credentials")
def issue_credential(body: VCIssueBody):
    """签发可验证凭证。"""
    return did_svc.issue_credential(
        issuer_did=body.issuer_did, subject_did=body.subject_did,
        credential_type=body.credential_type, claims=body.claims,
    )


@router.post("/did/verify")
def verify_credential(body: VCVerifyBody):
    """验证可验证凭证。"""
    return did_svc.verify_credential(credential=body.credential)


# ======================== 社交图谱 ========================

@router.post("/social/edges")
def add_social_edge(body: SocialEdgeBody):
    """添加社交关系边。"""
    return sg_svc.add_edge(
        from_id=body.from_id, to_id=body.to_id,
        relation_type=body.relation_type, weight=body.weight,
    )


@router.get("/social/connections/{citizen_id}")
def get_connections(citizen_id: int):
    """获取公民社交连接列表。"""
    return sg_svc.get_connections(citizen_id=citizen_id)


@router.get("/social/influencers")
def influencers(metric: str = Query(default="weighted_degree"), limit: int = Query(default=20)):
    """获取影响力排行榜。"""
    return sg_svc.get_influencers(metric=metric, limit=limit)


# ======================== 游戏化 ========================

@router.post("/gamification/achievements")
def define_achievement(body: AchievementBody):
    """定义新成就。"""
    return game_svc.define_achievement(
        key=body.key, name=body.name, description=body.description,
        category=body.category, xp_reward=body.xp_reward,
        condition_expr=body.condition_expr, rarity=body.rarity,
    )


@router.post("/gamification/check")
def check_unlock(body: GamificationCheckBody):
    """检查成就解锁条件。"""
    return game_svc.check_unlocks(
        citizen_id=body.citizen_id, event_type=body.event_type, event_data=body.metrics,
    )


@router.get("/gamification/{citizen_id}/achievements")
def list_achievements(citizen_id: int):
    """获取公民已解锁成就列表。"""
    return game_svc.get_citizen_achievements(citizen_id=citizen_id)


# ======================== 知识共享 ========================

@router.post("/knowledge/articles")
def create_article(body: ArticleBody):
    """创建知识文章。"""
    return kb_svc.create_article(
        title=body.title, content=body.content, author_type=body.author_type,
        author_id=body.author_id, category=body.category, tags=body.tags,
    )


@router.get("/knowledge/search")
def search_knowledge(q: str = Query(..., min_length=1), limit: int = Query(default=20)):
    """搜索知识库文章。"""
    return kb_svc.search(query=q, limit=limit)


# ======================== SLA ========================

@router.post("/sla/metrics")
def record_sla_metric(body: SLAMetricBody):
    """记录 SLA 指标数据点。"""
    return sla_svc.record_metric(
        service=body.service, metric_name=body.metric_name, value=body.value,
    )


@router.get("/sla/dashboard/{service}")
def sla_dashboard(service: str):
    """获取服务 SLA 仪表盘数据。"""
    return sla_svc.get_current_sla(service=service)
