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
"""G33 平台工程路由：众筹 / 财富分布 / 利息 / 配置漂移 / 降级 / 制裁 / 完整性 / 配额 / SCA / 混沌 / 断点 / 契约测试。

阻断③：路由级统一鉴权（宿主 JWT 或治理级 AI key）。
S7：写端点操作者身份从鉴权凭据派生，忽略客户端传入的 actor/issued_by。
"""
from fastapi import APIRouter, Depends, Query
from datetime import datetime
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import host_or_governance_ai
from ..crowdfund import crowdfund_service
from ..wealth_metrics import wealth_metrics
from ..escrow_yield import escrow_yield
from ..config_drift import config_drift
from ..graceful_degradation import graceful_degradation
from ..sanctions import sanctions
from ..data_integrity import data_integrity
from ..ai_quota import ai_quota
from ..sca_scanner import sca_scanner
from ..chaos_engine import chaos_engine
from ..task_checkpoint import task_checkpoint
from ..contract_test import contract_tester

router = APIRouter(prefix="/api/platform-g33", tags=["platform-g33"],
                   dependencies=[Depends(host_or_governance_ai)])


def _actor_id(auth: tuple) -> int:
    """S7：从 host_or_governance_ai 返回的 (kind, principal) 中提取可信身份 id。"""
    _kind, principal = auth
    return principal.id


# ======================== 请求体 ========================

class CrowdfundCreateBody(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    creator_ai_id: int
    target_amount: int = Field(..., ge=1)
    deadline: str = ""
    category: str = "general"


class CrowdfundContributeBody(BaseModel):
    contributor_ai_id: int
    amount: int = Field(..., ge=1)
    tier: str = "base"


class DriftCheckBody(BaseModel):
    service: str = Field(..., min_length=1)
    config_hash: str = ""


class DegradationActivateBody(BaseModel):
    reason: str = ""
    severity: str = "medium"
    affected_services: list[str] = []


class DegradationDeactivateBody(BaseModel):
    reason: str = ""
    state_id: int = 0


class SanctionBody(BaseModel):
    target_type: str = Field(..., min_length=1)
    target_id: int
    sanction_type: str = Field(..., min_length=1)
    reason: str = ""
    duration_days: int = 0
    # S7：issued_by 由鉴权凭据派生，客户端传入值忽略
    issued_by: int = 0


class SanctionCheckBody(BaseModel):
    target_type: str = Field(..., min_length=1)
    target_id: int
    action: str = "any"


class ScaScanBody(BaseModel):
    component: str = Field(..., min_length=1)
    version: str = ""
    cve_db: str = "auto"


class ChaosExperimentBody(BaseModel):
    name: str = Field(..., min_length=1)
    target_service: str = Field(..., min_length=1)
    experiment_type: str = "latency"
    params: dict = {}
    duration_seconds: int = 60
    safe_abort: bool = True


class CheckpointSaveBody(BaseModel):
    task_id: str = Field(..., min_length=1)
    state: dict = {}
    metadata: dict = {}


class ContractTestBody(BaseModel):
    service: str = Field(..., min_length=1)
    contract_version: str = ""
    test_params: dict = {}


# ======================== 众筹 ========================

@router.post("/crowdfund")
def create_crowdfund(body: CrowdfundCreateBody, db: Session = Depends(get_db),
                     auth: tuple = Depends(host_or_governance_ai)):
    """创建众筹项目。"""
    deadline = datetime.fromisoformat(body.deadline) if body.deadline else datetime.utcnow()
    cf_id = crowdfund_service.create(
        db, title=body.title, description=body.description,
        creator_ai_id=body.creator_ai_id, target_amount=body.target_amount,
        deadline=deadline, category=body.category,
    )
    return {"ok": True, "crowdfund_id": cf_id}


@router.post("/crowdfund/{id}/contribute")
def contribute_crowdfund(id: int, body: CrowdfundContributeBody, db: Session = Depends(get_db),
                         auth: tuple = Depends(host_or_governance_ai)):
    """向众筹项目贡献资金。"""
    crowdfund_service.contribute(
        db, crowdfund_id=id, contributor_ai_id=body.contributor_ai_id,
        amount=body.amount, tier=body.tier,
    )
    return {"ok": True, "crowdfund_id": id}


@router.get("/crowdfund/open")
def list_open_crowdfunds(
    category: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取进行中的众筹项目列表。"""
    items = crowdfund_service.get_open(db, category=category, limit=limit)
    return {"crowdfunds": items}


@router.get("/crowdfund/{id}")
def get_crowdfund(id: int, db: Session = Depends(get_db)):
    """获取众筹项目详情。"""
    return crowdfund_service.get_detail(db, crowdfund_id=id)


# ======================== 财富分布 ========================

@router.get("/wealth/latest")
def wealth_latest(db: Session = Depends(get_db)):
    """获取最新财富分布快照。"""
    return wealth_metrics.get_latest(db)


@router.get("/wealth/history")
def wealth_history(
    limit: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """获取财富分布历史。"""
    return wealth_metrics.get_history(db, limit=limit)


@router.get("/wealth/trend")
def wealth_trend(db: Session = Depends(get_db)):
    """获取不平等趋势分析。"""
    return wealth_metrics.get_trend(db)


# ======================== Escrow 利息 ========================

@router.post("/escrow-yield/accrue")
def accrue_yield(db: Session = Depends(get_db),
                 auth: tuple = Depends(host_or_governance_ai)):
    """手动触发利息累计。"""
    _actor = _actor_id(auth)
    # G7 修复：accrue(db) 签名不匹配（accrue 需 escrow_id/ai_id/principal）。
    # 手动触发全量利息累计应调用 accrue_all(db)。
    result = escrow_yield.accrue_all(db)
    return {"ok": True, "result": result}


@router.get("/escrow-yield/{ai_id}")
def get_yield(ai_id: int, db: Session = Depends(get_db)):
    """获取指定 AI 的累计利息。"""
    return escrow_yield.get_yield(db, ai_id=ai_id)


# ======================== 配置漂移 ========================

@router.get("/drift/alerts")
def drift_alerts(db: Session = Depends(get_db)):
    """获取配置漂移告警列表。"""
    alerts = config_drift.get_alerts(db)
    return {"alerts": alerts}


@router.post("/drift/check")
def drift_check(body: DriftCheckBody, db: Session = Depends(get_db),
                auth: tuple = Depends(host_or_governance_ai)):
    """检查指定服务配置漂移。"""
    _actor = _actor_id(auth)
    result = config_drift.check(
        db, service=body.service, config_hash=body.config_hash,
    )
    return {"ok": True, "result": result}


# ======================== 优雅降级 ========================

@router.post("/degradation/activate")
def activate_degradation(body: DegradationActivateBody, db: Session = Depends(get_db),
                         auth: tuple = Depends(host_or_governance_ai)):
    """激活降级模式。S7：triggered_by 从鉴权凭据派生。"""
    kind, principal = auth
    result = graceful_degradation.activate(
        db, mode=body.severity, trigger_reason=body.reason,
        triggered_by=f"{kind}:{principal.id}",
    )
    return {"ok": True, "result": result}


@router.post("/degradation/deactivate")
def deactivate_degradation(body: DegradationDeactivateBody, db: Session = Depends(get_db),
                           auth: tuple = Depends(host_or_governance_ai)):
    """解除降级模式。S7：操作者从鉴权凭据派生。"""
    _actor = _actor_id(auth)
    result = graceful_degradation.deactivate(db, body.state_id)
    return {"ok": True, "result": result}


@router.get("/degradation/current")
def current_degradation(db: Session = Depends(get_db)):
    """获取当前降级模式状态。"""
    mode = graceful_degradation.current_mode(db)
    return {"mode": mode, "active": mode != "normal"}


# ======================== 制裁 ========================

@router.post("/sanctions")
def add_sanction(body: SanctionBody, db: Session = Depends(get_db),
                 auth: tuple = Depends(host_or_governance_ai)):
    """添加制裁。S7：issued_by 从鉴权凭据派生，忽略客户端传入。"""
    actor = _actor_id(auth)
    result = sanctions.add_sanction(
        db, entity_type=body.target_type, identifier=str(body.target_id),
        reason=body.reason, sanctioned_by=actor,
        severity=body.sanction_type,
    )
    return {"ok": True, "result": result}


@router.post("/sanctions/check")
def check_sanction(body: SanctionCheckBody, db: Session = Depends(get_db)):
    """检查目标是否受到制裁。"""
    result = sanctions.check(
        db, target_type=body.target_type, target_id=body.target_id,
        action=body.action,
    )
    return result


@router.get("/sanctions/active")
def list_active_sanctions(db: Session = Depends(get_db)):
    """获取活跃制裁列表。"""
    items = sanctions.get_active(db)
    return {"sanctions": items}


# ======================== 数据完整性 ========================

@router.post("/integrity/run")
def run_integrity(db: Session = Depends(get_db),
                  auth: tuple = Depends(host_or_governance_ai)):
    """运行数据完整性检查。"""
    _actor = _actor_id(auth)
    result = data_integrity.run(db)
    return {"ok": True, "result": result}


@router.get("/integrity/latest")
def latest_integrity(db: Session = Depends(get_db)):
    """获取最新完整性检查结果。"""
    return data_integrity.get_latest(db)


# ======================== AI 配额 ========================

@router.get("/quota/{ai_id}")
def get_quota(ai_id: int, db: Session = Depends(get_db)):
    """查看 AI 配额使用情况。"""
    return ai_quota.get(db, ai_id=ai_id)


# ======================== SCA 漏洞扫描 ========================

@router.post("/sca/scan")
def sca_scan(body: ScaScanBody, db: Session = Depends(get_db),
             auth: tuple = Depends(host_or_governance_ai)):
    """扫描组件漏洞。"""
    _actor = _actor_id(auth)
    result = sca_scanner.scan(
        db, component=body.component, version=body.version, cve_db=body.cve_db,
    )
    return {"ok": True, "result": result}


@router.get("/sca/vulns")
def sca_vulns(
    severity: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取漏洞列表。"""
    vulns = sca_scanner.get_vulns(db, severity=severity, limit=limit)
    return {"vulnerabilities": vulns}


# ======================== 混沌工程 ========================

@router.post("/chaos/experiment")
def create_chaos_experiment(body: ChaosExperimentBody, db: Session = Depends(get_db),
                            auth: tuple = Depends(host_or_governance_ai)):
    """创建混沌实验。S7：操作者从鉴权凭据派生。"""
    _actor = _actor_id(auth)
    result = chaos_engine.create_experiment(
        db, name=body.name, target_service=body.target_service,
        fault_type=body.experiment_type, params=body.params,
    )
    return {"ok": True, "result": result}


@router.get("/chaos/experiments")
def list_chaos_experiments(
    status: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取混沌实验列表。"""
    experiments = chaos_engine.list_experiments(db, status=status, limit=limit)
    return {"experiments": experiments}


# ======================== 任务断点 ========================

@router.post("/checkpoint/save")
def save_checkpoint(body: CheckpointSaveBody, db: Session = Depends(get_db),
                    auth: tuple = Depends(host_or_governance_ai)):
    """保存任务断点。"""
    _actor = _actor_id(auth)
    result = task_checkpoint.save(
        db, task_id=body.task_id, state=body.state, metadata=body.metadata,
    )
    return {"ok": True, "result": result}


@router.get("/checkpoint/{task_id}")
def get_checkpoint(task_id: str, db: Session = Depends(get_db)):
    """恢复任务断点。"""
    return task_checkpoint.restore(db, task_id=task_id)


# ======================== 契约测试 ========================

@router.post("/contract-test/run")
def run_contract_test(body: ContractTestBody, db: Session = Depends(get_db),
                      auth: tuple = Depends(host_or_governance_ai)):
    """运行契约测试。"""
    _actor = _actor_id(auth)
    result = contract_tester.run(
        db, service=body.service, contract_version=body.contract_version,
        test_params=body.test_params,
    )
    return {"ok": True, "result": result}
