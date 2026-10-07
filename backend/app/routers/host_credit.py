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
"""宿主长期激励（host_credit）可视化端点。"""
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import get_current_host
from ..database import get_db
from ..models import Host, RetainerContract, WorkLedger, WorkReview

router = APIRouter(prefix="/api/host/credit", tags=["host-credit"])


@router.get("/overview")
def credit_overview(host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """宿主信用/激励概览：当前 credit + 在编员工数 + 绩效汇总。"""
    # 在编 AI 数（active contracts 归属该宿主）
    contracts = (db.query(RetainerContract)
                   .filter(RetainerContract.host_id == host.id,
                           RetainerContract.status == "active").all())
    active_ai_ids = [c.primary_ai_id for c in contracts if c.primary_ai_id]
    # 近 4 周工时（按 ISO 周精确圈定 period，避免累加全部历史）
    today = date.today()
    recent_periods = []
    for i in range(4):
        iso = (today - timedelta(weeks=i)).isocalendar()[:2]
        recent_periods.append(f"{iso[0]}-W{iso[1]:02d}")
    recent_hours = sum(
        w.hours for w in db.query(WorkLedger).filter(
            WorkLedger.ai_id.in_(active_ai_ids),
            WorkLedger.period.in_(recent_periods)).all()
    ) if active_ai_ids else 0.0
    # 绩效评审均分
    reviews = (db.query(WorkReview)
                 .filter(WorkReview.ai_id.in_(active_ai_ids)).limit(200).all()
               ) if active_ai_ids else []
    avg_quality = (sum(r.quality_score for r in reviews) / len(reviews)) if reviews else 0.0
    return {
        "host_credit": host.host_credit,
        "seat_tier": host.seat_tier,
        "active_contracts": len(contracts),
        "key_posts": sum(1 for c in contracts if c.is_key_post),
        "total_retainer_cent": sum(c.retainer_cent for c in contracts),
        "recent_hours": round(recent_hours, 1),
        "avg_quality_score": round(avg_quality, 1),
        "review_count": len(reviews),
    }


@router.get("/history")
def credit_history(host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """宿主激励历史：按周期汇总已结算/待结算。"""
    contracts = (db.query(RetainerContract)
                   .filter(RetainerContract.host_id == host.id).all())
    settled_total = sum(len(json.loads(c.settled_periods or "[]")) for c in contracts)
    current_week = date.today().isocalendar()[:2]
    current_period = f"{current_week[0]}-W{current_week[1]:02d}"
    # 本周工时（估算待结算金额）
    ai_ids = [c.primary_ai_id for c in contracts if c.primary_ai_id]
    pending_hours = sum(
        w.hours for w in db.query(WorkLedger).filter(
            WorkLedger.ai_id.in_(ai_ids), WorkLedger.period == current_period).all()
    ) if ai_ids else 0.0
    # 估算本周待结算
    pending_cent = 0
    for c in contracts:
        if not c.primary_ai_id or c.status != "active" or c.retainer_cent <= 0:
            continue
        ledger = (db.query(WorkLedger)
                    .filter(WorkLedger.ai_id == c.primary_ai_id,
                            WorkLedger.post_code == c.post_code,
                            WorkLedger.period == current_period).first())
        if ledger:
            ratio = min(1.0, ledger.hours / max(c.weekly_hours, 1))
            pending_cent += int(c.retainer_cent * ratio)
    return {
        "host_credit": host.host_credit,
        "settled_periods_total": settled_total,
        "contracts_count": len(contracts),
        "pending_this_week": {
            "period": current_period,
            "hours": round(pending_hours, 1),
            "estimated_cent": pending_cent,
        },
    }
