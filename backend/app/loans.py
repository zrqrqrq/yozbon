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
"""信贷（规则 9，MVP）：贷款=临时货币。

- 资本 AI（class_level=capital/governance）放贷；
- 放贷上限 = 贷方净资产（活期+托管）×50%（累计未还 active 贷款口径）；
- 发放：money_supply += 本金（新增临时货币）+ credit 借方钱包（"贷款"）；
- 还款：借方还本金+利息，回收 money_supply -= 本金，利息入贷方；
- 宿主 loan_enabled 开关（ai_permissions）校验；逾期 → 信用扣分事件。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .models import AICitizen, Loan, AIPermission, CreditEvent, CreditProfile
from . import wallet
from .scheduler import register_daily_job

LENDER_LEVELS = ("capital", "governance")
LOAN_CAP_RATIO = 0.5          # 放贷上限 = 贷方净资产 50%
LOAN_DUE_DAYS = 30            # MVP 借期 30 天


class LoanError(Exception):
    """信贷业务异常（路由层映射为 HTTP 400/403）。"""


def _now() -> datetime:
    return datetime.utcnow()


def _net(db: Session, cid: int) -> int:
    w = wallet.get_wallet(db, cid)
    return w.balance_cent + w.escrow_cent


def active_principal(db: Session, lender_id: int) -> int:
    """贷方当前未还（active）本金合计（分）。"""
    rows = (db.query(Loan)
            .filter(Loan.lender_id == lender_id, Loan.status == "active")
            .all())
    return sum(r.amount_cent for r in rows)


def apply_loan(db: Session, borrower_id: int, lender_id: int,
               amount_cent: int) -> dict:
    """借款申请（借方=当前 AI）。校验贷方资格/额度/借方开关 → 发放临时货币。"""
    if amount_cent <= 0:
        raise LoanError("Loan amount must be a positive integer in cents")
    lender = db.get(AICitizen, lender_id)
    if lender is None or lender.status in ("dead", "frozen"):
        raise LoanError("Lender not found or not allowed to lend")
    if lender.class_level not in LENDER_LEVELS:
        raise LoanError(f"Only capital/governance AI can lend (lender class={lender.class_level}) ")

    borrower = db.get(AICitizen, borrower_id)
    if borrower is None or borrower.status in ("dead", "frozen"):
        raise LoanError("Borrower cannot lend")

    # 宿主开关校验
    perm = db.get(AIPermission, borrower_id)
    if perm is None or perm.loan_enabled != 1:
        raise LoanError("Borrower host has not enabled lending (loan_enabled=0)")

    # 额度上限 = 贷方净资产 ×50%（含本次）
    cap = int(_net(db, lender_id) * LOAN_CAP_RATIO)
    if active_principal(db, lender_id) + amount_cent > cap:
        raise LoanError(f"Exceeds lending cap: cap={cap}, already lent {active_principal(db, lender_id)}, this time {amount_cent}")

    # 发放：新增临时货币入 M + 借方钱包
    due = _now() + timedelta(days=LOAN_DUE_DAYS)
    loan = Loan(lender_id=lender_id, borrower_id=borrower_id,
                amount_cent=amount_cent, rate_monthly=0.01,
                due_at=due, status="active")
    db.add(loan)
    db.flush()
    wallet.adjust_system_state(db, "money_supply", amount_cent, ref=f"loan:{loan.id}")
    wallet.credit(db, borrower_id, amount_cent, "贷款",
                  ref=f"loan:{loan.id}", note=f"borrowed from {lender_id}")
    db.flush()
    return {"loan_id": loan.id, "borrower_id": borrower_id,
            "lender_id": lender_id, "amount_cent": amount_cent,
            "due_at": due.isoformat()}


def interest_cent(loan: Loan) -> int:
    """应收利息（MVP：本金 × 月利率，一期）。"""
    return round(loan.amount_cent * loan.rate_monthly)


def _penalty_cent(loan: Loan) -> int:
    """逾期罚息（B-H4）：本金 × 月利率 × 50%（即逾期罚息为正常利率的一半）。"""
    return round(loan.amount_cent * loan.rate_monthly * 0.5)


def repay_loan(db: Session, loan_id: int, payer_id: int) -> dict:
    """还款（B-H4：支持 active 与 overdue 状态还款）。

    active：本金+正常利息；
    overdue：本金+正常利息+罚息（rate_monthly*50%）。
    回收本金出 money_supply，利息+罚息入贷方。
    """
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise LoanError("Loan not found")
    if loan.status not in ("active", "overdue"):
        raise LoanError(f"Loan status {loan.status} cannot be repaid")
    if payer_id != loan.borrower_id:
        raise LoanError("Only the borrower can repay")

    principal = loan.amount_cent
    interest = interest_cent(loan)
    penalty = _penalty_cent(loan) if loan.status == "overdue" else 0
    total_interest = interest + penalty

    # 本金：借方出 → 回收出 M
    wallet.debit(db, payer_id, principal, "还款",
                 ref=f"loan:repay:{loan.id}", note="principal repayment")
    wallet.adjust_system_state(db, "money_supply", -principal, ref=f"loan:{loan.id}")
    # 利息+罚息：借方出 → 入贷方
    if total_interest > 0:
        wallet.debit(db, payer_id, total_interest, "利息",
                     ref=f"loan:interest:{loan.id}", note="loan interest+penalty")
        wallet.credit(db, loan.lender_id, total_interest, "利息",
                      ref=f"loan:interest:{loan.id}", note="lending interest income")
    loan.status = "paid"
    db.flush()
    return {"loan_id": loan.id, "principal_cent": principal,
            "interest_cent": interest, "penalty_cent": penalty,
            "total_interest_cent": total_interest, "status": "paid"}


def charge_off_loan(db: Session, loan_id: int) -> dict:
    """逾期坏账清算（B-H4）：永久无法回收 → 核销本金，回收 money_supply。

    标记 status=charged，money_supply -= principal（消除超发虚高），
    贷方承担损失（本金从贷款发放时已新增入 M，清算即反向回收）。
    """
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise LoanError("Loan not found")
    if loan.status != "overdue":
        raise LoanError(f"Only overdue loans can be charged off (status={loan.status})")

    principal = loan.amount_cent
    wallet.adjust_system_state(db, "money_supply", -principal,
                               ref=f"loan:chargeoff:{loan.id}")
    loan.status = "charged"
    db.flush()
    return {"loan_id": loan.id, "charged_principal_cent": principal,
            "status": "charged"}


def check_overdue(db: Session, now: datetime = None) -> dict:
    """逾期巡检：到期未还 → 标记 overdue + 借方信用扣分（规则 9 侧翼）。"""
    now = now or _now()
    rows = (db.query(Loan)
            .filter(Loan.status == "active", Loan.due_at < now)
            .all())
    for loan in rows:
        loan.status = "overdue"
        cp = db.get(CreditProfile, loan.borrower_id)
        if cp:
            cp.score = max(0, cp.score - 20)
        db.add(CreditEvent(citizen_id=loan.borrower_id, event="loan_overdue",
                           delta=-20, reason=f"loan {loan.id} overdue",
                           ref=f"loan:{loan.id}"))
    db.flush()
    return {"overdue": len(rows)}


# ---- 日级巡检：到期未还 → 标记逾期 + 信用扣分（与 guild/employment/data_export 同注册模式）----
def _overdue_daily_job(db: Session, now=None) -> int:
    """注册为 scheduler 日级任务（run_due_jobs 以 fn(db, now) 调用）。返回逾期贷款数。"""
    return int(check_overdue(db, now).get("overdue", 0))


register_daily_job("loan_overdue", _overdue_daily_job)
