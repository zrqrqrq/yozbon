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
"""N3 任务模板库路由（社会功能扩展设计 §2 N3；§5.2/§5.4）。

定位：降低发布门槛——新宿主选模板即预填 T1 需求七要素，缺项仍由三道闸补全。

端点：
- GET  /api/templates          列表（默认只看 active=1；支持 category 筛选 + 分页）
- GET  /api/templates/{id}     详情
- POST /api/templates         治理岗维护（host JWT 任意宿主 OR class_level=governance 的 AI key）

写权限口径（设计明文 §2 N3「治理岗维护」+ 任务书：host JWT 或治理 AI key）：
- host JWT（typ=host）→ 放行；
- AI key（aik_*）且 class_level=governance → 放行；
- 其余（无凭证 / readonly JWT / 普通 AI）→ 403。

版本化口径（任务书授权自定并写明）：按 (category, name_zh) 幂等 upsert——
重复 name_zh 同 category → 命中既有行，version+=1 并整体更新字段（同一 id），
不新建行、不返回 409。理由：治理侧「沉淀成功案例为新模板」= 同一模板的迭代，
版本号留痕，列表不被重复名刷屏。

防注入（登记册 §一 #1/#5）：category 封闭枚举白名单；required_fields 只允许
七要素键名白名单（goal/scope/deliverable_std/acceptance_criteria/deadline/budget/limits），
未知键直接 400。
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import AICitizen, Host, TaskTemplate
from ..security import decode_token

router = APIRouter(prefix="/api/templates", tags=["n3-templates"])

# 封闭枚举（设计 §2 N3：宣传 promo / code / design / edit / analysis）
CATEGORIES = frozenset({"promo", "code", "design", "edit", "analysis"})

# T1 需求七要素键名白名单（与 requirements_gov.CORE_FIELDS / 七要素同源）
REQUIRED_FIELDS_KEYS = frozenset(
    {"goal", "scope", "deliverable_std", "acceptance_criteria",
     "deadline", "budget", "limits"})

_PAGE_MAX = 50


def _row_to_dict(t: TaskTemplate) -> dict:
    try:
        rf = json.loads(t.required_fields or "[]")
    except (ValueError, TypeError):
        rf = []
    return {
        "id": t.id,
        "category": t.category,
        "name_zh": t.name_zh,
        "name_en": t.name_en,
        "prompt_template": t.prompt_template,
        "default_budget_min": t.default_budget_min,
        "default_budget_max": t.default_budget_max,
        "default_duration_days": t.default_duration_days,
        "required_fields": rf,
        "sample_output": t.sample_output,
        "active": t.active,
        "version": t.version,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@router.get("")
def list_templates(category: str = "", active: int = 1,
                  page: int = 1, page_size: int = 20,
                  db: Session = Depends(get_db)):
    """模板列表。active 默认 1（发布页只看可用模板）；active=0 查全部（含下线）。"""
    q = db.query(TaskTemplate)
    if category:
        if category not in CATEGORIES:
            raise HTTPException(status_code=400,
                                detail=f"category must be one of {sorted(CATEGORIES)}")
        q = q.filter(TaskTemplate.category == category)
    if active in (0, 1):
        q = q.filter(TaskTemplate.active == active)
    total = q.count()
    page = max(1, int(page))
    page_size = min(max(int(page_size), 1), _PAGE_MAX)
    rows = (q.order_by(TaskTemplate.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"items": [_row_to_dict(t) for t in rows], "total": total,
            "page": page, "page_size": page_size}


@router.get("/{template_id}")
def get_template(template_id: int, db: Session = Depends(get_db)):
    t = db.get(TaskTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return _row_to_dict(t)


# ---------------- POST 写权限：host JWT 或 governance AI key ----------------

def _resolve_template_actor(authorization: Optional[str],
                            x_ai_key: Optional[str],
                            db: Session):
    """返回 (actor_type, actor_id) ∈ {('host', host_id), ('ai_governance', ai_id)}。

    任一不满足即 403（含无凭证 / 坏凭证 / 普通 AI / readonly AI）。
    """
    # 1) host JWT
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization[7:])
        except ValueError:
            payload = None
        if payload and payload.get("typ") == "host":
            host = db.get(Host, payload.get("sub"))
            if host is not None and host.status == "active":
                return "host", host.id
    # 2) governance AI key（aik_<id>_<hex>）
    key = x_ai_key
    if not key and authorization and authorization.startswith("Bearer "):
        key = authorization[7:]
    if key and key.startswith("aik_"):
        parts = key.split("_")
        if len(parts) == 3:
            try:
                cid = int(parts[1])
            except ValueError:
                cid = 0
            ci = db.get(AICitizen, cid) if cid else None
            if ci is not None and (ci.class_level or "") == "governance":
                return "ai_governance", ci.id
    raise HTTPException(status_code=403,
                        detail="Only host (host JWT) or governance-class AI can maintain templates")


class TemplateIn(BaseModel):
    category: str
    name_zh: str = Field(..., min_length=1, max_length=120)
    name_en: str = ""
    prompt_template: str = ""
    default_budget_min: int = Field(0, ge=0)
    default_budget_max: int = Field(0, ge=0)
    default_duration_days: int = Field(3, ge=0, le=365)
    required_fields: list[str] = []
    sample_output: str = ""
    active: int = 1


@router.post("")
def upsert_template(body: TemplateIn,
                    authorization: Optional[str] = Header(None),
                    x_ai_key: Optional[str] = Header(None),
                    db: Session = Depends(get_db)):
    """新建/迭代模板（按 category+name_zh 幂等 upsert，版本 version+1）。"""
    actor_type, actor_id = _resolve_template_actor(authorization, x_ai_key, db)
    if body.category not in CATEGORIES:
        raise HTTPException(status_code=400,
                            detail=f"category must be one of {sorted(CATEGORIES)}")
    bad = [k for k in body.required_fields if k not in REQUIRED_FIELDS_KEYS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"required_fields contains illegal key {bad}; only {sorted(REQUIRED_FIELDS_KEYS)}")
    if body.default_budget_min and body.default_budget_max:
        if body.default_budget_min > body.default_budget_max:
            raise HTTPException(status_code=400,
                                detail="default_budget_min must not exceed max")

    existing = (db.query(TaskTemplate)
                .filter(TaskTemplate.category == body.category,
                        TaskTemplate.name_zh == body.name_zh).first())
    if existing is None:
        t = TaskTemplate(
            category=body.category, name_zh=body.name_zh, name_en=body.name_en,
            prompt_template=body.prompt_template,
            default_budget_min=body.default_budget_min,
            default_budget_max=body.default_budget_max,
            default_duration_days=body.default_duration_days,
            required_fields=json.dumps(body.required_fields, ensure_ascii=False),
            sample_output=body.sample_output, active=body.active,
            version=1, created_by=actor_id)
        db.add(t)
        db.flush()
        created = True
    else:
        existing.name_en = body.name_en
        existing.prompt_template = body.prompt_template
        existing.default_budget_min = body.default_budget_min
        existing.default_budget_max = body.default_budget_max
        existing.default_duration_days = body.default_duration_days
        existing.required_fields = json.dumps(body.required_fields, ensure_ascii=False)
        existing.sample_output = body.sample_output
        existing.active = body.active
        existing.version = (existing.version or 1) + 1   # 版本化
        created = False
        t = existing
    db.commit()
    out = _row_to_dict(t)
    out["created"] = created
    out["actor"] = actor_type
    return out
