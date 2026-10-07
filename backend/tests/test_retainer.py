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
"""中长期运营模型：缺口修复 + 岗位长约/工时账/周结算/1:1 顶替。

覆盖：
- g1 run_tick 权重三档排序（高权=考核/裁决压过低权=例行巡检）；
- g2 全站出站并发总闸（_site_slot 封顶 ≤ SITE_MAX_CONCURRENCY）；
- i1 岗位长约 sign_contract / log_hours / weekly_settle（发放、折算、幂等、宿主信用）；
- i2 关键岗 1:1 待命顶替（heartbeat / failover_contract）。
"""
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import case  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (AICitizen, GovernanceTask, Host, RetainerContract,  # noqa: E402
                        WorkLedger)
from app import retainer, wallet  # noqa: E402
from app.retainer import RetainerError  # noqa: E402
from app import platform_compute as pc  # noqa: E402
from app import governor as gov  # noqa: E402
from app.models import Tool, CapabilityProfile  # noqa: E402
from tests.conftest import new_host, new_ai  # noqa: E402


def _set_level(db, ai_id, level):
    """设置某 AI 的最高认证等级（写入能力档案 CapabilityProfile）。"""
    db.add(CapabilityProfile(citizen_id=ai_id, skill="general",
                             profile_json="{}", verified_level=level))


def _db():
    return SessionLocal()


def _fixed_monday():
    return datetime(2026, 2, 2, 12, 0, 0)


def _seed_tax_pool(db, amount=5_000_000):
    wallet.adjust_system_state(db, "tax_pool", amount, ref="test:seed_pool")
    wallet.adjust_system_state(db, "money_supply", amount, ref="test:seed_ms")
    db.commit()


def _make_active_ai(client, host, name="在职AI", occupation="安全维护") -> dict:
    """创建并强制 active 的 AI（绕开入驻状态机，专供长约测试）。"""
    data = new_ai(client, host["token"], name=name, occupation=occupation)
    db = _db()
    ai = db.get(AICitizen, data["id"])
    ai.status = "active"
    ai.class_level = "governance"
    ai.is_internal = 0
    db.commit()
    db.close()
    data["host_id"] = host["host_id"]
    return data


# ============================ g1 权重排序 ============================

def _weighted_order(db, limit):
    """复刻 run_tick 的权重排序查询，返回按权重降序 + id 升序排好的 task id 列表。"""
    weights = settings.TASK_WEIGHTS or {}
    whens = [(GovernanceTask.type == t, int(w)) for t, w in weights.items() if w]
    weight_expr = case(*whens, else_=0) if whens else GovernanceTask.id * 0
    rows = (db.query(GovernanceTask.id)
              .filter(GovernanceTask.status.in_(("open", "bidding", "assigned")))
              .order_by(weight_expr.desc(), GovernanceTask.id.asc()).limit(limit).all())
    return [r[0] for r in rows]


def test_weight_config_parsed():
    """g1：TASK_WEIGHTS 解析出三档，review=arbitrate > platform_security。"""
    w = settings.TASK_WEIGHTS
    assert w["review"] == 3 and w["arbitrate"] == 3
    assert w["platform_security"] == 1
    assert w["audit"] == 2


def test_weight_order_beats_fifo(client):
    """g1：低权巡检（id 小）不得压过高权考核（id 大）——权重排序优于纯 FIFO。"""
    db = _db()
    # 先插入低权 platform_security（id 较小），后插入高权 review（id 较大）
    low = GovernanceTask(type="platform_security", status="open")
    low2 = GovernanceTask(type="cleanup", status="open")
    high = GovernanceTask(type="review", status="open")
    high2 = GovernanceTask(type="arbitrate", status="open")
    for t in (low, low2, high, high2):
        db.add(t)
    db.commit()

    order = _weighted_order(db, limit=4)
    ids = {t.type: t.id for t in (low, low2, high, high2)}
    # 高权（review/arbitrate）必排在低权（platform_security/cleanup）之前
    high_rank = min(order.index(ids["review"]), order.index(ids["arbitrate"]))
    low_rank = max(order.index(ids["platform_security"]), order.index(ids["cleanup"]))
    assert high_rank < low_rank
    # 若按旧 FIFO（id 升序），低权会被排在最前——确认已被权重推翻
    assert order[0] in (ids["review"], ids["arbitrate"])
    db.close()


def test_weight_same_tier_id_asc(client):
    """g1：同权重档内仍按 id 升序（先到先服务的次级公平）。"""
    db = _db()
    a = GovernanceTask(type="review", status="open")
    b = GovernanceTask(type="review", status="open")
    c = GovernanceTask(type="review", status="open")
    for t in (a, b, c):
        db.add(t)
    db.commit()
    order = _weighted_order(db, limit=3)
    assert order == [a.id, b.id, c.id]
    db.close()


# ============================ g2 全站并发总闸 ============================

def test_site_concurrency_limit():
    """g2：总闸上限 = SITE_MAX_CONCURRENCY，初始占用 0。"""
    assert pc.site_concurrency_limit() == settings.SITE_MAX_CONCURRENCY
    assert pc.site_concurrency_inuse() == 0


def test_site_slot_caps_concurrency():
    """g2：并发占用 _site_slot 时，同时持有数恒 ≤ SITE_MAX_CONCURRENCY。"""
    limit = pc.site_concurrency_limit()
    n_threads = limit + 8
    peak = {"v": 0}
    cur = {"v": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        with pc._site_slot():
            with lock:
                cur["v"] += 1
                if cur["v"] > peak["v"]:
                    peak["v"] = cur["v"]
            import time
            time.sleep(0.05)
            with lock:
                cur["v"] -= 1

    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert peak["v"] <= limit
    # 释放干净，避免污染后续用例
    assert pc.site_concurrency_inuse() == 0


# ============================ i1 长约 + 工时 + 周结算 ============================

def test_sign_contract_basic(client, host):
    """i1：签长约绑定岗位/在编/备份/宿主，写库成功。"""
    primary = _make_active_ai(client, host, name="主-安全岗")
    backup = _make_active_ai(client, host, name="备-安全岗")
    db = _db()
    c = retainer.sign_contract(
        db, post_code="sec_ops", title="网站安全维护", occupation="安全维护",
        primary_ai_id=primary["id"], backup_ai_id=backup["id"],
        host_id=host["host_id"], retainer_cent=10000, weekly_hours=40.0,
        is_key_post=True)
    db.commit()
    assert c.id and c.status == "active"
    assert c.primary_ai_id == primary["id"] and c.backup_ai_id == backup["id"]
    assert c.retainer_cent == 10000 and c.is_key_post == 1
    db.close()


def test_sign_contract_dup_post_rejected(client, host):
    """i1：同 post_code 已有 active 长约 → 拒绝（防一岗多编重复发薪）。"""
    a = _make_active_ai(client, host, name="主A")
    b = _make_active_ai(client, host, name="主B")
    db = _db()
    retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=a["id"],
                           retainer_cent=10000)
    db.commit()
    with pytest.raises(RetainerError, match="active retainer"):
        retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=b["id"],
                               retainer_cent=10000)
    db.close()


def test_sign_contract_backup_equals_primary_rejected(client, host):
    """i1：备份 = 在编 → 拒绝。"""
    a = _make_active_ai(client, host, name="唯一AI")
    db = _db()
    with pytest.raises(RetainerError, match="must differ"):
        retainer.sign_contract(db, post_code="ops", primary_ai_id=a["id"],
                               backup_ai_id=a["id"], retainer_cent=10000)
    db.close()


def test_log_hours_accumulates(client, host):
    """i1：同一 ai+post+period 工时累加（唯一约束下幂等累加）。"""
    a = _make_active_ai(client, host, name="工时AI")
    db = _db()
    period = "2026-W06"
    retainer.log_hours(db, a["id"], "sec_ops", 8.0, period=period)
    retainer.log_hours(db, a["id"], "sec_ops", 12.0, period=period, tasks_done=3)
    db.commit()
    row = (db.query(WorkLedger)
             .filter(WorkLedger.ai_id == a["id"], WorkLedger.period == period).one())
    assert row.hours == pytest.approx(20.0)
    assert row.tasks_done == 3
    db.close()


def test_weekly_settle_full(client, host):
    """i1：满勤结算 = retainer_cent（无评判默认系数 1.0），税池减、余额增、宿主信用+1。"""
    a = _make_active_ai(client, host, name="满勤AI")
    db = _db()
    _seed_tax_pool(db)
    c = retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=a["id"],
                               host_id=host["host_id"], retainer_cent=10000,
                               weekly_hours=40.0)
    period = retainer.get_weekly_period(_fixed_monday())
    retainer.log_hours(db, a["id"], "sec_ops", 40.0, period=period)
    db.commit()

    pool_before = wallet.get_system_state(db, "tax_pool")
    bal_before = wallet.balance(db, a["id"])
    host_obj = db.get(Host, host["host_id"])
    credit_before = host_obj.host_credit

    res = retainer.weekly_settle(db, _fixed_monday())

    assert len(res) == 1
    assert res[0]["amount_cent"] == 10000
    assert res[0]["coefficient"] == pytest.approx(1.0)
    assert wallet.get_system_state(db, "tax_pool") == pool_before - 10000
    assert wallet.balance(db, a["id"]) == bal_before + 10000
    host_obj = db.get(Host, host["host_id"])
    assert host_obj.host_credit == credit_before + retainer.HOST_CREDIT_BUMP
    db.close()


def test_weekly_settle_partial_hours(client, host):
    """i1：半勤（20/40h）→ 实发 = retainer_cent × 0.5。"""
    a = _make_active_ai(client, host, name="半勤AI")
    db = _db()
    _seed_tax_pool(db)
    retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=a["id"],
                           retainer_cent=10000, weekly_hours=40.0)
    period = retainer.get_weekly_period(_fixed_monday())
    retainer.log_hours(db, a["id"], "sec_ops", 20.0, period=period)
    db.commit()

    res = retainer.weekly_settle(db, _fixed_monday())
    assert len(res) == 1
    assert res[0]["ratio"] == pytest.approx(0.5)
    assert res[0]["amount_cent"] == 5000
    db.close()


def test_weekly_settle_idempotent(client, host):
    """i1：同周重复结算不重复发放。"""
    a = _make_active_ai(client, host, name="幂等AI")
    db = _db()
    _seed_tax_pool(db)
    retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=a["id"],
                           retainer_cent=10000, weekly_hours=40.0)
    period = retainer.get_weekly_period(_fixed_monday())
    retainer.log_hours(db, a["id"], "sec_ops", 40.0, period=period)
    db.commit()

    r1 = retainer.weekly_settle(db, _fixed_monday())
    bal1 = wallet.balance(db, a["id"])
    r2 = retainer.weekly_settle(db, _fixed_monday())
    assert len(r1) == 1
    assert r2 == []
    assert wallet.balance(db, a["id"]) == bal1
    db.close()


def test_weekly_settle_no_hours_no_pay(client, host):
    """i1：本周无工时 → 不空发。"""
    a = _make_active_ai(client, host, name="摸鱼AI")
    db = _db()
    _seed_tax_pool(db)
    retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=a["id"],
                           retainer_cent=10000, weekly_hours=40.0)
    db.commit()
    res = retainer.weekly_settle(db, _fixed_monday())
    assert res == []
    db.close()


# ============================ i2 1:1 掉线顶替 ============================

def test_failover_on_primary_down(client, host):
    """i2：在编主 AI 掉线（非 active）→ 备份转正、原主降级为待命。"""
    primary = _make_active_ai(client, host, name="主-将掉线")
    backup = _make_active_ai(client, host, name="备-待命")
    db = _db()
    c = retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=primary["id"],
                               backup_ai_id=backup["id"], retainer_cent=10000)
    # 主 AI 心跳新鲜，但状态掉线
    c.last_heartbeat_at = datetime.utcnow()
    p = db.get(AICitizen, primary["id"])
    p.status = "inactive"
    db.commit()

    r = retainer.failover_contract(db, c.id)
    db.commit()
    assert r["failover"] is True
    assert r["promoted_backup"] == backup["id"]
    assert r["demoted_primary"] == primary["id"]
    fresh = db.get(RetainerContract, c.id)
    assert fresh.primary_ai_id == backup["id"]
    assert fresh.backup_ai_id == primary["id"]
    db.close()


def test_failover_on_stale_heartbeat(client, host):
    """i2：心跳超时（>30 分钟）→ 视为掉线并顶替。"""
    primary = _make_active_ai(client, host, name="主-心跳停")
    backup = _make_active_ai(client, host, name="备-待命")
    db = _db()
    c = retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=primary["id"],
                               backup_ai_id=backup["id"], retainer_cent=10000)
    c.last_heartbeat_at = datetime.utcnow() - timedelta(minutes=retainer.HEARTBEAT_STALE_MINUTES + 10)
    db.commit()

    r = retainer.failover_contract(db, c.id)
    assert r["failover"] is True
    assert r["promoted_backup"] == backup["id"]
    db.close()


def test_failover_no_backup_waits(client, host):
    """i2：主 AI 掉线但无可用备份 → 不强行顶替，返回等待补位。"""
    primary = _make_active_ai(client, host, name="主-孤立")
    db = _db()
    c = retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=primary["id"],
                               retainer_cent=10000)
    c.last_heartbeat_at = datetime.utcnow()
    p = db.get(AICitizen, primary["id"])
    p.status = "inactive"
    db.commit()

    r = retainer.failover_contract(db, c.id)
    assert r["failover"] is False
    assert r["reason"] == "no_available_backup"
    fresh = db.get(RetainerContract, c.id)
    assert fresh.primary_ai_id == primary["id"]  # 未变更
    db.close()


def test_failover_skips_healthy(client, host):
    """i2：主 AI 在线且心跳新鲜 → 不顶替（返回 None）。"""
    primary = _make_active_ai(client, host, name="主-健康")
    backup = _make_active_ai(client, host, name="备-待命")
    db = _db()
    c = retainer.sign_contract(db, post_code="sec_ops", primary_ai_id=primary["id"],
                               backup_ai_id=backup["id"], retainer_cent=10000)
    retainer.heartbeat(db, c.id)
    db.commit()
    r = retainer.failover_contract(db, c.id)
    assert r is None
    db.close()


def test_sweep_key_post_failover(client, host):
    """i2：日巡检对有备份的掉线岗顶替、对无备份岗计入待补。"""
    down1 = _make_active_ai(client, host, name="主1-掉线")
    bk1 = _make_active_ai(client, host, name="备1-待命")
    down2 = _make_active_ai(client, host, name="主2-掉线")
    db = _db()
    c1 = retainer.sign_contract(db, post_code="post1", primary_ai_id=down1["id"],
                                backup_ai_id=bk1["id"], retainer_cent=10000,
                                is_key_post=True)
    c2 = retainer.sign_contract(db, post_code="post2", primary_ai_id=down2["id"],
                                retainer_cent=10000, is_key_post=True)
    for c in (c1, c2):
        c.last_heartbeat_at = datetime.utcnow()
    db.get(AICitizen, down1["id"]).status = "inactive"
    db.get(AICitizen, down2["id"]).status = "inactive"
    db.commit()

    stat = retainer.sweep_key_post_failover(db)
    assert stat["failed_over"] == 1   # post1 有备份 → 顶替
    assert stat["waiting_backup"] == 1  # post2 无备份 → 待补
    db.close()


# ============================ i3 智能化分派 + 工具补齐策略 ============================

def _governor_id(db):
    return gov.ensure_governor(db).id


def test_delegate_prefers_verified_security_retainer(client, host):
    """i3：安全岗在编长约员工（l3 + 安全职业）在 platform_security 任务上压过泛用型 AI。"""
    sec = _make_active_ai(client, host, name="安全在编", occupation="网站安全维护")
    generic = _make_active_ai(client, host, name="泛用打杂", occupation="通用")
    db = _db()
    gid = _governor_id(db)
    # 安全岗：设 l3 + 在编长约（岗位职业含"安全"）
    _set_level(db, sec["id"], "l3")
    retainer.sign_contract(db, post_code="sec_ops", title="网站安全维护",
                           occupation="安全维护", primary_ai_id=sec["id"],
                           retainer_cent=10000, min_verified_level="l3")
    db.commit()

    cands = gov._delegate_candidates(db, "platform_security", gid, top=5)
    assert cands and cands[0]["ai_id"] == sec["id"]
    top = cands[0]
    assert top["post_match"] == 1
    assert top["verified_level"] == "l3"
    assert top["retainer"] == 1
    db.close()


def test_delegate_verified_beats_unverified(client, host):
    """i3：同等条件下，认证等级高者优先（verified_level 计分生效）。"""
    a1 = _make_active_ai(client, host, name="低认证", occupation="综合")
    a2 = _make_active_ai(client, host, name="高认证", occupation="综合")
    db = _db()
    gid = _governor_id(db)
    _set_level(db, a1["id"], "unverified")
    _set_level(db, a2["id"], "l2")
    db.commit()

    cands = gov._delegate_candidates(db, "cleanup", gid, top=5)
    order = {c["ai_id"]: c["score"] for c in cands}
    assert order[a2["id"]] > order[a1["id"]]
    db.close()


def test_delegate_verified_tool_boost(client, host):
    """i3：持有效（verified）工具提升候选得分。"""
    withtool = _make_active_ai(client, host, name="有工具", occupation="综合")
    notool = _make_active_ai(client, host, name="无工具", occupation="综合")
    db = _db()
    gid = _governor_id(db)
    db.add(Tool(owner_ai_id=withtool["id"], name="漏洞扫描器", status="verified"))
    db.add(Tool(owner_ai_id=notool["id"], name="草稿工具", status="pending"))
    db.commit()

    cands = gov._delegate_candidates(db, "platform_security", gid, top=5)
    scores = {c["ai_id"]: c["score"] for c in cands}
    assert scores[withtool["id"]] > scores[notool["id"]]
    db.close()


def test_capability_gap_when_no_qualified(client, host):
    """i3 工具补齐：无满足岗位认证要求的候选 → 登记 CapabilityGap，返回 gap=True。"""
    weak = _make_active_ai(client, host, name="能力不足", occupation="综合")
    db = _db()
    gid = _governor_id(db)
    _set_level(db, weak["id"], "l1")
    db.commit()

    res = gov.post_capability_gap(db, "platform_security", gid, min_verified_level="l3")
    db.commit()
    assert res["gap"] is True
    assert res["qualified"] == 0
    assert res["suggested_strategy"] == "outsource"
    from app.models import CapabilityGap
    gap = db.get(CapabilityGap, res["gap_id"])
    assert gap.skill == "post:platform_security"
    assert gap.status == "detected"
    db.close()


def test_no_gap_when_qualified(client, host):
    """i3 工具补齐：存在达认证要求的候选 → 无缺口（gap=False）。"""
    strong = _make_active_ai(client, host, name="达标安全AI", occupation="安全")
    db = _db()
    gid = _governor_id(db)
    _set_level(db, strong["id"], "l3")
    db.commit()

    res = gov.post_capability_gap(db, "platform_security", gid, min_verified_level="l3")
    assert res["gap"] is False
    assert res["qualified"] >= 1
    db.close()


def test_capability_gap_reuses_existing(client, host):
    """i3：重复探测同一缺口复用已登记记录（不重复建）。"""
    weak = _make_active_ai(client, host, name="能力不足2", occupation="综合")
    db = _db()
    gid = _governor_id(db)
    _set_level(db, weak["id"], "unverified")
    db.commit()

    r1 = gov.post_capability_gap(db, "platform_code", gid, min_verified_level="l2")
    db.commit()
    r2 = gov.post_capability_gap(db, "platform_code", gid, min_verified_level="l2")
    assert r1["gap_id"] == r2["gap_id"]
    from app.models import CapabilityGap
    n = db.query(CapabilityGap).filter(
        CapabilityGap.skill == "post:platform_code").count()
    assert n == 1
    db.close()

