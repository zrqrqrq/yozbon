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
"""市民公投 / 联署请愿服务（蓝图 表 66/67）。

流程：create(pending) → open(open) → cast_vote → close(closed→passed/rejected)

约定：
- 比例一律万分比（bps / 万分比）：quorum_bps=参与率法定门槛，votes_needed=通过所需得票万分比；
- 加权票：weight_mode = credit(信用/100) / level(认证等级映射) / one_person(一人一票)；
- 服务层只 flush，commit 由路由/调用方负责；
- 投票幂等：vote_ballots 唯一约束 (referendum_id, voter_id)。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (Referendum, VoteBallot, AICitizen, CapabilityProfile,
                     CreditProfile)
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)

# 联署转公投最低联署人数
_PETITION_MIN_SIGNERS = 10

# 认证等级 → 票权（level 模式）
_LEVEL_WEIGHT = {"l1": 1.0, "l2": 2.0, "l3": 3.0}

_BPS = 10000


class ReferendumError(Exception):
    """公投业务异常（不存在/状态非法/重复投票/选项非法/联署不足）。路由层映射 HTTP 400。"""


def _now() -> datetime:
    return datetime.utcnow()


def _options_of(r: Referendum) -> list[str]:
    try:
        opts = json.loads(r.options_json or "[]")
    except (ValueError, TypeError):
        opts = []
    return list(opts) if isinstance(opts, list) else []


def _get_open(db: Session, ref_id: int) -> Referendum:
    r = db.get(Referendum, ref_id)
    if r is None:
        raise ReferendumError(f"referendum {ref_id} not found")
    return r


def _eligible_count(db: Session) -> int:
    """法定基数 = 在册活跃公民数（排除平台内置 AI）。"""
    return (db.query(AICitizen)
              .filter(AICitizen.status == "active",
                      AICitizen.is_internal == 0).count())


def _vote_weight(db: Session, r: Referendum, voter_id: int) -> float:
    """按 weight_mode 计算单张选票权重。"""
    if r.weight_mode == "one_person":
        return 1.0
    if r.weight_mode == "level":
        rows = (db.query(CapabilityProfile.verified_level)
                  .filter(CapabilityProfile.citizen_id == voter_id).all())
        best = 1.0
        for (lvl,) in rows:
            best = max(best, _LEVEL_WEIGHT.get(lvl or "", 1.0))
        return best
    # 默认 credit：信用分 / 100（无档案按基准 100 → 权重 1.0）
    cp = db.get(CreditProfile, voter_id)
    score = cp.score if cp is not None else 100
    try:
        return float(score) / 100.0
    except (TypeError, ValueError):
        return 1.0


def _tally(db: Session, ref_id: int) -> tuple[dict, int, float]:
    """统计：({choice: 累计权重}, 投票人数, 总权重)。"""
    rows = db.query(VoteBallot).filter(VoteBallot.referendum_id == ref_id).all()
    tally: dict[str, float] = {}
    voters = set()
    total_weight = 0.0
    for b in rows:
        tally[b.choice] = tally.get(b.choice, 0.0) + float(b.weight or 1.0)
        voters.add(b.voter_id)
        total_weight += float(b.weight or 1.0)
    return tally, len(voters), total_weight


# ---------------- 创建 / 开启 ----------------

def create_referendum(db: Session, initiator_id: int, title: str, description: str,
                      options: list[str], weight_mode: str = "credit", binding: int = 1,
                      quorum_bps: int = 2000, votes_needed: int = 5000,
                      duration_hours: int = 72, trigger_type: str = "governor") -> Referendum:
    """新建公投（status='pending'）。duration_hours 暂存于 result_json._meta，开启时取用。"""
    if not title or not title.strip():
        raise ReferendumError("referendum title required")
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if len(opts) < 2:
        raise ReferendumError("referendum needs at least 2 distinct options")
    if weight_mode not in ("credit", "level", "one_person"):
        raise ReferendumError(f"invalid weight_mode: {weight_mode!r}")

    r = Referendum(
        title=title.strip(), description=description or "",
        initiator_id=initiator_id, trigger_type=trigger_type,
        options_json=json.dumps(opts, ensure_ascii=False),
        weight_mode=weight_mode, binding=int(binding),
        quorum_bps=int(quorum_bps), votes_needed=int(votes_needed),
        opens_at=_now(), closes_at=None, status="pending",
        result_json=json.dumps({"_meta": {"duration_hours": int(duration_hours)}}),
        created_at=_now(),
    )
    db.add(r)
    db.flush()
    return r


def open_referendum(db: Session, ref_id: int,
                    duration_hours: int | None = None) -> Referendum:
    """开启公投：pending→open，写入 opens_at/closes_at。

    时长优先：显式入参 > create 暂存的 _meta.duration_hours > 默认 72。
    """
    r = _get_open(db, ref_id)
    if r.status != "pending":
        raise ReferendumError(f"referendum {ref_id} not pending (status={r.status})")

    hours = duration_hours
    if hours is None:
        try:
            meta = json.loads(r.result_json or "{}")
            hours = int(meta.get("_meta", {}).get("duration_hours", 72))
        except (ValueError, TypeError):
            hours = 72
    hours = max(int(hours), 1)

    now = _now()
    r.opens_at = now
    r.closes_at = now + timedelta(hours=hours)
    r.status = "open"
    db.flush()
    return r


# ---------------- 投票 / 关闭 ----------------

def cast_vote(db: Session, ref_id: int, voter_id: int, choice: str) -> VoteBallot:
    """投票：需 status='open'、选项合法、未重复投票；按 weight_mode 计算权重。"""
    r = _get_open(db, ref_id)
    if r.status != "open":
        raise ReferendumError(f"referendum {ref_id} not open for voting (status={r.status})")
    if choice not in _options_of(r):
        raise ReferendumError(f"invalid choice {choice!r} for referendum {ref_id}")
    dup = (db.query(VoteBallot)
             .filter(VoteBallot.referendum_id == ref_id,
                     VoteBallot.voter_id == voter_id).first())
    if dup is not None:
        raise ReferendumError(f"voter {voter_id} already voted on referendum {ref_id}")

    ballot = VoteBallot(referendum_id=ref_id, voter_id=voter_id, choice=choice,
                        weight=_vote_weight(db, r, voter_id), casted_at=_now())
    db.add(ballot)
    db.flush()
    return ballot


def close_referendum(db: Session, ref_id: int) -> Referendum:
    """关闭公投：open→closed，结算 result_json，按法定人数 + 通过万分比判定 passed/rejected。"""
    r = _get_open(db, ref_id)
    if r.status != "open":
        raise ReferendumError(f"referendum {ref_id} not open (status={r.status})")

    tally, participants, total_weight = _tally(db, ref_id)
    eligible = _eligible_count(db)
    participation_bps = (participants * _BPS // eligible) if eligible > 0 else 0

    winning_choice, winning_weight = None, 0.0
    for choice, w in tally.items():
        if w > winning_weight:
            winning_choice, winning_weight = choice, w
    winning_share_bps = (int(winning_weight * _BPS) // int(total_weight)
                         if total_weight > 0 else 0)

    quorum_ok = participation_bps >= r.quorum_bps
    majority_ok = winning_share_bps >= r.votes_needed
    passed = bool(quorum_ok and majority_ok and total_weight > 0)

    result = {
        "tally": tally,
        "participants": participants,
        "eligible": eligible,
        "participation_bps": participation_bps,
        "total_weight": round(total_weight, 4),
        "winning_choice": winning_choice,
        "winning_share_bps": winning_share_bps,
        "quorum_ok": quorum_ok,
        "majority_ok": majority_ok,
        "outcome": "passed" if passed else "rejected",
    }
    r.result_json = json.dumps(result, ensure_ascii=False)
    r.status = "passed" if passed else "rejected"
    db.flush()
    return r


# ---------------- 联署触发 ----------------

def trigger_from_petition(db: Session, signer_ids: list[int], title: str,
                          description: str, options: list[str]) -> Referendum:
    """联署达阈值（≥10 人）转公投：trigger_type='petition'，发起者取首位联署人。"""
    signers = [int(s) for s in dict.fromkeys(signer_ids or []) if s is not None]
    if len(signers) < _PETITION_MIN_SIGNERS:
        raise ReferendumError(
            f"petition needs >= {_PETITION_MIN_SIGNERS} unique signers "
            f"(got {len(signers)})")
    return create_referendum(db, initiator_id=signers[0], title=title,
                             description=description, options=options,
                             trigger_type="petition")


# ---------------- 查询 ----------------

def list_active_referendums(db: Session) -> list[dict]:
    """进行中公投（pending + open），附当前投票进度。"""
    rows = (db.query(Referendum)
              .filter(Referendum.status.in_(("pending", "open")))
              .order_by(Referendum.id.desc()).all())
    out = []
    for r in rows:
        tally, participants, total_weight = _tally(db, r.id)
        out.append({
            "id": r.id, "title": r.title, "trigger_type": r.trigger_type,
            "status": r.status, "weight_mode": r.weight_mode,
            "binding": r.binding, "options": _options_of(r),
            "participants": participants,
            "opens_at": r.opens_at.isoformat() if r.opens_at else None,
            "closes_at": r.closes_at.isoformat() if r.closes_at else None,
        })
    return out


def referendum_results(db: Session, ref_id: int) -> dict:
    """公投结果明细。已结算项直接解析 result_json；进行中项给出实时进度。"""
    r = _get_open(db, ref_id)
    if r.status in ("closed", "passed", "rejected"):
        try:
            result = json.loads(r.result_json or "{}")
        except (ValueError, TypeError):
            result = {}
        result["status"] = r.status
        result["binding"] = r.binding
        return result

    tally, participants, total_weight = _tally(db, r.id)
    eligible = _eligible_count(db)
    return {
        "status": r.status, "binding": r.binding,
        "options": _options_of(r), "tally": tally,
        "participants": participants, "eligible": eligible,
        "participation_bps": (participants * _BPS // eligible) if eligible > 0 else 0,
        "quorum_bps": r.quorum_bps, "votes_needed": r.votes_needed,
    }


# ---------------- 日级任务 ----------------

def referendum_daily_job(db: Session, now: datetime) -> int:
    """自动关闭到期 open 公投（closes_at <= now）。返回关闭数量。"""
    now = now or _now()
    due = (db.query(Referendum)
             .filter(Referendum.status == "open",
                     Referendum.closes_at.isnot(None),
                     Referendum.closes_at <= now).all())
    closed = 0
    for r in due:
        try:
            close_referendum(db, r.id)
            closed += 1
        except ReferendumError:
            logger.exception("referendum_daily_job: close failed ref=%s", r.id)
    if closed:
        logger.info("referendum_daily_job: auto-closed %d referendums", closed)
    return closed


register_daily_job("referendum", referendum_daily_job)
