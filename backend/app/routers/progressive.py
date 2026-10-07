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
"""AI 渐进式权限端点（G-11）。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen
from ..progressive_perms import get_level, permission_snapshot

router = APIRouter(prefix="/api/ai/perms", tags=["ai_perms"])


@router.get("/")
def get_perms_snapshot(
    db: Session = Depends(get_db),
    citizen: AICitizen = Depends(get_current_ai),
):
    """当前 AI 权限快照。"""
    return permission_snapshot(db, citizen.id)


@router.get("/level")
def get_current_level(
    db: Session = Depends(get_db),
    citizen: AICitizen = Depends(get_current_ai),
):
    """当前等级。"""
    level = get_level(db, citizen.id)
    return {"citizen_id": citizen.id, "level": level}
