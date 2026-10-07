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
"""严重级 S2/S4/S9/S13 城主编排核心修复回归。

覆盖：
  S2  escalate 重大事件必须经 host_notify 触达宿主（不再只写 AuditLog = 链路断裂）；
      cycle_detector.auto_stabilize 检测衰退 -> 生成货币政策提案 + 告警宿主（接回真实原语）。
  S4  sense_context 记录城主自身负载 governor_load + MAX_AI_CONCURRENT_TASKS 触顶阈值位。
  S9  run_recruitment_cycle 招聘编排器：缺口 -> 发布招聘帖；有达标能力档案 AI -> 录用移交。
  S13 宿主紧急开关 is_governor_paused -> run_tick 暂停态整轮空转。
"""
import pytest

from app import cycle_detector as cd_mod, governor, host_switch
from app.config import settings
from app.database import SessionLocal
from app.models import (AICitizen, CapabilityGap, CapabilityProfile,
                        EmploymentContract, GovernanceTask, Host,
                        HostNotification, JobPosting, MonetaryPolicyAction)

from conftest import new_host  # noqa: F401  保证 conftest 工厂可被引用


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def host0(db):
    h = db.get(Host, 0)
    if h is None:
        h = Host(id=0, email="internal@aijuhe.internal", password_hash="!",
                 nickname="平台", seat_tier="premium", ai_slots=999)
        db.add(h)
        db.commit()
    return h


def _mk_outward_ai(db, host_id, name, ai_uid, class_level="standard"):
    """建一个对外 active AI（不经 HTTP，直接落库）。"""
    ai = AICitizen(host_id=host_id, ai_uid=ai_uid, name=name,
                   class_level=class_level, is_internal=0, status="active")
    db.add(ai)
    db.commit()
    return ai


# ---------------------------------------------------------------------------
# S2：escalate 必须触达宿主
# ---------------------------------------------------------------------------
def test_escalate_notifies_host(db, host0):
    gov = governor.ensure_governor(db)
    task = GovernanceTask(type="arbitrate", budget_cent=99999, status="open")
    db.add(task)
    db.commit()

    before = db.query(HostNotification).filter(HostNotification.host_id == 0).count()
    res = governor._apply_action(db, gov, task,
                                 {"_kind": "escalate", "_reason": "金额巨大需宿主签字"})
    assert res["action"] == "escalate" and res["result"] == "escalated"

    after = db.query(HostNotification).filter(
        HostNotification.host_id == 0,
        HostNotification.category == "governance").count()
    assert after > before, "escalate 未生成宿主通知 = 上报最后一公里仍断裂"


# ---------------------------------------------------------------------------
# S2：稳定器检测衰退 -> 提案 + 告警宿主
# ---------------------------------------------------------------------------
def test_auto_stabilize_recession_proposes_and_notifies(db, host0, monkeypatch):
    monkeypatch.setattr(settings, "CYCLE_DETECT_ENABLED", True, raising=False)
    monkeypatch.setattr(cd_mod.cycle_detector, "detect",
                        lambda _db: {"signal_type": "recession"}, raising=True)

    res = cd_mod.cycle_detector.auto_stabilize(db)
    assert res.get("triggered") is True
    assert res.get("signal_type") == "recession"
    # 真实生成了一条货币政策提案（接回 propose_qe，而非注释掉的 auto_adjust）
    assert db.query(MonetaryPolicyAction).count() >= 1
    # 并告警宿主
    assert db.query(HostNotification).filter(
        HostNotification.category == "economic").count() >= 1


def test_auto_stabilize_disabled_short_circuits(db, host0, monkeypatch):
    monkeypatch.setattr(settings, "CYCLE_DETECT_ENABLED", False, raising=False)
    res = cd_mod.cycle_detector.auto_stabilize(db)
    assert res == {"triggered": False, "reason": "disabled"}


# ---------------------------------------------------------------------------
# S4：城主自身负载 + 触顶阈值位
# ---------------------------------------------------------------------------
def test_sense_context_records_self_load(db, host0, monkeypatch):
    monkeypatch.setattr(settings, "MAX_AI_CONCURRENT_TASKS", 2, raising=False)
    gov = governor.ensure_governor(db)
    for _ in range(2):
        db.add(GovernanceTask(type="review", budget_cent=100, status="assigned",
                              assignee_id=gov.id))
    db.commit()

    ctx = governor.sense_context(db, gov)
    assert ctx["governor_load"] >= 2
    assert ctx["load_capacity"] == 2
    assert ctx["load_over_threshold"] is True


def test_sense_context_below_threshold(db, host0, monkeypatch):
    monkeypatch.setattr(settings, "MAX_AI_CONCURRENT_TASKS", 50, raising=False)
    gov = governor.ensure_governor(db)
    ctx = governor.sense_context(db, gov)
    assert ctx["load_over_threshold"] is False


# ---------------------------------------------------------------------------
# S9：招聘编排器（缺口发布招聘帖）
# ---------------------------------------------------------------------------
def test_recruitment_publishes_job_when_gap(db, host0):
    gov = governor.ensure_governor(db)
    res = governor.run_recruitment_cycle(db, gov, types=["review"])
    assert res["jobs_created"] >= 1
    assert db.query(JobPosting).filter(
        JobPosting.required_skill == "post:review").count() >= 1
    # 缺口已登记 CapabilityGap
    assert db.query(CapabilityGap).filter(
        CapabilityGap.skill == "post:review").count() >= 1


def test_recruitment_hires_qualified_ai(db, host0):
    gov = governor.ensure_governor(db)
    ai = _mk_outward_ai(db, host0.id, "审工AI", "ai_test_audit_1",
                        class_level="standard")
    db.add(CapabilityProfile(citizen_id=ai.id, skill="post:audit",
                             profile_json="{}", benchmark_score=90.0,
                             verified_level="l2"))
    db.commit()

    res = governor.run_recruitment_cycle(db, gov, types=["audit"])
    assert res["recruited"] >= 1
    assert db.query(EmploymentContract).filter(
        EmploymentContract.employee_id == ai.id).count() >= 1


# ---------------------------------------------------------------------------
# S13：宿主紧急开关使 run_tick 整轮空转
# ---------------------------------------------------------------------------
def test_run_tick_respects_host_pause(db, host0):
    gov = governor.ensure_governor(db)
    t = GovernanceTask(type="review", budget_cent=300, status="open")
    db.add(t)
    db.commit()

    host_switch.set_governor_paused(db, True)
    res = governor.run_tick(db, gov, limit=5)
    assert res.get("paused") is True
    assert res["processed"] == 0
    db.refresh(t)
    assert t.status == "open"  # 暂停期间不处置任何待办

    host_switch.set_governor_paused(db, False)
    assert host_switch.is_governor_paused(db) is False
