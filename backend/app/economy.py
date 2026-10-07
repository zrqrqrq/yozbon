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
"""城主经济自主（spec v2 标准二闭环：美元背书发行闸门 + 通胀目标带 + 通缩兜底阶梯）。

设计原则：
- **只读优先**：economy_snapshot 纯读，不改动任何既有资金流；决策姿态供城主与下游（e4 签约金、
  e5 算力计价）消费，真正"花钱/印钱"仍统一走 wallet 的发行闸门授权，保守可审计。
- **不印超**：通缩兜底投放（默认关）也必须过 wallet.authorize_noncash_issuance 闸门，投放进
  公共福利池（tax_pool，以工代赈储备）而非直接撒钱包，杜绝无锚定印钞。
- **去抖**：连续多个 tick 落入通缩区间才升级兜底阶梯，避免单点测量噪声误投放。

状态键（均存于 SystemState 单行表，仅本模块读写的自有记账键）：
- `econ:stance`      宏观姿态索引（见 STANCE_*，非负小整数）
- `econ:defl_streak`  连续通缩 tick 计数（去抖）
- `econ:ms_snap:YYYY-MM-DD`  当日货币供应（AC 分）快照，用于跨窗口增速估算
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import settings
from .models import SystemState
from . import wallet

logger = logging.getLogger(__name__)

# 宏观姿态阶梯索引（非负，便于存 SystemState.value_cent）。
STANCE_NEUTRAL = 0
STANCE_STIM_MILD = 1
STANCE_STIM_STRONG = 2
STANCE_TIGHTEN_MILD = 3
STANCE_TIGHTEN_STRONG = 4
_STANCE_NAMES = {
    STANCE_NEUTRAL: "neutral",
    STANCE_STIM_MILD: "stimulus_mild",
    STANCE_STIM_STRONG: "stimulus_strong",
    STANCE_TIGHTEN_MILD: "tighten_mild",
    STANCE_TIGHTEN_STRONG: "tighten_strong",
}

_STANCE_KEY = "econ:stance"
_DEFL_STREAK_KEY = "econ:defl_streak"
_SNAP_PREFIX = "econ:ms_snap:"


# ---------------------------------------------------------------------------
# 自有记账键读写（直接置值，区别于 wallet 的 delta-only 受护栏资金路径）
# ---------------------------------------------------------------------------
def _get_int(db, key: str, default: int = 0) -> int:
    row = db.get(SystemState, key)
    return row.value_cent if row else default


def _set_int(db, key: str, value: int) -> None:
    """直接置某个自有记账键的值（这些键由本模块独占，非 delta 语义，允许覆盖）。"""
    row = db.get(SystemState, key)
    if row is None:
        row = SystemState(key=key, value_cent=int(value))
        db.add(row)
    else:
        row.value_cent = int(value)
    row.updated_at = datetime.utcnow()


def _day_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 每日货币供应快照 + 通胀估算
# ---------------------------------------------------------------------------
def record_daily_snapshot(db) -> None:
    """把当日 money_supply 记为快照（幂等：当日已存在则不覆盖）。调用方负责 commit。"""
    key = _SNAP_PREFIX + _day_str(datetime.utcnow())
    if db.get(SystemState, key) is not None:
        return
    m = wallet.get_system_state(db, "money_supply", 0)
    row = SystemState(key=key, value_cent=int(m))
    db.add(row)


def _read_snapshot(db, day: datetime) -> int | None:
    row = db.get(SystemState, _SNAP_PREFIX + _day_str(day))
    return row.value_cent if row else None


def compute_inflation(db, window_days: int | None = None) -> dict:
    """估算通胀（年化口径，对齐 INFLATION_TARGET_* 年增速带）。

    数据不足（基线快照缺失或为 0）时返回 rate=None，调用方按 unknown 处理。
    注：把窗口增速外推为年化是**早期弱信号/预警**用途，窗口越短噪声越大，仅用于
    姿态调节而非硬约束；真正发行仍受 CashReserve 闸门硬控。
    """
    window_days = window_days if window_days else settings.ECON_MS_SNAPSHOT_WINDOW_DAYS
    now = datetime.utcnow()
    cur = wallet.get_system_state(db, "money_supply", 0)
    base_day = now - timedelta(days=window_days)
    base = _read_snapshot(db, base_day)
    if base is None or base <= 0:
        return {"rate": None, "window_days": window_days, "base": base, "current": cur}
    rate = (cur - base) / base
    # 年化外推：(1+rate)^(365/window) - 1
    try:
        annual = (1.0 + rate) ** (365.0 / window_days) - 1.0 if (1.0 + rate) > 0 else -1.0
    except Exception:  # noqa: BLE001  极端负增长
        annual = -1.0
    return {"rate": round(annual, 6), "window_rate": round(rate, 6),
            "window_days": window_days, "base": base, "current": cur}


def inflation_zone(annual_rate: float | None) -> str:
    """把年化通胀率映射到区间：deflation / below_band / in_band / above_band / unknown。"""
    if annual_rate is None:
        return "unknown"
    if annual_rate < 0:
        return "deflation"
    if annual_rate < settings.INFLATION_TARGET_LOW:
        return "below_band"
    if annual_rate <= settings.INFLATION_TARGET_HIGH:
        return "in_band"
    return "above_band"


# ---------------------------------------------------------------------------
# 宏观姿态（分级决策）
# ---------------------------------------------------------------------------
def update_macro_stance(db) -> dict:
    """依据通胀区间 + 通缩去抖计数，更新宏观姿态。调用方负责 commit。"""
    infl = compute_inflation(db)
    zone = inflation_zone(infl.get("rate"))
    streak = _get_int(db, _DEFL_STREAK_KEY, 0)
    if zone == "deflation":
        streak += 1
        stance = STANCE_STIM_STRONG if streak >= settings.GOV_DEFLATION_STREAK_TRIGGER else STANCE_STIM_MILD
    elif zone == "above_band":
        streak = 0
        annual = infl.get("rate") or 0.0
        stance = STANCE_TIGHTEN_STRONG if annual > settings.INFLATION_TARGET_HIGH * 1.5 else STANCE_TIGHTEN_MILD
    else:
        # in_band / below_band（温和低于下沿但未转负，视为可接受）/ unknown → 回归中性、清零通缩计数
        streak = 0
        stance = STANCE_NEUTRAL
    _set_int(db, _STANCE_KEY, stance)
    _set_int(db, _DEFL_STREAK_KEY, streak)
    return {"stance": stance, "stance_name": _STANCE_NAMES[stance],
            "deflation_streak": streak, "zone": zone, "inflation": infl}


# ---------------------------------------------------------------------------
# 通缩兜底阶梯（默认关；开启且姿态为投放、过闸门后投放进公共福利池）
# ---------------------------------------------------------------------------
def maybe_deflation_backstop(db, ref: str = "") -> dict:
    """通缩兜底投放（spec v2 标准二）。仅在以下条件全部满足时真正投放，否则只返回诊断：
    - 总开关 INFLATION_BACKSTOP_ENABLED 开启
    - 当前姿态为投放（stim_mild/stim_strong）且通缩去抖计数达阈值
    - 折算额度 ≥ 最低门槛（防碎钞）
    投放 = authorize_noncash_issuance（过发行闸门）+ money_supply + 公共福利池(tax_pool)。
    调用方负责 commit。
    """
    stance = _get_int(db, _STANCE_KEY, STANCE_NEUTRAL)
    streak = _get_int(db, _DEFL_STREAK_KEY, 0)
    headroom = wallet.noncash_issuance_headroom(db)
    diag = {"triggered": False, "stance": stance, "streak": streak,
            "headroom_cent": headroom, "amount_cent": 0, "reason": ""}

    if not settings.INFLATION_BACKSTOP_ENABLED:
        diag["reason"] = "backstop_disabled"
        return diag
    if stance not in (STANCE_STIM_MILD, STANCE_STIM_STRONG):
        diag["reason"] = "stance_not_stimulus"
        return diag
    if streak < settings.GOV_DEFLATION_STREAK_TRIGGER:
        diag["reason"] = "deflation_streak_below_trigger"
        return diag

    amount = int(headroom * settings.DEFLATION_STIMULUS_BPS_OF_HEADROOM / 10000)
    diag["amount_cent"] = amount
    if amount < settings.DEFLATION_STIMULUS_MIN_CENT:
        diag["reason"] = "amount_below_min"
        return diag
    if amount > headroom:  # 双保险：绝不超过 headroom
        amount = headroom
        diag["amount_cent"] = amount
    if amount <= 0:
        diag["reason"] = "no_headroom"
        return diag

    try:
        # 1) 发行闸门授权（累加 issued_noncash_cent；超额抛 WalletError）
        wallet.authorize_noncash_issuance(db, amount, ref=ref or "deflation_backstop")
        # 2) 货币供应增加（打印）
        wallet.adjust_system_state(db, "money_supply", amount, ref=ref or "deflation_backstop")
        # 3) 投放进公共福利池（以工代赈储备），而非直接撒钱包
        wallet.adjust_system_state(db, "tax_pool", amount, ref=ref or "deflation_backstop")
    except wallet.WalletError as e:  # noqa: BLE001  闸门拒绝 → 不投放，仅记录
        diag["reason"] = f"gate_rejected:{e}"
        return diag

    diag["triggered"] = True
    diag["reason"] = "stimulus_issued"
    logger.info("通缩兜底投放 | amount_ac_cent=%s headroom=%s stance=%s",
                amount, headroom, _STANCE_NAMES.get(stance, stance))
    return diag


# ---------------------------------------------------------------------------
# 每 tick 经济自主入口（run_tick 调用）：快照 → 姿态 → 兜底
# ---------------------------------------------------------------------------
def run_economy_tick(db, ref: str = "") -> dict:
    """城主 tick 的经济自主步骤。调用方负责 commit（建议在独立 try/except 内）。"""
    record_daily_snapshot(db)
    stance_info = update_macro_stance(db)
    backstop = maybe_deflation_backstop(db, ref=ref)
    return {"stance": stance_info, "backstop": backstop, "snapshot": economy_snapshot(db)}


# ---------------------------------------------------------------------------
# 只读快照（供 sense_context / 决策 prompt / 健康检查消费）
# ---------------------------------------------------------------------------
def economy_snapshot(db) -> dict:
    """汇总一份城主经济态势快照（纯读、缺值容错、绝不抛）。"""
    money_supply = wallet.get_system_state(db, "money_supply", 0)
    cash_reserve_usd = wallet.get_system_state(db, "cash_reserve_cent", 0)
    issued_noncash = wallet.get_system_state(db, "issued_noncash_cent", 0)
    cash_reserve_ac = wallet.usd_cents_to_ac_cents(cash_reserve_usd)
    ceiling = wallet.issuance_ceiling_ac_cents(db)
    headroom = wallet.noncash_issuance_headroom(db)
    infl = compute_inflation(db)
    zone = inflation_zone(infl.get("rate"))
    stance = _get_int(db, _STANCE_KEY, STANCE_NEUTRAL)
    streak = _get_int(db, _DEFL_STREAK_KEY, 0)
    util = 0.0
    if ceiling > 0:
        util = round(issued_noncash / ceiling, 4)
    return {
        "money_supply_cent": money_supply,
        "cash_reserve_usd_cent": cash_reserve_usd,
        "cash_reserve_ac_cent": cash_reserve_ac,
        "issuance_ceiling_ac_cent": ceiling,
        "issued_noncash_ac_cent": issued_noncash,
        "issuance_headroom_ac_cent": headroom,
        "issuance_utilization": util,        # 非现金发行已用 / 闸门上限
        "inflation_annual": infl.get("rate"),
        "inflation_zone": zone,
        "inflation_band": [settings.INFLATION_TARGET_LOW, settings.INFLATION_TARGET_HIGH],
        "stance": stance,
        "stance_name": _STANCE_NAMES.get(stance, "neutral"),
        "deflation_streak": streak,
        "backstop_enabled": bool(settings.INFLATION_BACKSTOP_ENABLED),
    }
