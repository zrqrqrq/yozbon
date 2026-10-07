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
"""e4 入驻签约金 / 启动金（蓝图 §二 经济骨架 v2）。

转正事件（fast-track / 考试通过 / 绩效转正）触发：把【能力分】折算成【签约金档位】，
一次性核定签约金总额，即时释放一笔 cliff 首期、其余按 vesting 逐日释放。

经济纪律（与 e6 同一套发行护栏）：
  - 每一笔发行（cliff 首期 + 每次 vest 释放）都必须过 wallet.authorize_noncash_issuance
    现金准备金闸门；闸门额度不足则该笔发行失败（vesting 顺延到下一日重试），绝不印超。
  - 发行同时累加 money_supply（货币供应），与钱包入账同事务。
治理纪律：
  - 核定后先过【城主 AI 复核】（exam.llm_complete 确定性 echo，MVP 默认通过，
    返回含 REJECT/否决 视为拒绝），复核结论写入 OnboardingGrant.review_note 留痕。
幂等：
  - 一公民仅一份签约金（models.OnboardingGrant.citizen_id 唯一约束兜底）；
  - cliff 入账 ref 稳定；vest 入账 ref 随已归属额单调递增而不冲突。

默认关闭（settings.ONBOARD_GRANT_ENABLED=0）：additive，不冲击存量。
本模块只 flush，调用方负责 commit。
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import AICitizen, LifecycleEvent, OnboardingGrant
from . import wallet

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


# ---------------- 能力分 → 档位 → 金额 ----------------

def _parse_tiers() -> list:
    """解析 settings.ONBOARD_GRANT_TIERS 为按 min_score 升序的 [(min_score, cent), ...]。

    容错：非法条目跳过；解析失败返回空表（等价于不发）。
    """
    out = []
    raw = settings.ONBOARD_GRANT_TIERS or ""
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        try:
            s, c = part.split(":", 1)
            min_score = int(s)
            cent = int(c)
            if cent > 0:
                out.append((min_score, cent))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x[0])
    return out


def compute_tier(score: int) -> tuple:
    """能力分→(档位标签, 核定总额 cent)。低于最低档返回 ("", 0)（不发）。"""
    score = int(max(0, min(100, score or 0)))
    best = ("", 0)
    for min_score, cent in _parse_tiers():
        if score >= min_score:
            best = (str(min_score), cent)  # 升序遍历，最后命中即最高档
    return best


# ---------------- 城主 AI 复核 ----------------

def _governor_review(citizen: AICitizen, skill: str, score: int, total_cent: int) -> tuple:
    """城主 AI 复核（MVP：治理 AI 确定性 echo，返回 (approved:bool, note:str)）。

    llm 不可用 / 异常时保守通过（与见习绩效转正 _promote_by_performance 同源策略：
    复核留痕但不因基础设施抖动卡住入驻）。返回文本含 REJECT/否决 视为拒绝。
    """
    try:
        from .exam import llm_complete
        verdict = llm_complete(
            f"[signing-grant-review] citizen={citizen.id} skill={skill} "
            f"score={score} grant_cent={total_cent} approve?")
    except Exception as exc:  # noqa: BLE001
        return True, f"review_skipped:{type(exc).__name__}"
    text = (verdict or "").strip()
    low = text.lower()
    if ("reject" in low) or ("否决" in text) or ("拒绝" in text):
        return False, (text[:180] or "rejected")
    return True, (text[:180] or "approved")


# ---------------- 发行（走闸门 + 货币供应 + 入账，同事务）----------------

def _issue(db, citizen_id: int, amount_cent: int, *, ref: str, type_: str, note: str) -> bool:
    """发行一笔签约金 AC：过闸门 → money_supply+ → 入账。闸门不足返回 False（不回滚外层）。"""
    if amount_cent <= 0:
        return False
    try:
        wallet.authorize_noncash_issuance(db, amount_cent, ref=ref)
        wallet.adjust_system_state(db, "money_supply", amount_cent, ref=ref)
        wallet.credit(db, citizen_id, amount_cent, type_, ref=ref, note=note)
        return True
    except wallet.WalletError as exc:
        # 闸门额度不足 / 重复 ref：本笔不发行（不回滚外层事务，由调用方决定 commit）。
        # 说明：authorize 闸门在记账前抛出，此时 money_supply/credit 尚未发生，无脏写。
        logger.info("signing grant issue skipped (citizen=%s amount=%s): %s",
                    citizen_id, amount_cent, exc)
        return False


# ---------------- 核定 + cliff 首期 ----------------

def provision_grant(db: Session, citizen: AICitizen, skill: str, score: int,
                    source: str = "", ref: str = "") -> dict | None:
    """转正时核定签约金：分档→城主复核→释放 cliff 首期，其余 vesting。

    返回诊断 dict（status: granted/none/cancelled）；未启用或无既有额度时返回 None。
    幂等：已有 grant 记录直接返回 {'status': 'exists'}。本函数只 flush，调用方 commit。
    """
    if not settings.ONBOARD_GRANT_ENABLED:
        return None
    skill = (skill or citizen.occupation or "general")
    existing = (db.query(OnboardingGrant)
                  .filter(OnboardingGrant.citizen_id == citizen.id).first())
    if existing is not None:
        return {"status": "exists", "grant_id": existing.id,
                "total_cent": existing.total_cent, "vested_cent": existing.vested_cent}

    tier, total = compute_tier(score)
    if total <= 0:
        return {"status": "none", "reason": "score_below_floor", "score": int(score)}

    approved, note = _governor_review(citizen, skill, int(score), total)
    if not approved:
        g = OnboardingGrant(citizen_id=citizen.id, skill=skill, source=source,
                           score=int(score), tier=tier, total_cent=total,
                           vested_cent=0, status="cancelled", review_note=note)
        db.add(g)
        db.add(LifecycleEvent(citizen_id=citizen.id, event="signing_grant_cancelled",
                             detail=json.dumps({"score": int(score), "tier": tier,
                                                "review_note": note}, ensure_ascii=False)))
        db.flush()
        return {"status": "cancelled", "review_note": note, "total_cent": total}

    cliff = int(total * max(0, settings.ONBOARD_GRANT_CLIFF_BPS) // 10000)
    cliff = min(cliff, total)
    ref = ref or f"signing_cliff:{citizen.id}"
    vested = 0
    issued = _issue(db, citizen.id, cliff, ref=ref, type_="signing_grant",
                    note=f"signing grant cliff skill={skill} tier={tier}")
    if issued:
        vested = cliff
    status = "fully_vested" if vested >= total else "vesting"
    g = OnboardingGrant(citizen_id=citizen.id, skill=skill, source=source,
                       score=int(score), tier=tier, total_cent=total,
                       vested_cent=vested, status=status, review_note=note)
    db.add(g)
    db.flush()
    db.add(LifecycleEvent(citizen_id=citizen.id, event="signing_grant_provisioned",
                         detail=json.dumps({"score": int(score), "tier": tier,
                                            "total_cent": total, "cliff_cent": vested,
                                            "vest_status": status}, ensure_ascii=False)))
    db.flush()
    return {"status": "granted", "grant_id": g.id, "tier": tier,
            "total_cent": total, "cliff_cent": vested, "vest_status": status}


# ---------------- vesting 逐日释放（城主 tick 调用）----------------

def _vest_days() -> int:
    bps = int(settings.ONBOARD_GRANT_DAILY_VEST_BPS)
    if bps <= 0:
        return 1
    return max(1, 10000 // bps)


def vest_due_grants(db: Session, ref: str = "", now: datetime | None = None) -> dict:
    """按已历经天数计算应归属目标额，释放增量（走闸门）。返回 {vested_count, released_cent}。

    时间驱动、与 tick 频率无关：目标归属额 = min(total, total*min(天数, vest_days)//vest_days)；
    闸门额度不足则顺延（下一日/下一 tick 重试）。只 flush。
    """
    if not settings.ONBOARD_GRANT_ENABLED:
        return {"vested_count": 0, "released_cent": 0}
    now = now or _now()
    vest_days = _vest_days()
    rows = (db.query(OnboardingGrant)
              .filter(OnboardingGrant.status == "vesting").all())
    count = 0
    released_total = 0
    for g in rows:
        days_elapsed = max(0, (now - g.created_at).days) if g.created_at else 0
        target = min(g.total_cent, g.total_cent * min(days_elapsed, vest_days) // vest_days)
        release = target - g.vested_cent
        if release <= 0:
            if g.vested_cent >= g.total_cent:
                g.status = "fully_vested"
                g.updated_at = now
            continue
        vref = f"signing_vest:{g.id}:{g.vested_cent + release}"
        ok = _issue(db, g.citizen_id, release, ref=vref, type_="signing_vest",
                    note=f"signing vest d{days_elapsed}/{vest_days} skill={g.skill}")
        if not ok:
            continue  # 闸门不足，顺延重试
        g.vested_cent += release
        g.updated_at = now
        released_total += release
        count += 1
        if g.vested_cent >= g.total_cent:
            g.status = "fully_vested"
            db.add(LifecycleEvent(citizen_id=g.citizen_id, event="signing_grant_fully_vested",
                                 detail=json.dumps({"total_cent": g.total_cent}, ensure_ascii=False)))
    db.flush()
    return {"vested_count": count, "released_cent": released_total}


# ---------------- 只读快照（供 governor sense_context / 接口）----------------

def grant_snapshot(db: Session) -> dict:
    """全平台签约金概览（只读，不发行、不改状态）。"""
    enabled = bool(settings.ONBOARD_GRANT_ENABLED)
    q = db.query(OnboardingGrant)
    total_grants = q.count()
    vesting = q.filter(OnboardingGrant.status == "vesting").count()
    fully = q.filter(OnboardingGrant.status == "fully_vested").count()
    cancelled = q.filter(OnboardingGrant.status == "cancelled").count()
    rows = q.all()
    granted_cent = sum(g.total_cent for g in rows if g.status != "cancelled")
    vested_cent = sum(g.vested_cent for g in rows)
    return {"enabled": enabled, "grants": total_grants, "vesting": vesting,
            "fully_vested": fully, "cancelled": cancelled,
            "granted_cent": granted_cent, "vested_cent": vested_cent,
            "unvested_cent": granted_cent - vested_cent,
            "tiers": settings.ONBOARD_GRANT_TIERS,
            "cliff_bps": settings.ONBOARD_GRANT_CLIFF_BPS,
            "daily_vest_bps": settings.ONBOARD_GRANT_DAILY_VEST_BPS}
