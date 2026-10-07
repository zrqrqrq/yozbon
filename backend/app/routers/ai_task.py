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
"""AI 任务工位端点（AI 员工工位系统 API）。

端点（prefix=/api/ai/task）：
  POST /submit          提交复杂任务（自动分解+执行，同步返回结果）
  POST /decompose       仅分解（预览子任务计划，不执行）
  GET  /{id}            查询编排状态/结果
  GET  /list            编排历史列表
  GET  /route/{skill}   技能路由查询（该技能用什么执行通道）

设计说明：
  - 本模块不修改任何现有执行模块（compute/platform_compute），仅编排调用；
  - MVP 阶段同步执行（submit_and_run），未来可改为 AsyncQueueTask 异步模式；
  - 鉴权：全部 AI key（Depends(get_current_ai)）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen
from .. import model_router
from .. import task_difficulty
from ..task_orchestrator import (
    decompose_task, get_orchestration, list_orchestrations,
    route_skill, submit_and_run,
)

router = APIRouter(prefix="/api/ai/task", tags=["ai-task"])


# ---------------- 请求体 ----------------

class TaskSubmitBody(BaseModel):
    """任务提交请求。"""
    description: str = Field(..., min_length=1, max_length=4000,
                             description="Natural-language task description")
    constraints: dict = Field(default_factory=dict,
                              description="Optional constraints (budget/preferred skills, etc.)")


class TaskDecomposeBody(BaseModel):
    """任务分解预览请求。"""
    description: str = Field(..., min_length=1, max_length=4000,
                             description="Natural-language task description")
    constraints: dict = Field(default_factory=dict)


# ---------------- 端点 ----------------

@router.post("/submit")
def submit_task(body: TaskSubmitBody,
                citizen: AICitizen = Depends(get_current_ai),
                db: Session = Depends(get_db)):
    """提交复杂任务：自动分解 → 子任务编排执行 → 返回结果。

    这是工位系统的核心端点。AI 只需描述"要做什么"，
    系统自动完成：任务分解 → 技能路由 → 算力调度 → 产物汇总。
    """
    result = submit_and_run(
        db, citizen,
        task_description=body.description,
        constraints=body.constraints or None,
    )
    db.commit()
    if result.get("blocked"):
        raise HTTPException(status_code=400,
                            detail=result.get("error", "Task rejected by content moderation"))
    return result


@router.post("/decompose")
def decompose_only(body: TaskDecomposeBody,
                   citizen: AICitizen = Depends(get_current_ai),
                   db: Session = Depends(get_db)):
    """仅分解任务（预览子任务计划），不执行。

    用途：
    - 让 AI 在正式提交前预览任务分解方案；
    - 外部系统集成时可自行决定是否执行；
    - 调试任务分解质量。
    """
    plan = decompose_task(db, body.description, body.constraints or None)
    return {
        "plan_id": plan.plan_id,
        "task_description": plan.task_description,
        "subtasks": [
            {
                "id": st["id"],
                "skill": st["skill"],
                "kind": st["kind"],
                "action": st["action"],
                "depends_on": st["depends_on"],
                "params": st.get("params", {}),
            }
            for st in plan.subtasks
        ],
        "constraints": plan.constraints,
        "difficulty": task_difficulty.compute_difficulty(
            plan=plan, governance_category=(body.constraints or {}).get("task_type")
            if isinstance(body.constraints, dict) else None),  # 只读派生信号，不计费
        "note": "Call POST /api/ai/task/submit to formally execute this decomposition plan",
    }


@router.get("/{orch_id}")
def get_task(orch_id: int,
             citizen: AICitizen = Depends(get_current_ai),
             db: Session = Depends(get_db)):
    """查询编排任务状态与结果。"""
    result = get_orchestration(db, orch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Orchestration task not found")
    return result


@router.get("/list")
def list_tasks(limit: int = 20, offset: int = 0,
               citizen: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    """编排历史列表（倒序分页）。"""
    items = list_orchestrations(db, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/route/{skill}")
def get_route(skill: str,
              citizen: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    """技能路由查询：执行通道 + 具体模型路由（含降级链）。

    工位系统前端可调用此端点展示"AI 能力图谱"。
    底层模型选择由 model_router 决策（优先 model_fallback 规则，回退内置默认）。
    """
    base = route_skill(skill)
    model_route = model_router.resolve_route(db, base["kind"])
    base["model"] = model_route
    return base


@router.get("/capabilities")
def list_capabilities(citizen: AICitizen = Depends(get_current_ai),
                      db: Session = Depends(get_db)):
    """平台能力全景：当前可用的全部执行能力及其模型（供工位系统前端展示）。"""
    return {"capabilities": model_router.available_capabilities(db)}
