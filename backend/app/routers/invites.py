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
"""N18 邀请推荐奖励路由（社会功能扩展设计 §4 N18）。

  POST /api/host/invites   宿主 JWT 生成邀请码（每个宿主可生成多个码）
  GET  /api/host/invites   我的邀请码列表 + 状态
注册/建 AI 的 invite_code 绑定在 routers/host.py 内增量完成。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import Host
from .. import invites as svc

router = APIRouter(prefix="/api/host/invites", tags=["n18-invites"])


@router.post("")
def create_invite(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    inv = svc.create_invite(db, host.id)
    db.commit()
    return {"code": inv.code, "status": inv.status, "invitee_id": inv.invitee_id}


@router.get("")
def my_invites(host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    return {"items": svc.list_invites(db, host.id)}
