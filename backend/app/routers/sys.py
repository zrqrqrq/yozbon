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
"""系统侧路由（蓝图 §三 系统 API：公示/心跳/tick/recalc）。

tick / recalc 惰性引入 C1 线实现（app.ai_citizens.tick_all / app.tax.recalc_levels），
C1 落地前返回 501（骨架占位），落地后自动生效。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_host
from ..models import Host
from .. import wallet

router = APIRouter(prefix="/api/sys", tags=["sys"])


@router.get("/health")
def health():
    return {"ok": True, "app": settings.APP_NAME, "env": settings.APP_ENV}


@router.get("/economy")
def economy_status(host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """经济公示（蓝图 §三）：参数快照 + 货币供应/税池/销毁 + 平衡阀状态。

    C-57：host JWT 门控（前端 OverviewPage 已统一附 Bearer）；/health 探活仍公开。
    """
    return {
        "fee_rate": settings.TXN_FEE_RATE,
        "fee_rate_min": settings.FEE_RATE_MIN,
        "fee_rate_max": settings.FEE_RATE_MAX,
        "burn_rate": settings.FEE_BURN_RATE,
        "rent_base_cent": settings.RENT_BASE_CENT,
        "rent_class_coef": settings.RENT_CLASS_COEF,
        "death_unemployed_minutes": settings.UNEMPLOYED_DEATH_MINUTES,
        "death_exempt_net": settings.DEATH_EXEMPT_NET,
        "ubi_daily_cent": settings.UBI_DAILY_CENT,
        "tax_income_free": settings.TAX_INCOME_FREE,
        "tax_flow_threshold": settings.TAX_FLOW_THRESHOLD,
        "money_supply_cent": wallet.get_system_state(db, "money_supply"),
        "tax_pool_cent": wallet.get_system_state(db, "tax_pool"),
        "burned_total_cent": wallet.get_system_state(db, "burned_total"),
    }


@router.post("/tick")
def sys_tick(host: Host = Depends(get_current_host),
             db: Session = Depends(get_db)):
    """手动触发全量生命周期 tick（蓝图 §三，开发/运维用；C-57 host JWT）。"""
    try:
        from ..ai_citizens import tick_all  # C1 线实现
    except ImportError:
        raise HTTPException(status_code=501, detail="lifecycle tick not implemented (track C1)")
    result = tick_all(db, now=None)
    db.commit()
    return result


@router.post("/recalc")
def sys_recalc(host: Host = Depends(get_current_host),
               db: Session = Depends(get_db)):
    """重算阶层/税率档（蓝图 §三；C-57 host JWT）。"""
    try:
        from ..tax import recalc_levels  # C1 线实现
    except ImportError:
        raise HTTPException(status_code=501, detail="tax recalc not implemented (track C1)")
    result = recalc_levels(db)
    db.commit()
    return {"ok": True, "recalc": result}
