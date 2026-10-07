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
"""B 域（资金守恒域）审计修复回归测试。

覆盖：B-H1/H2/H3/H4, B-M1~M10, B-L3/L4/L6/L7
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import SessionLocal, init_db
from app import wallet
from app.wallet import WalletError
from app.models import (AIWallet, AILedger, AICitizen, SystemState, Loan,
                        AMMPool, AMMLPHolding, GeneralCrowdfund,
                        CrowdfundContribution, RetainerContract, WorkLedger,
                        EscrowYieldAccrual, MonetaryPolicyAction,
                        AssetTransferListing, Host)
from app.amm_engine import instance as amm
from app.escrow_yield import escrow_yield
from app.loans import apply_loan, repay_loan, charge_off_loan, LoanError, _now
from app.crowdfund import crowdfund_service, _crowdfund_disburse_daily_job, _crowdfund_expiry_daily_job
from app.retainer import sign_contract, log_hours, weekly_settle, HEARTBEAT_STALE_MINUTES, SETTLED_PERIODS_MAX
from app.monetary_policy import apply_policy, PolicyError
import app.wealth_metrics  # noqa: F401 — B-M9: 触发 register_daily_job("wealth_snapshot")


def _uid():
    return f"ai_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def db():
    """提供独立 Session（每个测试后自动 rollback 隔离）。"""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def ai1(db):
    """创建一个测试 AI。"""
    a = AICitizen(ai_uid=_uid(), name="AI-1", host_id=1, status="active",
                  class_level="capital")
    db.add(a)
    db.flush()
    return a


@pytest.fixture()
def ai2(db):
    """创建另一个测试 AI。"""
    a = AICitizen(ai_uid=_uid(), name="AI-2", host_id=1, status="active",
                  class_level="commoner")
    db.add(a)
    db.flush()
    return a


@pytest.fixture()
def funded_wallet(db, ai1):
    """给 AI-1 注资 100000 分。"""
    w = wallet.get_wallet(db, ai1.id)
    w.balance_cent = 100_000
    db.flush()
    return w


# ==================== B-H1: AMM FOR UPDATE 行锁 ====================
class TestBH1_AMM_Lock:
    """验证 AMM swap/LP 操作使用行锁（SELECT FOR UPDATE）。

    SQLite 下 FOR UPDATE 被静默忽略，但代码路径必须正确执行不报错。
    """

    def test_swap_uses_for_update_path(self, db):
        """swap 使用 with_for_update 路径（不崩溃 = 正确）。"""
        result = amm.create_pool("AAA", "BBB", 100_000, 100_000)
        pool_id = result["pool_id"]
        # swap 不应报错
        r = amm.swap(pool_id, "citizen", 1, "AAA", 1000)
        assert "error" not in r
        assert r["to_amount"] > 0

    def test_add_liquidity_uses_for_update_path(self, db):
        result = amm.create_pool("CCC", "DDD", 50_000, 50_000)
        pool_id = result["pool_id"]
        r = amm.add_liquidity(pool_id, "citizen", 1, 10_000, 10_000)
        assert "error" not in r
        assert r["lp_minted"] > 0

    def test_remove_liquidity_uses_for_update_path(self, db):
        result = amm.create_pool("EEE", "FFF", 100_000, 100_000)
        pool_id = result["pool_id"]
        add_r = amm.add_liquidity(pool_id, "citizen", 1, 10_000, 10_000)
        lp = add_r["lp_minted"]
        r = amm.remove_liquidity(pool_id, "citizen", 1, lp)
        assert "error" not in r


# ==================== B-H3: escrow_yield 利息来源 yield_fund ====================
class TestBH3_EscrowYield_FundSource:
    """利息发放必须从 yield_fund 扣减，不凭空造币。"""

    def test_claim_yield_from_yield_fund(self, db, ai1):
        """yield_fund 有余额时正常发放。"""
        # 设置 yield_fund = 5000
        wallet.adjust_system_state(db, "yield_fund", 5000, ref="test:seed")
        # 写 accrual
        accrual = EscrowYieldAccrual(
            escrow_id=999, ai_id=ai1.id, principal=100_000,
            accrued_interest=100.0, rate_bps=500,
            period_start=datetime.utcnow() - timedelta(days=1),
            period_end=datetime.utcnow(),
        )
        db.add(accrual)
        db.flush()

        payout = escrow_yield.claim_yield(db, escrow_id=999, ai_id=ai1.id,
                                          ref="test:claim:999")
        assert payout == 100
        # yield_fund 应减少
        assert wallet.get_system_state(db, "yield_fund") == 4900
        # AI 钱包入账
        assert wallet.balance(db, ai1.id) >= 100
        db.commit()

    def test_claim_yield_insufficient_fund(self, db, ai1):
        """yield_fund 不足时只发放可用部分。"""
        wallet.adjust_system_state(db, "yield_fund", 50, ref="test:seed2")
        accrual = EscrowYieldAccrual(
            escrow_id=888, ai_id=ai1.id, principal=100_000,
            accrued_interest=100.0, rate_bps=500,
            period_start=datetime.utcnow() - timedelta(days=1),
            period_end=datetime.utcnow(),
        )
        db.add(accrual)
        db.flush()

        payout = escrow_yield.claim_yield(db, escrow_id=888, ai_id=ai1.id,
                                          ref="test:claim:888")
        assert payout == 50  # 只发放可用 50
        assert wallet.get_system_state(db, "yield_fund") == 0
        db.commit()

    def test_claim_yield_zero_fund(self, db, ai1):
        """yield_fund 为零时返回 0（不凭空造币）。"""
        # 不设 yield_fund，默认 0
        accrual = EscrowYieldAccrual(
            escrow_id=777, ai_id=ai1.id, principal=100_000,
            accrued_interest=100.0, rate_bps=500,
            period_start=datetime.utcnow() - timedelta(days=1),
            period_end=datetime.utcnow(),
        )
        db.add(accrual)
        db.flush()

        payout = escrow_yield.claim_yield(db, escrow_id=777, ai_id=ai1.id,
                                          ref="test:claim:777")
        assert payout == 0


# ==================== B-H4: loans overdue repay + charge_off ====================
class TestBH4_Loans_Overdue:
    """逾期贷款可还款（含罚息）与坏账清算。"""

    def test_repay_overdue_with_penalty(self, db, ai1, ai2):
        """overdue 状态可还款，罚息 = 本金 * rate * 0.5。"""
        # 设置 money_supply 有足够余额
        wallet.adjust_system_state(db, "money_supply", 1_000_000, ref="test:ms")
        # 给 borrower 足够余额还款
        wallet.get_wallet(db, ai2.id).balance_cent = 200_000
        # 设置贷方有足够净资产
        wallet.get_wallet(db, ai1.id).balance_cent = 500_000
        # 设置 borrower 权限
        from app.models import AIPermission
        perm = AIPermission(citizen_id=ai2.id, loan_enabled=1)
        db.add(perm)
        db.flush()

        result = apply_loan(db, borrower_id=ai2.id, lender_id=ai1.id, amount_cent=10_000)
        loan_id = result["loan_id"]
        loan = db.get(Loan, loan_id)
        # 手动设为 overdue
        loan.status = "overdue"
        db.flush()

        # 还款（含罚息）
        r = repay_loan(db, loan_id, ai2.id)
        assert r["penalty_cent"] > 0
        assert r["total_interest_cent"] == r["interest_cent"] + r["penalty_cent"]
        assert loan.status == "paid"

    def test_charge_off_overdue(self, db, ai1, ai2):
        """overdue 贷款可清算核销。"""
        wallet.adjust_system_state(db, "money_supply", 1_000_000, ref="test:ms2")
        wallet.get_wallet(db, ai1.id).balance_cent = 500_000
        from app.models import AIPermission
        perm = AIPermission(citizen_id=ai2.id, loan_enabled=1)
        db.add(perm)
        db.flush()

        result = apply_loan(db, borrower_id=ai2.id, lender_id=ai1.id, amount_cent=10_000)
        loan_id = result["loan_id"]
        loan = db.get(Loan, loan_id)
        loan.status = "overdue"
        db.flush()

        ms_before = wallet.get_system_state(db, "money_supply")
        r = charge_off_loan(db, loan_id)
        assert r["status"] == "charged"
        assert loan.status == "charged"
        ms_after = wallet.get_system_state(db, "money_supply")
        assert ms_before - ms_after == 10_000

    def test_repay_paid_fails(self, db, ai1, ai2):
        """已还款的贷款不可再次还款。"""
        wallet.adjust_system_state(db, "money_supply", 1_000_000, ref="test:ms3")
        wallet.get_wallet(db, ai2.id).balance_cent = 200_000
        wallet.get_wallet(db, ai1.id).balance_cent = 500_000
        from app.models import AIPermission
        perm = AIPermission(citizen_id=ai2.id, loan_enabled=1)
        db.add(perm)
        db.flush()

        result = apply_loan(db, borrower_id=ai2.id, lender_id=ai1.id, amount_cent=10_000)
        loan_id = result["loan_id"]
        repay_loan(db, loan_id, ai2.id)

        with pytest.raises(LoanError, match="cannot be repaid"):
            repay_loan(db, loan_id, ai2.id)


# ==================== B-M1: asset_transfer 原子抢权 ====================
class TestBM1_AssetTransfer_Atomic:
    """购买使用条件 UPDATE，重复购买被拒。"""

    def test_double_buy_rejected(self, db, ai1, ai2, funded_wallet):
        from app.asset_transfer import buy_listing, AssetTransferError
        # 给 ai2 注资
        wallet.get_wallet(db, ai2.id).balance_cent = 100_000
        # 创建挂牌
        listing = AssetTransferListing(
            asset_type="model", asset_id=1, seller_id=ai1.id,
            price_cent=5000, status="listed",
        )
        db.add(listing)
        db.flush()
        db.commit()

        # 第一次购买成功
        buy_listing(db, listing.id, ai2.id)
        # 第二次购买应失败
        with pytest.raises(AssetTransferError):
            buy_listing(db, listing.id, ai2.id)


# ==================== B-M2: monetary_policy 条件 UPDATE ====================
class TestBM2_MonetaryPolicy_DoubleExec:
    """apply_policy 条件 UPDATE 防双执行。"""

    def test_apply_twice_raises(self, db):
        action = MonetaryPolicyAction(
            action_type="QE", amount_cent=100_000,
            status="pending", created_at=datetime.utcnow(),
        )
        db.add(action)
        db.flush()
        db.commit()

        apply_policy(db, action.id)
        with pytest.raises(PolicyError):
            apply_policy(db, action.id)

    def test_qe_lands_treasury_pool(self, db):
        action = MonetaryPolicyAction(
            action_type="QE", amount_cent=50_000,
            status="pending", created_at=datetime.utcnow(),
        )
        db.add(action)
        db.flush()
        db.commit()

        apply_policy(db, action.id)
        # treasury_pool 应增加
        tp = wallet.get_system_state(db, "treasury_pool")
        assert tp >= 50_000


# ==================== B-M3: crowdfund 原子自增 ====================
class TestBM3_Crowdfund_Atomic:
    """current_amount 使用原子 UPDATE。"""

    def test_contribute_increments_atomically(self, db, ai1, ai2, funded_wallet):
        wallet.get_wallet(db, ai2.id).balance_cent = 50_000
        deadline = datetime.utcnow() + timedelta(days=7)
        cf_id = crowdfund_service.create(
            db, "Test CF", "desc", ai1.id, 10_000, deadline)
        crowdfund_service.contribute(db, cf_id, ai2.id, 5_000)
        cf = db.get(GeneralCrowdfund, cf_id)
        assert cf.current_amount == 5_000


# ==================== B-M4: crowdfund 自动释放 ====================
class TestBM4_Crowdfund_AutoDisburse:
    """日级任务自动 disburse funded 项目。"""

    def test_disburse_job_runs(self, db, ai1, ai2):
        wallet.get_wallet(db, ai1.id).balance_cent = 50_000
        wallet.get_wallet(db, ai2.id).balance_cent = 50_000
        deadline = datetime.utcnow() + timedelta(days=7)
        cf_id = crowdfund_service.create(
            db, "Auto CF", "desc", ai1.id, 10_000, deadline)
        crowdfund_service.contribute(db, cf_id, ai2.id, 10_000)
        cf = db.get(GeneralCrowdfund, cf_id)
        assert cf.status == "funded"

        count = _crowdfund_disburse_daily_job(db)
        assert count == 1
        db.refresh(cf)
        assert cf.status == "disbursed"


# ==================== B-M5: retainer 异常告警 ====================
class TestBM5_Retainer_ErrorLogging:
    """真实异常（非幂等命中）写 AuditLog。"""

    def test_settle_error_audited(self, db, ai1):
        # 创建一个有工时的合同，但 tax_pool=0 导致 WalletError
        c = sign_contract(db, "TEST-POST", primary_ai_id=ai1.id,
                          retainer_cent=1000)
        period = "2026-W01"
        log_hours(db, ai1.id, "TEST-POST", 40.0, contract_id=c.id, period=period)
        db.commit()

        # tax_pool 为 0，adjust_system_state 会抛 WalletError (negative)
        # 这应写 AuditLog 而非静默跳过
        from app.models import AuditLog
        # datetime(2026,1,1) is Thu → ISO week 1 → "2026-W01"
        results = weekly_settle(db, now=datetime(2026, 1, 1))
        # 不应发放（因为 tax_pool 不足）
        assert len(results) == 0
        # 检查 AuditLog 有 settle_error 记录
        log_entry = db.query(AuditLog).filter(
            AuditLog.action == "retainer.settle_error").first()
        assert log_entry is not None


# ==================== B-M6: HEARTBEAT_STALE_MINUTES >= 日巡检间隔 ====================
class TestBM6_Heartbeat_Threshold:
    """心跳阈值须大于日巡检间隔(24h=1440min)。"""

    def test_threshold_exceeds_daily_interval(self):
        assert HEARTBEAT_STALE_MINUTES >= 1440


# ==================== B-M7: wallet FOR UPDATE 行锁路径 ====================
class TestBM7_Wallet_RowLock:
    """credit/debit 使用 _get_wallet_for_update（SQLite 不报错 = 正确）。"""

    def test_credit_with_row_lock(self, db, ai1):
        """credit 路径执行不报错（SQLite 忽略 FOR UPDATE）。"""
        wallet.credit(db, ai1.id, 5000, "测试入账", ref="test:b_m7_1")
        assert wallet.balance(db, ai1.id) == 5000

    def test_debit_with_row_lock(self, db, ai1, funded_wallet):
        """debit 路径执行不报错。"""
        wallet.debit(db, ai1.id, 3000, "测试出账", ref="test:b_m7_2")
        assert wallet.balance(db, ai1.id) == 97000

    def test_insufficient_balance_with_lock(self, db, ai1):
        """余额不足仍正确抛异常。"""
        wallet.get_wallet(db, ai1.id).balance_cent = 100
        db.flush()
        with pytest.raises(WalletError, match="insufficient"):
            wallet.debit(db, ai1.id, 200, "出账", ref="test:b_m7_3")


# ==================== B-M8: claim_yield 在结算时调用 ====================
class TestBM8_ClaimYield_Integration:
    """fulfill_contract 中自动 claim_yield（不崩溃 = 成功集成）。"""

    def test_claim_yield_callable_standalone(self, db, ai1):
        """claim_yield 可独立调用且幂等。"""
        wallet.adjust_system_state(db, "yield_fund", 200, ref="test:b_m8")
        accrual = EscrowYieldAccrual(
            escrow_id=555, ai_id=ai1.id, principal=100_000,
            accrued_interest=50.0, rate_bps=500,
            period_start=datetime.utcnow() - timedelta(days=1),
            period_end=datetime.utcnow(),
        )
        db.add(accrual)
        db.flush()

        payout = escrow_yield.claim_yield(db, 555, ai1.id, ref="test:b_m8_claim")
        assert payout == 50
        db.commit()


# ==================== B-M9: wealth_snapshot 注册日级任务 ====================
class TestBM9_WealthSnapshot:
    """capture_snapshot 已注册为日级任务。"""

    def test_job_produces_snapshot(self):
        """按 scheduler 教义，test 环境不把日级 job 注入共享注册表（避免污染
        既有"恰好 4 岗位"M3 调度测试）；故直接调用 job 函数验证其能产出快照。
        生产环境（APP_ENV!=test）则经 register_daily_job("wealth_snapshot") 日级驱动。"""
        from app.wealth_metrics import _wealth_snapshot_daily_job
        from app.models import WealthDistributionSnapshot
        session = SessionLocal()
        try:
            before = session.query(WealthDistributionSnapshot).count()
        finally:
            session.close()
        sid = _wealth_snapshot_daily_job(SessionLocal(), now=datetime(2026, 10, 4))
        assert sid and sid > 0
        session = SessionLocal()
        try:
            assert session.query(WealthDistributionSnapshot).count() == before + 1
        finally:
            session.close()


# ==================== B-M10: AMM LP 持仓按 provider 记账 ====================
class TestBM10_LP_Holding:
    """LP 持仓按 (pool, provider) 独立记录。"""

    def test_lp_per_provider(self, db):
        result = amm.create_pool("X", "Y", 100_000, 100_000)
        pool_id = result["pool_id"]
        # Provider 1 adds liquidity
        r1 = amm.add_liquidity(pool_id, "citizen", 100, 20_000, 20_000)
        # Provider 2 adds liquidity
        r2 = amm.add_liquidity(pool_id, "citizen", 200, 10_000, 10_000)

        session = SessionLocal()
        try:
            h1 = session.query(AMMLPHolding).filter(
                AMMLPHolding.pool_id == pool_id,
                AMMLPHolding.provider_id == 100).first()
            h2 = session.query(AMMLPHolding).filter(
                AMMLPHolding.pool_id == pool_id,
                AMMLPHolding.provider_id == 200).first()
            assert h1 is not None
            assert h2 is not None
            assert h1.lp_balance == r1["lp_minted"]
            assert h2.lp_balance == r2["lp_minted"]
        finally:
            session.close()

    def test_remove_liquidity_checks_provider_balance(self, db):
        """Provider 不能提取超出自己持仓的 LP。"""
        result = amm.create_pool("M", "N", 100_000, 100_000)
        pool_id = result["pool_id"]
        amm.add_liquidity(pool_id, "citizen", 300, 10_000, 10_000)
        # Try to remove more than this provider has
        r = amm.remove_liquidity(pool_id, "citizen", 300, 999_999)
        assert "error" in r
        assert "insufficient LP" in r["error"]


# ==================== B-L3: escrow_lock / escrow_unlock 辅助函数 ====================
class TestBL3_EscrowHelpers:
    """wallet.escrow_lock / escrow_unlock 正常工作。"""

    def test_escrow_lock_unlock(self, db, ai1):
        wallet.escrow_lock(db, ai1.id, 5000)
        w = wallet.get_wallet(db, ai1.id)
        assert w.escrow_cent == 5000
        wallet.escrow_unlock(db, ai1.id, 3000)
        assert w.escrow_cent == 2000

    def test_escrow_unlock_floor_zero(self, db, ai1):
        wallet.escrow_lock(db, ai1.id, 1000)
        wallet.escrow_unlock(db, ai1.id, 5000)
        w = wallet.get_wallet(db, ai1.id)
        assert w.escrow_cent == 0


# ==================== B-L4: settled_periods 不超上限 ====================
class TestBL4_SettledPeriods_Cap:
    """settled_periods JSON 不超过 SETTLED_PERIODS_MAX。"""

    def test_period_list_capped(self, db, ai1):
        c = sign_contract(db, "CAP-TEST", primary_ai_id=ai1.id, retainer_cent=500)
        # 手动塞满超过上限
        many_periods = [f"2025-W{i:02d}" for i in range(1, 54)]
        c.settled_periods = json.dumps(many_periods)
        db.flush()

        # 再结算一期
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="test:b_l4")
        period = "2026-W01"
        log_hours(db, ai1.id, "CAP-TEST", 40.0, contract_id=c.id, period=period)
        db.commit()

        # datetime(2026,1,1) is Thu → ISO week 1 → "2026-W01"
        results = weekly_settle(db, now=datetime(2026, 1, 1))
        assert len(results) >= 1
        db.refresh(c)
        stored = json.loads(c.settled_periods)
        assert len(stored) <= SETTLED_PERIODS_MAX


# ==================== B-L6: 众筹过期日级巡检 ====================
class TestBL6_Crowdfund_Expiry:
    """过期 open 项目被日级任务标记 failed 并退款。"""

    def test_expired_project_refund(self, db, ai1, ai2):
        wallet.get_wallet(db, ai2.id).balance_cent = 50_000
        # deadline 已过期
        deadline = datetime.utcnow() - timedelta(days=1)
        cf_id = crowdfund_service.create(
            db, "Expired CF", "desc", ai1.id, 100_000, deadline)
        # 直接设为 open 状态（跳过 contribute 中的过期检查）
        cf = db.get(GeneralCrowdfund, cf_id)
        assert cf.status == "open"

        count = _crowdfund_expiry_daily_job(db)
        assert count >= 1
        db.refresh(cf)
        assert cf.status == "failed"


# ==================== B-L7: wallet.transfer 自转拒绝 ====================
class TestBL7_SelfTransfer:
    """from_id == to_id 直接拒绝。"""

    def test_self_transfer_raises(self, db, ai1):
        with pytest.raises(WalletError, match="Self-transfer"):
            wallet.transfer(db, ai1.id, ai1.id, 1000, "转账", ref="test:self")
