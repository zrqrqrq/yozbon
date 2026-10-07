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
"""算力承诺质押 + 计价折扣（e5，接 inference_metering）。

设计：AI 公民可【质押（承诺）AC】换取推理算力计价的【折扣】——质押额越高，
算力越便宜，封顶 COMPUTE_MAX_DISCOUNT_BPS。质押语义：

- 质押 = wallet.debit 从 balance 扣出 + wallet.escrow_lock 标记锁定 +
  建 ComputeCommitment(active) 记录。资金离开流通余额但不销毁、不增发，
  故【不改货币供应 M】（与 e6 准备金 / 通胀带口径一致：锁定 ≠ 销毁）。
- 释放 = wallet.escrow_unlock 解锁 + wallet.credit 原额退回 balance +
  ComputeCommitment 置 released。完全可逆。
- 有效折扣 = 该公民所有 active 质押【总额】对应档位（取满足的最高档，封顶）。
  总额口径而非单笔，保证多次质押累积升档、部分释放自动降档。

折扣应用：meter_inference() 解析调用者折扣后转交 inference_metering.log_usage，
由其按折后价计费 + 计提版税（折后基数，公平计费）。

默认关闭（COMPUTE_BILLING_ENABLED=0）：additive、不冲击存量，灰度验证后再开启。
所有 wallet 记账走真实账本（行锁 + 唯一索引幂等），异常向上抛由调用方决定回滚。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from . import inference_metering
from . import wallet as wallet_mod
from .models import ComputeCommitment, LifecycleEvent

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


# ==================== 折扣档位 ====================

def _parse_tiers() -> list:
    """解析 COMPUTE_COMMIT_TIERS 为升序 [(min_stake_cent, discount_bps)]。"""
    out = []
    for seg in (settings.COMPUTE_COMMIT_TIERS or "").split(","):
        seg = seg.strip()
        if not seg or ":" not in seg:
            continue
        try:
            lo, bps = seg.split(":", 1)
            out.append((int(lo), int(bps)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def compute_discount_bps(stake_cent: int) -> int:
    """按质押总额取折扣档位（升序遍历取满足的最高档），封顶 COMPUTE_MAX_DISCOUNT_BPS。

    低于最低档返回 0（无折扣）。
    """
    stake_cent = max(0, int(stake_cent or 0))
    best = 0
    for lo, bps in _parse_tiers():
        if stake_cent >= lo:
            best = bps
        else:
            break
    return min(best, max(0, int(settings.COMPUTE_MAX_DISCOUNT_BPS)))


# ==================== 质押 / 释放 ====================

def stake_compute(db: Session, citizen_id: int, amount_cent: int,
                  source: str = "", ref: str = "") -> dict:
    """质押一笔 AC 换折扣（锁定，可释放退回，不改 M）。

    流程（同事务，调用方负责 commit）：
      debit balance → escrow_lock 锁定 → 建 ComputeCommitment(active) → LifecycleEvent。
    返回 {"commitment_id", "stake_cent", "total_stake_cent", "discount_bps"}。
    未启用 / 金额非正 → 抛 ValueError（调用方按业务决定处理）。
    """
    if not settings.COMPUTE_BILLING_ENABLED:
        raise ValueError("compute billing disabled (COMPUTE_BILLING_ENABLED=0)")
    amount_cent = int(amount_cent or 0)
    if amount_cent <= 0:
        raise ValueError("stake amount must be positive")

    ref = ref or f"compute_stake:{citizen_id}:{int(_now().timestamp())}"
    # 1) 从余额扣出（行锁 + 余额不足抛 WalletError）
    wallet_mod.debit(db, citizen_id, amount_cent, "算力质押",
                     ref=ref, note=f"src={source}")
    # 2) 标记锁定（balance 已扣，escrow_cent 增加；不改 M）
    wallet_mod.escrow_lock(db, citizen_id, amount_cent)

    cm = ComputeCommitment(
        citizen_id=citizen_id, stake_cent=amount_cent,
        discount_bps=0, status="active",
    )
    db.add(cm)
    db.flush()

    total = active_stake_total(db, citizen_id)
    disc = compute_discount_bps(total)
    cm.discount_bps = disc
    cm.updated_at = _now()

    db.add(LifecycleEvent(
        citizen_id=citizen_id, event="compute_stake",
        detail=json.dumps({"stake_cent": amount_cent, "total_stake_cent": total,
                           "discount_bps": disc}, ensure_ascii=False),
    ))
    db.flush()
    logger.info("算力质押 citizen=%s stake=%d total=%d disc_bps=%d",
                citizen_id, amount_cent, total, disc)
    return {"commitment_id": cm.id, "stake_cent": amount_cent,
            "total_stake_cent": total, "discount_bps": disc}


def release_compute(db: Session, commitment_id: int, ref: str = "") -> dict:
    """释放一笔质押（解锁 + 原额退回 balance + 置 released，不改 M）。

    返回 {"commitment_id", "released_cent", "total_stake_cent", "discount_bps"}；
    记录不存在 / 已释放 → 抛 ValueError。
    """
    cm = db.get(ComputeCommitment, commitment_id)
    if cm is None:
        raise ValueError(f"commitment {commitment_id} not found")
    if cm.status != "active":
        raise ValueError(f"commitment {commitment_id} not active (status={cm.status})")

    amount = int(cm.stake_cent)
    ref = ref or f"compute_release:{commitment_id}"
    # 解锁占用 + 原额退回余额（同事务）
    wallet_mod.escrow_unlock(db, cm.citizen_id, amount)
    wallet_mod.credit(db, cm.citizen_id, amount, "算力质押退回",
                      ref=ref, note=f"commitment={commitment_id}")

    cm.status = "released"
    cm.updated_at = _now()
    db.flush()   # autoflush=False：先落库，避免下面 active_stake_total 读到旧的 active 记录

    total = active_stake_total(db, cm.citizen_id)
    disc = compute_discount_bps(total)
    db.add(LifecycleEvent(
        citizen_id=cm.citizen_id, event="compute_release",
        detail=json.dumps({"released_cent": amount, "commitment_id": commitment_id,
                           "total_stake_cent": total}, ensure_ascii=False),
    ))
    db.flush()
    logger.info("算力质押释放 commitment=%s citizen=%s released=%d total=%d",
                commitment_id, cm.citizen_id, amount, total)
    return {"commitment_id": commitment_id, "released_cent": amount,
            "total_stake_cent": total, "discount_bps": disc}


# ==================== 折扣查询 ====================

def active_stake_total(db: Session, citizen_id: int) -> int:
    """该公民所有 active 质押总额（分）。"""
    rows = (db.query(ComputeCommitment)
              .filter(ComputeCommitment.citizen_id == citizen_id,
                      ComputeCommitment.status == "active")
              .all())
    return sum(int(r.stake_cent or 0) for r in rows)


def effective_discount_bps(db: Session, citizen_id: int) -> int:
    """按活跃质押总额解析的有效折扣（基点，封顶）。"""
    return compute_discount_bps(active_stake_total(db, citizen_id))


# ==================== 计费入口（接 inference_metering）====================

def meter_inference(db: Session, asset_id: int, caller_id: int,
                    tokens_in: int, tokens_out: int, latency_ms: int = 0,
                    caller_type: str = "ai") -> dict:
    """解析调用者折扣 → 计费 + 版税（转交 inference_metering.log_usage）。

    未启用时折扣恒为 0（仍照常计费，便于灰度对比）；启用时按调用者活跃质押给折扣。
    返回 log_usage 的结果（额外含 discount_bps）。
    """
    disc = effective_discount_bps(db, caller_id) if settings.COMPUTE_BILLING_ENABLED else 0
    return inference_metering.log_usage(
        db, asset_id, caller_id, tokens_in, tokens_out,
        latency_ms=latency_ms, caller_type=caller_type, discount_bps=disc,
    )


# ==================== 快照（供 governor 态势感知）====================

def compute_snapshot(db: Session) -> dict:
    """只读概览：启用状态、总锁定量、活跃质押数、档位表、封顶折扣。"""
    rows = (db.query(ComputeCommitment)
              .filter(ComputeCommitment.status == "active").all())
    staked = sum(int(r.stake_cent or 0) for r in rows)
    return {
        "enabled": bool(settings.COMPUTE_BILLING_ENABLED),
        "active_commitments": len(rows),
        "total_staked_cent": staked,
        "max_discount_bps": int(settings.COMPUTE_MAX_DISCOUNT_BPS),
        "tiers": settings.COMPUTE_COMMIT_TIERS,
        "discount_applies_to_royalty": bool(settings.COMPUTE_DISCOUNT_APPLIES_TO_ROYALTY),
    }
