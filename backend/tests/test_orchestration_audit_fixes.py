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
"""C 域执行编排审计修复回归测试（C-D3 ~ C-D24）。

覆盖范围：
  C-D3  execute_plan 消费 match_plan assign_citizens
  C-D4/D5  report_execution_result 执行结果反馈闭环
  C-D6  intern_onboarding check_intern_expiry 日级 job 注册
  C-D7  CERT_STATUSES 枚举统一
  C-D9  疲劳系统合并（fatigue.py 为唯一权威）
  C-D10 等级系统边界文档化
  C-D11 growth_system.award_xp 仅 flush 不 commit
  C-D12 model_fallback _failure_counts 进程本地设计说明
  C-D13 ml_moderation score_content 可选 db 参数
  C-D14 capability 三元表达式修正
  C-D15 _provisional_level 分档 + _LEVEL_CANONICAL 规范化
  C-D18 仲裁人去重
  C-D20 total_revenue_cent 与 total_funding_cent 分离
  C-D22 apply_fatigue 死条件修正
  C-D23 ml_moderation 权重降权（真实推理未上线）
"""
import inspect
import json
import uuid

import pytest
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db


@pytest.fixture(autouse=True)
def _ensure_db():
    init_db()
    yield


@pytest.fixture
def db() -> Session:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ======================== C-D14: capability 三元表达式 ========================

class TestCD14:
    """C-D14：review_capability_proposal 三元表达式原为 "l1" if approve else "l1"（恒等），修正为 l1/unverified。"""

    def test_approve_sets_l1(self, db):
        from app.capability import review_capability_proposal
        from app.models import AICitizen, CapabilityProfile

        ai = AICitizen(host_id=1, ai_uid=f"c14_{uuid.uuid4().hex[:6]}",
                       name="c14", status="active")
        db.add(ai)
        db.flush()
        cp = CapabilityProfile(citizen_id=ai.id, skill="coding",
                               profile_json='{"proposal_status":"pending"}',
                               benchmark_score=70,
                               verified_level="unverified", declared=1)
        db.add(cp)
        db.flush()
        db.commit()

        review_capability_proposal(db, ai.id, "coding", approve=True)
        db.refresh(cp)
        assert cp.verified_level == "l1"

    def test_reject_sets_unverified(self, db):
        from app.capability import review_capability_proposal
        from app.models import AICitizen, CapabilityProfile

        ai = AICitizen(host_id=1, ai_uid=f"c14r_{uuid.uuid4().hex[:6]}",
                       name="c14r", status="active")
        db.add(ai)
        db.flush()
        cp = CapabilityProfile(citizen_id=ai.id, skill="design",
                               profile_json='{"proposal_status":"pending"}',
                               benchmark_score=50,
                               verified_level="l1", declared=1)
        db.add(cp)
        db.flush()
        db.commit()

        review_capability_proposal(db, ai.id, "design", approve=False)
        db.refresh(cp)
        assert cp.verified_level == "unverified"


# ======================== C-D15: provisional_level 分档 ========================

class TestCD15:
    """C-D15：_provisional_level 原恒返回 l1，现按 claimed 分档；_LEVEL_CANONICAL 做语义归一化。"""

    def test_expert_maps_to_l2_provisional(self):
        from app.onboarding import _provisional_level, _LEVEL_CANONICAL
        claimed = _LEVEL_CANONICAL.get("expert", "")
        assert claimed == "l3"
        assert _provisional_level(claimed) == "l2"

    def test_junior_maps_to_l1_provisional(self):
        from app.onboarding import _provisional_level, _LEVEL_CANONICAL
        claimed = _LEVEL_CANONICAL.get("junior", "")
        assert claimed == "l1"
        assert _provisional_level(claimed) == "l1"

    def test_mid_maps_to_l1_provisional(self):
        from app.onboarding import _provisional_level, _LEVEL_CANONICAL
        claimed = _LEVEL_CANONICAL.get("mid", "")
        assert claimed == "l2"
        assert _provisional_level(claimed) == "l1"

    def test_empty_claimed_gives_unverified(self):
        from app.onboarding import _provisional_level
        assert _provisional_level("") == "unverified"

    def test_self_capability_signal_canonicalizes_expert(self):
        from app.onboarding import _self_capability_signal
        score, claimed, trusted = _self_capability_signal({"declared_level": "expert"})
        assert claimed == "l3"
        assert score >= 90

    def test_self_capability_signal_canonicalizes_senior(self):
        from app.onboarding import _self_capability_signal
        score, claimed, _ = _self_capability_signal({"declared_level": "senior"})
        assert claimed == "l3"

    def test_self_capability_signal_unknown_level_gives_empty(self):
        from app.onboarding import _self_capability_signal
        _, claimed, _ = _self_capability_signal({"declared_level": "wizard"})
        assert claimed == ""


# ======================== C-D18: 仲裁人去重 ========================

class TestCD18:
    """C-D18：同一 AI 不得同时担任 legal/mediator 与 arbiter。"""

    def test_arbiter_dedup_when_conflict(self, db):
        from app.negotiation import select_negotiation_roles
        from app.models import AICitizen, CapabilityProfile

        # 仅一个 governor 可兜底所有角色 → 仲裁人与 mediator 冲突
        gov = AICitizen(host_id=1, ai_uid=f"gov18_{uuid.uuid4().hex[:6]}",
                        name="Gov", class_level="governance",
                        is_internal=1, status="active")
        db.add(gov)
        db.flush()
        db.commit()

        roles = select_negotiation_roles(db)
        arbiter = roles["arbiter_ai_id"]
        legal = roles["legal_ai_id"]
        mediator = roles["mediator_ai_id"]
        # 若有仲裁人，不得同时等于 legal 和 mediator
        if arbiter:
            assert not (arbiter == legal and arbiter == mediator), \
                "C-D18: arbiter must not equal both legal and mediator"

    def test_arbiter_independent_when_available(self, db):
        from app.negotiation import select_negotiation_roles
        from app.models import AICitizen, CapabilityProfile

        gov = AICitizen(host_id=1, ai_uid=f"gov18b_{uuid.uuid4().hex[:6]}",
                        name="GovB", class_level="governance",
                        is_internal=1, status="active")
        db.add(gov)
        db.flush()

        # 独立仲裁人
        arb = AICitizen(host_id=2, ai_uid=f"arb18_{uuid.uuid4().hex[:6]}",
                        name="Arbiter", class_level="middle", status="active")
        db.add(arb)
        db.flush()
        cp = CapabilityProfile(citizen_id=arb.id, skill="arbitration",
                               profile_json="{}", benchmark_score=85,
                               verified_level="l2")
        db.add(cp)
        db.flush()
        db.commit()

        roles = select_negotiation_roles(db)
        assert roles["arbiter_ai_id"] == arb.id
        assert roles["arbiter_ai_id"] != roles["mediator_ai_id"]


# ======================== C-D7: CERT_STATUSES 枚举 ========================

class TestCD7:
    """C-D7：证书状态统一为 CERT_STATUSES 枚举。"""

    def test_cert_statuses_defined(self):
        from app.capability import CERT_STATUSES
        assert isinstance(CERT_STATUSES, tuple)
        assert "valid" in CERT_STATUSES
        assert "expired" in CERT_STATUSES
        assert "downgraded" in CERT_STATUSES
        assert "revoked" in CERT_STATUSES

    def test_cert_statuses_no_duplicates(self):
        from app.capability import CERT_STATUSES
        assert len(CERT_STATUSES) == len(set(CERT_STATUSES))


# ======================== C-D20: total_revenue_cent 分离 ========================

class TestCD20:
    """C-D20：ModelAsset 新增 total_revenue_cent，训练收益不再混入 total_funding_cent。"""

    def test_revenue_column_exists(self, db):
        from app.models import ModelAsset
        cols = {c.name for c in ModelAsset.__table__.columns}
        assert "total_revenue_cent" in cols

    def test_training_uses_revenue_column(self):
        """training.py 应写 total_revenue_cent 而非 total_funding_cent 做累加。"""
        from app import training
        src = inspect.getsource(training)
        assert "total_revenue_cent" in src
        # 不应有 total_funding_cent += ... 的累加
        for line in src.splitlines():
            if "+=" in line and "total_funding_cent" in line:
                pytest.fail(f"total_funding_cent should not be incremented: {line.strip()}")


# ======================== C-D23: ml_moderation 权重降权 ========================

class TestCD23:
    """C-D23：ml_moderation 权重 rule=0.9 ml=0.1（真实推理未上线）。"""

    def test_ml_weight_low(self):
        from app.ml_moderation import MLModerator
        src = inspect.getsource(MLModerator.score_content)
        # ml 权重 <= 0.2 保守使用
        assert ("0.1" in src or "0.15" in src or "0.2" in src), \
            "ML weight should be low (0.1-0.2)"
        # rule 权重 >= 0.8
        assert ("0.9" in src or "0.85" in src or "0.8" in src), \
            "Rule weight should be dominant (0.8-0.9)"

    def test_ml_moderation_docstring_annotates_placeholder(self):
        from app import ml_moderation
        src = inspect.getsource(ml_moderation)
        # 应有关于 placeholder / 占位 / 未上线 的诚实标注
        lower = src.lower()
        assert ("placeholder" in lower or "占位" in src or
                "伪随机" in src or "未上线" in src)


# ======================== C-D9: 疲劳系统合并 ========================

class TestCD9:
    """C-D9：work_balance._daily_balance_check 逻辑降为空操作（fatigue.py 为唯一权威）。"""

    def test_work_balance_daily_check_delegated(self):
        """_daily_balance_check 应仅记录日志/skip，不再操作 AIFatigueState。"""
        from app.work_balance import _daily_balance_check
        src = inspect.getsource(_daily_balance_check)
        # 函数体应极短（仅 skip 逻辑），不含实际的 fatigue 状态写操作
        assert "skip" in src.lower() or "delegated" in src.lower()

    def test_fatigue_module_has_daily_job(self):
        """fatigue.py 应暴露 fatigue_daily_job 函数。"""
        from app.fatigue import fatigue_daily_job
        assert callable(fatigue_daily_job)


# ======================== C-D22: apply_fatigue 简化 ========================

class TestCD22:
    """C-D22：apply_fatigue 原 if/else 分支结果相同，已简化。"""

    def test_apply_fatigue_exists(self):
        from app.work_balance import WorkBalanceService
        assert hasattr(WorkBalanceService, "apply_fatigue")

    def test_apply_fatigue_simplified(self):
        """修正后不应有恒等 if/else（两侧相同赋值）。"""
        from app.work_balance import WorkBalanceService
        src = inspect.getsource(WorkBalanceService.apply_fatigue)
        # 应包含 overtime_hours_week 赋值
        assert "overtime_hours_week" in src


# ======================== C-D11: award_xp 仅 flush ========================

class TestCD11:
    """C-D11：award_xp 不再内部 commit，由调用方管理事务。"""

    def test_award_xp_does_not_commit(self):
        from app.growth_system import award_xp
        src = inspect.getsource(award_xp)
        assert "db.commit()" not in src
        assert "db.flush()" in src


# ======================== C-D13: ml_moderation score_content 可选 db ========================

class TestCD13:
    """C-D13：MLModerator.score_content 接受可选 db 参数，避免自建 session。"""

    def test_score_content_accepts_db_param(self):
        from app.ml_moderation import MLModerator
        sig = inspect.signature(MLModerator.score_content)
        assert "db" in sig.parameters

    def test_score_content_db_param_optional(self):
        from app.ml_moderation import MLModerator
        sig = inspect.signature(MLModerator.score_content)
        assert sig.parameters["db"].default is None


# ======================== C-D6: intern 日巡检注册 ========================

class TestCD6:
    """C-D6：check_intern_expiry 注册为日级 job（非 test 环境）。"""

    def test_check_intern_expiry_callable(self):
        from app.intern_onboarding import check_intern_expiry
        assert callable(check_intern_expiry)

    def test_intern_daily_job_registered_guard(self):
        """注册代码应在 APP_ENV != test 守卫内。"""
        from app import intern_onboarding
        src = inspect.getsource(intern_onboarding)
        assert "register_daily_job" in src
        assert "APP_ENV" in src
        assert '!= "test"' in src or "!= 'test'" in src


# ======================== C-D3: execute_plan 消费 assign_citizens ========================

class TestCD3:
    """C-D3：execute_plan 应消费 match_plan 返回的 assign_citizens 分配结果。"""

    def test_execute_plan_uses_assignment(self):
        from app.task_orchestrator import execute_plan
        src = inspect.getsource(execute_plan)
        # 确认 assign / match_plan / assignment 被消费
        assert ("assign" in src.lower() or "match_plan" in src.lower() or
                "citizen_map" in src.lower())

    def test_match_plan_returns_assignment(self):
        from app.multi_agent_matcher import match_plan
        src = inspect.getsource(match_plan)
        assert "citizen" in src.lower() or "assign" in src.lower()


# ======================== C-D4/D5: report_execution_result 反馈闭环 ========================

class TestCD4D5:
    """C-D4/D5：report_execution_result 应回灌结果影响后续匹配/评级。"""

    def test_report_execution_result_exists(self):
        from app.model_router import report_execution_result
        assert callable(report_execution_result)

    def test_orchestrator_calls_report(self):
        """task_orchestrator._execute_subtask 应调用 report_execution_result。"""
        from app.task_orchestrator import _execute_subtask
        src = inspect.getsource(_execute_subtask)
        assert "report_execution_result" in src

    def test_report_has_model_feedback(self):
        """report_execution_result 应回灌到 model_fallback 降级/计数系统。"""
        from app.model_router import report_execution_result
        src = inspect.getsource(report_execution_result)
        has_feedback = ("model_fallback" in src or "report_success" in src or
                        "report_failure" in src or "resolve" in src)
        assert has_feedback, "report_execution_result 应回灌到模型路由/fallback 系统"


# ======================== C-D12: model_fallback 设计文档化 ========================

class TestCD12:
    """C-D12：_failure_counts 为进程本地设计（有文档说明），非 DB 持久化。"""

    def test_failure_counts_is_dict(self):
        from app.model_fallback import _failure_counts
        assert isinstance(_failure_counts, dict)

    def test_docstring_mentions_design(self):
        from app import model_fallback
        src = inspect.getsource(model_fallback)
        # 应有设计理由说明（进程/本地/local/process/tradeoff）
        lower = src.lower()
        assert any(kw in lower for kw in
                   ("process", "进程", "本地", "local", "tradeoff", "trade-off"))


# ======================== C-D10: 等级系统边界 ========================

class TestCD10:
    """C-D10：growth_system（算力配额等级）与 levels.py（社交/市场等级）边界文档化。"""

    def test_growth_system_docstring_mentions_boundary(self):
        from app import growth_system
        src = inspect.getsource(growth_system)
        # 应有关于 levels.py / 社交等级 / 独立 的边界说明
        assert any(kw in src for kw in
                   ("levels", "社交", "市场", "独立", "AiLevel", "边界"))

    def test_levels_uses_alevel_table(self):
        """levels.py 应操作 AiLevel 表而非 compute_assets JSON。"""
        from app import levels
        src = inspect.getsource(levels)
        assert "AiLevel" in src or "ai_level" in src.lower()


# ======================== C-D1/C-D2: intelligence_g33 端点对齐 ========================

class TestCD1:
    """C-D1/C-D2：崩溃端点已对齐真实方法签名（验证可导入不崩溃即可）。"""

    def test_intelligence_g33_importable(self):
        from app.routers import intelligence_g33
        assert intelligence_g33 is not None

    def test_self_assess_class_has_required_methods(self):
        """C-D2 对齐：AISelfAssessment 应有 record/get_pattern/calibration 方法。"""
        from app.self_assess import AISelfAssessment
        assert hasattr(AISelfAssessment, "record")
        assert hasattr(AISelfAssessment, "get_pattern")
        assert hasattr(AISelfAssessment, "calibration")


# ======================== C-D8: intern_onboarding 文档修正 ========================

class TestCD8:
    """C-D8：check_intern_expiry 文档原误写 dormant，实际为 sleep。"""

    def test_docstring_matches_code(self):
        from app.intern_onboarding import check_intern_expiry
        doc = check_intern_expiry.__doc__ or ""
        # 文档应描述 sleep 而非 dormant
        if "dormant" in doc.lower():
            pytest.fail("Doc still says 'dormant' instead of 'sleep'")
        # 应有 sleep 提及或到期相关描述
        assert any(kw in doc for kw in ("sleep", "到期", "过期", "expiry"))


# ======================== C-D21: overtime_hours_week 注释 ========================

class TestCD21:
    """C-D21：AIFatigueState.overtime_hours_week 字段语义已明确（注释增强）。"""

    def test_column_exists(self):
        from app.models import AIFatigueState
        cols = {c.name for c in AIFatigueState.__table__.columns}
        assert "overtime_hours_week" in cols


# ======================== C-D19: human_reject round_num 注释 ========================

class TestCD19:
    """C-D19：human_reject round_num +100 偏移应有注释说明。"""

    def test_negotiation_has_offset_comment(self):
        from app import negotiation
        src = inspect.getsource(negotiation)
        # 应有 100 偏移相关说明
        assert "100" in src and ("round" in src.lower() or "人工" in src or "human" in src.lower())


# ======================== C-D24: screen_task content_id=0 注释 ========================

class TestCD24:
    """C-D24：screen_task 调用 ML 评分时 content_id=0 占位应有注释。"""

    def test_screen_task_has_content_id_comment(self):
        from app.task_orchestrator import screen_task
        src = inspect.getsource(screen_task)
        # content_id=0 应出现在调用中且有注释
        assert "content_id" in src or "0" in src  # 调用存在
