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
"""N18 邀请推荐奖励服务（社会功能扩展设计 §4 N18）。

模型：
- 宿主在 /api/host/invites 生成邀请码（Invite 行，code 唯一 uq_invite_code，inviter_id=宿主）。
- 受邀方使用码：
  * 注册新宿主（POST /api/host/register?invite_code=）→ invitee_id = 新宿主 id；
  * 创建新 AI（POST /api/host/ai?invite_code=）→ invitee_id = 新 AI id。
- 防羊毛（硬验收）：受邀方必须完成「首次真实任务（有结算的合约 accepted）」或
  「充值到账（ai_ledger type=充值）」后才结算奖励；批量空注册一律 pending 不发奖；
  码绑定唯一、同一受邀方只结算一次（status pending→accepted 一次性转移）。
- 结算入账：
  * 受邀方是宿主 → 推荐 bounty 入账 邀请宿主 的人类积分账户 system_state host_cred:<inviter_id>；
  * 受邀方是 AI  → 小额入驻 bootstrap 入账 受邀 AI 自己的 AI 钱包（wallet.credit，ref 幂等）。

结算触发（稳定方案）：
- event_bus 挂 contract.settled（escrow 已 emit）：受邀 AI 一旦结算即立即结算其奖励；
- register_daily_job 日级巡检：兜底「充值到账」条件（host.py topup 无事件）与事件遗漏。
禁止改 escrow.py 本体（事件现成）；topup 不改（走日巡检）。
"""
import json
import logging
import secrets

from sqlalchemy.orm import Session

from . import wallet
from .event_bus import register_handler
from .models import (AICitizen, AILedger, AuditLog, Contract, Host, Invite)
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# 奖励面额（分）——MVP 起点，治理可调
HOST_BOUNTY_CRED = 2000      # 邀请宿主：推荐 bounty（人类积分，20 积分）
AI_BOOTSTRAP_CENT = 500      # 邀请 AI：入驻 bootstrap（5 AC 到受邀 AI 钱包）


def _audit(db: Session, actor_type: str, actor_id: int, action: str, detail: dict):
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id, action=action,
                    detail=json.dumps(detail, ensure_ascii=False)))


# ---------------- 生成 / 列表 / 绑定 ----------------
def create_invite(db: Session, host_id: int) -> Invite:
    for _ in range(5):
        code = secrets.token_urlsafe(8)[:10]
        if not db.query(Invite).filter(Invite.code == code).first():
            break
    inv = Invite(inviter_id=host_id, invitee_id=0, code=code,
                 status="pending", reward_credit=0)
    db.add(inv)
    db.flush()
    return inv


def list_invites(db: Session, host_id: int) -> list:
    rows = (db.query(Invite).filter(Invite.inviter_id == host_id)
            .order_by(Invite.id.desc()).all())
    return [{
        "code": r.code, "status": r.status, "invitee_id": r.invitee_id,
        "reward_credit": r.reward_credit,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


def bind_invite(db: Session, code: str, invitee_id: int) -> Invite | None:
    """注册/建 AI 时绑定邀请码：找到未绑定（invitee_id=0）的码，回填受邀方。

    找不到码 / 已绑定 / 自邀 → 返回 None（不阻断注册，邀请码可选）。
    """
    code = (code or "").strip()
    if not code:
        return None
    inv = db.query(Invite).filter(Invite.code == code).first()
    if inv is None:
        return None
    if inv.invitee_id and inv.invitee_id != invitee_id:
        return None          # 码已被别的受邀方绑定（一码一人）
    if inv.inviter_id == invitee_id:
        return None          # 自邀无意义
    inv.invitee_id = invitee_id
    db.flush()
    return inv


# ---------------- 结算条件判定 ----------------
def _condition_met(db: Session, inv: Invite) -> bool:
    iid = inv.invitee_id
    if not iid:
        return False
    ai = db.get(AICitizen, iid)
    host = db.get(Host, iid)
    if ai is not None:
        # 受邀 AI：有结算合约（worker）或有充值到账
        settled = (db.query(Contract)
                    .filter(Contract.worker_id == ai.id,
                            Contract.status == "accepted").first()) is not None
        if settled:
            return True
        topped = (db.query(AILedger)
                  .filter(AILedger.citizen_id == ai.id,
                          AILedger.type == "充值").first()) is not None
        return topped
    if host is not None:
        # 受邀宿主：名下任一 AI 有结算合约（真实经济活动）
        row = (db.query(Contract)
               .join(AICitizen, Contract.worker_id == AICitizen.id)
               .filter(AICitizen.host_id == host.id,
                       Contract.status == "accepted").first())
        return row is not None
    return False


def settle_pending(db: Session, only_invitee_id: int = 0) -> int:
    """扫描 pending 邀请，满足条件者一次性结算奖励。返回结算条数。"""
    q = db.query(Invite).filter(Invite.status == "pending", Invite.invitee_id != 0)
    if only_invitee_id:
        q = q.filter(Invite.invitee_id == only_invitee_id)
    settled = 0
    for inv in q.all():
        if not _condition_met(db, inv):
            continue
        ai = db.get(AICitizen, inv.invitee_id)
        try:
            if ai is not None:
                # 受邀 AI：小额 bootstrap 到其钱包（ref 幂等）
                inv.reward_credit = AI_BOOTSTRAP_CENT
                wallet.credit(db, ai.id, AI_BOOTSTRAP_CENT, "奖励",
                              ref=f"invite:{inv.id}",
                              note="invite bootstrap")
            else:
                # 受邀宿主：推荐 bounty 到邀请宿主积分账户
                inv.reward_credit = HOST_BOUNTY_CRED
                wallet.adjust_system_state(
                    db, f"host_cred:{inv.inviter_id}", HOST_BOUNTY_CRED,
                    ref=f"invite:{inv.id}")
        except wallet.WalletError as exc:
            logger.warning("invite reward skip invite=%s: %s", inv.id, exc)
            continue
        inv.status = "accepted"
        _audit(db, "system", 0, "invite.settled",
               {"invite_id": inv.id, "inviter": inv.inviter_id,
                "invitee": inv.invitee_id, "reward": inv.reward_credit})
        settled += 1
    db.flush()
    return settled


# ---------------- 事件 / 日巡检注册 ----------------
def _on_settled(db: Session, event_type: str, payload: dict) -> None:
    """contract.settled：受邀 AI 完成首次真实任务 → 立即结算其 pending 邀请。"""
    try:
        ai_id = payload.get("ai_id") or payload.get("worker_id")
        if ai_id:
            settle_pending(db, only_invitee_id=int(ai_id))
    except Exception:  # noqa: BLE001
        logger.exception("invites on_settled failed")


def _daily_inspect(db: Session, now) -> int:
    """日级巡检：兜底充值条件 + 事件遗漏。"""
    try:
        return settle_pending(db)
    except Exception:  # noqa: BLE001
        logger.exception("invites daily inspect failed")
        return 0


register_handler("contract.settled", _on_settled)
register_daily_job("invite_reward_settle", _daily_inspect)
