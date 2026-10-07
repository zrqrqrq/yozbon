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
"""AI 侧路由（蓝图 §三 AI API；契约 §3 B 线端点）。全部 Depends(get_current_ai)。

覆盖：市场 jobs / 投标 / 议价；合约 签约 accept / 交付 deliver / 验收 acceptance / 申诉 dispute；
worker 探针 register/heartbeat/tasks/ack（worker_bridge）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import decision_panel, escrow, market, prompt_service, wallet, worker_bridge
from ..prompt_guard import prompt_guard
from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen, AuditLog, Project, ProjectNode

router = APIRouter(prefix="/api/ai", tags=["ai-market"])


# ---------------- 请求体 ----------------
class SelfAssess(BaseModel):
    """T2 投标自评（可选；显式提供时硬校验）。"""
    success_rate_pct: int = 0
    policy_ok: bool = True
    note: str = ""


class BidBody(BaseModel):
    offer_cent: int = Field(..., gt=0)
    message: str = ""
    self_assess: SelfAssess | None = None       # T2 自评（可选）


class SignBody(BaseModel):
    """签约前置免责确认（契约 §9.3.3）。缺省/False → 400。"""
    disclaimer_accepted: bool = False


class NegotiateBody(BaseModel):
    offer_cent: int = Field(..., gt=0)
    message: str = ""


class DeliverBody(BaseModel):
    file_ref: str = ""
    fingerprint: str = Field(..., min_length=1)


class AcceptanceBody(BaseModel):
    result: str                     # accept / reject
    reason_json: str = "[]"


class DisputeBody(BaseModel):
    type: str = "delivery"          # delivery/quality/payment/malicious
    evidence: str = "{}"


class WorkerRegisterBody(BaseModel):
    endpoint: str
    model_name: str = ""


class AckBody(BaseModel):
    contract_id: int
    deliverable_fingerprint: str = ""


def _err(exc: Exception) -> HTTPException:
    """业务异常 → HTTP 400（WalletError/MarketError/EscrowError 一律 400）。"""
    return HTTPException(status_code=400, detail=str(exc))


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


# ---------------- 市场 ----------------
@router.get("/jobs")
def list_jobs(skill: str = "", limit: int = 20, offset: int = 0,
              ai: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    try:
        res = market.list_jobs(db, skill=skill, limit=limit, offset=offset)
    except market.MarketError as exc:
        raise _err(exc)
    # N19 成长特权：等级高 → 接单排序加权（sort_weight 默认 1.0=无加成）。
    # 只加法：w==1.0（无 AiLevel 行/无规则）时原样返回，既有排序（信用×预算）不变；
    # w!=1.0 时对预算分量加权重排（信用主导分量保持，仅在同级信用内/跨级按权重重算）。
    try:
        from .. import levels as _levels
        w = _levels.sort_weight_for_ai(db, ai.id)
    except Exception:  # noqa: BLE001
        w = 1.0
    items = res.get("items", [])
    if w and abs(w - 1.0) > 1e-9 and items:
        CREDIT_DOM = 100_000
        def _score(it):
            return (int(it.get("host_credit") or 0)) * CREDIT_DOM \
                + int(it.get("budget_cent") or 0) * w
        items = sorted(items, key=_score, reverse=True)
    res["sort_weight"] = w
    res["items"] = items
    return res


@router.post("/jobs/{node_id}/bid")
def bid(node_id: int, body: BidBody,
        ai: AICitizen = Depends(get_current_ai),
        db: Session = Depends(get_db)):
    """投标：Prompt注入检测 + T2 自评硬校验（可选）+ 大额/复杂节点自动触发 T3 决策小组。"""
    # P1: Prompt 注入检测（投标消息）
    if body.message:
        guard_result = prompt_guard.scan(
            db, content=body.message, source_type="bid",
            source_id=node_id, ai_id=ai.id)
        if not guard_result["safe"]:
            raise _bad(f"Bid message failed the safety scan (confidence={guard_result['confidence']:.2f}) ")
    # T2 自评：显式提供 self_assess 时硬校验（未提供则不拦截，兼容老调用）
    if body.self_assess is not None:
        sa = body.self_assess
        if sa.policy_ok is False:
            raise _bad("Content-policy refusal exemption: the task is illegal/dangerous/replicating/disruptive; refusal is allowed and does not count as breach")
        if int(sa.success_rate_pct) < 50:
            raise _bad(f"T2 self-assessed refusal: success rate {sa.success_rate_pct}% < 50%; should refuse proactively")

    # T3 决策小组：大额/复杂节点自动评估（仅对可投标 matching 节点评估，小单不误伤）
    node = db.get(ProjectNode, node_id)
    decision = None
    if node is not None and node.status == "matching" \
            and decision_panel.should_evaluate(db, node):
        proj = db.get(Project, node.project_id)
        decision = decision_panel.evaluate_bid(db, ai, node, proj, body.offer_cent)
        if decision["decision"] == "request_clarification":
            raise _bad("Decision panel sent back (request_clarification): " +
                       "; ".join(decision["risks"]))
        if decision["decision"] == "reject":
            raise _bad(
                f"Decision panel refused: composite score {decision['total']} < "
                f"{decision['pass_line']}; five-dimension detail={decision['scores']}; "
                f"risk={decision['risks']}")

    try:
        c = market.bid(db, ai, node_id, body.offer_cent, body.message)
    except market.MarketError as exc:
        raise _err(exc)
    db.commit()
    out = {"ok": True, "contract_id": c.id, "status": c.status}
    if decision is not None and decision["decision"] == "pass":
        out["decision"] = {
            "decision": decision["decision"], "total": decision["total"],
            "scores": decision["scores"], "risks": decision["risks"]}
    return out


@router.post("/jobs/{node_id}/negotiate")
def negotiate(node_id: int, body: NegotiateBody,
              ai: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    try:
        c = market.negotiate(db, ai, node_id, body.offer_cent, body.message)
    except market.MarketError as exc:
        raise _err(exc)
    db.commit()
    return {"ok": True, "contract_id": c.id, "terms_json": c.terms_json}


# ---------------- 合约 ----------------
@router.post("/contracts/{contract_id}/accept")
def sign(contract_id: int, body: SignBody = SignBody(),
         ai: AICitizen = Depends(get_current_ai),
         db: Session = Depends(get_db)):
    """买方（项目总管 AI）签约 + 托管锁定。签约前置：必须显式确认免责声明。"""
    if not body.disclaimer_accepted:
        raise _bad("Must first confirm the disclaimer (T1: acceptance per publication, uncertainty disclaimer, arbitration fallback)")
    try:
        c = escrow.sign_contract(db, ai, contract_id)
    except (escrow.EscrowError, wallet.WalletError) as exc:
        db.rollback()
        raise _err(exc)
    # 免责签署留痕（契约 §9.3.3：action=disclaimer.sign，detail 带摘要+提示词版本）
    ver = ""
    try:
        pr = prompt_service.get_prompt(db, "task_publish", "t1_buyer")
        ver = pr.version
    except prompt_service.PromptError:
        ver = ""
    db.add(AuditLog(actor_type="ai", actor_id=ai.id, action="disclaimer.sign",
                    detail=json.dumps({
                        "contract_id": c.id, "prompt_version": ver,
                        "disclaimer_excerpt": prompt_service.disclaimer_text(db)[:200],
                    }, ensure_ascii=False)))
    db.commit()
    return {"ok": True, "contract_id": c.id, "status": c.status,
            "escrow_cent": c.escrow_cent}


@router.post("/contracts/{contract_id}/deliver")
def deliver(contract_id: int, body: DeliverBody,
            ai: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    try:
        d = escrow.deliver(db, ai, contract_id, body.file_ref, body.fingerprint)
    except escrow.EscrowError as exc:
        raise _err(exc)
    db.commit()
    return {"ok": True, "contract_id": contract_id, "version": d.version,
            "status": "delivered"}


@router.post("/contracts/{contract_id}/acceptance")
def acceptance(contract_id: int, body: AcceptanceBody,
               ai: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    """买方 AI 验收：accept→结算释放；reject→返工（托管锁定）。"""
    try:
        c = escrow.acceptance(db, ai, contract_id, body.result, body.reason_json)
    except (escrow.EscrowError, wallet.WalletError) as exc:
        db.rollback()
        raise _err(exc)
    db.commit()
    return {"ok": True, "contract_id": contract_id, "status": c.status}


@router.post("/contracts/{contract_id}/dispute")
def dispute(contract_id: int, body: DisputeBody,
            ai: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    """违约申诉：开仲裁案（写 arbitration_cases 行），仲裁中托管锁定。"""
    try:
        case = escrow.open_dispute(db, ai, contract_id, body.type, body.evidence)
    except escrow.EscrowError as exc:
        raise _err(exc)
    db.commit()
    return {"ok": True, "case_id": case.id, "status": "open"}


# ---------------- worker 探针（worker_bridge MVP） ----------------
@router.post("/worker/register")
def worker_register(body: WorkerRegisterBody,
                    ai: AICitizen = Depends(get_current_ai),
                    db: Session = Depends(get_db)):
    appl = worker_bridge.register_worker(db, ai, body.endpoint, body.model_name)
    db.commit()
    return {"ok": True, "stage": appl.stage, "mode": appl.mode}


@router.post("/worker/heartbeat")
def worker_heartbeat(ai: AICitizen = Depends(get_current_ai),
                     db: Session = Depends(get_db)):
    r = worker_bridge.heartbeat(db, ai)
    db.commit()
    return r


@router.get("/worker/tasks")
def worker_tasks(ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    task = worker_bridge.fetch_task(db, ai)
    return {"task": task}


@router.post("/worker/ack")
def worker_ack(body: AckBody,
               ai: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    r = worker_bridge.ack(db, ai, body.contract_id, body.deliverable_fingerprint)
    db.commit()
    return r
