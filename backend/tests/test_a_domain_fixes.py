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
"""A 域（治理与调度）审计修复回归测试。

覆盖：A-H1 / A-H2 / A-H3 / A-H4 / A-M1 / A-M2 / A-M3 / A-M4 / A-M5 / A-L1 / A-L2 / A-L3
"""
import json
import pytest
from datetime import datetime, timedelta

from app.database import SessionLocal


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ======================== A-H1: governor dialect-aware upsert ========================

class TestAH1DialectUpsert:
    """_record_llm_call 使用方言感知 upsert，SQLite 环境不崩。"""

    def test_record_llm_call_sqlite(self, db):
        """SQLite 方言下 upsert 正常执行并累加。"""
        from app.governor import _record_llm_call
        from app.models import LlmBudgetLog

        _record_llm_call(db, "test_h1")
        db.commit()
        key = f"llm_budget:test_h1:{datetime.utcnow().strftime('%Y-%m-%d')}"
        row = db.get(LlmBudgetLog, key)
        assert row is not None
        assert row.call_count == 1

        # 第二次调用应累加
        _record_llm_call(db, "test_h1")
        db.commit()
        db.refresh(row)
        assert row.call_count == 2

    def test_record_llm_call_uses_correct_dialect(self, db):
        """验证方言检测逻辑在 SQLite 环境走 sqlite 分支。"""
        dialect = db.get_bind().dialect.name
        assert dialect == "sqlite"  # conftest 强制 SQLite


# ======================== A-H2: ImpeachmentVote 唯一约束 ========================

class TestAH2ImpeachmentVoteDedup:
    """弹劾投票表唯一约束防重复投票。"""

    def test_duplicate_vote_raises(self, db):
        from app.impeachment import impeachment_service

        cid = impeachment_service.initiate(
            db, target_type="governor", target_id=99,
            initiated_by=1, charges="test")
        impeachment_service.vote(db, cid, voter_host_id=42, vote="for")
        with pytest.raises(ValueError, match="already voted"):
            impeachment_service.vote(db, cid, voter_host_id=42, vote="against")

    def test_different_voters_allowed(self, db):
        from app.impeachment import impeachment_service

        cid = impeachment_service.initiate(
            db, target_type="governor", target_id=99,
            initiated_by=1, charges="test")
        impeachment_service.vote(db, cid, voter_host_id=10, vote="for")
        impeachment_service.vote(db, cid, voter_host_id=11, vote="for")
        impeachment_service.vote(db, cid, voter_host_id=12, vote="against")

        from app.models import ImpeachmentCase
        case = db.get(ImpeachmentCase, cid)
        assert case.vote_total == 3
        assert case.vote_for == 2
        assert case.vote_against == 1

    def test_vote_row_persisted(self, db):
        """投票记录写入独立表。"""
        from app.impeachment import impeachment_service
        from app.models import ImpeachmentVote

        cid = impeachment_service.initiate(
            db, target_type="governor", target_id=99,
            initiated_by=1, charges="test")
        impeachment_service.vote(db, cid, voter_host_id=7, vote="for")

        votes = db.query(ImpeachmentVote).filter(
            ImpeachmentVote.case_id == cid).all()
        assert len(votes) == 1
        assert votes[0].voter_host_id == 7
        assert votes[0].vote == "for"


# ======================== A-H3: QuadraticPoll 持久化 ========================

class TestAH3QuadraticPollPersistence:
    """poll 元数据持久化到 QuadraticPoll 表。"""

    def test_poll_persisted_to_table(self, db):
        from app.quadratic_vote import instance as qv
        from app.models import QuadraticPoll

        result = qv.create_poll("A-H3 Test", ["X", "Y", "Z"], credits_per_voter=50)
        poll_id = result["poll_id"]

        row = db.get(QuadraticPoll, poll_id)
        assert row is not None
        assert row.title == "A-H3 Test"
        assert row.credits_per_voter == 50
        assert row.status == "open"
        assert json.loads(row.options_json) == ["X", "Y", "Z"]


# ======================== A-H4: FOR UPDATE 不崩 (SQLite) ========================

class TestAH4ForUpdate:
    """cast_vote 中 with_for_update() 在 SQLite 不报错。"""

    def test_cast_vote_no_for_update_error(self):
        from app.quadratic_vote import instance as qv

        poll = qv.create_poll("H4", ["A", "B"], credits_per_voter=16)
        r = qv.cast_vote(poll["poll_id"], "citizen", 100, 0, 9)
        assert "vote_weight" in r
        assert r["vote_weight"] == 3  # sqrt(9)=3
        assert r["credits_spent"] == 9

    def test_insufficient_credits(self):
        from app.quadratic_vote import instance as qv

        poll = qv.create_poll("H4b", ["A", "B"], credits_per_voter=4)
        r = qv.cast_vote(poll["poll_id"], "citizen", 200, 0, 9)
        assert "error" in r
        assert "insufficient" in r["error"]


# ======================== A-M1: PostQuota 白名单 ========================

class TestAM1PostQuotaWhitelist:
    """trigger 端点接受 PostQuota active 岗位。"""

    def test_trigger_dynamic_post(self, client, host):
        from app.models import PostQuota

        db = SessionLocal()
        try:
            pq = PostQuota(
                post_code="custom_dynamic_post",
                gov_type="platform_security",
                budget_cent=300,
                params='{"scope":"dynamic"}',
                status="active",
            )
            db.add(pq)
            db.commit()
        finally:
            db.close()

        r = client.post("/api/sys/platform-jobs/trigger",
                        json={"job_type": "custom_dynamic_post"},
                        headers={"Authorization": f"Bearer {host['token']}"})
        assert r.status_code == 200
        data = r.json()
        assert data["job_type"] == "custom_dynamic_post"

    def test_trigger_invalid_still_400(self, client, host):
        r = client.post("/api/sys/platform-jobs/trigger",
                        json={"job_type": "totally_nonexistent_xyz"},
                        headers={"Authorization": f"Bearer {host['token']}"})
        assert r.status_code == 400


# ======================== A-M2: only_type 按 post_code 精确过滤 ========================

class TestAM2PostCodeFilter:
    """run_due_jobs only_type 使用 post_code 而非 gov_type。"""

    def test_post_code_filter_isolation(self, db):
        """同 gov_type 不同 post_code，只触发指定的那一个。"""
        from app.scheduler import run_due_jobs
        from app.models import PostQuota, SchedulerRun

        # 两个岗位同 gov_type=platform_security，不同 post_code
        p1 = PostQuota(post_code="sec_alpha", gov_type="platform_security",
                       budget_cent=300, params='{"scope":"a"}', status="active")
        p2 = PostQuota(post_code="sec_beta", gov_type="platform_security",
                       budget_cent=300, params='{"scope":"b"}', status="active")
        db.add_all([p1, p2])
        db.commit()

        created = run_due_jobs(db, only_type="sec_alpha")
        db.commit()
        assert len(created) == 1

        # 验证只有 sec_alpha 被调度，sec_beta 未被触发
        run_key = datetime.utcnow().strftime("%Y-%m-%d")
        run_a = db.query(SchedulerRun).filter(
            SchedulerRun.job_type == "sec_alpha",
            SchedulerRun.run_key == run_key).first()
        run_b = db.query(SchedulerRun).filter(
            SchedulerRun.job_type == "sec_beta",
            SchedulerRun.run_key == run_key).first()
        assert run_a is not None
        assert run_b is None


# ======================== A-M3: accepted_count 原子自增 ========================

class TestAM3AtomicIncrement:
    """TOS accept 使用原子 UPDATE 自增。"""

    def test_multiple_accepts(self, db):
        from app.tos_manager import tos_manager

        vid = tos_manager.publish_version(
            db, "v-atomic-test", "content", datetime.utcnow(), mandatory=False)

        for i in range(5):
            tos_manager.accept(db, vid, host_id=1000 + i)

        from app.models import TermsOfServiceVersion
        db.expire_all()
        tos = db.get(TermsOfServiceVersion, vid)
        assert tos.accepted_count == 5


# ======================== A-M4: Futarchy 状态校验 ========================

class TestAM4FutarchyValidation:
    """link_outcome 仅 pending 可关联；execute 需 approved + winning_outcome。"""

    def test_link_outcome_rejects_non_pending(self, db):
        from app.futarchy import futarchy_service

        fid = futarchy_service.propose(db, "test", prediction_market_id=999)
        futarchy_service.link_outcome(db, fid, "yes")
        # 第二次关联应失败（已是 approved 状态）
        with pytest.raises(ValueError, match="already resolved"):
            futarchy_service.link_outcome(db, fid, "no")

    def test_execute_requires_approved(self, db):
        from app.futarchy import futarchy_service

        fid = futarchy_service.propose(db, "test2", prediction_market_id=999)
        # 未 link_outcome，状态仍 pending，不可 execute
        with pytest.raises(ValueError, match="not approved"):
            futarchy_service.execute(db, fid)

    def test_execute_requires_winning_outcome(self, db):
        from app.futarchy import futarchy_service
        from app.models import FutarchyProposal

        fid = futarchy_service.propose(db, "test3", prediction_market_id=999)
        # 手动设为 approved 但 winning_outcome 为空
        fp = db.get(FutarchyProposal, fid)
        fp.implementation_status = "approved"
        fp.winning_outcome = ""
        db.commit()

        with pytest.raises(ValueError, match="[Ww]inning outcome"):
            futarchy_service.execute(db, fid)

    def test_happy_path_link_and_execute(self, db):
        """正常流程：pending → link → approved → execute。"""
        from app.futarchy import futarchy_service

        fid = futarchy_service.propose(db, "test4", prediction_market_id=999)
        futarchy_service.link_outcome(db, fid, "yes")
        futarchy_service.execute(db, fid)

        from app.models import FutarchyProposal
        fp = db.get(FutarchyProposal, fid)
        assert fp.implementation_status == "executed"


# ======================== A-M5: TOCTOU 注释更正 ========================

class TestAM5CommentCorrection:
    """验证注释已更正（非误导性的"消除竞态"措辞）。"""

    def test_comment_corrected_with_negation(self):
        """确保 A-M5 更正注释用"并非"否定了旧误导措辞，并引入正确描述。"""
        import app.governor
        import inspect
        source = inspect.getsource(app.governor)
        # 更正后应包含否定表述（"并非"消除…）和"有界超调"正确描述
        assert "并非" in source and "有界超调" in source


# ======================== A-L1: rollback in exception path ========================

class TestAL1Rollback:
    """governor_loop 异常路径调用 db.rollback()。"""

    def test_rollback_in_source(self):
        """验证 governor.py 异常路径包含 db.rollback()。"""
        import app.governor
        import inspect
        source = inspect.getsource(app.governor.governor_loop)
        assert "db.rollback()" in source


# ======================== A-L2: 弹劾罢免撤销委托 ========================

class TestAL2DelegateRevoke:
    """delegate 弹劾通过后撤销 active delegations。"""

    def test_delegate_removal_revokes(self, db):
        from app.impeachment import impeachment_service
        from app.models import Delegation, AICitizen

        # 造 delegate AI + active delegation
        ai = AICitizen(name="delegate-bot", status="active", host_id=1,
                       ai_uid="ai_1_delegate_test")
        db.add(ai)
        db.commit()

        d = Delegation(host_id=1, ai_id=ai.id, scope_json='["publish_task"]',
                       status="active")
        db.add(d)
        db.commit()
        delegation_id = d.id

        # 发起弹劾（target_type=delegate）
        cid = impeachment_service.initiate(
            db, target_type="delegate", target_id=ai.id,
            initiated_by=1, charges="misuse")
        # 投够票（IMPEACHMENT_MIN_VOTES=3）
        impeachment_service.vote(db, cid, voter_host_id=1, vote="for")
        impeachment_service.vote(db, cid, voter_host_id=2, vote="for")
        impeachment_service.vote(db, cid, voter_host_id=3, vote="for")

        # 判定
        impeachment_service.resolve(db, cid)

        # 验证 delegation 已 revoked
        db.refresh(d)
        assert d.status == "revoked"


# ======================== A-L3: today_status 包含 PostQuota 岗位 ========================

class TestAL3TodayStatus:
    """today_status 返回结果包含 PostQuota active 岗位。"""

    def test_dynamic_post_in_dashboard(self, client, host):
        from app.models import PostQuota

        db = SessionLocal()
        try:
            pq = PostQuota(
                post_code="planner_dynamic_x",
                gov_type="platform_code",
                budget_cent=300,
                params='{"scope":"dynamic"}',
                status="active",
            )
            db.add(pq)
            db.commit()
        finally:
            db.close()

        r = client.get("/api/sys/platform-jobs",
                       headers={"Authorization": f"Bearer {host['token']}"})
        assert r.status_code == 200
        data = r.json()
        today = data.get("today", {})
        assert "planner_dynamic_x" in today

    def test_static_posts_still_present(self, client, host):
        """既有静态 PLATFORM_JOBS 仍保留。"""
        r = client.get("/api/sys/platform-jobs",
                       headers={"Authorization": f"Bearer {host['token']}"})
        assert r.status_code == 200
        today = r.json().get("today", {})
        # 至少包含硬编码四岗位
        for jt in ("platform_security", "platform_code",
                   "platform_file", "platform_intel"):
            assert jt in today
