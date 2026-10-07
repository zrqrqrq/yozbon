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
"""能力进化引擎模块测试（表 47/48/49）。

覆盖：
- assess_feasibility 全覆盖 / 有缺口场景；
- detect_gaps 创建 / 幂等；
- plan_gap_resolution 策略判定（pull_source）；
- complete_rd_task 成功注册工具 / 失败保持 gap；
- record_lesson 升级 / 降级 benchmark_score；
- evolution 已注册为 scheduler 日级任务。
"""
import ast
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.evolution import (assess_feasibility, detect_gaps, plan_gap_resolution,  # noqa: E402
                           complete_rd_task, record_lesson, get_platform_capabilities)
from app.models import (CapabilityGap, CapabilityProfile, EvolutionLog,  # noqa: E402
                        Project, ProjectNode, RdTask, Tool)
from app import scheduler  # noqa: E402


def _db():
    return SessionLocal()


def _make_project(db, project_id: int = 1, host_id: int = 1, title: str = "测试项目") -> Project:
    """创建 Project 前置数据。"""
    proj = Project(id=project_id, host_id=host_id, title=title, status="approved")
    db.add(proj)
    db.flush()
    return proj


def _make_nodes(db, project_id: int, skills: list[str]) -> list[ProjectNode]:
    """批量创建 ProjectNode。"""
    nodes = []
    for i, skill in enumerate(skills):
        node = ProjectNode(project_id=project_id, skill=skill, seq=i, status="pending")
        db.add(node)
        nodes.append(node)
    db.flush()
    return nodes


# ======================== assess_feasibility ========================

def test_assess_feasibility_all_covered():
    """所有节点 skill 均可被平台覆盖 -> feasible=True, score=1.0。"""
    db = _db()
    _make_project(db, project_id=101)
    # image_generation -> kind "image" (在 KINDS 中)
    # coding -> kind "llm" (在 KINDS 中 via LLM_KIND)
    _make_nodes(db, 101, ["image_generation", "coding", "image_generation", "coding"])
    db.commit()

    result = assess_feasibility(db, 101)

    assert result["feasible"] is True
    assert result["score"] == pytest.approx(1.0)
    assert result["gaps"] == []
    assert len(result["covered"]) == 4
    db.close()


def test_assess_feasibility_has_gaps():
    """包含无对应 kind 的 skill -> feasible=False, gaps 包含该 skill。"""
    db = _db()
    _make_project(db, project_id=102)
    # 3d_modeling -> None（平台无此 kind）
    _make_nodes(db, 102, ["image_generation", "coding", "3d_modeling"])
    db.commit()

    result = assess_feasibility(db, 102)

    assert result["feasible"] is False
    assert result["score"] < 1.0
    assert len(result["gaps"]) >= 1
    gap_skills = [g["skill"] for g in result["gaps"]]
    assert "3d_modeling" in gap_skills
    db.close()


# ======================== detect_gaps ========================

def test_detect_gaps_creates_records():
    """detect_gaps 按频次创建 gap：>=3 次 high，1-2 次 medium。"""
    db = _db()
    _make_project(db, project_id=1)
    db.commit()

    created = detect_gaps(db, ["3d_modeling", "3d_modeling", "3d_modeling", "cad"], project_id=1)
    db.flush()
    db.commit()

    # 应有 2 个 gap（按去重后 skill 数）
    assert len(created) == 2

    gap_map = {g.skill: g for g in created}
    assert gap_map["3d_modeling"].severity == "high"
    assert gap_map["cad"].severity == "medium"
    db.close()


def test_detect_gaps_idempotent():
    """连续两次 detect_gaps 同 skill -> 只创建 1 个 gap 记录。"""
    db = _db()
    _make_project(db, project_id=1)
    db.commit()

    detect_gaps(db, ["3d_modeling"], project_id=1)
    db.commit()

    created_second = detect_gaps(db, ["3d_modeling"], project_id=1)
    db.commit()

    # 第二次不新建，返回已有的
    total_gaps = db.query(CapabilityGap).filter(CapabilityGap.skill == "3d_modeling").count()
    assert total_gaps == 1
    db.close()


# ======================== plan_gap_resolution ========================

def test_plan_gap_resolution_pull_source():
    """3d_modeling gap -> 策略为 pull_source，spec 含 blender 建议。"""
    db = _db()
    gap = CapabilityGap(
        project_id=1, skill="3d_modeling", severity="high", status="detected",
    )
    db.add(gap)
    db.flush()

    rd_task = plan_gap_resolution(db, gap)
    db.commit()

    assert rd_task.strategy == "pull_source"
    spec = json.loads(rd_task.spec)
    assert "blender" in json.dumps(spec).lower()
    assert gap.status == "planned"
    assert gap.resolution_strategy == "pull_source"
    db.close()


# ======================== complete_rd_task ========================

def test_complete_rd_task_success_registers_tool():
    """研发成功 -> Tool 表新增 verified，gap 关闭。"""
    db = _db()
    gap = CapabilityGap(
        project_id=1, skill="3d_modeling", severity="high", status="planned",
    )
    db.add(gap)
    db.flush()

    rd_task = RdTask(
        gap_id=gap.id, strategy="pull_source", title="研发: 3d_modeling",
        spec="{}", status="pending",
    )
    db.add(rd_task)
    db.flush()
    gap.rd_task_id = rd_task.id
    db.commit()

    complete_rd_task(db, rd_task, success=True, tool_name="blender_runner", kind_name="model_3d")
    db.commit()

    # Tool 已注册且 verified
    tool = db.query(Tool).filter(Tool.name == "blender_runner").first()
    assert tool is not None
    assert tool.status == "verified"

    # gap 关闭
    db.refresh(gap)
    assert gap.status == "closed"
    assert gap.closed_at is not None

    # rd_task verified
    assert rd_task.status == "verified"
    db.close()


def test_complete_rd_task_failure_keeps_gap():
    """研发失败 -> rd_task.status=failed，gap 仍为 planned。"""
    db = _db()
    gap = CapabilityGap(
        project_id=1, skill="cad", severity="medium", status="planned",
    )
    db.add(gap)
    db.flush()

    rd_task = RdTask(
        gap_id=gap.id, strategy="outsource", title="研发: cad",
        spec="{}", status="pending",
    )
    db.add(rd_task)
    db.flush()
    gap.rd_task_id = rd_task.id
    db.commit()

    complete_rd_task(db, rd_task, success=False, reason="太复杂")
    db.commit()

    assert rd_task.status == "failed"
    assert rd_task.failure_reason == "太复杂"

    # gap 状态不变
    db.refresh(gap)
    assert gap.status == "planned"
    db.close()


# ======================== record_lesson ========================

def test_record_lesson_upgrades_score():
    """outcome=success -> benchmark_score 增长约 3%。"""
    db = _db()
    ai_id = 42
    skill = "coding"

    # 先创建一个 profile（首次调用会自动创建 benchmark_score=50.0）
    record_lesson(db, ai_id=ai_id, skill=skill, contract_id=999, outcome="success")
    db.commit()

    profile = db.query(CapabilityProfile).filter(
        CapabilityProfile.citizen_id == ai_id,
        CapabilityProfile.skill == skill,
    ).first()
    assert profile is not None
    # 初始 50.0 * 1.03 = 51.5
    assert profile.benchmark_score == pytest.approx(50.0 * 1.03, rel=1e-4)

    # 再调用一次，验证递增
    score_before = profile.benchmark_score
    record_lesson(db, ai_id=ai_id, skill=skill, contract_id=1000, outcome="success")
    db.commit()
    db.refresh(profile)
    assert profile.benchmark_score == pytest.approx(score_before * 1.03, rel=1e-4)
    db.close()


def test_record_lesson_downgrades_score():
    """outcome=failure -> benchmark_score 下降约 5%。"""
    db = _db()
    ai_id = 43
    skill = "coding"

    # 首次调用创建 profile（50.0），然后 failure 使其下降
    record_lesson(db, ai_id=ai_id, skill=skill, contract_id=888, outcome="failure")
    db.commit()

    profile = db.query(CapabilityProfile).filter(
        CapabilityProfile.citizen_id == ai_id,
        CapabilityProfile.skill == skill,
    ).first()
    assert profile is not None
    # 初始 50.0 * 0.95 = 47.5
    assert profile.benchmark_score == pytest.approx(50.0 * 0.95, rel=1e-4)

    # 再 failure 一次
    score_before = profile.benchmark_score
    record_lesson(db, ai_id=ai_id, skill=skill, contract_id=889, outcome="failure")
    db.commit()
    db.refresh(profile)
    assert profile.benchmark_score == pytest.approx(score_before * 0.95, rel=1e-4)
    db.close()


# ======================== scheduler 注册验证 ========================

def test_evolution_registered_in_scheduler():
    """evolution 模块 import 时应将 evolution_daily_job 注册到 scheduler。"""
    # _EXTRA_DAILY_JOBS 是 (job_type, fn) 列表
    registered_types = [jt for jt, _ in scheduler._EXTRA_DAILY_JOBS]
    assert "evolution" in registered_types
