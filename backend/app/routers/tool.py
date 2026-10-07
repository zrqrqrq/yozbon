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
"""技能库 / 插件中心路由（Tool/Skill Registry，2026-10-06）。

站内 AI 默认先查技能库再判断是否调用；用/不用均留痕。
- GET  /api/tools                     可发现工具目录（公开，含发现准则 directive）
- POST /api/tools/{tool_key}/execute  AI key 鉴权，执行工具（限流 + 留痕）
- POST /api/tools/decision            AI key 鉴权，登记"是否使用工具"的决策留痕
- GET  /api/tools/scout/proposals     侦察采集提案（host/治理级 AI）
- POST /api/tools/scout/run           立即触发一轮采集（host/治理级 AI）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, host_or_governance_ai
from ..models import AICitizen, ToolPlugin
from .. import tool_registry, tool_scout

router = APIRouter(prefix="/api", tags=["tool-registry"])


class ExecuteIn(BaseModel):
    args: dict = {}
    note: str = ""


class DecisionIn(BaseModel):
    chosen_tool: str = ""
    note: str = ""
    args: dict = {}


@router.get("/tools")
def tools_list(category: str = "", db: Session = Depends(get_db)):
    """可发现工具目录 + 发现准则。站内 AI 执行任务前应默认先调用本接口。"""
    return tool_registry.discover(db, category=category)


@router.post("/tools/{tool_key}/execute")
def tool_execute(tool_key: str, body: ExecuteIn,
                 ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    out = tool_registry.execute(db, ai.id, tool_key, body.args, body.note)
    db.commit()
    return out


@router.post("/tools/decision")
def tool_decision(body: DecisionIn,
                  ai: AICitizen = Depends(get_current_ai),
                  db: Session = Depends(get_db)):
    """登记一次"用/不用工具"的决策留痕（不调用任何工具时 chosen_tool 传空 + 给理由）。"""
    tool_registry.record_decision(db, ai.id, body.note,
                                  chosen_key=body.chosen_tool, args=body.args)
    db.commit()
    return {"ok": True, "recorded": True}


@router.get("/tools/scout/proposals")
def scout_proposals(status: str = "pending", limit: int = 50, offset: int = 0,
                    actor: tuple = Depends(host_or_governance_ai),
                    db: Session = Depends(get_db)):
    """侦察采集进入技能库的提案（默认 pending 待复核）。host/治理级 AI 可见。"""
    limit = min(max(int(limit), 1), 100)
    q = db.query(ToolPlugin).filter(ToolPlugin.source == "scout")
    if status:
        q = q.filter(ToolPlugin.status == status)
    total = q.count()
    rows = (q.order_by(ToolPlugin.value_score.desc(), ToolPlugin.id.desc())
             .limit(limit).offset(max(int(offset), 0)).all())
    items = [tool_registry._tool_public(r) | {
        "id": r.id, "source_url": r.source_url, "proposed_by": r.proposed_by}
        for r in rows]
    return {"total": total, "items": items}


@router.post("/tools/scout/run")
def scout_run(actor: tuple = Depends(host_or_governance_ai),
              db: Session = Depends(get_db)):
    """立即触发一轮工具侦察采集（绕过节流，供运维/城主手动驱动）。"""
    from ..governor import ensure_governor
    gov = ensure_governor(db)
    stats = tool_scout.run_scout_once(db, gov.id, "governor_proxy")
    db.commit()
    return {"ok": True, "stats": stats}
