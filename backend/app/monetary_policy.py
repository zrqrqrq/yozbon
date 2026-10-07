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
"""经济调控工具（利率调整 / 公开市场操作 / 量化宽松）。

SystemState 为 key-value 单行聚合账户（key=String PK, value_cent=Integer），
本模块使用以下 key：
  - "interest_rate_bps" : 基准利率（基点）
  - "money_supply"      : 流通中货币总量（分）
  - "reserve_ratio_bps" : 准备金率（基点）
  - "treasury_pool"     : 国库账户（QE/OM 注入落地账户）

B-M2：apply_policy 使用条件 UPDATE 抢权防双执行；
QE/OM buy 注入资金必须落地到 treasury_pool（国库钱包），确保资金有对手方、
不悬空印钞——money_supply += amount 且 treasury_pool += amount。

所有金额/利率均以整数存储，金额单位为分，利率/准备金率单位为基点(bps)。
服务层只 flush，commit 由路由/调用方负责。
"""
import json
import logging
from datetime import datetime

from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from .models import MonetaryPolicyAction, SystemState
from . import wallet as wallet_mod

logger = logging.getLogger(__name__)


class PolicyError(Exception):
    """经济调控业务异常（路由层映射为 HTTP 400）。"""


def _now() -> datetime:
    return datetime.utcnow()


# ======================== 内部工具 ========================

def _get_state_value(db: Session, key: str) -> int:
    """读取 SystemState 中指定 key 的 value_cent（不存在返回 0）。"""
    row = db.get(SystemState, key)
    return row.value_cent if row else 0


def _set_state_value(db: Session, key: str, new_value: int, ref: str = "") -> SystemState:
    """将 SystemState[key] 设为绝对值（通过 delta 实现）。"""
    current = _get_state_value(db, key)
    delta = new_value - current
    return wallet_mod.adjust_system_state(db, key, delta, ref=ref)


# ======================== 提议操作 ========================

def propose_rate_change(db: Session, initiated_by: int, new_rate_bps: int) -> MonetaryPolicyAction:
    """提议调整基准利率（基点）。

    param_before: {"rate_bps": <当前值>}
    param_after:  {"rate_bps": <新值>}
    """
    if new_rate_bps < 0:
        raise PolicyError("Interest rate cannot be negative")
    current = _get_state_value(db, "interest_rate_bps")
    action = MonetaryPolicyAction(
        action_type="rate_change",
        param_before=json.dumps({"rate_bps": current}),
        param_after=json.dumps({"rate_bps": new_rate_bps}),
        amount_cent=0,
        initiated_by=initiated_by,
        status="pending",
    )
    db.add(action)
    db.flush()
    return action


def propose_open_market(db: Session, initiated_by: int, amount_cent: int,
                        direction: str = "buy") -> MonetaryPolicyAction:
    """提议公开市场操作。

    direction="buy"  → 注入货币（扩表）
    direction="sell" → 回收货币（缩表）
    amount_cent 必须为正整数。
    """
    if amount_cent <= 0:
        raise PolicyError("amount_cent must be a positive integer")
    if direction not in ("buy", "sell"):
        raise PolicyError(f"direction must be 'buy' or 'sell'; received {direction!r}")
    current_supply = _get_state_value(db, "money_supply")
    action = MonetaryPolicyAction(
        action_type="open_market",
        param_before=json.dumps({"direction": direction, "money_supply": current_supply}),
        param_after=json.dumps({
            "direction": direction,
            "money_supply": current_supply + (amount_cent if direction == "buy" else -amount_cent),
        }),
        amount_cent=amount_cent,
        initiated_by=initiated_by,
        status="pending",
    )
    db.add(action)
    db.flush()
    return action


def propose_qe(db: Session, initiated_by: int, amount_cent: int) -> MonetaryPolicyAction:
    """提议量化宽松（直接注入货币）。"""
    if amount_cent <= 0:
        raise PolicyError("amount_cent must be a positive integer")
    current_supply = _get_state_value(db, "money_supply")
    action = MonetaryPolicyAction(
        action_type="QE",
        param_before=json.dumps({"money_supply": current_supply}),
        param_after=json.dumps({"money_supply": current_supply + amount_cent}),
        amount_cent=amount_cent,
        initiated_by=initiated_by,
        status="pending",
    )
    db.add(action)
    db.flush()
    return action


# ======================== 执行 / 回滚 ========================

def apply_policy(db: Session, action_id: int) -> MonetaryPolicyAction:
    """执行一项 pending 状态的经济调控操作（B-M2：条件 UPDATE 抢权防双执行）。"""
    action = db.get(MonetaryPolicyAction, action_id)
    if action is None:
        raise PolicyError(f"Action {action_id} not found")

    # B-M2：原子抢权 pending→applied，rowcount!=1 即并发重复执行
    res = db.execute(
        _update(MonetaryPolicyAction)
        .where(MonetaryPolicyAction.id == action_id,
               MonetaryPolicyAction.status == "pending")
        .values(status="applied", applied_at=_now())
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise PolicyError(f"Action {action_id} status is {action.status}; cannot execute")

    if action.action_type == "rate_change":
        params = json.loads(action.param_after)
        new_rate = params["rate_bps"]
        _set_state_value(db, "interest_rate_bps", new_rate, ref=f"policy:{action.id}")

    elif action.action_type == "open_market":
        before = json.loads(action.param_before)
        direction = before["direction"]
        if direction == "buy":
            wallet_mod.adjust_system_state(db, "money_supply", action.amount_cent,
                                           ref=f"policy:{action.id}")
            # B-M2：QE/OM buy 注入落地到国库
            wallet_mod.adjust_system_state(db, "treasury_pool", action.amount_cent,
                                           ref=f"policy_treasury:{action.id}")
        else:
            wallet_mod.adjust_system_state(db, "money_supply", -action.amount_cent,
                                           ref=f"policy:{action.id}")
            wallet_mod.adjust_system_state(db, "treasury_pool", -action.amount_cent,
                                           ref=f"policy_treasury:{action.id}")

    elif action.action_type == "QE":
        wallet_mod.adjust_system_state(db, "money_supply", action.amount_cent,
                                       ref=f"policy:{action.id}")
        # B-M2：QE 注入落地到国库（不悬空印钞）
        wallet_mod.adjust_system_state(db, "treasury_pool", action.amount_cent,
                                       ref=f"policy_treasury:{action.id}")
    else:
        raise PolicyError(f"Unknown action type: {action.action_type}")

    # 同步 ORM identity map
    action.status = "applied"
    action.applied_at = _now()
    db.flush()
    return action


def rollback_policy(db: Session, action_id: int) -> MonetaryPolicyAction:
    """回滚一项已执行的（applied）经济调控操作（B-M2：条件 UPDATE 抢权）。"""
    action = db.get(MonetaryPolicyAction, action_id)
    if action is None:
        raise PolicyError(f"Action {action_id} not found")

    # 原子抢权 applied→rolled_back
    res = db.execute(
        _update(MonetaryPolicyAction)
        .where(MonetaryPolicyAction.id == action_id,
               MonetaryPolicyAction.status == "applied")
        .values(status="rolled_back")
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise PolicyError(f"Action {action_id} status is {action.status}; cannot roll back")

    if action.action_type == "rate_change":
        params = json.loads(action.param_before)
        old_rate = params["rate_bps"]
        _set_state_value(db, "interest_rate_bps", old_rate, ref=f"policy_rb:{action.id}")

    elif action.action_type == "open_market":
        before = json.loads(action.param_before)
        direction = before["direction"]
        # 反向操作
        if direction == "buy":
            wallet_mod.adjust_system_state(db, "money_supply", -action.amount_cent,
                                           ref=f"policy_rb:{action.id}")
            wallet_mod.adjust_system_state(db, "treasury_pool", -action.amount_cent,
                                           ref=f"policy_rb_treasury:{action.id}")
        else:
            wallet_mod.adjust_system_state(db, "money_supply", action.amount_cent,
                                           ref=f"policy_rb:{action.id}")
            wallet_mod.adjust_system_state(db, "treasury_pool", action.amount_cent,
                                           ref=f"policy_rb_treasury:{action.id}")

    elif action.action_type == "QE":
        wallet_mod.adjust_system_state(db, "money_supply", -action.amount_cent,
                                       ref=f"policy_rb:{action.id}")
        wallet_mod.adjust_system_state(db, "treasury_pool", -action.amount_cent,
                                       ref=f"policy_rb_treasury:{action.id}")
    else:
        raise PolicyError(f"Unknown action type: {action.action_type}")

    # 同步 ORM identity map
    action.status = "rolled_back"
    db.flush()
    return action


# ======================== 查询 ========================

def list_policy_actions(db: Session, limit: int = 30) -> list[dict]:
    """列出最近的经济调控操作（倒序）。"""
    limit = min(max(limit, 1), 100)
    rows = (db.query(MonetaryPolicyAction)
            .order_by(MonetaryPolicyAction.id.desc())
            .limit(limit).all())
    return [
        {
            "id": r.id,
            "action_type": r.action_type,
            "param_before": json.loads(r.param_before) if r.param_before else {},
            "param_after": json.loads(r.param_after) if r.param_after else {},
            "amount_cent": r.amount_cent,
            "initiated_by": r.initiated_by,
            "status": r.status,
            "applied_at": r.applied_at.isoformat() if r.applied_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def current_economic_params(db: Session) -> dict:
    """返回当前宏观经济参数快照。"""
    return {
        "interest_rate_bps": _get_state_value(db, "interest_rate_bps"),
        "money_supply_cent": _get_state_value(db, "money_supply"),
        "reserve_ratio_bps": _get_state_value(db, "reserve_ratio_bps"),
    }
