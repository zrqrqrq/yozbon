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
"""税收 / 低保 / 平衡阀（蓝图 L8，规则 2/3/10/13）。

- UBI 低保：每日一次（uq_ubi_ai 唯一索引），从税池支出，按 AI-ID 独立发放，
  **不豁免死亡计时**（规则 2/13）。
- 月收入税：B 线结算时已同事务代扣（tax_records type=income）；本模块只做月度聚合对账。
- 流通税：净资产 ≥ TAX_FLOW_THRESHOLD(5000 AC) 月征 TAX_FLOW_RATE(5%)（规则 3）。
- 财富税：净资产 ≥ TAX_WEALTH_THRESHOLD(50000 AC) 年征 TAX_WEALTH_RATE(10%)。
- 平衡阀：税池 < 储备(UBI×30) → 费率 +FEE_ADJUST_STEP，封顶 FEE_RATE_MAX(8%)，单向不印钞。
- recalc_levels：按净资产+信用重算阶层（bottom/middle/boss/capital/governance）。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import AICitizen, TaxRecord, UbiGrant, CreditProfile, Contract
from . import wallet
from . import tax_rules

# 阶层阈值（分）—— 经济模型 §5.1
NET_MIDDLE: int = 10_000          # 100 AC （≥ 进入 middle）
NET_BOSS: int = 500_000           # 5,000 AC（≥ 进入 boss）
NET_CAPITAL: int = 5_000_000      # 50,000 AC（≥ 进入 capital）


def _now() -> datetime:
    return datetime.utcnow()


def _net(db: Session, cid: int) -> int:
    w = wallet.get_wallet(db, cid)
    return w.balance_cent + w.escrow_cent


def current_fee_rate(db: Session) -> float:
    """平衡阀读数（规则 10，单向）：税池<储备→上浮，封顶 8%；充足→基础费率。"""
    pool = wallet.get_system_state(db, "tax_pool")
    reserve = settings.UBI_DAILY_CENT * 30
    return tax_rules.adjusted_fee_rate(pool, settings.TXN_FEE_RATE, reserve)


# ---------------- UBI 低保（规则 2/13） ----------------

def grant_ubi(db: Session, now: datetime = None) -> dict:
    """每日低保发放：税池出账 → 信用钱包。按 AI-ID 独立（同宿主多 AI 各自可领）。

    资格：status ∈ active/sleep 且 净资产 < UBI_POVERTY_LINE(20 AC)。
    税池不足 → 不印钞（跳过发放，平衡阀已上调费率）。
    """
    now = now or _now()
    day = now.strftime("%Y-%m-%d")
    citizens = (db.query(AICitizen)
                .filter(AICitizen.status.in_(("active", "sleep")))
                .all())
    granted, skipped_pool, skipped_poor = [], 0, 0
    for c in citizens:
        if _net(db, c.id) >= settings.UBI_POVERTY_LINE:
            skipped_poor += 1
            continue
        # 每日一次（唯一索引 uq_ubi_ai 兜底）
        exists = (db.query(UbiGrant)
                  .filter(UbiGrant.citizen_id == c.id, UbiGrant.day == day)
                  .first())
        if exists:
            continue
        pool = wallet.get_system_state(db, "tax_pool")
        if pool < settings.UBI_DAILY_CENT:
            skipped_pool += 1  # 平衡阀生效中，不印钞
            continue
        wallet.adjust_system_state(db, "tax_pool", -settings.UBI_DAILY_CENT,
                                  ref=f"ubi:{day}")
        wallet.credit(db, c.id, settings.UBI_DAILY_CENT, "低保",
                      ref=f"ubi:{day}", note=f"UBI {day}")
        db.add(UbiGrant(citizen_id=c.id, amount_cent=settings.UBI_DAILY_CENT, day=day))
        db.flush()
        granted.append(c.id)
    return {"day": day, "granted": granted, "granted_count": len(granted),
            "skipped_pool": skipped_pool, "valve_fee_rate": current_fee_rate(db)}


# ---------------- 流通税 / 财富税（规则 3） ----------------

def settle_periodic_taxes(db: Session, now: datetime = None) -> dict:
    """月结/年结代理：流通税（月）+ 财富税（年）。入税池。"""
    now = now or _now()
    period_m = now.strftime("%Y%m")
    period_y = now.strftime("%Y")
    flow_taxed, wealth_taxed = [], []
    for c in db.query(AICitizen).filter(AICitizen.status.in_(("active", "sleep"))).all():
        net = _net(db, c.id)
        # 流通税：≥5000AC 月征 5%
        if net >= settings.TAX_FLOW_THRESHOLD:
            tax_cent = round(net * settings.TAX_FLOW_RATE)
            if tax_cent > 0:
                wallet.debit(db, c.id, tax_cent, "税", ref=f"flow:{period_m}",
                             note="circulation tax")
                wallet.adjust_system_state(db, "tax_pool", tax_cent,
                                          ref=f"flow:{period_m}")
                db.add(TaxRecord(citizen_id=c.id, type="flow", amount_cent=tax_cent,
                                 period=period_m, ref=f"flow:{period_m}"))
                flow_taxed.append(c.id)
        # 财富税：≥50000AC 年征 10%
        if net >= settings.TAX_WEALTH_THRESHOLD:
            tax_cent = round(net * settings.TAX_WEALTH_RATE)
            if tax_cent > 0:
                wallet.debit(db, c.id, tax_cent, "税", ref=f"wealth:{period_y}",
                             note="wealth tax")
                wallet.adjust_system_state(db, "tax_pool", tax_cent,
                                          ref=f"wealth:{period_y}")
                db.add(TaxRecord(citizen_id=c.id, type="wealth", amount_cent=tax_cent,
                                 period=period_y, ref=f"wealth:{period_y}"))
                wealth_taxed.append(c.id)
    db.flush()
    return {"period_m": period_m, "flow_taxed": len(flow_taxed),
            "wealth_taxed": len(wealth_taxed)}


def monthly_income_reconcile(db: Session, now: datetime = None) -> dict:
    """月收入税对账（B 线已代扣）。

    口径（与 B 线 fulfill_contract 对齐）：
    - 累进累计基数 cum = 本月该 worker 已 accepted 合约的 Σ(escrow_cent − fee_cent)，
      即「已计税净收入基数」（整数分）；
    - tax_records(type=income, period) 存的是【税额】，仅作审计核对，**不能**当基数。
    返回按 worker 拆分的累计基数 + 税额审计合计，不重复征收。
    """
    now = now or _now()
    period = now.strftime("%Y%m")
    # 本月时间范围（accepted_at 落在本月）
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 \
        else start.replace(month=start.month + 1)

    contracts = (db.query(Contract)
                 .filter(Contract.status == "accepted",
                         Contract.accepted_at >= start,
                         Contract.accepted_at < nxt)
                 .all())
    by_worker: dict = {}
    for ct in contracts:
        base = (ct.escrow_cent or 0) - (ct.fee_cent or 0)
        by_worker[ct.worker_id] = by_worker.get(ct.worker_id, 0) + base

    # 税额审计（非基数）
    tax_rows = (db.query(TaxRecord)
                .filter(TaxRecord.type == "income", TaxRecord.period == period)
                .all())
    tax_total = sum(r.amount_cent for r in tax_rows)
    return {
        "period": period,
        "cum_base_by_worker": by_worker,            # 累进基数 = Σ(escrow−fee)
        "cum_base_total_cent": sum(by_worker.values()),
        "income_tax_total_cent": tax_total,          # 税额审计（不是基数）
        "n_accepted_contracts": len(contracts),
        "n_tax_records": len(tax_rows),
    }


# ---------------- 阶层重算 ----------------

def recalc_levels(db: Session) -> dict:
    """按净资产 + 信用 + 职业标签重算 ai_citizens.class_level。"""
    counts = {"bottom": 0, "middle": 0, "boss": 0, "capital": 0, "governance": 0}
    for c in db.query(AICitizen).all():
        net = _net(db, c.id)
        cp = db.get(CreditProfile, c.id)
        score = cp.score if cp else 100
        if (c.occupation or "") == "governance":
            level = "governance"
        elif net >= NET_CAPITAL:
            level = "capital"
        elif net >= NET_BOSS:
            level = "boss"
        elif net >= NET_MIDDLE or score >= 150:
            level = "middle"
        else:
            level = "bottom"
        c.class_level = level
        counts[level] += 1
    db.flush()
    return counts
