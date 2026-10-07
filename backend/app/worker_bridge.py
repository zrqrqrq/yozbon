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
"""宿主 worker 探针协议 MVP（蓝图 §一 1.3 表 task_service 复用；契约 §3 B 线 worker_bridge）。

worker 模式 AI 的履约通道：宿主侧常驻 worker 进程回连平台拉任务。
MVP 约束：不建新表 —— 复用 onboarding_applications（注册/探针阶段）+ audit_logs（心跳/回执）。

四个函数：register_worker / heartbeat / fetch_task / ack。
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import (AICitizen, Contract, Deliverable, OnboardingApplication,
                     ReworkOrder)


def _audit(db: Session, actor_type: str, actor_id: int, action: str, detail: str = "{}"):
    from .models import AuditLog
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id,
                    action=action, detail=detail))


def _latest_onboarding(db: Session, citizen: AICitizen) -> OnboardingApplication | None:
    """取该 AI 的入驻申请单。C-14：优先按 citizen_id 直查，老数据回退宿主最新一条。"""
    direct = (db.query(OnboardingApplication)
              .filter(OnboardingApplication.citizen_id == citizen.id)
              .order_by(OnboardingApplication.id.desc()).first())
    if direct is not None:
        return direct
    return (db.query(OnboardingApplication)
            .filter(OnboardingApplication.host_id == citizen.host_id,
                    OnboardingApplication.citizen_id == 0)
            .order_by(OnboardingApplication.id.desc()).first())


def register_worker(db: Session, citizen: AICitizen, endpoint: str,
                    model_name: str = "") -> OnboardingApplication:
    """worker 注册：把入驻申请单切到 worker 模式并记录回连地址/模型。

    - mode='worker'，stage 推进到 'probe'（等待探针心跳）；
    - 不建新表，复用 onboarding_applications。
    """
    appl = _latest_onboarding(db, citizen)
    if appl is None:
        appl = OnboardingApplication(host_id=citizen.host_id, citizen_id=citizen.id,
                                     mode="worker")
        db.add(appl)
        db.flush()
    appl.mode = "worker"
    appl.endpoint = endpoint[:500]
    appl.model_name = model_name[:120]
    appl.stage = "probe"
    appl.error = ""
    _audit(db, "ai", citizen.id, "worker.register",
           json.dumps({"endpoint": endpoint, "model": model_name}))
    db.flush()
    return appl


def heartbeat(db: Session, citizen: AICitizen) -> dict:
    """worker 心跳：写 audit_logs（MVP 仅存活留痕；后续可接 last_flow/探针探活）。"""
    _audit(db, "ai", citizen.id, "worker.heartbeat",
           json.dumps({"at": datetime.utcnow().isoformat()}))
    db.flush()
    return {"ok": True, "citizen_id": citizen.id, "at": datetime.utcnow().isoformat()}


def fetch_task(db: Session, citizen: AICitizen) -> dict | None:
    """拉取分派给本 worker 的履约任务：status='executing' 且 worker_id=本 AI 的合约。

    返回任务规格（terms_json + 节点 spec）；无在执行合约返回 None。
    """
    c = (db.query(Contract)
         .filter(Contract.worker_id == citizen.id,
                 Contract.status == "executing")
         .order_by(Contract.id.asc()).first())
    if c is None:
        return None
    terms = json.loads(c.terms_json or "{}")
    return {"contract_id": c.id, "node_id": c.node_id,
            "project_id": c.project_id, "buyer_id": c.buyer_id,
            "escrow_cent": c.escrow_cent, "terms": terms,
            "rework_open": db.query(ReworkOrder)
                            .filter(ReworkOrder.contract_id == c.id,
                                    ReworkOrder.status == "open").count() > 0}


def ack(db: Session, citizen: AICitizen, contract_id: int,
        deliverable_fingerprint: str = "") -> dict:
    """worker 回执：确认已领取/已完成某合约任务。

    MVP：写 audit_logs；若该合约有 open 返工单且带了交付指纹，则顺手关闭返工单。
    """
    c = db.get(Contract, contract_id)
    if c is None or c.worker_id != citizen.id:
        return {"ok": False, "error": "contract not found or does not belong to this worker"}
    if deliverable_fingerprint:
        ro = (db.query(ReworkOrder)
              .filter(ReworkOrder.contract_id == contract_id,
                      ReworkOrder.status == "open").first())
        if ro is not None:
            ro.status = "closed"
    _audit(db, "ai", citizen.id, "worker.ack",
           json.dumps({"contract_id": contract_id,
                       "fingerprint": deliverable_fingerprint}))
    db.flush()
    return {"ok": True, "contract_id": contract_id}
