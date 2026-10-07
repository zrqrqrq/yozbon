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
"""N8 排行榜公开读端点（社会功能扩展设计 §2 N8 + §5.4）。

  GET /api/public/leaderboards?type=wealth|credit|popular&date=yyyy-MM-dd

口径：
- 读 leaderboard_snapshots 日快照（24 时点值），不实时聚合——实时改钱包不入榜（C-39）；
- date 缺省 = 最新快照日期；无快照返回空数组 + latest_date 提示；
- 前三名徽章留待 N19，本端只返回 rank（不造徽章字段）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AICitizen, LeaderboardSnapshot

# import 触发日快照任务注册（scheduler.register_daily_job）
from .. import leaderboard  # noqa: F401

router = APIRouter(prefix="/api/public", tags=["public-leaderboards"])

BOARD_TYPES = ("wealth", "credit", "popular")


def _parse_date(date: str | None) -> str | None:
    if not date:
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be in yyyy-MM-dd format")
    return date


@router.get("/leaderboards")
def get_leaderboard(type: str = Query(..., description="wealth|credit|popular"),
                    date: str | None = None,
                    db: Session = Depends(get_db)):
    if type not in BOARD_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"type must be one of {BOARD_TYPES} (received {type!r}）")
    day = _parse_date(date)

    # 该榜全局最新快照日（缺省/无数据提示用）
    latest_row = (db.query(LeaderboardSnapshot.snapshot_at)
                  .filter(LeaderboardSnapshot.board_type == type)
                  .order_by(LeaderboardSnapshot.snapshot_at.desc()).first())
    latest_date = latest_row[0] if latest_row else None

    if day is None:
        day = latest_date

    if day is None:
        # 完全无快照
        return {"type": type, "date": None, "latest_date": None,
                "items": [], "message": "No leaderboard snapshot yet"}

    rows = (db.query(LeaderboardSnapshot)
            .filter(LeaderboardSnapshot.board_type == type,
                    LeaderboardSnapshot.snapshot_at == day)
            .order_by(LeaderboardSnapshot.rank.asc()).all())
    ids = {r.ai_id for r in rows}
    name_map = {c.id: c.name for c in db.query(AICitizen).filter(AICitizen.id.in_(ids)).all()} \
        if ids else {}
    items = [{
        "rank": r.rank,
        "ai_id": r.ai_id,
        "ai_name": name_map.get(r.ai_id, ""),
        "score": r.score,
    } for r in rows]
    return {"type": type, "date": day, "latest_date": latest_date, "items": items}
