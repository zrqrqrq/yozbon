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
"""共享税规则（纯函数，无 DB 依赖）—— B 线结算与 C1 线税收共用同一套算法，杜绝口径分裂。

- income_tax_cent: 超额累进收入税（月累计），蓝图 §六 规则 6「收入税在结算时同事务代扣」
- adjusted_fee_rate: 平衡阀读取（规则 10「税池余额不足 → 手续费 +0.5%，上限 8%」，单向不印钞）
金额一律 integer 分；禁止浮点累计。
"""
from .config import settings


def parse_brackets(spec: str):
    """解析 "50000:10,500000:20,999999999:30" → [(上限分, 税率%), ...]（升序）。"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        cap, rate = part.split(":")
        out.append((int(cap), int(rate)))
    return out


def income_tax_cent(net_cent: int, cum_cent: int = 0) -> int:
    """超额累进收入税（分，integer 数学）。

    - net_cent: 本次净收入（结算到手额）
    - cum_cent: 本月此前已计税的累计收入（免税线以下部分不计入）
    免税线(TAX_INCOME_FREE)以下不征税；档位按 (上限,税率) 边际计算。
    返回税金额（分），单调不减，非负。
    """
    if net_cent <= 0:
        return 0
    free = int(settings.TAX_INCOME_FREE)
    low = max(cum_cent, free)            # 本次应税起点（免税线截断）
    high = cum_cent + net_cent           # 本次应税终点
    if high <= low:
        return 0
    tax = 0
    prev_cap = free                      # 免税线作为第一档下界
    for cap, rate in parse_brackets(settings.TAX_INCOME_BRACKETS):
        lo = max(low, prev_cap)          # 该档内本次应税段下界
        hi = min(high, cap)              # 该档内本次应税段上界
        if hi > lo:
            tax += (hi - lo) * rate // 100   # integer 分
        if high <= cap:
            break
        prev_cap = cap
    return tax


def adjusted_fee_rate(pool_cent: int, base_rate: float, reserve_cent: int) -> float:
    """平衡阀读取（规则 10）：税池余额低于低保储备线 → 手续费 +FEE_ADJUST_STEP，上限 FEE_RATE_MAX。

    单向：只升不降（阀值由 C1 线平衡阀状态机管理；本函数只做判定+涨幅封顶）。
    """
    rate = base_rate
    if pool_cent < reserve_cent:
        rate = min(base_rate + settings.FEE_ADJUST_STEP, settings.FEE_RATE_MAX)
    return rate


def fee_split(amount_cent: int, fee_rate: float):
    """结算手续费拆分（规则 6）：fee = 5%(可调)，其中 3% 销毁 + 2% 税池（FEE_BURN_RATE 配置）。

    返回 (fee_cent, burn_cent, taxpool_cent)，均为整数分；销毁=round(fee*FEE_BURN_RATE)。
    """
    fee_cent = round(amount_cent * fee_rate)
    burn_cent = round(fee_cent * settings.FEE_BURN_RATE)
    taxpool_cent = fee_cent - burn_cent
    return fee_cent, burn_cent, taxpool_cent
