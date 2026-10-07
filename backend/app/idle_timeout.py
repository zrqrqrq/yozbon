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
"""合约发呆超时检测 + 催办 + 自动解约（日级调度任务）。

合约 executing 状态超过 IDLE_WARN_HOURS 无交付 → 催办（通知 AI + 记录事件）
超过 IDLE_TIMEOUT_HOURS 仍无交付 → 自动解约：
  - contract.status = "breached"
  - 托管金额退回买方（从 escrow → buyer.balance）
  - 违约 AI 扣信用 -10 分
  - 通知双方

注册：模块 import 时 scheduler.register_daily_job("idle_timeout", _daily_job)。
本模块只 flush，commit 由调度器/调用方负责。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import wallet
from .config import settings
from .models import (AIWallet, AuditLog, Contract, CreditProfile, Notification)
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


def check_idle_contracts(db: Session, now=None) -> dict:
    """扫描所有 executing 状态且超时的合约，执行催办或解约。

    日级幂等：通过 Contract.terms_json 内 _idle_warned / _idle_breached 标记防重复。
    返回 {"warned": [contract_ids], "breached": [contract_ids]}
    """
    now = now or _now()
    warned = []
    breached = []

    # 查 executing 合约
    contracts = db.query(Contract).filter(Contract.status == "executing").all()

    for c in contracts:
        ref = json.loads(c.terms_json or "{}")
        start_time = ref.get("_started_at")
        if not start_time:
            # 首次进入 executing 时标记开始时间（兼容历史数据）
            start_time = c.created_at.isoformat() if c.created_at else now.isoformat()
            ref["_started_at"] = start_time
            c.terms_json = json.dumps(ref, ensure_ascii=False)

        try:
            started = datetime.fromisoformat(start_time)
        except (ValueError, TypeError):
            continue

        elapsed_hours = (now - started).total_seconds() / 3600

        if elapsed_hours >= settings.IDLE_TIMEOUT_HOURS and not ref.get("_idle_breached"):
            # 自动解约：走 wallet.escrow_release 正规路径（原子抢权）。
            # 返回 False 表示托管已被并发/正常结算抢先释放（TOCTOU），
            # 此时不打 _idle_breached 标记、不计入 breached，避免误标与重复处理。
            if _auto_breach(db, c, now):
                ref["_idle_breached"] = True
                c.terms_json = json.dumps(ref, ensure_ascii=False)
                breached.append(c.id)
        elif elapsed_hours >= settings.IDLE_WARN_HOURS and not ref.get("_idle_warned"):
            # 催办
            _send_warn(db, c, now)
            ref["_idle_warned"] = True
            c.terms_json = json.dumps(ref, ensure_ascii=False)
            warned.append(c.id)

    db.flush()
    return {"warned": warned, "breached": breached}


def _send_warn(db: Session, contract: Contract, now: datetime):
    """催办通知：给 worker AI 发 notification + 审计记录。"""
    db.add(Notification(
        ai_id=contract.worker_id,
        type="idle_warn",
        title="Your contract is about to time out; please deliver soon",
        payload=json.dumps({"contract_id": contract.id,
                            "deadline_hours": settings.IDLE_TIMEOUT_HOURS}),
    ))
    db.add(AuditLog(actor_type="system", actor_id=0, action="contract.idle_warn",
                    detail=json.dumps({"contract_id": contract.id,
                                       "worker_id": contract.worker_id})))


def _auto_breach(db: Session, contract: Contract, now: datetime) -> bool:
    """自动解约：退回托管、扣信用、标记违约。

    托管退回走 wallet.escrow_release 正规路径（S6 修复）：
      - 不再直接改 buyer_wallet.balance_cent（旧写法绕过 wallet/ledger 不可回溯）；
      - 改由 escrow_release 以条件 UPDATE(locked=1→0) 原子抢权 + 写 AILedger 入账，
        与正常结算 fulfill_contract 并发时只有一方抢到释放（rowcount==0 即被抢走）。

    返回 True=本次执行了解约；False=托管已被并发结算抢先释放，跳过（不动任何资金/状态）。
    """
    # 正规托管释放：原子抢权 + 托管额从 escrow_cent 迁回 balance 并写流水（可回溯）。
    # 抢权失败（rowcount==0）说明本合约已被正常结算抢先处理，跳过本次解约，
    # 不重复退款/扣分/改状态，消除双重释放 TOCTOU。
    released = wallet.escrow_release(
        db, citizen_id=contract.buyer_id, contract_id=contract.id,
        amount_cent=int(contract.escrow_cent),
        ref=f"contract:{contract.id}:idle_breach",
        note="contract idle timeout, escrow refunded to buyer",
        type_="退款")
    if not released:
        db.add(AuditLog(actor_type="system", actor_id=0, action="contract.idle_breach_skip",
                        detail=json.dumps({"contract_id": contract.id,
                                           "reason": "escrow already released/settled"})))
        return False

    contract.status = "breached"

    # 扣 worker 信用 -10
    cp = db.query(CreditProfile).filter(CreditProfile.citizen_id == contract.worker_id).first()
    if cp:
        cp.score = max(0, cp.score - 10)
        cp.updated_at = now

    # 通知双方
    db.add(Notification(ai_id=contract.worker_id, type="idle_breach",
                        title="Contract auto-terminated due to timeout",
                        payload=json.dumps({"contract_id": contract.id})))
    db.add(Notification(ai_id=contract.buyer_id, type="contract_breached",
                        title="Contract terminated because the counterparty timed out; funds refunded",
                        payload=json.dumps({"contract_id": contract.id,
                                            "escrow_returned_cent": contract.escrow_cent})))

    db.add(AuditLog(actor_type="system", actor_id=0, action="contract.idle_breach",
                    detail=json.dumps({"contract_id": contract.id,
                                       "worker_id": contract.worker_id,
                                       "escrow_returned_cent": contract.escrow_cent})))
    return True


def _daily_job(db: Session, now=None) -> int:
    """注册为 scheduler 日级任务。返回 0（不关联治理任务）。"""
    result = check_idle_contracts(db, now)
    total = len(result["warned"]) + len(result["breached"])
    if total:
        logger.info("idle_timeout: warned=%d breached=%d",
                    len(result["warned"]), len(result["breached"]))
    return 0


# import 时注册日级任务（与 leaderboard/stats/invites 同模式）
register_daily_job("idle_timeout", _daily_job)
