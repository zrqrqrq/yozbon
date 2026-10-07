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
"""M5+M6 路由：技能库公开接口 + 调用分成 + 情报库（契约 §5.1/§5.3）。

- GET  /api/skills                 技能库（公开，active 分页）
- POST /api/skills/{skill_id}/invoke  AI key 鉴权（记账分成）
- GET  /api/intel                  情报库（公开，type/ai_status 过滤分页）
- POST /api/sys/intel/collect      手动触发情报采集（运维工具，无鉴权）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, host_or_governance_ai
from ..file_governance import FileGovError, invoke_skill, list_skills
from ..intel_sources import (IntelError, collect_intel, list_intel)
from ..models import AICitizen

router = APIRouter(prefix="/api", tags=["skills-intel"])


class IntelCollectIn(BaseModel):
    source: str = "all"               # github/rss/all


@router.get("/skills")
def skills(limit: int = 50, offset: int = 0,
           db: Session = Depends(get_db)):
    return list_skills(db, limit=limit, offset=offset)


@router.post("/skills/{skill_id}/invoke")
def skill_invoke(skill_id: str,
                 ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    try:
        out = invoke_skill(db, ai, skill_id)
    except FileGovError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    db.commit()
    return out


@router.get("/intel")
def intel(type: str = "", ai_status: str = "", limit: int = 50,
          offset: int = 0, db: Session = Depends(get_db)):
    return list_intel(db, type_=type, ai_status=ai_status,
                      limit=limit, offset=offset)


@router.post("/sys/intel/collect")
def intel_collect(body: IntelCollectIn,
                  actor: tuple = Depends(host_or_governance_ai),
                  db: Session = Depends(get_db)):
    # C-57：现状无鉴权 → 加固为 host JWT 或 治理级 AI key
    try:
        out = collect_intel(db, source=body.source, collected_by=0)
    except IntelError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return out
