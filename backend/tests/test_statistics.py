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
"""N4 统计报表单测（社会功能扩展设计 §2 N4）。

覆盖：overview（宿主名下收益/活跃/信用）、platform（GMV/笔数/活跃/税池/货币/阶层）、
daily_snapshot（写库 + 同日幂等）、trends（读曲线，空库不报错）。
端到端对账（宿主+AI+合约结算 → 统计数字与明细一致）见 test_n_stats_reconcile.py。
"""
from datetime import datetime, timedelta

import pytest

from app import credit, escrow, market, project as proj, stats, wallet
from app.database import SessionLocal
from app.models import AICitizen, AIWallet, AIPermission, Contract, CreditProfile, Project, ProjectNode, StatSnapshot
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


def _mk_ai(db, host_id, uid, balance=0, status="active", occupation="通用"):
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status=status,
                  class_level="bottom", occupation=occupation)
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    if balance:
        wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}")
    return c.id


def _accepted_contract(db, worker_id, buyer_id, P):
    """造一条已 accepted 合约（直接落库 + 钱包 gross 结算口径），返回 (cid)。"""
    p = Project(host_id=999, title="P", pm_citizen_id=buyer_id, status="running")
    db.add(p); db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", budget_cent=P, status="signed")
    db.add(n); db.flush()
    c = Contract(node_id=n.id, project_id=p.id, worker_id=worker_id,
                 buyer_id=buyer_id, status="accepted", escrow_cent=P,
                 accepted_at=datetime.utcnow(),
                 terms_json='{"offer_cent": %d}' % P)
    db.add(c); db.flush()
    return c.id


def test_overview_empty_host(client):
    h = new_host(client)
    db = _db()
    try:
        o = stats.overview(db, h["host_id"])
        assert o["ai_total"] == 0
        assert o["income_cent"] == 0
        assert o["active_ai"] == 0
        assert o["credit_avg"] == 0
    finally:
        db.close()


def test_overview_income_and_credit(client):
    h = new_host(client)
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s:pool")
        wid = _mk_ai(db, h["host_id"], "ov-worker", balance=50_000)
        bid = _mk_ai(db, h["host_id"], "ov-buyer", balance=50_000)
        _accepted_contract(db, wid, bid, 10_000)
        # 让 worker 近 7 日活跃（有交易 = 钱包流水）
        db.commit()
        o = stats.overview(db, h["host_id"])
        assert o["ai_total"] == 2
        assert o["income_cent"] == 10_000     # 名下 worker 已结算合约 escrow 总额
        assert o["active_ai"] >= 1            # worker 有流水
        assert o["credit_avg"] > 0
    finally:
        db.close()


def test_platform_gmv_and_class_dist(client):
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="p:pool")
        wid = _mk_ai(db, 1, "pf-worker", balance=0)
        bid = _mk_ai(db, 1, "pf-buyer", balance=200_000)  # 净产 200000 分 → middle 档
        _accepted_contract(db, wid, bid, 7_000)
        db.commit()
        pf = stats.platform(db)
        assert pf["gmv_cent"] == 7_000         # accepted escrow 总额（非充值）
        assert pf["gmv_txns"] == 1
        assert pf["tax_pool_cent"] >= 100_000
        # 阶层分布：buyer 净产 200000 ≥ NET_MIDDLE(10000) → middle；worker 0 → bottom
        assert pf["class_dist"]["middle"] >= 1
        assert pf["class_dist"]["bottom"] >= 1
    finally:
        db.close()


def test_daily_snapshot_writes_and_idempotent(client):
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="d:pool")
        day = "2026-10-04"
        n1 = stats.daily_snapshot(db, now=datetime(2026, 10, 4, 9, 0, 0))
        assert n1 > 0
        # 同日再跑 → 自查跳过，返回 0，不重复建行
        n2 = stats.daily_snapshot(db, now=datetime(2026, 10, 4, 18, 0, 0))
        assert n2 == 0
        rows = db.query(StatSnapshot).filter(StatSnapshot.date == day).all()
        metrics = {r.metric for r in rows}
        assert {"gmv", "gmv_txns", "active_ai", "tax_pool", "money_supply"} <= metrics
        assert "class_dist" in metrics
        db.commit()
    finally:
        db.close()


def test_trends_empty_returns_empty(client):
    db = _db()
    try:
        items = stats.trends(db, 30)
        assert items == []     # 空库不报错，返回空数组
    finally:
        db.close()


def test_trends_reads_snapshot(client):
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="t:pool")
        stats.daily_snapshot(db, now=datetime(2026, 10, 4, 9, 0, 0))
        db.commit()
        items = stats.trends(db, 30)
        assert len(items) == 1
        row = items[0]
        assert row["date"] == "2026-10-04"
        assert "gmv" in row and "class_dist" in row
    finally:
        db.close()
