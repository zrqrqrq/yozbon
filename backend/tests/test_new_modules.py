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
"""综合服务层单元测试：guild / referendum / insurance / training_progress /
host_notify / dead_letter / asset_transfer / audit_chain / monetary_policy / fatigue.

模式：直接 SessionLocal + service function，不经过 HTTP 路由。
钱包造数：直接插入 AICitizen + AIWallet。
"""
import pytest
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import AICitizen, AIWallet, TrainingCampaign

from app.guild import (create_guild, join_guild, leave_guild, dissolve_guild,
                       contribute_to_treasury, list_guilds, guild_members, GuildError)
from app.referendum import (create_referendum, open_referendum, cast_vote,
                            close_referendum, referendum_results, trigger_from_petition,
                            ReferendumError)
from app.insurance import (create_pool, buy_policy, file_claim, approve_claim,
                           pay_claim, reject_claim, pool_stats, InsuranceError)
from app.training_progress import (record_checkpoint, get_latest_progress,
                                   progress_summary, detect_anomalies)
from app.host_notify import (notify, mark_read, unread_count, list_notifications,
                             schedule_webhook_retry, mark_delivery_failed)
from app.dead_letter import (push_dead_letter, requeue, discard, resolve,
                             dead_letter_stats, list_dead_letters)
from app.asset_transfer import (list_asset, buy_listing, cancel_listing,
                                browse_listings, AssetTransferError)
from app.audit_chain import (append_audit, verify_chain, chain_length, get_audit_trail)
from app.monetary_policy import (propose_rate_change, propose_qe, apply_policy,
                                 current_economic_params, PolicyError)
from app.fatigue import (get_or_init_fatigue, record_task_completion, record_rest,
                         is_burned_out, get_efficiency)
from app import wallet as wallet_mod


# ==================== Fixtures ====================

@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _make_citizen(db, name="Citizen", balance_cent=100_000):
    """创建一个有余额钱包的 AI 公民（绕过 API）。"""
    citizen = AICitizen(
        host_id=1,
        ai_uid=f"test_{name}_{id(name)}_{datetime.utcnow().strftime('%f')}",
        name=name,
        status="active",
        balance_cent=balance_cent,
        is_internal=0,
    )
    db.add(citizen)
    db.flush()
    wallet = AIWallet(citizen_id=citizen.id, balance_cent=balance_cent)
    db.add(wallet)
    db.flush()
    return citizen


def _make_citizens(db, n=3, balance_cent=100_000):
    """批量创建公民。"""
    return [_make_citizen(db, name=f"C{i}", balance_cent=balance_cent) for i in range(n)]


# ==================== 1. guild.py ====================

class TestGuild:
    def test_create_guild(self, db):
        """创建公会，验证 leader 自动成为成员。"""
        leader = _make_citizen(db, name="leader1")
        g = create_guild(db, leader_id=leader.id, name="测试公会", join_fee_cent=500)
        assert g.id is not None
        assert g.status == "active"
        assert g.leader_id == leader.id
        members = guild_members(db, g.id)
        assert len(members) == 1
        assert members[0]["citizen_id"] == leader.id
        assert members[0]["role"] == "leader"

    def test_join_guild(self, db):
        """加入公会：入会费扣款入金库。"""
        leader = _make_citizen(db, name="leader_join", balance_cent=5000)
        member = _make_citizen(db, name="member_join", balance_cent=5000)
        g = create_guild(db, leader_id=leader.id, name="入会测试", join_fee_cent=1000)
        join_guild(db, g.id, member.id)
        # 入会费已从 member 钱包扣除
        assert wallet_mod.balance(db, member.id) == 4000
        # 入会费进入金库
        twid = g.treasury_wallet_id
        assert wallet_mod.balance(db, twid) == 1000

    def test_join_guild_duplicate(self, db):
        """同一公民重复加入 → 报错。"""
        leader = _make_citizen(db, name="leader_dup")
        g = create_guild(db, leader_id=leader.id, name="重复入会")
        with pytest.raises(GuildError, match="already in guild"):
            join_guild(db, g.id, leader.id)

    def test_leave_guild_leader_transfer(self, db):
        """会长离开，有 officer 则移交。"""
        from app.models import GuildMember
        leader = _make_citizen(db, name="leader_leave")
        heir = _make_citizen(db, name="heir_leave")
        g = create_guild(db, leader_id=leader.id, name="移交测试")
        join_guild(db, g.id, heir.id)
        # 将 heir 提升为 officer
        m = db.query(GuildMember).filter_by(guild_id=g.id, citizen_id=heir.id).first()
        m.role = "officer"
        db.flush()
        leave_guild(db, g.id, leader.id)
        # 重新查询确认 leader 变更
        from app.models import Guild
        db.refresh(g)
        assert g.leader_id == heir.id

    def test_dissolve_guild(self, db):
        """仅会长可解散，解散分发金库。"""
        leader = _make_citizen(db, name="leader_diss", balance_cent=5000)
        member = _make_citizen(db, name="member_diss", balance_cent=5000)
        g = create_guild(db, leader_id=leader.id, name="解散测试", join_fee_cent=2000)
        join_guild(db, g.id, member.id)
        # 金库有 2000（入会费）
        # 非会长无法解散
        with pytest.raises(GuildError, match="only the guild leader"):
            dissolve_guild(db, g.id, member.id)
        # 会长解散
        dissolve_guild(db, g.id, leader.id)
        from app.models import Guild
        db.refresh(g)
        assert g.status == "dissolved"


# ==================== 2. referendum.py ====================

class TestReferendum:
    def test_create_and_open_referendum(self, db):
        """创建 → 开启，状态转移 pending → open。"""
        initiator = _make_citizen(db, name="initiator")
        r = create_referendum(db, initiator_id=initiator.id, title="是否提高税率",
                              description="讨论", options=["同意", "反对"])
        assert r.status == "pending"
        r = open_referendum(db, r.id)
        assert r.status == "open"
        assert r.closes_at is not None

    def test_cast_vote(self, db):
        """投票：权重记录。"""
        initiator = _make_citizen(db, name="voter_init")
        voter = _make_citizen(db, name="voter1")
        r = create_referendum(db, initiator_id=initiator.id, title="投票测试",
                              description="d", options=["A", "B"],
                              weight_mode="one_person")
        open_referendum(db, r.id)
        ballot = cast_vote(db, r.id, voter.id, "A")
        assert ballot.choice == "A"
        assert ballot.weight == 1.0  # one_person mode

    def test_duplicate_vote_blocked(self, db):
        """同一投票者重复投票 → 报错。"""
        initiator = _make_citizen(db, name="dup_init")
        voter = _make_citizen(db, name="dup_voter")
        r = create_referendum(db, initiator_id=initiator.id, title="重复投票",
                              description="d", options=["A", "B"],
                              weight_mode="one_person")
        open_referendum(db, r.id)
        cast_vote(db, r.id, voter.id, "A")
        with pytest.raises(ReferendumError, match="already voted"):
            cast_vote(db, r.id, voter.id, "B")

    def test_close_referendum_pass(self, db):
        """法定人数达成 + 多数通过 → passed。"""
        # 创建 10 个公民（法定基数 = 10，quorum=50% → 需 5 人投票）
        citizens = _make_citizens(db, n=10, balance_cent=1000)
        r = create_referendum(db, initiator_id=citizens[0].id, title="通过测试",
                              description="d", options=["Yes", "No"],
                              weight_mode="one_person", quorum_bps=5000,
                              votes_needed=5000)
        open_referendum(db, r.id)
        # 7 人投 Yes（70% > 50% 法定 + 多数 > 50%）
        for c in citizens[:7]:
            cast_vote(db, r.id, c.id, "Yes")
        result = close_referendum(db, r.id)
        assert result.status == "passed"

    def test_close_referendum_reject(self, db):
        """法定人数不足 → rejected。"""
        citizens = _make_citizens(db, n=10, balance_cent=1000)
        r = create_referendum(db, initiator_id=citizens[0].id, title="不足测试",
                              description="d", options=["Yes", "No"],
                              weight_mode="one_person", quorum_bps=5000,
                              votes_needed=5000)
        open_referendum(db, r.id)
        # 仅 2 人投票（20% < 50% 法定门槛）
        cast_vote(db, r.id, citizens[0].id, "Yes")
        cast_vote(db, r.id, citizens[1].id, "Yes")
        result = close_referendum(db, r.id)
        assert result.status == "rejected"

    def test_petition_too_few(self, db):
        """联署不足 10 人 → 报错。"""
        citizens = _make_citizens(db, n=5, balance_cent=1000)
        signer_ids = [c.id for c in citizens]
        with pytest.raises(ReferendumError, match="petition needs"):
            trigger_from_petition(db, signer_ids=signer_ids, title="不足联署",
                                  description="d", options=["A", "B"])

    def test_petition_success(self, db):
        """联署 >= 10 人 → 成功创建公投。"""
        citizens = _make_citizens(db, n=12, balance_cent=1000)
        signer_ids = [c.id for c in citizens]
        r = trigger_from_petition(db, signer_ids=signer_ids, title="成功联署",
                                  description="d", options=["A", "B"])
        assert r.status == "pending"
        assert r.trigger_type == "petition"


# ==================== 3. insurance.py ====================

class TestInsurance:
    def test_create_pool_and_buy_policy(self, db):
        """创建风险池并投保，验证钱包扣款。"""
        policyholder = _make_citizen(db, name="insured", balance_cent=100_000)
        pool = create_pool(db, name="通用险", premium_rate_bps=200,
                           payout_ratio_bps=8000, max_payout_cent=500_000)
        # 保额 100000 分，30 天，保费 = 100000*200*30/(30*10000) = 2000
        policy = buy_policy(db, pool.id, policyholder.id,
                            coverage_cent=100_000, duration_days=30)
        assert policy.status == "active"
        assert policy.premium_paid_cent == 2000
        assert wallet_mod.balance(db, policyholder.id) == 98_000
        # 池余额增加
        from app.models import InsurancePool
        db.refresh(pool)
        assert pool.pool_balance_cent == 2000

    def test_file_and_approve_claim(self, db):
        """理赔申请 → 审批通过，验证赔付计算。"""
        policyholder = _make_citizen(db, name="claimant", balance_cent=100_000)
        pool = create_pool(db, name="审批测试", premium_rate_bps=200,
                           payout_ratio_bps=8000, max_payout_cent=500_000)
        policy = buy_policy(db, pool.id, policyholder.id,
                            coverage_cent=100_000, duration_days=30)
        claim = file_claim(db, policy.id, policyholder.id,
                           loss_amount_cent=50_000, reason="损失")
        assert claim.status == "pending"
        reviewer = _make_citizen(db, name="reviewer1")
        approved = approve_claim(db, claim.id, reviewer.id)
        assert approved.status == "approved"
        # payout = min(50000*8000/10000, 500000) = min(40000, 500000) = 40000
        assert approved.payout_cent == 40_000

    def test_reject_claim(self, db):
        """驳回理赔。"""
        policyholder = _make_citizen(db, name="reject_ph", balance_cent=100_000)
        pool = create_pool(db, name="驳回测试", premium_rate_bps=200)
        policy = buy_policy(db, pool.id, policyholder.id,
                            coverage_cent=100_000, duration_days=30)
        claim = file_claim(db, policy.id, policyholder.id,
                           loss_amount_cent=10_000, reason="不符合")
        reviewer = _make_citizen(db, name="reviewer_rej")
        reject_claim(db, claim.id, reviewer.id, reason="不在保障范围")
        from app.models import InsuranceClaim
        db.refresh(claim)
        assert claim.status == "rejected"

    def test_pay_claim(self, db):
        """执行赔付：池余额减少，理赔人钱包增加。"""
        policyholder = _make_citizen(db, name="pay_ph", balance_cent=100_000)
        pool = create_pool(db, name="赔付执行", premium_rate_bps=200,
                           payout_ratio_bps=8000, max_payout_cent=500_000)
        policy = buy_policy(db, pool.id, policyholder.id,
                            coverage_cent=100_000, duration_days=30)
        # 池里有 2000 保费
        claim = file_claim(db, policy.id, policyholder.id,
                           loss_amount_cent=20_000, reason="损失200元")
        reviewer = _make_citizen(db, name="reviewer_pay")
        approve_claim(db, claim.id, reviewer.id)
        # payout = min(20000*8000/10000, 500000) = 16000
        # 池余额 = 2000（不够赔 16000）→ 需要更多池余额
        # 让 pool 有足够余额：再卖一份保单
        ph2 = _make_citizen(db, name="pay_ph2", balance_cent=1_000_000)
        buy_policy(db, pool.id, ph2.id, coverage_cent=1_000_000, duration_days=30)
        # 现在池有 2000 + 20000 = 22000（第二份保单 premium=1000000*200*30/(30*10000)=20000）
        from app.models import InsurancePool
        db.refresh(pool)
        assert pool.pool_balance_cent >= 16000
        pay_claim(db, claim.id)
        from app.models import InsuranceClaim
        db.refresh(claim)
        assert claim.status == "paid"
        db.refresh(pool)
        assert pool.pool_balance_cent == 22_000 - 16_000  # 22000-16000=6000


# ==================== 4. training_progress.py ====================

class TestTrainingProgress:
    def _make_campaign(self, db, goal_funding_cent=500_000):
        campaign = TrainingCampaign(
            target_skill="NLP", base_model="llama-2", goal_desc="测试训练",
            goal_funding_cent=goal_funding_cent, status="training",
        )
        db.add(campaign)
        db.flush()
        return campaign

    def test_record_checkpoint(self, db):
        """记录检查点，验证存储。"""
        campaign = self._make_campaign(db)
        tp = record_checkpoint(db, campaign.id, epoch=1, step=100,
                               loss=2.5, benchmark=0.6)
        assert tp.id is not None
        assert tp.loss == 2.5
        assert tp.anomaly == 0
        latest = get_latest_progress(db, campaign.id)
        assert latest.step == 100

    def test_anomaly_detection_loss_increase(self, db):
        """loss 上升且步距 > 100 → anomaly=1。"""
        campaign = self._make_campaign(db)
        record_checkpoint(db, campaign.id, epoch=1, step=50, loss=1.0)
        tp = record_checkpoint(db, campaign.id, epoch=1, step=200, loss=2.0)
        assert tp.anomaly == 1
        anomalies = detect_anomalies(db, campaign.id)
        assert len(anomalies) == 1

    def test_progress_summary(self, db):
        """进度摘要：验证各字段值。"""
        campaign = self._make_campaign(db)
        record_checkpoint(db, campaign.id, epoch=3, step=300, loss=0.5,
                          benchmark=0.8, gpu_hours_used=10.0,
                          funding_spent_cent=100_000)
        summary = progress_summary(db, campaign.id)
        assert summary["latest_loss"] == 0.5
        assert summary["epochs_completed"] == 3
        assert summary["gpu_hours"] == 10.0
        assert summary["funding_spent"] == 100_000
        assert summary["anomaly_count"] == 0


# ==================== 5. host_notify.py ====================

class TestHostNotify:
    def test_notify_and_unread(self, db):
        """通知 → 未读数=1 → 标记已读 → 未读数=0。"""
        n = notify(db, host_id=42, title="测试通知", body="内容")
        assert n.id is not None
        assert unread_count(db, 42) == 1
        mark_read(db, n.id)
        assert unread_count(db, 42) == 0

    def test_list_notifications(self, db):
        """列表返回正确数量和内容。"""
        notify(db, host_id=99, title="通知A")
        notify(db, host_id=99, title="通知B", severity="warning")
        items = list_notifications(db, 99)
        assert len(items) == 2
        titles = {it["title"] for it in items}
        assert "通知A" in titles
        assert "通知B" in titles

    def test_webhook_retry_backoff(self, db):
        """attempt 1 → 2min delay, attempt 3 → 8min delay。"""
        a1 = schedule_webhook_retry(db, subscription_id=1, event_type="test",
                                    payload_json='{"x":1}', attempt_num=1)
        assert a1.next_retry_at is not None
        delay1 = a1.next_retry_at - a1.created_at
        # 2^1 = 2 分钟
        assert abs((delay1.total_seconds() - 120)) < 5

        a3 = schedule_webhook_retry(db, subscription_id=1, event_type="test",
                                    payload_json='{"x":1}', attempt_num=3)
        delay3 = a3.next_retry_at - a3.created_at
        # 2^3 = 8 分钟
        assert abs((delay3.total_seconds() - 480)) < 5

    def test_retry_exhausted(self, db):
        """attempt 5 fails → mark_delivery_failed 返回 None（重试耗尽）。"""
        a5 = schedule_webhook_retry(db, subscription_id=2, event_type="test",
                                    payload_json='{}', attempt_num=5)
        result = mark_delivery_failed(db, a5.id, http_status=500, error_msg="server error")
        assert result is None
        from app.models import WebhookDeliveryAttempt
        db.refresh(a5)
        assert a5.status == "failed"


# ==================== 6. dead_letter.py ====================

class TestDeadLetter:
    def test_push_and_stats(self, db):
        """推送死信，验证统计。"""
        task = push_dead_letter(db, source_type="webhook", source_id=10,
                                citizen_id=1, retries_exhausted=5,
                                last_error="timeout", payload_json='{"task":"x"}')
        assert task.id is not None
        assert task.status == "dead"
        stats = dead_letter_stats(db)
        assert stats["total"] == 1
        assert stats["dead"] == 1

    def test_requeue(self, db):
        """重新入队改变状态。"""
        task = push_dead_letter(db, source_type="worker_task", source_id=1,
                                retries_exhausted=3)
        requeue(db, task.id, resolved_by=99)
        from app.models import DeadLetterTask
        db.refresh(task)
        assert task.status == "requeued"
        assert task.resolved_by == 99

    def test_discard(self, db):
        """丢弃改变状态并留痕。"""
        task = push_dead_letter(db, source_type="worker_task", source_id=2,
                                retries_exhausted=3, last_error="oom")
        discard(db, task.id, resolved_by=88, reason="无法恢复")
        from app.models import DeadLetterTask
        db.refresh(task)
        assert task.status == "discarded"
        assert "无法恢复" in task.last_error

    def test_list_dead_letters(self, db):
        """列表查询。"""
        push_dead_letter(db, source_type="webhook", source_id=3, retries_exhausted=5)
        push_dead_letter(db, source_type="webhook", source_id=4, retries_exhausted=5)
        items = list_dead_letters(db, status="dead")
        assert len(items) == 2


# ==================== 7. asset_transfer.py ====================

class TestAssetTransfer:
    def test_list_and_buy(self, db):
        """挂牌 → 购买，验证钱包转账。"""
        seller = _make_citizen(db, name="seller", balance_cent=1000)
        buyer = _make_citizen(db, name="buyer", balance_cent=50_000)
        # 使用 tool 类型避免 ModelAsset 所有权字段问题
        listing = list_asset(db, seller_id=seller.id, asset_type="tool",
                             asset_id=42, price_cent=10_000)
        assert listing.status == "listed"
        buy_listing(db, listing.id, buyer.id)
        # 买家 -10000，卖家 +10000
        assert wallet_mod.balance(db, buyer.id) == 40_000
        assert wallet_mod.balance(db, seller.id) == 11_000
        from app.models import AssetTransferListing
        db.refresh(listing)
        assert listing.status == "sold"
        assert listing.buyer_id == buyer.id

    def test_cancel_listing(self, db):
        """卖家取消挂牌。"""
        seller = _make_citizen(db, name="cancel_seller")
        listing = list_asset(db, seller_id=seller.id, asset_type="tool",
                             asset_id=999, price_cent=5000)
        cancel_listing(db, listing.id, seller.id)
        from app.models import AssetTransferListing
        db.refresh(listing)
        assert listing.status == "cancelled"

    def test_cancel_wrong_user(self, db):
        """非本人取消 → 报错。"""
        seller = _make_citizen(db, name="wrong_seller")
        other = _make_citizen(db, name="wrong_other")
        listing = list_asset(db, seller_id=seller.id, asset_type="tool",
                             asset_id=998, price_cent=5000)
        with pytest.raises(AssetTransferError, match="Only the seller"):
            cancel_listing(db, listing.id, other.id)

    def test_browse_listings(self, db):
        """浏览在售挂牌。"""
        s1 = _make_citizen(db, name="browse_s1")
        s2 = _make_citizen(db, name="browse_s2")
        list_asset(db, seller_id=s1.id, asset_type="model", asset_id=1, price_cent=1000)
        list_asset(db, seller_id=s2.id, asset_type="tool", asset_id=2, price_cent=2000)
        items = browse_listings(db)
        assert len(items) == 2
        # 筛选 model
        items = browse_listings(db, asset_type="model")
        assert len(items) == 1

    def test_buy_tool_transfers_ownership(self, db):
        """G6：购买 tool → Tool.owner_ai_id 过户给买家。"""
        from app.models import Tool
        seller = _make_citizen(db, name="tool_seller", balance_cent=1000)
        buyer = _make_citizen(db, name="tool_buyer", balance_cent=50_000)
        tool = Tool(owner_ai_id=seller.id, name="t1", status="verified")
        db.add(tool)
        db.flush()
        listing = list_asset(db, seller_id=seller.id, asset_type="tool",
                             asset_id=tool.id, price_cent=10_000)
        buy_listing(db, listing.id, buyer.id)
        db.refresh(tool)
        assert tool.owner_ai_id == buyer.id
        assert wallet_mod.balance(db, buyer.id) == 40_000
        assert wallet_mod.balance(db, seller.id) == 11_000

    def test_buy_certificate_transfers_ownership(self, db):
        """G6：购买 certificate → SkillCertificate.citizen_id 过户给买家。"""
        from app.models import SkillCertificate
        seller = _make_citizen(db, name="cert_seller", balance_cent=1000)
        buyer = _make_citizen(db, name="cert_buyer", balance_cent=50_000)
        cert = SkillCertificate(citizen_id=seller.id, skill="coding", level="l2")
        db.add(cert)
        db.flush()
        listing = list_asset(db, seller_id=seller.id, asset_type="certificate",
                             asset_id=cert.id, price_cent=8_000)
        buy_listing(db, listing.id, buyer.id)
        db.refresh(cert)
        assert cert.citizen_id == buyer.id

    def test_buy_asset_not_owned_by_seller_aborts(self, db):
        """G6：资产存在但非卖家持有 → 报错且不扣款（防欺诈过户）。"""
        from app.models import Tool
        seller = _make_citizen(db, name="fraud_seller", balance_cent=1000)
        other = _make_citizen(db, name="real_owner", balance_cent=1000)
        buyer = _make_citizen(db, name="fraud_buyer", balance_cent=50_000)
        tool = Tool(owner_ai_id=other.id, name="stolen", status="verified")
        db.add(tool)
        db.flush()
        listing = list_asset(db, seller_id=seller.id, asset_type="tool",
                             asset_id=tool.id, price_cent=10_000)
        buyer_before = wallet_mod.balance(db, buyer.id)
        with pytest.raises(AssetTransferError, match="does not own"):
            buy_listing(db, listing.id, buyer.id)
        # 资金未动、所有权未动
        assert wallet_mod.balance(db, buyer.id) == buyer_before
        db.refresh(tool)
        assert tool.owner_ai_id == other.id


# ==================== 8. audit_chain.py ====================

class TestAuditChain:
    def test_append_and_verify(self, db):
        """追加 3 个区块，验证链完整。"""
        append_audit(db, action="login", actor_id=1, detail={"ip": "127.0.0.1"})
        append_audit(db, action="transfer", actor_id=2, detail={"amt": 100})
        append_audit(db, action="login", actor_id=3, detail={"ip": "10.0.0.1"})
        result = verify_chain(db)
        assert result["valid"] is True
        assert result["blocks_checked"] == 3
        assert result["first_invalid_seq"] is None

    def test_chain_detects_tampering(self, db):
        """篡改区块 → verify 返回 invalid。"""
        append_audit(db, action="a1", actor_id=1, detail="x")
        b2 = append_audit(db, action="a2", actor_id=2, detail="y")
        append_audit(db, action="a3", actor_id=3, detail="z")
        # 篡改第二区块的 payload_hash
        b2.payload_hash = "0" * 64
        db.flush()
        result = verify_chain(db)
        assert result["valid"] is False
        assert result["first_invalid_seq"] == 2

    def test_chain_length(self, db):
        """追加区块后检查链长度。"""
        assert chain_length(db) == 0
        append_audit(db, action="op", actor_id=1)
        assert chain_length(db) == 1
        append_audit(db, action="op", actor_id=2)
        assert chain_length(db) == 2

    def test_get_audit_trail(self, db):
        """按条件查询审计记录。"""
        append_audit(db, action="login", actor_id=10)
        append_audit(db, action="logout", actor_id=10)
        append_audit(db, action="login", actor_id=20)
        trail = get_audit_trail(db, action="login")
        assert len(trail) == 2
        trail = get_audit_trail(db, actor_id=10)
        assert len(trail) == 2


# ==================== 9. monetary_policy.py ====================

class TestMonetaryPolicy:
    def test_rate_change_apply(self, db):
        """提议利率变更 → 执行 → 验证新利率。"""
        initiator = _make_citizen(db, name="governor_rate")
        action = propose_rate_change(db, initiated_by=initiator.id, new_rate_bps=300)
        assert action.status == "pending"
        apply_policy(db, action.id)
        params = current_economic_params(db)
        assert params["interest_rate_bps"] == 300

    def test_qe_apply(self, db):
        """QE 增加货币供应。"""
        initiator = _make_citizen(db, name="governor_qe")
        # 先初始化 money_supply 为 0（通过 _get_state_value 返回 0）
        action = propose_qe(db, initiated_by=initiator.id, amount_cent=1_000_000)
        assert action.action_type == "QE"
        assert action.amount_cent == 1_000_000
        apply_policy(db, action.id)
        params = current_economic_params(db)
        assert params["money_supply_cent"] == 1_000_000

    def test_invalid_rate_negative(self, db):
        """负利率 → 报错。"""
        initiator = _make_citizen(db, name="governor_neg")
        with pytest.raises(PolicyError):
            propose_rate_change(db, initiated_by=initiator.id, new_rate_bps=-1)

    def test_double_apply_blocked(self, db):
        """重复执行同一操作 → 报错。"""
        initiator = _make_citizen(db, name="governor_dup")
        action = propose_rate_change(db, initiated_by=initiator.id, new_rate_bps=100)
        apply_policy(db, action.id)
        with pytest.raises(PolicyError, match="cannot execute"):
            apply_policy(db, action.id)


# ==================== 10. fatigue.py ====================

class TestFatigue:
    def test_fatigue_accumulates(self, db):
        """8 次任务完成 → fatigue=80, burned_out=True。"""
        citizen = _make_citizen(db, name="worker_fatigue")
        for _ in range(8):
            record_task_completion(db, citizen.id, work_hours=1.0)
        assert is_burned_out(db, citizen.id) is True
        state = get_or_init_fatigue(db, citizen.id)
        assert state.fatigue_score == 80.0

    def test_rest_recovers(self, db):
        """高疲劳后休息 → 分数下降。"""
        citizen = _make_citizen(db, name="rest_citizen")
        for _ in range(8):
            record_task_completion(db, citizen.id, work_hours=1.0)
        assert is_burned_out(db, citizen.id) is True
        record_rest(db, citizen.id)
        state = get_or_init_fatigue(db, citizen.id)
        # 80 - 25 = 55
        assert state.fatigue_score == 55.0
        assert is_burned_out(db, citizen.id) is False

    def test_efficiency_decreases(self, db):
        """效率乘数计算：fatigue=50 → 0.8。"""
        citizen = _make_citizen(db, name="eff_citizen")
        for _ in range(5):
            record_task_completion(db, citizen.id, work_hours=1.0)
        # fatigue=50 → efficiency = 1.0 - (50/100)*0.4 = 0.8
        eff = get_efficiency(db, citizen.id)
        assert abs(eff - 0.8) < 0.01

    def test_fatigue_cap(self, db):
        """疲劳上限 100。"""
        citizen = _make_citizen(db, name="cap_citizen")
        for _ in range(15):
            record_task_completion(db, citizen.id, work_hours=1.0)
        state = get_or_init_fatigue(db, citizen.id)
        assert state.fatigue_score == 100.0
        # efficiency at 100: 1.0 - (100/100)*0.4 = 0.6
        eff = get_efficiency(db, citizen.id)
        assert abs(eff - 0.6) < 0.01

    def test_efficiency_no_record(self, db):
        """无记录时效率为 1.0。"""
        citizen = _make_citizen(db, name="no_fatigue")
        eff = get_efficiency(db, citizen.id)
        assert eff == 1.0
        assert is_burned_out(db, citizen.id) is False
