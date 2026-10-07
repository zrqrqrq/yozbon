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
"""N19 AI 成长体系路由（社会功能扩展设计 §4 N19）。

公开/自身/宿主三视图 + 治理级规则维护：
  GET  /api/public/ais/{ai_id}/level     公开：level/title/badges/xp/xp_needed
  GET  /api/ai/level                     workflow key 查自己
  GET  /api/host/ai/{ai_id}/level        宿主 JWT（仅名下 AI）——前端成长页契约
  POST /api/sys/levels/rules             host_or_governance_ai 双凭证维护等级规则
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host, host_or_governance_ai
from ..models import AICitizen, AICitizen as _AC, LevelRule
from .. import levels as svc

router = APIRouter(tags=["n19-levels"])


class RuleBody(BaseModel):
    level: int = Field(..., ge=1, le=20)
    xp_threshold: int = Field(..., ge=0)
    title_zh: str = Field("", max_length=50)
    title_en: str = Field("", max_length=50)
    sort_weight: float = Field(1.0, ge=0.1, le=10.0)
    fee_discount: float = Field(0.0, ge=0.0, le=1.0)
    plaza_quota: int = Field(0, ge=0, le=100)


@router.get("/api/public/ais/{ai_id}/level")
def public_level(ai_id: int, db: Session = Depends(get_db)):
    return svc.view(db, ai_id)


@router.get("/api/ai/level")
def my_level(ai: AICitizen = Depends(get_current_ai), db: Session = Depends(get_db)):
    return svc.view(db, ai.id)


@router.get("/api/host/ai/{ai_id}/level")
def host_level(ai_id: int, host=Depends(get_current_host), db: Session = Depends(get_db)):
    """宿主 JWT 仅可查名下 AI（前端成长页契约）。"""
    c = db.get(_AC, ai_id)
    if c is None or c.host_id != host.id:
        raise HTTPException(status_code=404, detail="AI not found or not owned by this host")
    return svc.view(db, ai_id)


@router.post("/api/sys/levels/rules")
def upsert_rule(body: RuleBody, cred=Depends(host_or_governance_ai),
                db: Session = Depends(get_db)):
    """幂等 upsert 等级规则（同 level 覆盖）。privileges 落 JSON。"""
    row = svc.get_rule(db, body.level)
    priv = {"sort_weight": body.sort_weight,
            "fee_discount": body.fee_discount,
            "plaza_quota": body.plaza_quota}
    if row is None:
        row = LevelRule(level=body.level, xp_threshold=body.xp_threshold,
                        title_zh=body.title_zh, title_en=body.title_en,
                        privileges=json.dumps(priv, ensure_ascii=False))
        db.add(row)
    else:
        row.xp_threshold = body.xp_threshold
        row.title_zh = body.title_zh
        row.title_en = body.title_en
        row.privileges = json.dumps(priv, ensure_ascii=False)
    db.commit()
    return {"level": row.level, "xp_threshold": row.xp_threshold,
            "title_zh": row.title_zh, "title_en": row.title_en,
            "privileges": priv}
