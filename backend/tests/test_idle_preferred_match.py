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
"""闲置优先撮合回归（任务分级 + idle_rank 排序）。

复验 A（闲置信号实时聚合，零 schema 变更）+ B（小任务闲置优先撮合）：
- find_best_match 默认纯择优（boss 压 bottom），prefer_idle=True 时改由
  (技能达标, 在办数↑, 最近接单时间↑, 等级) 决定——把活派给最近最久没活/在办最少的达标 AI。
- 从未接单 AI 以 ts=0 哨兵排最前（最该优先派活）。
- 技能门槛仍生效：闲置但技能域不同(match_level=2)者不得挤掉达标候选。
- is_small_task 预算分级 + compute_idle_metrics 聚合正确。
- find_best_match_for_plan 依据 plan.constraints.budget_cent 自动分级。

各用例用独立 host_id 隔离候选池（get_available_agents 按 host_id 过滤），免依赖建 Host 行。
"""
import pathlib
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from app.database import SessionLocal  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import AICitizen, Contract  # noqa: E402
from app import multi_agent_matcher as m  # noqa: E402


def _db():
    return SessionLocal()


def _mk_ai(db, host_id, name, occupation="", level="bottom", status="active"):
    ai = AICitizen(host_id=host_id, ai_uid=f"ai_{host_id}_{name}", name=name,
                   occupation=occupation, class_level=level, status=status,
                   is_internal=0)
    db.add(ai)
    db.commit()
    return ai


def _mk_contract(db, worker_id, status="escrowed", created_at=None):
    c = Contract(worker_id=worker_id, buyer_id=0, status=status, escrow_cent=100,
                 created_at=created_at or datetime.utcnow())
    db.add(c)
    db.commit()
    return c


# ---------------- B：撮合排序 prefer_idle ----------------

def test_prefer_idle_flips_busy_boss_to_idle_bottom():
    """默认纯择优选 boss；启用闲置优先后改选无在办合同的 bottom。"""
    db = _db()
    try:
        host = 900001
        boss = _mk_ai(db, host, "boss", level="boss")
        bottom = _mk_ai(db, host, "bottom", level="bottom")
        _mk_contract(db, boss.id, status="escrowed")  # boss 在办 1 单，bottom 从没干

        # 纯择优：等级压制 → boss
        assert m.find_best_match(db, host, "llm").id == boss.id
        # 闲置优先：在办数(0<1) → bottom 胜出，等级降为次级
        assert m.find_best_match(db, host, "llm", prefer_idle=True).id == bottom.id
    finally:
        db.close()


def test_prefer_idle_picks_longest_idle_when_equal_load():
    """同等在办(均无在办合同)时，按最近接单时间升序选最久没活的。"""
    db = _db()
    try:
        host = 900002
        recent = _mk_ai(db, host, "recent", level="bottom")
        long_idle = _mk_ai(db, host, "idle", level="bottom")
        _mk_contract(db, recent.id, status="accepted",
                     created_at=datetime.utcnow() - timedelta(days=2))
        _mk_contract(db, long_idle.id, status="accepted",
                     created_at=datetime.utcnow() - timedelta(days=10))

        picked = m.find_best_match(db, host, "llm", prefer_idle=True)
        assert picked.id == long_idle.id  # 10 天前 > 2 天前 → 更该派活
    finally:
        db.close()


def test_never_worked_ai_ranks_first():
    """从未接单 AI（ts=0 哨兵）优先于近期接过单者。"""
    db = _db()
    try:
        host = 900003
        worked = _mk_ai(db, host, "worked", level="bottom")
        fresh = _mk_ai(db, host, "fresh", level="bottom")
        _mk_contract(db, worked.id, status="accepted",
                     created_at=datetime.utcnow() - timedelta(days=1))
        # fresh 无任何合同

        assert m.find_best_match(db, host, "llm", prefer_idle=True).id == fresh.id
    finally:
        db.close()


def test_skill_gate_not_beaten_by_idle_mismatch():
    """闲置优先仍守技能门槛：技能域不同(match_level=2)的闲置 AI 不得挤掉达标候选。"""
    db = _db()
    try:
        host = 900004
        qualified_idle = _mk_ai(db, host, "qual", occupation="")  # 通用兜底 match_level=1
        mismatch_busy = _mk_ai(db, host, "mism", occupation="totally_unknown_xyz")  # 明确另一域
        # 让 mismatch 看起来"更闲"也无效：它属技能域不同
        assert m.find_best_match(db, host, "llm", prefer_idle=True).id == qualified_idle.id
    finally:
        db.close()


# ---------------- A：信号聚合 + 分级 ----------------

def test_compute_idle_metrics_aggregates_active_and_last():
    db = _db()
    try:
        host = 900006
        busy = _mk_ai(db, host, "busy")
        idle = _mk_ai(db, host, "idle")
        _mk_contract(db, busy.id, status="escrowed")
        _mk_contract(db, busy.id, status="delivered")
        _mk_contract(db, busy.id, status="accepted",
                     created_at=datetime.utcnow() - timedelta(days=5))
        _mk_contract(db, idle.id, status="refunded",
                     created_at=datetime.utcnow() - timedelta(days=20))

        metrics = m.compute_idle_metrics(db, [busy.id, idle.id])
        assert metrics[busy.id][0] == 2  # escrowed+delivered 在办=2（accepted 终局不计）
        assert metrics[idle.id][0] == 0  # refunded 不计在办
        assert metrics[idle.id][1] < metrics[busy.id][1]  # 闲置者最近接单更旧
    finally:
        db.close()


def test_is_small_task_threshold_grading():
    assert m.is_small_task(constraints={"budget_cent": 1000}) is True
    assert m.is_small_task(constraints={"budget_cent": 99999999}) is False
    assert m.is_small_task(constraints={}) is False       # 无预算 → 保守大任务
    assert m.is_small_task(budget_cent=1) is True
    # 边界：等于阈值 → 小任务
    assert m.is_small_task(budget_cent=settings.IDLE_PREFERRED_MAX_BUDGET_CENT) is True


def test_plan_auto_grades_by_budget():
    """find_best_match_for_plan 依 plan.constraints.budget_cent 自动决定是否闲置优先。"""
    db = _db()
    try:
        host = 900005
        boss = _mk_ai(db, host, "boss", level="boss")
        bottom = _mk_ai(db, host, "bottom", level="bottom")
        _mk_contract(db, boss.id, status="escrowed")

        # 小预算 → 自动闲置优先 → bottom
        small = SimpleNamespace(subtasks=[{"id": "s1", "kind": "llm"}],
                                constraints={"budget_cent": 500})
        assert m.find_best_match_for_plan(db, host, small).id == bottom.id

        # 大预算 → 维持纯择优 → boss
        big = SimpleNamespace(subtasks=[{"id": "s1", "kind": "llm"}],
                              constraints={"budget_cent": 99999999})
        assert m.find_best_match_for_plan(db, host, big).id == boss.id
    finally:
        db.close()


def test_match_plan_idle_small_plan_activates_idle():
    """match_plan 小预算计划：子任务派给闲置 bottom，验证编排层闲置优先端到端。"""
    db = _db()
    try:
        host = 900007
        boss = _mk_ai(db, host, "boss", level="boss")
        bottom = _mk_ai(db, host, "bottom", level="bottom")
        _mk_contract(db, boss.id, status="escrowed")

        plan = SimpleNamespace(subtasks=[{"id": "a", "kind": "llm"},
                                         {"id": "b", "kind": "llm"}],
                               constraints={"budget_cent": 500})
        assign = m.match_plan(db, host, plan)  # 自动分级=小任务→闲置优先
        assert assign.get("a") == bottom.id
        assert assign.get("b") == bottom.id
    finally:
        db.close()
