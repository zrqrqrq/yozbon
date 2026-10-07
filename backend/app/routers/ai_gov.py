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
"""AI 侧治理路由（蓝图 §三：gov 竞标/报告、评审意见、仲裁判定、事件轮询）。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen, GovernanceTask
from .. import governance, intel_sources, prompt_service
from .. import project as proj
from ..governance import GovError
from ..project import ProjectError

router = APIRouter(prefix="/api/ai", tags=["ai-gov"])


class BidIn(BaseModel):
    price_cent: int = 0
    message: str = ""


class NodeIn(BaseModel):
    key: str
    skill: str = ""
    spec: str = ""
    deliverable_std: str = ""
    budget_cent: int = 0
    duration_h: int = 24


class DepIn(BaseModel):
    from_key: str
    to_key: str


class NodesSubmitIn(BaseModel):
    nodes: list[NodeIn]
    deps: list[DepIn] = []


class GovReportIn(BaseModel):
    conclusion: str = ""
    evidence: dict | str = "{}"


class ReviewSubmitIn(BaseModel):
    verdict: str                     # feasible/infeasible/conditional
    risk: str = ""
    budget_suggest_cent: int = 0
    duration_suggest_h: int = 0
    breakdown: dict | str = "{}"


class VerdictIn(BaseModel):
    decision: str                  # support_worker/refund/split
    ratio: float = 1.0
    reason: str = ""


class AppealIn(BaseModel):
    reason: str = ""


def _err(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc))


@router.post("/gov/tasks/{task_id}/bid")
def bid(task_id: int, body: BidIn, ai: AICitizen = Depends(get_current_ai),
        db: Session = Depends(get_db)):
    try:
        r = governance.bid_task(db, ai.id, task_id, body.price_cent, body.message)
    except GovError as exc:
        _err(exc)
    db.commit()
    return {"id": r.id, "task_id": task_id, "status": r.status}


@router.post("/gov/tasks/{task_id}/report")
def gov_report(task_id: int, body: GovReportIn,
               ai: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    try:
        r = governance.submit_task_report(db, ai.id, task_id, body.conclusion,
                                          body.evidence)
    except GovError as exc:
        _err(exc)
    # C-34（契约 §9.3.5）：platform_intel 且结论=collected → 自动情报采集入库
    # （幂等去重；none_new 不触发；采集失败不影响报告提交主链路）
    try:
        t = db.get(GovernanceTask, task_id)
        if t is not None and t.type == "platform_intel" \
                and body.conclusion == "collected":
            intel_sources.collect_intel(db, "all", collected_by=ai.id)
    except Exception:  # noqa: BLE001  联动容错，不阻塞报告
        pass
    db.commit()
    return {"id": r.id, "task_id": task_id, "status": r.status}


@router.post("/review/{panel_id}/submit")
def review_submit(panel_id: int, body: ReviewSubmitIn,
                  ai: AICitizen = Depends(get_current_ai),
                  db: Session = Depends(get_db)):
    try:
        out = governance.submit_review_opinion(db, ai.id, panel_id, body.verdict,
                                               body.risk, body.budget_suggest_cent,
                                               body.duration_suggest_h, body.breakdown)
    except GovError as exc:
        _err(exc)
    db.commit()
    return out


@router.post("/arbitration/{case_id}/verdict")
def arbitration_verdict(case_id: int, body: VerdictIn,
                        ai: AICitizen = Depends(get_current_ai),
                        db: Session = Depends(get_db)):
    try:
        out = governance.submit_verdict(db, ai.id, case_id, body.decision,
                                         body.ratio, body.reason)
    except GovError as exc:
        db.rollback()
        _err(exc)
    except ImportError:
        # B 线 escrow.release_refund 未落地：联动由测试 importorskip 跳过
        db.rollback()
        raise HTTPException(status_code=501, detail="escrow.release_refund not implemented (track B)")
    db.commit()
    # T4 提示词挂接（契约 §9.3.4）：回带当前 t4 版本号；行为不变
    try:
        out["prompt_version"] = prompt_service.get_prompt(
            db, "arbitration", "t4_arbitrator").version
    except prompt_service.PromptError:
        out["prompt_version"] = ""
    return out


@router.post("/arbitration/{case_id}/appeal")
def arbitration_appeal(case_id: int, body: AppealIn,
                       ai: AICitizen = Depends(get_current_ai),
                       db: Session = Depends(get_db)):
    """败诉方申诉：把已裁决案件推进为 appealed（S12）。

    仅被诉败诉方可申诉、案件须处于已裁决(verdict)态且未过申诉期；
    身份/败诉方/申诉期校验全部下沉 governance.appeal，校验不过 → 400。
    """
    try:
        out = governance.appeal(db, ai.id, case_id, body.reason)
    except GovError as exc:
        db.rollback()
        _err(exc)
    db.commit()
    return out


@router.post("/arbitration/{case_id}/form_panel")
def arbitration_form_panel(case_id: int,
                           ai: AICitizen = Depends(get_current_ai),
                           db: Session = Depends(get_db)):
    """组庭：为 open+空庭 仲裁案自动选任治理级仲裁员（仅治理级 AI，如城主）。

    阻断④：补齐 form_arbitration_panel 的 HTTP 入口，使争议案可被接管并裁决；
    幂等——已组庭/非 open/无合格仲裁员时返回 formed=False。
    """
    if ai.class_level != "governance":
        raise HTTPException(
            status_code=403,
            detail="Only governance-level AI may form an arbitration tribunal")
    case = governance.auto_form_arbitration_panel(db, case_id, seed_ids=[ai.id])
    db.commit()
    if case is None:
        return {"case_id": case_id, "formed": False, "panel": []}
    return {"case_id": case_id, "formed": True, "panel": json.loads(case.panel or "[]")}


@router.get("/events")
def events(limit: int = 20, offset: int = 0,
           ai: AICitizen = Depends(get_current_ai),
           db: Session = Depends(get_db)):
    return governance.my_events(db, ai.id, limit=limit, offset=offset)


@router.post("/projects/{project_id}/nodes")
def submit_nodes(project_id: int, body: NodesSubmitIn,
                 ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    """分解 AI 提交 WBS（节点 + 依赖边）；成环→400。"""
    nodes = [n.model_dump() for n in body.nodes]
    deps = [{"from": d.from_key, "to": d.to_key} for d in body.deps]
    try:
        out = proj.submit_nodes(db, project_id, nodes, deps)
    except ProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return out
