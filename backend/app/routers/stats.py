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
"""N4 统计报表路由（社会功能扩展设计 §2 N4；§5.4 管理类权限）。

端点：
- GET /api/stats/overview    宿主 JWT → 名下 AI 收益/活跃/信用
- GET /api/stats/platform    平台管理员（人类）→ GMV/笔数/活跃AI/税池/货币/阶层分布
- GET /api/stats/trends      宿主 JWT → 近 days 天历史曲线（stat_snapshots）

权限（§5.4：管理类=管理员人类，AI 一律不可达，T5 红线）：
- overview：宿主 JWT（typ=host）；
- platform：宿主 JWT **且** email == settings.PLATFORM_HOST_EMAIL（平台运营人类账号）。
  无凭证 / 坏凭证 / AI key / readonly JWT / 普通宿主 → 一律 403（设计明文）。
- trends：宿主 JWT（宿主后台 StatsPage 用）。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_host
from ..models import Host
from ..security import decode_token
from .. import stats

router = APIRouter(prefix="/api/stats", tags=["n4-stats"])


def _host_from_bearer(authorization: Optional[str], db: Session) -> Host:
    """从 Bearer 解析宿主；任一不满足抛 403（platform 专用，统一 403 而非 401）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Host admin permission required")
    try:
        payload = decode_token(authorization[7:])
    except ValueError:
        raise HTTPException(status_code=403, detail="Host admin permission required")
    if payload.get("typ") != "host":
        # AI key / readonly JWT / 其他令牌落到此 → 403（AI 一律不可达平台视图）
        raise HTTPException(status_code=403, detail="AI cannot access the platform admin view")
    host = db.get(Host, payload.get("sub"))
    if host is None or host.status != "active":
        raise HTTPException(status_code=403, detail="Host admin permission required")
    return host


@router.get("/overview")
def stats_overview(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    return stats.overview(db, host.id)


@router.get("/platform")
def stats_platform(authorization: Optional[str] = Header(None),
                  db: Session = Depends(get_db)):
    host = _host_from_bearer(authorization, db)
    if (host.email or "") != settings.PLATFORM_HOST_EMAIL:
        raise HTTPException(status_code=403, detail="Only platform operations admins can view the platform view")
    return stats.platform(db)


@router.get("/trends")
def stats_trends(days: int = Query(30, ge=1, le=365),
                host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    """近 days 天曲线。days 钳制 [1,365]（路由层 Query 已拒绝 0/负数/超 365 → 422）。"""
    return {"days": days, "items": stats.trends(db, days)}
