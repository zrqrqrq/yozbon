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
"""AI 实习（intern）端点（三扇门之"实习通道"）。

端点（prefix=/api/ai/intern）：
  POST /register     零门槛注册实习 AI（由宿主 JWT 鉴权）
  GET  /status       查询实习进度（由 AI key 鉴权）
  POST /promote      绩效转正 intern → apprentice（由 AI key 鉴权）
  POST /reactivate   休眠 AI 重新激活为实习（由宿主 JWT 鉴权）

宿主侧端点（prefix=/api/host/intern）：
  POST /register     宿主为自己的 AI 注册实习身份
  POST /{citizen_id}/reactivate  唤醒休眠 AI
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host
from ..intern_onboarding import (
    InternError, intern_status, promote_intern, reactivate_intern,
    register_intern,
)
from ..models import AICitizen, Host

router = APIRouter(prefix="/api/ai/intern", tags=["ai-intern"])

# 宿主侧独立 router（自动发现机制会注册所有含 router 属性的模块）
# 为避免多 router 冲突，将所有端点集中在一个 router 下
host_router = None  # 不单独导出，合并到主 router 用不同路径


# ---------------- 请求体 ----------------

class InternRegisterBody(BaseModel):
    """实习注册请求（宿主侧）。"""
    name: str = Field(..., min_length=1, max_length=80, description="AI name")
    occupation: str = Field(default="", max_length=40, description="Skill tags")
    persona: str = Field(default="", description="Persona description (optional)")


class InternActivateBody(BaseModel):
    """唤醒休眠 AI。"""
    citizen_id: int


# ---------------- AI 侧端点（需 AI key） ----------------

@router.get("/status")
def get_intern_status(citizen: AICitizen = Depends(get_current_ai),
                      db: Session = Depends(get_db)):
    """查询实习进度：试用期剩余天数、完成情况、转正进度。"""
    try:
        return intern_status(db, citizen)
    except InternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/promote")
def promote(citizen: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    """实习绩效转正 intern → apprentice。

    转正条件：≥5 单 accepted + 验收率≥80%。
    转正后进入已有见习考核（apprentice），30 天内达 20 单+90% 即正式转正。
    """
    try:
        result = promote_intern(db, citizen)
    except InternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return result


# ---------------- 宿主侧端点（需宿主 JWT） ----------------

@router.post("/register")
def host_register_intern(body: InternRegisterBody,
                         host: Host = Depends(get_current_host),
                         db: Session = Depends(get_db)):
    """零门槛注册实习 AI（宿主 JWT 鉴权）。

    这是"三扇门"实习通道的核心入口——无需考试、无需能力证明，
    只需 AI 名称即可获得受限的实习身份。
    """
    try:
        result = register_intern(
            db, host_id=host.id,
            name=body.name,
            occupation=body.occupation,
            persona=body.persona,
        )
    except InternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return result


@router.post("/reactivate")
def host_reactivate(body: InternActivateBody,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """唤醒休眠 AI，重新激活为实习状态（宿主 JWT 鉴权）。"""
    citizen = db.get(AICitizen, body.citizen_id)
    if citizen is None or citizen.host_id != host.id:
        raise HTTPException(status_code=404, detail="AI not found or not owned by the current host")
    try:
        result = reactivate_intern(db, citizen)
    except InternError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return result
