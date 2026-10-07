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
"""G33 安全增强路由：Token吊销 / Prompt注入检测 / 模型降级 / 健康检查 / CORS策略。

阻断③：除匿名健康探针（/health*）外，全部受路由级统一鉴权保护
（宿主 JWT 或治理级 AI key）。健康检查保持公开以支持存活/就绪探针。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import host_or_governance_ai
from ..token_revocation import token_revocation_service
from ..prompt_guard import prompt_guard
from ..model_fallback import model_fallback
from ..health_check import health_check
from ..cors_config import cors_config

router = APIRouter(prefix="/api/security-g33", tags=["security-g33"])
# 受保护子路由：纳入路由级统一鉴权后并入 router（健康探针保持在 router 上匿名）。
protected = APIRouter()


# ======================== 请求体 ========================

class TokenRevokeBody(BaseModel):
    jti: str = Field(..., min_length=1)
    ai_id: int | None = None
    reason: str = ""


class PromptScanBody(BaseModel):
    content: str = Field(..., min_length=1)
    source_type: str = "user_input"
    source_id: int = 0


class FallbackRegisterBody(BaseModel):
    name: str = Field(..., min_length=1)
    primary_model: str = Field(..., min_length=1)
    fallback_chain: list[str] = Field(..., min_length=1)
    trigger_conditions: dict = {"timeout": True, "error_5xx": True}
    timeout_ms: int = 30000


class CORSPolicyBody(BaseModel):
    origin_pattern: str = Field(..., min_length=1)
    methods: str = "*"
    headers: str = "*"
    credentials: bool = False
    max_age: int = 3600
    tenant_code: str = "default"


# ======================== Token 吊销 ========================

@protected.post("/token/revoke")
def revoke_token(body: TokenRevokeBody, db: Session = Depends(get_db)):
    """吊销 token，将 jti 加入黑名单。"""
    token_revocation_service.revoke(
        db, jti=body.jti, ai_id=body.ai_id, reason=body.reason,
    )
    return {"ok": True, "jti": body.jti}


@protected.get("/token/revoked/{jti}")
def check_token_revoked(jti: str, db: Session = Depends(get_db)):
    """检查指定 jti 是否已被吊销。"""
    revoked = token_revocation_service.is_revoked(db, jti)
    return {"jti": jti, "revoked": revoked}


# ======================== Prompt 注入检测 ========================

@protected.post("/prompt/scan")
def scan_prompt(body: PromptScanBody, db: Session = Depends(get_db)):
    """扫描内容，检测 prompt injection 攻击。"""
    result = prompt_guard.scan(
        db, content=body.content,
        source_type=body.source_type, source_id=body.source_id,
    )
    return result


@protected.get("/prompt/stats")
def prompt_stats(db: Session = Depends(get_db)):
    """获取注入检测统计数据。"""
    return prompt_guard.get_stats(db)


# ======================== 模型降级 ========================

@protected.post("/fallback/register")
def register_fallback(body: FallbackRegisterBody, db: Session = Depends(get_db)):
    """注册模型降级规则。"""
    rule = model_fallback.register_rule(
        db, name=body.name, primary_model=body.primary_model,
        fallback_chain=body.fallback_chain,
        trigger_conditions=body.trigger_conditions,
        timeout_ms=body.timeout_ms,
    )
    return {"ok": True, "rule_id": rule.id if hasattr(rule, "id") else None}


@protected.get("/fallback/resolve/{task_type}")
def resolve_fallback(task_type: str, db: Session = Depends(get_db)):
    """根据任务类型获取当前可用模型。"""
    result = model_fallback.resolve(db, task_type)
    return result


# ======================== 健康检查（公开匿名探针）========================

@router.get("/health")
def health_all(db: Session = Depends(get_db)):
    """全局健康检查，返回所有服务状态。"""
    results = health_check.probe_all(db)
    return {"probes": results, "total": len(results)}


@router.get("/health/probes")
def health_probes(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取所有探针历史记录。"""
    records = health_check.get_probe_history(db, limit=limit)
    return {"probes": records}


@router.get("/health/{service}")
def health_single(service: str, db: Session = Depends(get_db)):
    """单服务健康检查。"""
    result = health_check.probe_one(db, service)
    return result


# ======================== CORS 策略 ========================

@protected.get("/cors/policies")
def cors_list(db: Session = Depends(get_db)):
    """获取 CORS 策略列表。"""
    policies = cors_config.get_policies(db)
    return {"policies": policies}


@protected.post("/cors/policy")
def cors_add(body: CORSPolicyBody, db: Session = Depends(get_db)):
    """添加 CORS 策略。"""
    policy = cors_config.add_policy(
        db, origin_pattern=body.origin_pattern, methods=body.methods,
        headers=body.headers, credentials=body.credentials,
        max_age=body.max_age, tenant_code=body.tenant_code,
    )
    return {"ok": True, "id": policy.id if hasattr(policy, "id") else None}


@protected.delete("/cors/policy/{id}")
def cors_delete(id: int, db: Session = Depends(get_db)):
    """删除指定 CORS 策略。"""
    cors_config.remove_policy(db, policy_id=id)
    return {"ok": True, "deleted_id": id}


# 受保护子路由统一挂载鉴权依赖后并入公开 router。
router.include_router(protected, dependencies=[Depends(host_or_governance_ai)])
