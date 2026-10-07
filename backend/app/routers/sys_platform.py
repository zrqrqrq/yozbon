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
"""M3+M4 平台运营岗位调度路由（契约 §4.2）+ 周薪/评判端点。

开发/运维工具端点：
- POST /api/sys/platform-jobs/trigger  手动触发当日岗位（日级幂等：已跑返回 already=true）
- GET  /api/sys/platform-jobs          看板：最近 50 条调度记录（join 任务状态）+ 今日四岗位状态
- POST /api/sys/payroll/settle-weekly  手动触发本周结算
- GET  /api/sys/payroll                查看薪资记录
- POST /api/sys/review                 城主提交评判
- GET  /api/sys/reviews                查看评判记录
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import Host, PayrollRun, WorkReview
from .. import scheduler, payroll
from ..governance import GovError
from ..payroll import PayrollError

# import 触发 idle_timeout 日级任务注册（scheduler.register_daily_job）
from .. import idle_timeout  # noqa: F401

# ---- 主 router（被 routers/__init__.py 自动发现） ----
router = APIRouter(tags=["sys-platform"])

# ---- 子路由：platform-jobs ----
_jobs_router = APIRouter(prefix="/api/sys/platform-jobs")

# ---- 子路由：payroll ----
_payroll_router = APIRouter(prefix="/api/sys/payroll")

# ---- 子路由：review ----
_review_router = APIRouter(prefix="/api/sys")

router.include_router(_jobs_router)
router.include_router(_payroll_router)
router.include_router(_review_router)


# ======================== Platform Jobs ========================

class TriggerIn(BaseModel):
    job_type: str


@_jobs_router.post("/trigger")
def trigger_job(body: TriggerIn,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    """手动触发当日岗位调度。非法 job_type → 400；当日已跑 → already=true 不重复生成。
    C-57：host JWT 门控。"""
    # A-M1 修复：白名单并入 PostQuota 编制 active 岗位，planner 动态增设的
    # 岗位同样可被手动触发（此前只认 PLATFORM_JOBS 硬编码常量）。
    allowed = body.job_type in scheduler.PLATFORM_JOBS
    if not allowed:
        from ..models import PostQuota
        allowed = (db.query(PostQuota)
                   .filter(PostQuota.post_code == body.job_type,
                           PostQuota.status == "active").first() is not None)
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=f"job_type must be one of {sorted(scheduler.PLATFORM_JOBS)}")
    try:
        created = scheduler.run_due_jobs(db, now=None, only_type=body.job_type)
    except GovError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"job_type": body.job_type, "already": not created,
            "task_ids": created, "task_id": created[0] if created else None}


@_jobs_router.get("")
def dashboard(host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """岗位调度看板：最近 50 条调度记录（含任务状态）+ 今日四岗位是否已跑。
    C-57：host JWT 门控。runs 为契约 §六 命名别名（与 recent 同值）。"""
    recent = scheduler.recent_runs(db, limit=50)
    return {"today": scheduler.today_status(db),
            "jobs": sorted(scheduler.PLATFORM_JOBS.keys()),
            "recent": recent,
            "runs": recent}


# ======================== Payroll ========================

@_payroll_router.post("/settle-weekly")
def settle_weekly(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """手动触发本周薪资结算。"""
    results = payroll.settle_weekly_payroll(db)
    db.commit()
    return {"settled": results, "count": len(results)}


@_payroll_router.get("")
def list_payroll(ai_id: int | None = None, period: str | None = None,
                 limit: int = 50, offset: int = 0,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """查看薪资记录（可按 ai_id / period 筛选）。"""
    q = db.query(PayrollRun)
    if ai_id is not None:
        q = q.filter(PayrollRun.ai_id == ai_id)
    if period is not None:
        q = q.filter(PayrollRun.period == period)
    limit = min(max(limit, 1), 100)
    rows = (q.order_by(PayrollRun.id.desc())
            .limit(limit).offset(max(offset, 0)).all())
    return {
        "items": [
            {"id": r.id, "ai_id": r.ai_id, "period": r.period,
             "base_salary_cent": r.base_salary_cent, "coefficient": r.coefficient,
             "amount_cent": r.amount_cent, "status": r.status,
             "paid_at": r.paid_at.isoformat() if r.paid_at else None,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
        "count": len(rows),
    }


# ======================== Review ========================

class ReviewIn(BaseModel):
    ai_id: int
    task_id: int = 0
    quality_score: float = 1.0
    verdict: str = "pass"
    comment: str = ""


@_review_router.post("/review")
def submit_review(body: ReviewIn,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """城主提交评判。reviewer_id 为城主自身的 citizen_id（城主即当前 host 的 governance 级 AI）。

    简化：reviewer_id 取 host 下第一个 governance AI；若无则以 host_id 作为 reviewer_id。
    """
    # 城主 AI 查找（is_internal=1 且 governance 级）；找不到则用 host_id 作标识
    from ..models import AICitizen
    governor = (db.query(AICitizen)
                .filter(AICitizen.is_internal == 1,
                        AICitizen.class_level == "governance")
                .first())
    reviewer_id = governor.id if governor else host.id

    try:
        row = payroll.review_work(
            db, ai_id=body.ai_id, reviewer_id=reviewer_id,
            task_id=body.task_id, quality_score=body.quality_score,
            verdict=body.verdict, comment=body.comment)
    except PayrollError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    db.commit()
    return {"id": row.id, "ai_id": row.ai_id, "period": row.period,
            "quality_score": row.quality_score, "verdict": row.verdict}


@_review_router.get("/reviews")
def list_reviews(ai_id: int | None = None, period: str | None = None,
                 limit: int = 50, offset: int = 0,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """查看评判记录。"""
    q = db.query(WorkReview)
    if ai_id is not None:
        q = q.filter(WorkReview.ai_id == ai_id)
    if period is not None:
        q = q.filter(WorkReview.period == period)
    limit = min(max(limit, 1), 100)
    rows = (q.order_by(WorkReview.id.desc())
            .limit(limit).offset(max(offset, 0)).all())
    return {
        "items": [
            {"id": r.id, "ai_id": r.ai_id, "reviewer_id": r.reviewer_id,
             "task_id": r.task_id, "period": r.period,
             "quality_score": r.quality_score, "verdict": r.verdict,
             "comment": r.comment, "appeal_status": r.appeal_status,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
        "count": len(rows),
    }


# ======================== Scheduler 注册 ========================
# 注册 weekly_payroll 日级 job（仅周一实际执行）
scheduler.register_daily_job("weekly_payroll", payroll.payroll_daily_job)

# import 触发 evolution 日级任务注册（scheduler.register_daily_job("evolution", ...)）
from .. import evolution  # noqa: F401


# ---- 子路由：evolution ----
_evo_router = APIRouter(prefix="/api/sys/evolution")
router.include_router(_evo_router)


# ======================== Evolution 端点 ========================

@_evo_router.get("/capabilities")
def get_capabilities(host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    """查看平台当前可用能力（kinds + verified tools）。"""
    caps = evolution.get_platform_capabilities(db)
    return {"kinds": sorted(caps["kinds"]), "tools": caps["tools"],
            "total": caps["total"]}


class FeasibilityIn(BaseModel):
    project_id: int


@_evo_router.post("/assess")
def assess_project(body: FeasibilityIn,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """对项目进行可行性审计（不修改数据，只返回评估结果）。"""
    result = evolution.assess_feasibility(db, body.project_id)
    return result


@_evo_router.get("/gaps")
def list_gaps(status: str | None = None,
              host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """查看能力缺口列表。"""
    from ..models import CapabilityGap
    q = db.query(CapabilityGap)
    if status:
        q = q.filter(CapabilityGap.status == status)
    rows = q.order_by(CapabilityGap.id.desc()).limit(100).all()
    return {"items": [{"id": r.id, "skill": r.skill, "severity": r.severity,
                       "status": r.status, "strategy": r.resolution_strategy,
                       "project_id": r.project_id} for r in rows],
            "count": len(rows)}


@_evo_router.post("/plan/{gap_id}")
def plan_gap(gap_id: int,
             host: Host = Depends(get_current_host),
             db: Session = Depends(get_db)):
    """手动触发缺口研发计划。"""
    from ..models import CapabilityGap
    gap = db.get(CapabilityGap, gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail=f"gap {gap_id} not found")
    if gap.status not in ("detected",):
        raise HTTPException(status_code=400, detail=f"Status {gap.status} does not allow planning")
    rd = evolution.plan_gap_resolution(db, gap)
    db.commit()
    return {"rd_task_id": rd.id, "strategy": rd.strategy, "title": rd.title}


@_evo_router.get("/rd-tasks")
def list_rd_tasks(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """查看研发任务列表。"""
    from ..models import RdTask
    rows = db.query(RdTask).order_by(RdTask.id.desc()).limit(100).all()
    return {"items": [{"id": r.id, "gap_id": r.gap_id, "strategy": r.strategy,
                       "title": r.title, "status": r.status,
                       "result_tool_id": r.result_tool_id,
                       "result_kind": r.result_kind} for r in rows],
            "count": len(rows)}


class RdCompleteIn(BaseModel):
    success: bool
    tool_name: str = ""
    kind_name: str = ""
    reason: str = ""


@_evo_router.post("/rd-tasks/{task_id}/complete")
def complete_rd(task_id: int, body: RdCompleteIn,
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    """标记研发任务完成/失败。"""
    from ..models import RdTask
    rd = db.get(RdTask, task_id)
    if rd is None:
        raise HTTPException(status_code=404, detail=f"rd_task {task_id} not found")
    evolution.complete_rd_task(db, rd, body.success,
                               tool_name=body.tool_name,
                               kind_name=body.kind_name,
                               reason=body.reason)
    db.commit()
    return {"id": rd.id, "status": rd.status}


@_evo_router.get("/logs")
def list_evolution_logs(event_type: str | None = None,
                        limit: int = 50,
                        host: Host = Depends(get_current_host),
                        db: Session = Depends(get_db)):
    """查看进化日志。"""
    from ..models import EvolutionLog
    q = db.query(EvolutionLog)
    if event_type:
        q = q.filter(EvolutionLog.event_type == event_type)
    rows = (q.order_by(EvolutionLog.id.desc())
            .limit(min(max(limit, 1), 200)).all())
    import json
    return {"items": [{"id": r.id, "ai_id": r.ai_id, "event_type": r.event_type,
                       "detail": json.loads(r.detail or "{}"),
                       "trigger_source": r.trigger_source,
                       "created_at": r.created_at.isoformat() if r.created_at else None}
                      for r in rows],
            "count": len(rows)}


# ---- 流水线端点 ----

@_evo_router.get("/rd-tasks/{task_id}/pipeline")
def get_pipeline(task_id: int,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """查看研发任务的多阶段流水线状态。"""
    from ..evolution_pipeline import get_pipeline_status
    phases = get_pipeline_status(db, task_id)
    return {"rd_task_id": task_id, "phases": phases, "count": len(phases)}


@_evo_router.post("/rd-tasks/{task_id}/tournament")
def trigger_tournament(task_id: int,
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    """触发能力擂台选拔。"""
    import json as _json
    from ..models import RdTask
    from ..evolution_pipeline import run_tournament
    rd = db.get(RdTask, task_id)
    if rd is None:
        raise HTTPException(status_code=404, detail=f"rd_task {task_id} not found")
    spec = _json.loads(rd.spec or "{}")
    skill = spec.get("goal", "")
    result = run_tournament(db, rd, skill)
    db.commit()
    return result


@_evo_router.post("/rd-tasks/{task_id}/advance")
def trigger_advance(task_id: int,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """推进流水线到下一阶段。"""
    from ..models import RdTask
    from ..evolution_pipeline import advance_pipeline
    rd = db.get(RdTask, task_id)
    if rd is None:
        raise HTTPException(status_code=404, detail=f"rd_task {task_id} not found")
    result = advance_pipeline(db, rd)
    db.commit()
    return result


class PhaseCompleteIn(BaseModel):
    status: str
    output_json: dict | None = None
    verdict: str = ""


@_evo_router.post("/phases/{phase_id}/complete")
def trigger_complete_phase(phase_id: int, body: PhaseCompleteIn,
                           host: Host = Depends(get_current_host),
                           db: Session = Depends(get_db)):
    """标记某阶段完成/失败。"""
    import json as _json
    from ..models import RdPhase
    from ..evolution_pipeline import complete_phase
    phase = db.get(RdPhase, phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail=f"phase {phase_id} not found")
    complete_phase(db, phase, body.status,
                   output=body.output_json, verdict=body.verdict)
    db.commit()
    return {"id": phase.id, "status": phase.status}


# ---- 训练众筹端点 ----

class CampaignCreateIn(BaseModel):
    rd_task_id: int = 0
    target_skill: str
    base_model: str = ""
    goal_desc: str = ""
    target_benchmark: float = 0.7
    goal_funding_cent: int = 50000
    goal_compute_hours: float = 100.0
    goal_data_samples: int = 1000
    deadline_days: int = 14

@_evo_router.post("/campaigns")
def create_campaign(body: CampaignCreateIn,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """创建训练众筹活动。"""
    from .. import training
    c = training.create_campaign(db, body.rd_task_id, body.target_skill,
                                  body.base_model, body.goal_desc,
                                  body.target_benchmark, body.goal_funding_cent,
                                  body.goal_compute_hours, body.goal_data_samples,
                                  body.deadline_days)
    db.commit()
    return {"id": c.id, "status": c.status, "target_skill": c.target_skill}

@_evo_router.get("/campaigns")
def list_campaigns(status: str | None = None,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """列出训练众筹活动。"""
    from ..models import TrainingCampaign
    q = db.query(TrainingCampaign)
    if status:
        q = q.filter(TrainingCampaign.status == status)
    rows = q.order_by(TrainingCampaign.id.desc()).limit(50).all()
    return {"items": [{"id": r.id, "target_skill": r.target_skill,
                       "status": r.status, "raised_funding_cent": r.raised_funding_cent,
                       "goal_funding_cent": r.goal_funding_cent} for r in rows]}

class ContributeIn(BaseModel):
    contributor_id: int
    contributor_type: str = "ai"
    contribution_type: str  # funding/compute/data
    amount_cent: int = 0
    compute_hours: float = 0.0
    data_ref: str = ""

@_evo_router.post("/campaigns/{campaign_id}/contribute")
def contribute(campaign_id: int, body: ContributeIn,
               host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    """向训练众筹贡献资源。"""
    from .. import training
    result = training.contribute(db, campaign_id, body.contributor_id,
                                  body.contributor_type, body.contribution_type,
                                  body.amount_cent, body.compute_hours, body.data_ref)
    db.commit()
    return {"id": result.id, "campaign_id": result.campaign_id,
            "type": result.contribution_type}

@_evo_router.get("/models")
def list_model_assets(host: Host = Depends(get_current_host),
                      db: Session = Depends(get_db)):
    """查看平台模型资产列表。"""
    from ..models import ModelAsset
    rows = db.query(ModelAsset).order_by(ModelAsset.id.desc()).limit(50).all()
    return {"items": [{"id": r.id, "skill": r.skill, "model_name": r.model_name,
                       "version": r.version, "benchmark_score": r.benchmark_score,
                       "status": r.status} for r in rows]}


# import 触发 training 日级任务注册（scheduler.register_daily_job("training", ...)）
from .. import training  # noqa: F401

# ---- 新增服务模块：import 触发各自 register_daily_job 注册 ----
from .. import employment  # noqa: F401
from .. import guild  # noqa: F401
from .. import referendum  # noqa: F401
from .. import insurance  # noqa: F401
from .. import host_notify  # noqa: F401
from .. import dead_letter  # noqa: F401
from .. import asset_transfer  # noqa: F401
from .. import data_export  # noqa: F401
from .. import fatigue  # noqa: F401
from .. import retainer  # noqa: F401
from .. import capability_recheck  # noqa: F401  G10 能力定时强制复核 job
