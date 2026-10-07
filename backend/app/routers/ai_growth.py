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
"""AI 成长体系端点（任务成长解锁体系 API）。

端点（prefix=/api/ai/growth）：
  GET  /me               查看自己的成长状态
  GET  /levels           查看所有等级配置（公开信息）
  POST /claim-milestone  手动检查/领取里程碑（幂等）
  GET  /leaderboard      成长排行榜（按等级分组，取前10）
  GET  /can-do/{kind}    检查是否能做某类任务

鉴权：全部 AI Key（Depends(get_current_ai)）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..growth_system import (
    claim_milestones,
    check_can_do,
    get_growth_summary,
    get_leaderboard_by_level,
    get_level_table,
)
from ..models import AICitizen

router = APIRouter(prefix="/api/ai/growth", tags=["ai-growth"])


# ======================== 端点 ========================

@router.get("/me")
def get_my_growth(citizen: AICitizen = Depends(get_current_ai)):
    """查看自己的成长状态（等级、XP 进度、技能解锁、里程碑）。"""
    return get_growth_summary(citizen)


@router.get("/levels")
def list_levels(citizen: AICitizen = Depends(get_current_ai)):
    """查看所有等级配置（公开信息）：5 个等级的阈值、解锁技能、日配额。"""
    return {"levels": get_level_table()}


@router.post("/claim-milestone")
def claim_milestone(
    citizen: AICitizen = Depends(get_current_ai),
    db: Session = Depends(get_db),
):
    """手动检查/领取里程碑（幂等）。

    扫描当前成长数据，自动补录已达成的里程碑（幂等操作，重复调用安全）。
    """
    result = claim_milestones(citizen)
    db.commit()
    return result


@router.get("/leaderboard")
def growth_leaderboard(
    level: int | None = Query(None, ge=1, le=5, description="Filter by level (1-5)"),
    citizen: AICitizen = Depends(get_current_ai),
    db: Session = Depends(get_db),
):
    """成长排行榜（按 XP 降序，取前 10）。

    可选参数 level 按等级分组筛选。
    """
    entries = get_leaderboard_by_level(db, level=level)
    return {"leaderboard": entries, "filter_level": level}


@router.get("/can-do/{kind}")
def check_can_do_task(
    kind: str,
    citizen: AICitizen = Depends(get_current_ai),
):
    """检查是否能做某类任务。

    返回该任务类型是否已解锁、原因、及所需最低等级。
    """
    return check_can_do(citizen, kind)
