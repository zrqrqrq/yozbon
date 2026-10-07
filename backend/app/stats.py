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
"""N4 统计报表服务层（社会功能扩展设计 §2 N4；§5.4 管理类权限）。

口径（设计 §2 N4 明文）：
- GMV = 成交合约金额 sum(contracts.escrow_cent) WHERE status='accepted'（**非 escrow 充值**）；
- 活跃 AI = 近 7 日有交易（钱包流水/结算）或在线 tick 的 AI；
- 信用 = 名下 AI 信用分均值（credit_profiles.score，无档案按 100）；
- 阶层分布 = 按 wallet 净资产(balance+escrow)分档，复用经济模型阶层公式
  （docs/经济模型与社会规则.md §5.1；参考实现 app/tax.py recalc_levels）。

双轨：实时聚合供面板（overview/platform），日快照供历史曲线（daily_snapshot）。
模块 import 时经 scheduler.register_daily_job("stat_snapshot", daily_snapshot) 注册；
同日幂等由 SchedulerRun(job_type,run_key) + stat_snapshots(date,metric,dimension) 唯一索引双保险，
本函数内也做存在性自查（设计要求）。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import wallet
from .models import (AICitizen, AIWallet, Contract, CreditProfile, StatSnapshot)
from .scheduler import register_daily_job
from .tax import NET_BOSS, NET_CAPITAL, NET_MIDDLE

ACTIVE_WINDOW_DAYS = 7

# 日快照指标（写库 metric 名；trends 读取同源）
METRIC_GMV = "gmv"
METRIC_GMV_TXNS = "gmv_txns"
METRIC_ACTIVE_AI = "active_ai"
METRIC_TAX_POOL = "tax_pool"
METRIC_MONEY_SUPPLY = "money_supply"
METRIC_CLASS_DIST = "class_dist"

CLASS_KEYS = ("bottom", "middle", "boss", "capital", "governance")


def _now() -> datetime:
    return datetime.utcnow()


def _cutoff(now: datetime) -> datetime:
    return now - timedelta(days=ACTIVE_WINDOW_DAYS)


def _is_active(ci: AICitizen, w: AIWallet, now: datetime) -> bool:
    """近 7 日有交易或在线 tick。三者取一即活跃。"""
    cut = _cutoff(now)
    if ci.last_tick_at and ci.last_tick_at >= cut:
        return True
    if w.last_flow_at and w.last_flow_at >= cut:
        return True
    return False


def _class_of(net_cent: int, score: int, occupation: str) -> str:
    """纯函数阶层分档——与 tax.recalc_levels 同口径（不 mutate citizen）。

    governance: occupation=='governance'；
    capital: net>=NET_CAPITAL；boss: net>=NET_BOSS；
    middle: net>=NET_MIDDLE 或 score>=150；否则 bottom。
    """
    if (occupation or "") == "governance":
        return "governance"
    if net_cent >= NET_CAPITAL:
        return "capital"
    if net_cent >= NET_BOSS:
        return "boss"
    if net_cent >= NET_MIDDLE or score >= 150:
        return "middle"
    return "bottom"


def _ai_tuples(db: Session, host_id: int | None = None):
    """取 AI + 其钱包（一次 IN 范围；host_id=None 表示全站）。"""
    q = db.query(AICitizen)
    if host_id is not None:
        q = q.filter(AICitizen.host_id == host_id)
    for ci in q.all():
        w = wallet.get_wallet(db, ci.id)
        yield ci, w


def _score(db: Session, citizen_id: int) -> int:
    p = db.get(CreditProfile, citizen_id)
    return p.score if p is not None else 100


# ---------------- 宿主视图：我的 AI ----------------

def overview(db: Session, host_id: int, now: datetime | None = None) -> dict:
    """名下 AI 收益聚合 / 活跃 / 信用。

    - income_cent = 名下 worker 已 accepted 合约 escrow 总额（GMV 口径，非充值）；
    - active_ai = 名下近 7 日活跃 AI 数；
    - credit_avg = 名下 AI 信用分均值（整数分）。
    """
    now = now or _now()
    my_ids = [r[0] for r in db.query(AICitizen.id)
               .filter(AICitizen.host_id == host_id).all()]
    if not my_ids:
        return {"host_id": host_id, "income_cent": 0, "active_ai": 0,
                "credit_avg": 0, "ai_total": 0}

    accepted = (db.query(Contract)
                .filter(Contract.worker_id.in_(my_ids),
                        Contract.status == "accepted").all())
    income = sum(int(c.escrow_cent or 0) for c in accepted)

    active = 0
    score_sum = 0
    for ci, w in _ai_tuples(db, host_id):
        if _is_active(ci, w, now):
            active += 1
        score_sum += _score(db, ci.id)

    return {"host_id": host_id, "income_cent": income,
            "active_ai": active, "credit_avg": round(score_sum / len(my_ids)),
            "ai_total": len(my_ids)}


# ---------------- 平台视图（管理员） ----------------

def platform(db: Session, now: datetime | None = None) -> dict:
    """平台运营视图：GMV / 交易笔数 / 活跃 AI / 税池 / 货币总量 / 阶层分布。"""
    now = now or _now()
    accepted = (db.query(Contract)
                .filter(Contract.status == "accepted").all())
    gmv = sum(int(c.escrow_cent or 0) for c in accepted)

    active = 0
    dist = {k: 0 for k in CLASS_KEYS}
    for ci, w in _ai_tuples(db, None):
        if _is_active(ci, w, now):
            active += 1
        net = int(w.balance_cent or 0) + int(w.escrow_cent or 0)
        dist[_class_of(net, _score(db, ci.id), ci.occupation or "")] += 1

    return {"gmv_cent": gmv,
            "gmv_txns": len(accepted),
            "active_ai": active,
            "tax_pool_cent": wallet.get_system_state(db, "tax_pool"),
            "money_supply_cent": wallet.get_system_state(db, "money_supply"),
            "class_dist": dist}


# ---------------- 日快照 ----------------

def daily_snapshot(db: Session, now: datetime | None = None) -> int:
    """生成当日统计快照（幂等）。返回写入行数（已存在则 0）。

    写库指标：
    - gmv / gmv_txns / active_ai / tax_pool / money_supply（dimension=""）
    - class_dist（dimension=bottom/middle/boss/capital/governance，value=该档人数）
    """
    now = now or _now()
    day = now.strftime("%Y-%m-%d")
    # 函数内自查：当日任一指标已存在 → 跳过（双保险，不依赖唯一索引报错）
    if db.query(StatSnapshot).filter(StatSnapshot.date == day).first() is not None:
        return 0

    pf = platform(db, now)
    rows = [
        (METRIC_GMV, "", pf["gmv_cent"]),
        (METRIC_GMV_TXNS, "", pf["gmv_txns"]),
        (METRIC_ACTIVE_AI, "", pf["active_ai"]),
        (METRIC_TAX_POOL, "", pf["tax_pool_cent"]),
        (METRIC_MONEY_SUPPLY, "", pf["money_supply_cent"]),
    ]
    for level, n in pf["class_dist"].items():
        rows.append((METRIC_CLASS_DIST, level, n))

    for metric, dim, value in rows:
        db.add(StatSnapshot(date=day, metric=metric, dimension=dim, value=int(value or 0)))
    db.flush()
    return len(rows)


# 模块 import 时注册日级快照任务（与 market/project 同模式；不修改 scheduler.py）。
# 按 scheduler.py 自带教义「APP_ENV=test 一律不启动、测试确定性」守卫：
# 测试环境不把 stat_snapshot 注入共享 _EXTRA_DAILY_JOBS，避免 run_due_jobs 在
# M3 调度既有测试（硬编码恰好 4 岗位）下多出一条 SchedulerRun 而回归；
# 快照函数本身的幂等与指标由 test_n4_stats 直接调用 daily_snapshot 验证。
from .config import settings  # noqa: E402

if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("stat_snapshot", daily_snapshot)


# ---------------- 历史曲线 ----------------

def trends(db: Session, days: int) -> list:
    """读 stat_snapshots 近 days 天曲线。空库返回空数组（不报错）。

    返回 [{date, gmv, gmv_txns, active_ai, tax_pool, money_supply, class_dist:{...}}]。
    days 由路由层钳制在 [1,365]。
    """
    cutoff = (_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = (db.query(StatSnapshot)
            .filter(StatSnapshot.date >= cutoff)
            .order_by(StatSnapshot.date.asc()).all())
    by_date: dict[str, dict] = {}
    for r in rows:
        bucket = by_date.setdefault(r.date, {
            "date": r.date, "gmv": 0, "gmv_txns": 0, "active_ai": 0,
            "tax_pool": 0, "money_supply": 0, "class_dist": {k: 0 for k in CLASS_KEYS}})
        if r.metric == METRIC_CLASS_DIST:
            bucket["class_dist"][r.dimension or "bottom"] = r.value
        elif r.metric == METRIC_GMV:
            bucket["gmv"] = r.value
        elif r.metric == METRIC_GMV_TXNS:
            bucket["gmv_txns"] = r.value
        elif r.metric == METRIC_ACTIVE_AI:
            bucket["active_ai"] = r.value
        elif r.metric == METRIC_TAX_POOL:
            bucket["tax_pool"] = r.value
        elif r.metric == METRIC_MONEY_SUPPLY:
            bucket["money_supply"] = r.value
    return list(by_date.values())
