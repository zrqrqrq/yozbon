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
"""任务难度档（difficulty）回归。

覆盖：
- 因子① 预算档 budget_tier：S/M/L 边界 + 无预算上下文（中性 M、known=False）。
- 因子② 编排规模 orchestration_scale：子任务数、依赖深度（链式 DAG）、空计划、环不卡死。
- 因子③ 治理类目 governance_hard：arbitration/compliance/security 命中。
- 三因子合成 compute_difficulty：预算/编排/治理各自升档、治理直接 hard 且转 G 级。
- 准入门槛 min_capability_level / passes_admission（easy 不限、medium≥l1、hard≥l2；无档案放行）。
- 撮合集成：is_small_task 委托 budget_tier 行为不变；开关开启时难度准入门槛软偏好合格执行者。
"""
import pathlib
import sys

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from types import SimpleNamespace

from app.database import SessionLocal  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import AICitizen, CapabilityProfile  # noqa: E402
from app import multi_agent_matcher as m  # noqa: E402
from app import task_difficulty as d  # noqa: E402


def _db():
    return SessionLocal()


def _mk_ai(db, host_id, name, occupation="", level="bottom"):
    ai = AICitizen(host_id=host_id, ai_uid=f"aiv_{host_id}_{name}", name=name,
                   occupation=occupation, class_level=level, status="active",
                   is_internal=0)
    db.add(ai)
    db.commit()
    return ai


def _mk_cap(db, citizen_id, skill, verified_level):
    cp = CapabilityProfile(citizen_id=citizen_id, skill=skill, profile_json="{}",
                           verified_level=verified_level)
    db.add(cp)
    db.commit()
    return cp


# ---------------- 因子①：预算档 ----------------

def test_budget_tier_boundaries():
    # S 档上沿 = BUDGET_TIER_M_CENT（默认复用 IDLE 阈值 10000）
    assert d.budget_tier(settings.BUDGET_TIER_M_CENT) == ("S", True)
    assert d.budget_tier(settings.BUDGET_TIER_M_CENT + 1)[0] == "M"
    # L 档：> BUDGET_TIER_L_CENT
    assert d.budget_tier(settings.BUDGET_TIER_L_CENT) == ("M", True)
    assert d.budget_tier(settings.BUDGET_TIER_L_CENT + 1) == ("L", True)
    # 无预算上下文 → 中性 M、known=False
    assert d.budget_tier(None) == ("M", False)
    assert d.budget_tier("bad") == ("M", False)


# ---------------- 因子②：编排规模 ----------------

def test_orchestration_scale_count_and_depth():
    assert d.orchestration_scale([]) == (0, 0)
    assert d.orchestration_scale([{"id": "a"}]) == (1, 1)
    # 链式 A→B→C → 深度 3
    chained = [{"id": "a", "depends_on": []},
               {"id": "b", "depends_on": ["a"]},
               {"id": "c", "depends_on": ["b"]}]
    assert d.orchestration_scale(chained) == (3, 3)
    # 并行无依赖 → 深度 1
    par = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert d.orchestration_scale(par) == (3, 1)


def test_orchestration_scale_cycle_safe():
    # 构造环（理论上已被 cycle_detector 拦截）：深度计算不得死循环/栈溢出。
    cyc = [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]
    count, depth = d.orchestration_scale(cyc)
    assert count == 2
    assert depth >= 1  # 有限且合理


# ---------------- 因子③：治理类目 ----------------

def test_governance_hard_categories():
    assert d.governance_hard("arbitration") is True
    assert d.governance_hard("compliance") is True
    assert d.governance_hard("platform_security") is True
    assert d.governance_hard("  Arbitrate  ") is True  # 归一化
    assert d.governance_hard("platform_file") is False
    assert d.governance_hard(None) is False


# ---------------- 三因子合成 ----------------

def test_compute_difficulty_budget_only():
    easy = d.compute_difficulty(subtasks=[{"id": "a"}], budget_cent=100)
    assert easy["difficulty"] == "easy"
    assert easy["budget_tier"] == "S"
    assert easy["factors"]["orchestration"] == 0
    hard = d.compute_difficulty(subtasks=None, budget_cent=settings.BUDGET_TIER_L_CENT + 1)
    assert hard["difficulty"] == "hard"
    assert hard["min_capability"] == "l2"


def test_compute_difficulty_orchestration_bump():
    # 中等预算（M 基础分 1）+ 3 子任务编排（+1）→ hard（score 2）
    r = d.compute_difficulty(
        subtasks=[{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]},
                  {"id": "c", "depends_on": ["b"]}],
        budget_cent=settings.BUDGET_TIER_M_CENT + 1)
    assert r["factors"]["orchestration"] == 1
    assert r["dependency_depth"] == 3
    assert r["difficulty"] == "hard"


def test_compute_difficulty_governance_forces_hard_and_route():
    # 低预算单子任务本应 easy，但命中治理硬类目 → 直接 hard 且转 G 级。
    r = d.compute_difficulty(subtasks=[{"id": "a"}], budget_cent=50,
                             governance_category="arbitration")
    assert r["difficulty"] == "hard"
    assert r["governance_hard"] is True
    assert r["route_governor"] is True
    assert r["factors"]["governance"] == 1
    assert r["budget_tier"] == "S"  # 预算档仍如实反映（不计费、只读）


# ---------------- 准入门槛 ----------------

def test_min_capability_level():
    assert d.min_capability_level("easy") is None
    assert d.min_capability_level("medium") == "l1"
    assert d.min_capability_level("hard") == "l2"


def test_passes_admission():
    assert d.passes_admission("l2", "hard") is True
    assert d.passes_admission("l3", "hard") is True
    assert d.passes_admission("l1", "hard") is False
    assert d.passes_admission("unverified", "medium") is False
    assert d.passes_admission("l1", "medium") is True
    # easy 不设限；无档案保守放行
    assert d.passes_admission("unverified", "easy") is True
    assert d.passes_admission(None, "hard") is True


# ---------------- 撮合集成：行为不变 + 软准入 ----------------

def test_is_small_task_delegates_to_budget_tier():
    # 行为逐条保持不变
    assert m.is_small_task(constraints={"budget_cent": 1000}) is True
    assert m.is_small_task(constraints={"budget_cent": 99999999}) is False
    assert m.is_small_task(constraints={}) is False
    assert m.is_small_task(budget_cent=1) is True
    assert m.is_small_task(budget_cent=settings.IDLE_PREFERRED_MAX_BUDGET_CENT) is True


def test_admission_soft_prefers_qualified_for_hard(monkeypatch):
    """难度=hard 时，准入门槛软偏好能力达标者（即便其 class_level 更低）。

    两个候选均技能达标（occupation 空 → 通用兜底 match_level=1）：
      boss：class_level=boss 但能力仅 l1（不达 l2 门槛）；
      expert：class_level=bottom 但能力 l2（达门槛）。
    开关开启 → 选 expert；门槛软约束不硬拒（boss 仍可作兜底，此处不触发）。
    """
    monkeypatch.setattr(settings, "MATCH_ADMISSION_BY_DIFFICULTY", True)
    db = _db()
    try:
        host = 900101
        boss = _mk_ai(db, host, "bossLv", level="boss")
        expert = _mk_ai(db, host, "expertLv", level="bottom")
        _mk_cap(db, boss.id, "llm", "l1")
        _mk_cap(db, expert.id, "llm", "l2")
        plan = SimpleNamespace(
            subtasks=[{"id": "s0", "kind": "llm", "depends_on": []}],
            constraints={"budget_cent": settings.BUDGET_TIER_L_CENT + 1},  # → hard
        )
        assign = m.match_plan(db, host, plan)
        assert assign["s0"] == expert.id
    finally:
        db.close()


def test_admission_off_keeps_pure_merit(monkeypatch):
    """开关关闭（默认）时难度不影响撮合：纯择优选 boss。"""
    monkeypatch.setattr(settings, "MATCH_ADMISSION_BY_DIFFICULTY", False)
    db = _db()
    try:
        host = 900102
        boss = _mk_ai(db, host, "bossLv2", level="boss")
        expert = _mk_ai(db, host, "expertLv2", level="bottom")
        _mk_cap(db, boss.id, "llm", "l1")
        _mk_cap(db, expert.id, "llm", "l2")
        plan = SimpleNamespace(
            subtasks=[{"id": "s0", "kind": "llm", "depends_on": []}],
            constraints={"budget_cent": settings.BUDGET_TIER_L_CENT + 1},
        )
        assign = m.match_plan(db, host, plan)
        assert assign["s0"] == boss.id
    finally:
        db.close()
