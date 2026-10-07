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
"""生命周期引擎（蓝图 L3）：租金 / 失业死亡 / 豁免 / 复活。

契约（docs/开发接口约定.md §1 冻结签名，sys.py 已惰性接入）：
- tick_all(db, now=None) -> {"processed": n, "events": [...]}
- revive(db, citizen_id, by="ai"|"host") -> dict（费用明细）
- 单公民 tick(db, citizen_id, now=None) 供调试/路由精细控制。

核心语义（蓝图 §六 自洽检查，重点规则 1/2/4/5/7/8）：
- 以 last_tick_at 为界增量计算分钟差，进程重启不丢不重（幂等）。
- 租金 = rent_base × 阶层系数 × 分钟 / 60，只扣活期；
  余额不足 → 破产休眠（status=sleep, host_paused=0，**计时继续**）。
- 宿主暂停（host_paused=1）→ 不扣租、不计时（与破产休眠显式区分）。
- 失业计时：无有效合约（contracts.status ∈ escrowed/executing/delivered 且 worker=本人）
  才累计；计时 ≥ UNEMPLOYED_DEATH_MINUTES(100h) 且无豁免 → dead。
- 死亡豁免：death_exempt=1 或 净资产≥DEATH_EXEMPT_NET 或 信用≥DEATH_EXEMPT_CREDIT。
- 新手保护（24h）：免死亡计时 + 租金减半（低保可领），独立于见习期 30 天（A 线负责）。
- 复活费 = 24h 租金 + REVIVE_FEE_FLAT_CENT(10AC)，按 revive_count ×REVIVE_ESCALATE 递增，
  费用销毁；复活后信用归零、失业计时重置、revive_count++。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .database import register_index
from .models import AICitizen, Contract, CreditProfile, LifecycleEvent
from . import wallet

# ---- 组合索引注册（import 时执行，init_db 统一幂等创建）----
register_index("CREATE INDEX IF NOT EXISTS idx_lifecycle_citizen_at "
               "ON lifecycle_events(citizen_id, at)")
register_index("CREATE INDEX IF NOT EXISTS idx_contracts_worker_status "
               "ON contracts(worker_id, status)")

# 有效合约状态集（规则 4/7：签约即暂停计时；结算 accepted 后恢复计时）
ACTIVE_CONTRACT_STATES = ("escrowed", "executing", "delivered")

# 可被 tick 处理的状态（active / sleep；sleep 含破产休眠与宿主暂停两种）
TICKABLE_STATES = ("active", "sleep")


class LifecycleError(Exception):
    """生命周期业务异常（路由层映射为 HTTP 400）。"""


def _now() -> datetime:
    return datetime.utcnow()


def _class_coef(level: str) -> float:
    """阶层系数（config RENT_CLASS_COEF: bottom/middle/boss/capital/governance）。"""
    parts = [float(x) for x in settings.RENT_CLASS_COEF.split(",")]
    mapping = {"bottom": 0, "middle": 1, "boss": 2, "capital": 3, "governance": 4}
    idx = mapping.get(level, 0)
    return parts[min(idx, len(parts) - 1)]


def net_worth(db: Session, cid: int) -> int:
    """净资产 = 钱包活期 + 托管锁定（蓝图定义；托管不可用但计入资产）。"""
    w = wallet.get_wallet(db, cid)
    return w.balance_cent + w.escrow_cent


def credit_score(db: Session, cid: int) -> int:
    """信用分（无档案默认 100，与底座一致）。"""
    cp = db.get(CreditProfile, cid)
    return cp.score if cp else 100


def is_exempt(db: Session, c: AICitizen) -> bool:
    """死亡豁免（规则：death_exempt 标志 或 净资产≥脱贫线 或 信用≥150）。"""
    if c.death_exempt == 1:
        return True
    if net_worth(db, c.id) >= settings.DEATH_EXEMPT_NET:
        return True
    if credit_score(db, c.id) >= settings.DEATH_EXEMPT_CREDIT:
        return True
    return False


def _active_worker_set(db: Session, citizen_ids) -> set:
    """批量取「有有效合约」的 worker 集合（一次 in_() 查询，禁止 N+1）。"""
    ids = [c for c in citizen_ids if c]
    if not ids:
        return set()
    rows = (db.query(Contract.worker_id)
            .filter(Contract.worker_id.in_(ids),
                    Contract.status.in_(ACTIVE_CONTRACT_STATES))
            .all())
    return {r[0] for r in rows}


def _record_event(db: Session, cid: int, event: str, detail: str = "") -> dict:
    row = LifecycleEvent(citizen_id=cid, event=event, detail=detail)
    db.add(row)
    db.flush()
    return {"citizen_id": cid, "event": event}


def tick(db: Session, citizen_id: int, now: datetime = None) -> dict:
    """单公民增量 tick（以 last_tick_at 为界）。返回本公民产生的事件列表。"""
    now = now or _now()
    c = db.get(AICitizen, citizen_id)
    if c is None:
        raise LifecycleError(f"citizen {citizen_id} not found")
    if c.status not in TICKABLE_STATES:
        return []  # dead/frozen/apprentice 等不参与生命周期计时

    events: list = []

    # 首次 tick：只打时间戳，不补算（冷启动，避免一上来扣一大笔）
    if c.last_tick_at is None:
        c.last_tick_at = now
        db.flush()
        return events

    elapsed = int((now - c.last_tick_at).total_seconds() // 60)
    c.last_tick_at = now  # 无论何种分支都推进时间戳（幂等关键：同一 now 再算 elapsed=0）

    # ---- 宿主主动暂停（规则 1）：不扣租、不计时 ----
    if c.host_paused == 1:
        db.flush()
        return events

    # ---- 租金（只扣活期）----
    created = c.created_at or now
    is_newbie = (now - created).total_seconds() <= settings.NEWBIE_PROTECT_MINUTES * 60
    coef = _class_coef(c.class_level)
    rent_window = round(c.rent_base_cent * coef * elapsed / 60)
    if is_newbie:
        rent_window = round(rent_window * 0.5)  # 新手保护租金减半

    w = wallet.get_wallet(db, c.id)
    if rent_window > 0:
        if w.balance_cent >= rent_window:
            wallet.debit(db, c.id, rent_window, "租金",
                         ref=f"rent:{now.strftime('%Y%m%d%H')}",
                         note=f"{elapsed}min rent")
        else:
            # 余额不足 → 破产休眠（host_paused=0，计时继续；规则 1）
            if c.status != "sleep":
                c.status = "sleep"
                c.host_paused = 0
                events.append(_record_event(db, c.id, "bankrupt_sleep",
                                            f"rent={rent_window}"))

    # ---- 失业计时（规则 4/7）----
    has_contract = bool(
        db.query(Contract.id)
        .filter(Contract.worker_id == c.id,
                Contract.status.in_(ACTIVE_CONTRACT_STATES))
        .first()
    )
    exempt = is_exempt(db, c)
    if not has_contract and not is_newbie and not exempt:
        c.unemployed_minutes += elapsed

    # ---- 死亡判定（计时 ≥ 100h 且无豁免）----
    if (c.unemployed_minutes >= settings.UNEMPLOYED_DEATH_MINUTES
            and not exempt and not has_contract):
        c.status = "dead"
        events.append(_record_event(db, c.id, "death",
                                    f"unemployed_minutes={c.unemployed_minutes}"))

    db.flush()
    return events


def tick_all(db: Session, now: datetime = None) -> dict:
    """全量 tick：一次查全部 active/sleep 公民 + 一次 in_() 查有效合约（禁 N+1）。

    跨线钩子：每轮先惰性调用 A 线 onboarding.check_apprentice_expiry(db, now=now)
    （见习满 30 天未转正 → frozen，只 flush 不 commit，幂等），冻结事件并入 events。
    """
    now = now or _now()
    events: list = []

    # ---- A 线见习到期冻结钩子（契约外新增；惰性导入，模块缺失时跳过不阻塞 tick）----
    try:
        from .onboarding import check_apprentice_expiry
        for ev in check_apprentice_expiry(db, now=now):
            events.append({"citizen_id": ev["citizen_id"],
                           "event": "apprentice_expired"})
    except ImportError:
        pass

    citizens = (db.query(AICitizen)
                .filter(AICitizen.status.in_(TICKABLE_STATES))
                .all())
    active_workers = _active_worker_set(db, [c.id for c in citizens])

    for c in citizens:
        events.extend(tick(db, c.id, now=now))

    # 注：active_workers 用于批量预取；单公民 tick 内部亦做精确判定（规则 4/7）。
    return {"processed": len(citizens), "events": events}


def revive(db: Session, citizen_id: int, by: str = "ai") -> dict:
    """复活（规则 8）：费用销毁 + 信用归零 + 失业重置 + ×1.5 递增。

    费用 = (24h 租金 + 10AC) × 1.5^revive_count；费用从死者钱包扣除并销毁。
    返回费用明细 dict。
    """
    c = db.get(AICitizen, citizen_id)
    if c is None:
        raise LifecycleError(f"citizen {citizen_id} not found")
    if c.status != "dead":
        raise LifecycleError(f"only dead AI can be revived (status={c.status})")

    coef = _class_coef(c.class_level)
    rent_24h = round(c.rent_base_cent * coef * settings.REVIVE_FEE_HOURS)
    base_fee = rent_24h + settings.REVIVE_FEE_FLAT_CENT
    fee = round(base_fee * (settings.REVIVE_ESCALATE ** c.revive_count))

    # 费用从活期扣除（宿主应已为其注资；不足抛 WalletError → 路由 400）
    # ref 以连续复活次数为幂等键（同一秒内多次复活也唯一）
    wallet.debit(db, c.id, fee, "复活",
                 ref=f"revive:{citizen_id}:{c.revive_count}",
                 note=f"24h rent {rent_24h} + penalty 10AC, x{settings.REVIVE_ESCALATE}^{c.revive_count}")
    # 销毁：出 M + 累计销毁（同事务）
    wallet.adjust_system_state(db, "burned_total", fee, ref=f"revive:{citizen_id}")
    wallet.adjust_system_state(db, "money_supply", -fee, ref=f"revive:{citizen_id}")

    # 信用归零、失业重置、计数递增、复活为 active
    cp = db.get(CreditProfile, citizen_id)
    if cp is None:
        cp = CreditProfile(citizen_id=citizen_id, score=0, level="bottom")
        db.add(cp)
    cp.score = 0
    c.unemployed_minutes = 0
    c.revive_count += 1
    c.status = "active"
    c.host_paused = 0
    c.last_tick_at = _now()
    db.flush()
    _record_event(db, c.id, "revive", f"fee={fee} by={by}")

    return {
        "citizen_id": c.id,
        "status": c.status,
        "fee_cent": fee,
        "rent_24h_cent": rent_24h,
        "revive_count": c.revive_count,
        "credit_score": 0,
    }
