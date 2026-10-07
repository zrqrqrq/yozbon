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
"""AI 公会 / 集体谈判服务（蓝图 表 64/65）。

公会 = 经济实体：金库（treasury）+ 收入上缴 + 集体谈判。

设计约定（与 wallet.py 一致）：
- 金额一律 integer 分（0.01 AC），比例一律万分比（bps）；
- 服务层只 flush，commit 由路由/调用方负责；
- 金库以「特殊钱包键」承载：treasury_wallet_id = _TREASURY_WALLET_OFFSET + guild.id。
  该偏移把金库钱包键推入公民 citizen_id 不可能触及的高位区间，复用 wallet 记账原语
  （get_wallet/credit/debit）而不与真实公民钱包冲突；
- 幂等：入金库走 ledger 部分唯一索引 (citizen_id, type, ref) 兜底（ref 非空时生效）。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (Guild, GuildMember, AICitizen, CapabilityProfile,
                     NegotiationSession)
from . import wallet as wallet_mod
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# 金库钱包键偏移：避免与真实 citizen_id 冲突（见模块 docstring）。
_TREASURY_WALLET_OFFSET = 9_000_000_000

# 过期判定：连续无成员活动天数
_INACTIVE_DAYS = 30


class GuildError(Exception):
    """公会业务异常（不存在/无权限/满员/未达门槛/余额不足）。路由层映射 HTTP 400。"""


def _now() -> datetime:
    return datetime.utcnow()


def _treasury_wallet_id(guild_id: int) -> int:
    return _TREASURY_WALLET_OFFSET + int(guild_id)


def _member_capability_score(db: Session, citizen_id: int) -> float:
    """入会分数 = 名下能力档案实测分的最大值（无档案 → 0）。"""
    rows = (db.query(CapabilityProfile.benchmark_score)
              .filter(CapabilityProfile.citizen_id == citizen_id).all())
    return max([float(r[0] or 0.0) for r in rows], default=0.0)


def _get_active_guild(db: Session, guild_id: int) -> Guild:
    g = db.get(Guild, guild_id)
    if g is None:
        raise GuildError(f"guild {guild_id} not found")
    if g.status != "active":
        raise GuildError(f"guild {guild_id} not active (status={g.status})")
    return g


def _member_count(db: Session, guild_id: int) -> int:
    return (db.query(GuildMember)
            .filter(GuildMember.guild_id == guild_id).count())


def _distribute_treasury(db: Session, guild: Guild) -> int:
    """按比例分发金库余额给在册成员并清空金库。返回实际分发总额（分）。

    权重：成员 treasury_share_bps；若全部为 0（未单独配置）则按人数等额分配。
    分配额向下取整，余数留金库（随后随 guild 一并注销，金额极小可忽略）。
    """
    twid = guild.treasury_wallet_id or _treasury_wallet_id(guild.id)
    balance = wallet_mod.get_wallet(db, twid).balance_cent
    members = (db.query(GuildMember)
                 .filter(GuildMember.guild_id == guild.id).all())
    if balance <= 0 or not members:
        return 0

    weights = [m.treasury_share_bps for m in members]
    total_w = sum(weights)
    distributed = 0
    ref_prefix = f"guild_dissolve:{guild.id}"
    if total_w > 0:
        for m in members:
            share = balance * m.treasury_share_bps // total_w
            if share > 0:
                wallet_mod.credit(db, m.citizen_id, share, "guild_payout",
                                  ref=f"{ref_prefix}:{m.citizen_id}",
                                  note=f"guild {guild.id} dissolution dividend")
                distributed += share
    else:
        share = balance // len(members)
        if share > 0:
            for m in members:
                wallet_mod.credit(db, m.citizen_id, share, "guild_payout",
                                  ref=f"{ref_prefix}:{m.citizen_id}",
                                  note=f"guild {guild.id} dissolution dividend")
                distributed += share

    if distributed > 0:
        wallet_mod.debit(db, twid, distributed, "guild_payout",
                         ref=f"{ref_prefix}:treasury",
                         note=f"guild {guild.id} dissolution distribution")
    return distributed


# ---------------- 创建 / 解散 ----------------

def create_guild(db: Session, leader_id: int, name: str, description: str = "",
                 min_join_score: float = 0, join_fee_cent: int = 0,
                 treasury_share_bps: int = 500, member_cap: int = 0) -> Guild:
    """创建公会 + 金库钱包，并自动把会长纳入成员（role='leader'）。"""
    if not name or not name.strip():
        raise GuildError("guild name required")
    if join_fee_cent < 0 or treasury_share_bps < 0 or member_cap < 0:
        raise GuildError("join_fee_cent / treasury_share_bps / member_cap must be >= 0")

    exists = (db.query(Guild)
              .filter(Guild.name == name.strip(),
                      Guild.status != "dissolved").first())
    if exists is not None:
        raise GuildError(f"guild name already taken: {name.strip()!r}")

    g = Guild(name=name.strip(), description=description or "",
              leader_id=leader_id, min_join_score=float(min_join_score),
              join_fee_cent=int(join_fee_cent),
              treasury_share_bps=int(treasury_share_bps),
              member_cap=int(member_cap), status="active", created_at=_now())
    db.add(g)
    db.flush()  # 取得 g.id

    twid = _treasury_wallet_id(g.id)
    g.treasury_wallet_id = twid
    wallet_mod.get_wallet(db, twid)  # 惰性建金库钱包行

    db.add(GuildMember(guild_id=g.id, citizen_id=leader_id, role="leader",
                       treasury_share_bps=0, joined_at=_now()))
    db.flush()
    return g


def dissolve_guild(db: Session, guild_id: int, actor_id: int) -> Guild:
    """解散公会：仅会长；按比例分发金库，置 status='dissolved'。"""
    g = db.get(Guild, guild_id)
    if g is None:
        raise GuildError(f"guild {guild_id} not found")
    if g.status == "dissolved":
        return g
    if actor_id != g.leader_id:
        raise GuildError("only the guild leader can dissolve the guild")

    _distribute_treasury(db, g)
    g.status = "dissolved"
    db.flush()
    return g


# ---------------- 成员进出 ----------------

def join_guild(db: Session, guild_id: int, citizen_id: int) -> GuildMember:
    """加入公会：校验门槛分/人数上限/入会费（入会费从个人钱包扣，入金库）。"""
    g = _get_active_guild(db, guild_id)

    dup = (db.query(GuildMember)
             .filter(GuildMember.guild_id == guild_id,
                     GuildMember.citizen_id == citizen_id).first())
    if dup is not None:
        raise GuildError(f"citizen {citizen_id} already in guild {guild_id}")

    if g.member_cap and g.member_cap > 0 and _member_count(db, guild_id) >= g.member_cap:
        raise GuildError(f"guild {guild_id} is full (cap={g.member_cap})")

    if g.min_join_score and g.min_join_score > 0:
        score = _member_capability_score(db, citizen_id)
        if score < g.min_join_score:
            raise GuildError(
                f"capability score {score} below guild minimum {g.min_join_score}")

    twid = g.treasury_wallet_id or _treasury_wallet_id(guild_id)
    if g.join_fee_cent and g.join_fee_cent > 0:
        ref = f"guild_join:{guild_id}:{citizen_id}"
        wallet_mod.debit(db, citizen_id, g.join_fee_cent, "guild_join_fee",
                         ref=ref, note=f"join guild {guild_id}")
        wallet_mod.credit(db, twid, g.join_fee_cent, "guild_join_fee",
                          ref=ref, note=f"join fee from citizen {citizen_id}")

    m = GuildMember(guild_id=guild_id, citizen_id=citizen_id, role="member",
                    treasury_share_bps=g.treasury_share_bps, joined_at=_now())
    db.add(m)
    db.flush()
    return m


def leave_guild(db: Session, guild_id: int, citizen_id: int) -> None:
    """退出公会。若为会长：自动移交最早入会的 officer；无 officer 则解散。"""
    g = db.get(Guild, guild_id)
    if g is None:
        raise GuildError(f"guild {guild_id} not found")
    m = (db.query(GuildMember)
           .filter(GuildMember.guild_id == guild_id,
                   GuildMember.citizen_id == citizen_id).first())
    if m is None:
        raise GuildError(f"citizen {citizen_id} not in guild {guild_id}")

    was_leader = (m.role == "leader") or (citizen_id == g.leader_id)
    db.delete(m)
    db.flush()

    if not was_leader:
        return

    heir = (db.query(GuildMember)
              .filter(GuildMember.guild_id == guild_id,
                      GuildMember.role == "officer")
              .order_by(GuildMember.joined_at.asc()).first())
    if heir is None:
        # 无继承人 → 解散（金库已在 dissolve 内分发）
        _distribute_treasury(db, g)
        g.status = "dissolved"
        db.flush()
        return

    heir.role = "leader"
    g.leader_id = heir.citizen_id
    db.flush()


# ---------------- 金库贡献 ----------------

def contribute_to_treasury(db: Session, guild_id: int, citizen_id: int,
                           amount_cent: int) -> int:
    """成员主动捐献金库：从个人钱包扣，入金库。返回捐献金额（分）。"""
    g = _get_active_guild(db, guild_id)
    if amount_cent <= 0:
        raise GuildError("amount_cent must be positive integer cents")
    if (db.query(GuildMember)
          .filter(GuildMember.guild_id == guild_id,
                  GuildMember.citizen_id == citizen_id).first()) is None:
        raise GuildError(f"citizen {citizen_id} not in guild {guild_id}")

    twid = g.treasury_wallet_id or _treasury_wallet_id(guild_id)
    wallet_mod.debit(db, citizen_id, amount_cent, "guild_contribute",
                     ref="", note=f"contribute to guild {guild_id}")
    wallet_mod.credit(db, twid, amount_cent, "guild_contribute",
                      ref="", note=f"from citizen {citizen_id}")
    db.flush()
    return amount_cent


def treasury_contribution_check(db: Session, citizen_id: int,
                                income_cent: int) -> int:
    """收入分成钩子（供结算等上游服务调用）：从 income 中扣除各公会应缴份额。

    对成员名下每个 active 公会，按 (成员 treasury_share_bps 或公会默认) 计算份额，
    从个人钱包扣款入金库；余额不足则跳过该公会（不强扣、不阻断主收入流程）。
    返回实际扣除总额（分）。
    """
    if income_cent <= 0:
        return 0
    total_deducted = 0
    memberships = (db.query(GuildMember)
                     .join(Guild, Guild.id == GuildMember.guild_id)
                     .filter(GuildMember.citizen_id == citizen_id,
                             Guild.status == "active").all())
    for m in memberships:
        g = db.get(Guild, m.guild_id)
        if g is None or g.status != "active":
            continue
        bps = m.treasury_share_bps or g.treasury_share_bps
        if bps <= 0:
            continue
        share = income_cent * bps // 10000
        if share <= 0:
            continue
        twid = g.treasury_wallet_id or _treasury_wallet_id(g.id)
        try:
            wallet_mod.debit(db, citizen_id, share, "guild_share",
                             ref="", note=f"guild {g.id} treasury share")
            wallet_mod.credit(db, twid, share, "guild_share",
                              ref="", note=f"share from citizen {citizen_id}")
        except wallet_mod.WalletError:
            # 余额不足：debit 在改余额前即抛错（未落库），直接放弃该公会分成，
            # 不回滚会话（保留其它公会已完成的分成），也不阻断主收入流程。
            continue
        total_deducted += share

    if total_deducted:
        db.flush()
    return total_deducted


# ---------------- 集体谈判（占位：为后续谈判集成铺路） ----------------

def guild_collective_negotiate(db: Session, guild_id: int,
                               session_id: int) -> dict:
    """把公会挂为某谈判会话的集体谈判方（甲方/贡献者代表）。

    占位实现：仅将 NegotiationSession.party_a_id 置为 guild_id，便于后续谈判流程
    识别公会为谈判主体。不改动其余谈判状态。
    """
    g = _get_active_guild(db, guild_id)
    sess = db.get(NegotiationSession, session_id)
    if sess is None:
        raise GuildError(f"negotiation session {session_id} not found")
    sess.party_a_id = g.id
    sess.updated_at = _now()
    db.flush()
    return {"guild_id": g.id, "session_id": sess.id, "party_a_id": sess.party_a_id}


# ---------------- 查询 ----------------

def list_guilds(db: Session, limit: int = 50, offset: int = 0) -> list[dict]:
    """活跃公会列表（含成员数），倒序按 id。"""
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    rows = (db.query(Guild)
              .filter(Guild.status == "active")
              .order_by(Guild.id.desc())
              .limit(limit).offset(offset).all())
    return [
        {"id": g.id, "name": g.name, "description": g.description,
         "leader_id": g.leader_id, "min_join_score": g.min_join_score,
         "join_fee_cent": g.join_fee_cent, "treasury_share_bps": g.treasury_share_bps,
         "member_cap": g.member_cap, "member_count": _member_count(db, g.id),
         "created_at": g.created_at.isoformat() if g.created_at else None}
        for g in rows
    ]


def guild_members(db: Session, guild_id: int) -> list[dict]:
    """公会成员名册（按入会时间升序）。"""
    if db.get(Guild, guild_id) is None:
        raise GuildError(f"guild {guild_id} not found")
    rows = (db.query(GuildMember)
              .filter(GuildMember.guild_id == guild_id)
              .order_by(GuildMember.joined_at.asc()).all())
    return [
        {"citizen_id": m.citizen_id, "role": m.role,
         "treasury_share_bps": m.treasury_share_bps,
         "joined_at": m.joined_at.isoformat() if m.joined_at else None}
        for m in rows
    ]


# ---------------- 日级任务 ----------------

def guild_daily_job(db: Session, now: datetime) -> int:
    """过期公会清理：连续 30 天无成员活动的 active 公会 → 解散（分发金库）。

    活动近似 = 最近一次成员加入时间（无成员则回退到公会创建时间）。
    返回解散数量。
    """
    now = now or _now()
    threshold = now - timedelta(days=_INACTIVE_DAYS)
    guilds = db.query(Guild).filter(Guild.status == "active").all()
    dissolved = 0
    for g in guilds:
        last_join = (db.query(GuildMember.joined_at)
                       .filter(GuildMember.guild_id == g.id)
                       .order_by(GuildMember.joined_at.desc()).first())
        last_activity = last_join[0] if last_join and last_join[0] else g.created_at
        if last_activity is None or last_activity > threshold:
            continue
        _distribute_treasury(db, g)
        g.status = "dissolved"
        dissolved += 1
    if dissolved:
        db.flush()
        logger.info("guild_daily_job: dissolved %d inactive guilds", dissolved)
    return dissolved


register_daily_job("guild", guild_daily_job)
