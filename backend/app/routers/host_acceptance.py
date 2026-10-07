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
"""宿主侧项目级验收路由（蓝图 §三 POST /api/host/acceptance/{contract_id}）。

- JWT 鉴权（get_current_host）；
- 归属校验：合约所属 project.host_id 必须等于当前宿主，否则 403/404；
- accept → fulfill_contract（§2 结算释放）；reject → 返工（托管锁定，规则14）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import escrow, wallet
from ..database import get_db
from ..deps import get_current_host
from ..models import Contract, Host, Project

router = APIRouter(prefix="/api/host", tags=["host-acceptance"])


class HostAcceptanceBody(BaseModel):
    result: str                     # accept / reject
    reason_json: str = Field(default="[]")


@router.post("/acceptance/{contract_id}")
def host_acceptance(contract_id: int, body: HostAcceptanceBody,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    proj = db.get(Project, c.project_id)
    if proj is None or proj.host_id != host.id:
        raise HTTPException(status_code=403, detail="This contract does not belong to the current host project")
    try:
        c = escrow.host_acceptance(db, contract_id, body.result, body.reason_json)
    except (escrow.EscrowError, wallet.WalletError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"ok": True, "contract_id": contract_id, "status": c.status}
