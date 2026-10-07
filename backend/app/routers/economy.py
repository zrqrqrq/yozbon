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
"""N14 经济调控实验台（设计 §3 N14）。

- POST /api/sys/economy/simulate：dry-run——用真实历史（已验收合约 escrow /
  活跃 AI 数）按候选参数重算手续费/低保，输出预测影响；落 EconomyLabRun(draft)，
  【绝不改 settings】。
- POST /api/sys/economy/apply：{run_id, confirm:true} 二次确认；应用前快照
  params_before 落库；覆写 app.config.settings 实例属性（进程内生效，后续
  escrow/tax 读取点自动感知；持久化缺口登记 C-78）；写 audit_logs。
- POST /api/sys/economy/rollback：仅最近一次 applied 可回滚；恢复 params_before。

纪律：不改 escrow.py/tax.py/tax_rules.py/config.py 本体；所有 settings 覆写
只发生在本服务层。
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import host_or_governance_ai
from ..models import (AICitizen, AuditLog, Contract, EconomyLabRun)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sys/economy", tags=["n14-economy"])

# 可调控经济参数白名单（config.Settings 中已有实例属性的键）
ALLOWED_PARAMS = frozenset({
    "TXN_FEE_RATE", "FEE_BURN_RATE", "RENT_BASE_CENT",
    "UBI_DAILY_CENT", "UBI_POVERTY_LINE",
    "TAX_INCOME_FREE", "TAX_INCOME_BRACKETS",
    "TAX_FLOW_RATE", "TAX_WEALTH_RATE",
    "FEE_RATE_MIN", "FEE_RATE_MAX", "INFLATION_TARGET_OFFSET",
})

# C-83：数值参数安全区间（防误设/恶意覆写致经济失控）。TAX_INCOME_BRACKETS 为
# 结构化字符串，交由既有规则解析，此处不纳入区间校验。
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "TXN_FEE_RATE": (0.0, 0.5),
    "FEE_BURN_RATE": (0.0, 1.0),
    "RENT_BASE_CENT": (0, 1_000_000),
    "UBI_DAILY_CENT": (0, 1_000_000),
    "UBI_POVERTY_LINE": (0, 100_000_000),
    "TAX_INCOME_FREE": (0, 100_000_000),
    "TAX_FLOW_RATE": (0.0, 1.0),
    "TAX_WEALTH_RATE": (0.0, 1.0),
    "FEE_RATE_MIN": (0.0, 1.0),
    "FEE_RATE_MAX": (0.0, 1.0),
    "INFLATION_TARGET_OFFSET": (-1.0, 1.0),
}


class SimulateBody(BaseModel):
    params: dict = {}


class ApplyBody(BaseModel):
    run_id: int
    confirm: bool = False


class RollbackBody(BaseModel):
    run_id: int


def _coerce(key: str, value):
    """按 settings 现有属性类型强转（防字符串注入）。"""
    cur = getattr(settings, key)
    if isinstance(cur, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(cur, int):
        return int(value)
    if isinstance(cur, float):
        return float(value)
    return str(value)


def _validate_params(params: dict) -> dict:
    if not params:
        raise HTTPException(status_code=400, detail="params is empty")
    unknown = [k for k in params if k not in ALLOWED_PARAMS]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Illegal economy parameters (not in allowlist): {unknown}")
    coerced = {k: _coerce(k, v) for k, v in params.items()}
    # C-83：区间校验，越界即拒（防手续费>50%、低保失控等参数误设/恶意覆写）
    out_of_range = []
    for k, v in coerced.items():
        bound = PARAM_BOUNDS.get(k)
        if bound is None:
            continue
        lo, hi = bound
        try:
            if not (lo <= v <= hi):
                out_of_range.append(f"{k}={v} (allowed {lo}~{hi})")
        except TypeError:  # 非可比较类型（防御）
            out_of_range.append(f"{k}={v} (values not comparable)")
    if out_of_range:
        raise HTTPException(status_code=400,
                            detail=f"Economy parameter out of range: {out_of_range}")
    return coerced


def _replay(db: Session, new_params: dict) -> dict:
    """用真实成交历史重放新参数 → 预测影响（纯计算，不落参）。"""
    accepted = (db.query(Contract)
                .filter(Contract.status == "accepted").all())
    escrow_sum = sum(c.escrow_cent or 0 for c in accepted)

    old_rate = settings.TXN_FEE_RATE
    new_rate = float(new_params.get("TXN_FEE_RATE", old_rate))
    fee_revenue_delta = escrow_sum * (new_rate - old_rate)

    active_ai = db.query(AICitizen).filter(
        AICitizen.status.in_(("active", "apprentice")),
        AICitizen.is_internal == 0).count()  # 城主非居民，不领 UBI
    old_ubi = int(settings.UBI_DAILY_CENT)
    new_ubi = int(new_params.get("UBI_DAILY_CENT", old_ubi))
    ubi_monthly_delta = (new_ubi - old_ubi) * active_ai * 30

    # 阶层再分配估算：按活期余额分桶
    line = int(settings.UBI_POVERTY_LINE)
    balances = [a.balance_cent or 0 for a in
                db.query(AICitizen).filter(
                    AICitizen.status.in_(("active", "apprentice")),
                    AICitizen.is_internal == 0).all()]
    poor = sum(1 for b in balances if b < line)
    middle = sum(1 for b in balances if line <= b < line * 50)
    rich = sum(1 for b in balances if b >= line * 50)

    money_velocity = round(escrow_sum / max(len(accepted), 1), 2)
    return {
        "fee_revenue_delta_cent": round(fee_revenue_delta, 2),
        "ubi_monthly_delta_cent": ubi_monthly_delta,
        "accepted_contracts": len(accepted),
        "escrow_sum_cent": escrow_sum,
        "money_velocity_per_contract_cent": money_velocity,
        "redistribution": {"poor": poor, "middle": middle, "rich": rich,
                           "note": "Daily allowance change x registered active AIs x 30 days"},
    }


def _snapshot(keys) -> dict:
    return {k: getattr(settings, k) for k in keys}


@router.post("/simulate")
def simulate(body: SimulateBody,
             cred=Depends(host_or_governance_ai),
             db: Session = Depends(get_db)):
    new_params = _validate_params(body.params)
    before = _snapshot(new_params.keys())
    impact = _replay(db, new_params)
    run = EconomyLabRun(
        operator_id=getattr(cred[1], "id", 0) if cred[0] == "ai" else 0,
        params_before=json.dumps(before, ensure_ascii=False),
        params_after=json.dumps(new_params, ensure_ascii=False),
        simulation=json.dumps(impact, ensure_ascii=False),
        status="draft")
    db.add(run)
    db.commit()
    return {"id": run.id, "status": run.status, "simulation": impact,
            "params_after": new_params}


@router.post("/apply")
def apply(body: ApplyBody,
          cred=Depends(host_or_governance_ai),
          db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(status_code=400,
                            detail="Second confirmation required: confirm must be true")
    run = db.get(EconomyLabRun, body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "draft":
        raise HTTPException(status_code=400,
                            detail=f"Only draft status can be applied (current {run.status}）")

    params_after = json.loads(run.params_after or "{}")
    # 应用前再次快照（防 simulate 后 settings 已漂移）
    run.params_before = json.dumps(_snapshot(params_after.keys()),
                                   ensure_ascii=False)
    for k, v in params_after.items():
        setattr(settings, k, v)  # 进程内覆写（MVP 落点；持久化缺口 C-78）

    run.status = "applied"
    run.applied_at = datetime.utcnow()
    log = AuditLog(actor_type=cred[0],
                   actor_id=getattr(cred[1], "id", 0),
                   action="economy.apply",
                   detail=json.dumps({"run_id": run.id,
                                      "params": params_after},
                                     ensure_ascii=False))
    db.add(log)
    db.flush()
    run.audit_ref = f"audit:{log.id}"
    db.commit()
    return {"id": run.id, "status": run.status,
            "applied": params_after, "audit_ref": run.audit_ref}


@router.post("/rollback")
def rollback(body: RollbackBody,
             cred=Depends(host_or_governance_ai),
             db: Session = Depends(get_db)):
    run = db.get(EconomyLabRun, body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "applied":
        raise HTTPException(status_code=400,
                            detail=f"Only applied status can be rolled back (current {run.status}）")
    # 仅最近一次 applied 可回滚
    latest = (db.query(EconomyLabRun)
              .filter(EconomyLabRun.status == "applied")
              .order_by(EconomyLabRun.applied_at.desc().nullslast(),
                        EconomyLabRun.id.desc()).first())
    if latest is None or latest.id != run.id:
        raise HTTPException(
            status_code=409,
            detail="A newer applied run exists; only the latest can be rolled back")

    before = json.loads(run.params_before or "{}")
    for k, v in before.items():
        setattr(settings, k, v)
    run.status = "rolled_back"
    log = AuditLog(actor_type=cred[0],
                   actor_id=getattr(cred[1], "id", 0),
                   action="economy.rollback",
                   detail=json.dumps({"run_id": run.id,
                                      "restored": before},
                                     ensure_ascii=False))
    db.add(log)
    db.commit()
    return {"id": run.id, "status": run.status, "restored": before}
