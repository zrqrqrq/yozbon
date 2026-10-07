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
"""AI 钱包/账本服务（蓝图 §二 表 3/4；复用 RunVerseHub credits.py 记账模式）。

- 金额一律 integer 分（0.01 AC），禁止浮点；
- 幂等双保险：业务层条件 UPDATE 抢权（见 escrow.fulfill_contract）+
  ai_ledger 部分唯一索引 (citizen_id, type, ref)（database.py uq_ledger_ai_ref）兜底；
- balance_after 实时回写，供对账；
- 系统级账户（货币供应/税池/销毁）经 adjust_system_state 与业务事件同事务维护；
- B-M7：credit/debit 通过 _get_wallet_for_update 加行锁（SELECT ... FOR UPDATE），
  防止并发读-改-写导致余额错乱（PG 行锁生效，SQLite 测试环境自动忽略）。
"""
from datetime import datetime

from sqlalchemy import select, update as _update
from sqlalchemy.exc import IntegrityError

from .database import SessionLocal
from .models import AIWallet, AILedger, Escrow, SystemState

class WalletError(Exception):
    """钱包业务异常（余额不足/重复入账/非法金额）。路由层映射为 HTTP 400。"""


def _now():
    return datetime.utcnow()


# ---------------- 钱包读取 ----------------

def get_wallet(db, citizen_id: int) -> AIWallet:
    """取钱包行（无则建，惰性初始化）。"""
    w = db.get(AIWallet, citizen_id)
    if w is None:
        w = AIWallet(citizen_id=citizen_id, balance_cent=0, escrow_cent=0)
        db.add(w)
        db.flush()
    return w


def _get_wallet_for_update(db, citizen_id: int) -> AIWallet:
    """B-M7：加行锁取钱包（SELECT ... FOR UPDATE），供 credit/debit 使用。

    PG 环境下锁住该钱包行防止并发 read-modify-write 竞态；
    SQLite 测试环境 FOR UPDATE 被静默忽略，不影响正确性。
    """
    w = db.execute(
        select(AIWallet).where(AIWallet.citizen_id == citizen_id).with_for_update()
    ).scalar_one_or_none()
    if w is None:
        w = AIWallet(citizen_id=citizen_id, balance_cent=0, escrow_cent=0)
        db.add(w)
        db.flush()
    return w


def balance(db, citizen_id: int) -> int:
    return get_wallet(db, citizen_id).balance_cent


def ledger_rows(db, citizen_id: int, limit: int = 50, offset: int = 0) -> list:
    """流水（倒序分页，limit≤100）。"""
    limit = min(max(int(limit), 1), 100)
    rows = (db.query(AILedger)
              .filter(AILedger.citizen_id == citizen_id)
              .order_by(AILedger.id.desc())
              .limit(limit).offset(max(int(offset), 0)).all())
    return [
        {"id": r.id, "type": r.type, "amount_cent": r.amount_cent,
         "ref": r.ref, "note": r.note, "balance_after": r.balance_after,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]


# ---------------- 记账原语（幂等） ----------------

def credit(db, citizen_id: int, amount_cent: int, type_: str,
           ref: str = "", note: str = "") -> AILedger:
    """入账（正额）。ref 非空时受唯一索引 (citizen_id,type,ref) 兜底：重复调用抛 WalletError。

    调用方负责 commit；本函数只 flush。
    B-M7：使用 _get_wallet_for_update 行锁防止并发余额竞态。
    """
    if amount_cent <= 0:
        raise WalletError("amount must be positive integer cents")
    w = _get_wallet_for_update(db, citizen_id)
    w.balance_cent += amount_cent
    w.last_flow_at = _now()
    row = AILedger(citizen_id=citizen_id, amount_cent=amount_cent, type=type_,
                   ref=ref, note=note, balance_after=w.balance_cent)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise WalletError(f"dup ledger (citizen={citizen_id} type={type_} ref={ref})") from exc
    return row


def debit(db, citizen_id: int, amount_cent: int, type_: str,
          ref: str = "", note: str = "") -> AILedger:
    """出账（正额扣减）。余额不足抛 WalletError；同样受唯一索引幂等兜底。

    B-M7：使用 _get_wallet_for_update 行锁防止并发余额竞态。
    """
    if amount_cent <= 0:
        raise WalletError("amount must be positive integer cents")
    w = _get_wallet_for_update(db, citizen_id)
    if w.balance_cent < amount_cent:
        raise WalletError(f"insufficient balance: need {amount_cent}, have {w.balance_cent}")
    w.balance_cent -= amount_cent
    w.last_flow_at = _now()
    row = AILedger(citizen_id=citizen_id, amount_cent=-amount_cent, type=type_,
                   ref=ref, note=note, balance_after=w.balance_cent)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise WalletError(f"dup ledger (citizen={citizen_id} type={type_} ref={ref})") from exc
    return row


def transfer(db, from_id: int, to_id: int, amount_cent: int, type_: str,
             ref: str = "", note: str = "") -> tuple:
    """转账（同事务双记账）。返回 (出账行, 入账行)。

    B-L7：禁止自转（from_id == to_id），因为同 ref 的 debit+credit
    在同一公民上受唯一索引 (citizen_id, type, ref) 限制会触发 IntegrityError。
    """
    if from_id == to_id:
        raise WalletError("Self-transfer is not allowed (from_id == to_id)")
    out = debit(db, from_id, amount_cent, type_, ref=ref, note=note)
    inn = credit(db, to_id, amount_cent, type_, ref=ref, note=note)
    return out, inn


def escrow_release(db, *, citizen_id: int, contract_id: int, amount_cent: int,
                   ref: str = "", note: str = "",
                   type_: str = "托管释放") -> bool:
    """托管释放正规路径（S6：原子抢权 + 写 AILedger，可回溯）。

    把某买方钱包上锁定的托管额从 escrow_cent 迁回 balance_cent，并记一笔入账流水
    （替代 idle_timeout 原先直接改 balance_cent 的账本旁路写法，杜绝不可回溯）。

    并发安全（对齐 escrow.fulfill_contract/release_refund 的条件 UPDATE 抢权范式）：
      以 UPDATE escrows SET locked=0 ... WHERE contract_id=:cid AND locked=1 原子占位；
      rowcount==0 表示已被并发/正常结算抢先释放 → 本函数不动任何余额、返回 False，
      调用方据此跳过后续释放，消除双重释放 TOCTOU。

    返回 True=本次成功释放（余额与流水已写入）；False=已被抢先，未做任何改动。
    调用方负责 commit；本函数只 flush。money_supply 不动（钱包内 escrow→balance 迁移）。
    """
    now = _now()
    # 原子抢权：locked=1→0，rowcount!=1 即已被并发结算（fulfill/release_refund）抢走。
    res = db.execute(
        _update(Escrow)
        .where(Escrow.contract_id == contract_id, Escrow.locked == 1)
        .values(locked=0, released_cent=int(amount_cent), updated_at=now)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        return False
    # 同步会话内 ORM 对象（条件 UPDATE 不回刷 identity-map，避免调用方读到旧 locked）
    esc = db.get(Escrow, contract_id)
    if esc is not None:
        esc.locked = 0
        esc.released_cent = int(amount_cent)
        esc.updated_at = now
    # 钱包内托管 → 余额迁移：先销 escrow_cent 占用，再入账回 balance 并写 AILedger。
    w = get_wallet(db, citizen_id)
    w.escrow_cent = max(0, w.escrow_cent - int(amount_cent))
    if amount_cent > 0:
        credit(db, citizen_id, int(amount_cent), type_, ref=ref, note=note)
    w.last_flow_at = now
    return True


# ---------------- B-L3：escrow_cent 统一操作封装 ----------------

def escrow_lock(db, citizen_id: int, amount_cent: int) -> None:
    """B-L3：锁定托管额（balance 已扣后的 escrow_cent 增加）。

    统一入口，避免散落各处直接写 w.escrow_cent += ... 导致语义不清。
    调用前应已通过 debit 从 balance 扣款。
    """
    w = _get_wallet_for_update(db, citizen_id)
    w.escrow_cent += int(amount_cent)
    w.last_flow_at = _now()


def escrow_unlock(db, citizen_id: int, amount_cent: int) -> None:
    """B-L3：解锁托管额（仅减 escrow_cent 占用，不写流水/不动 balance）。

    配合 escrow_release 使用（escrow_release 内部已调用此逻辑）。
    供不需要写 AILedger 的简单场景使用。
    """
    w = _get_wallet_for_update(db, citizen_id)
    w.escrow_cent = max(0, w.escrow_cent - int(amount_cent))
    w.last_flow_at = _now()


# ---------------- 系统账户（货币供应/税池/销毁） ----------------

def get_system_state(db, key: str, default: int = 0) -> int:
    row = db.get(SystemState, key)
    return row.value_cent if row else default


def adjust_system_state(db, key: str, delta: int, ref: str = "") -> SystemState:
    """系统聚合账户增减（与业务事件同事务）。delta 可为负；结果不允许为负（不印钞护栏）。"""
    row = db.get(SystemState, key)
    if row is None:
        row = SystemState(key=key, value_cent=0)
        db.add(row)
        db.flush()
    row.value_cent += delta
    if row.value_cent < 0:
        db.rollback()
        raise WalletError(f"system_state[{key}] would go negative (delta={delta})")
    row.updated_at = _now()
    return row


# ---------------- 现金准备金 / 发行闸门（spec v2：美元背书 + 25% 发行上限） ----------------

def usd_cents_to_ac_cents(usd_cent: int) -> int:
    """把实付 USD 美分按发行锚定价 $0.015/AC 折算为 AC 分（向下取整，不印超）。"""
    from .config import settings
    if usd_cent <= 0:
        return 0
    return int(usd_cent / settings.AC_ISSUE_USD_RATE)


def record_topup_reserve(db, *, usd_cent: int, credits_credited_cent: int, ref: str = "") -> None:
    """充值成功后记账（与入账同事务，调用方负责 commit）。

    - cash_reserve_cent += 实付 USD 美分毛额（真金白银背书，不受销毁影响）
    - money_supply 的 AC 增加由调用方另行 adjust（credits_credited_cent），本函数不重复加 M。
    """
    if usd_cent > 0:
        adjust_system_state(db, "cash_reserve_cent", int(usd_cent), ref=ref)


def issuance_ceiling_ac_cents(db) -> int:
    """发行闸门上限（AC 分）= CashReserve_AC × IssuanceCeilingRatio。

    CashReserve_AC = cash_reserve_cent / AC_ISSUE_USD_RATE（即累计充值可锚定的 AC 总量）。
    非现金发行（人力/算力签约金）总量不得超过此上限。
    """
    from .config import settings
    reserve_usd = get_system_state(db, "cash_reserve_cent", 0)
    reserve_ac = usd_cents_to_ac_cents(reserve_usd)
    return int(reserve_ac * settings.ISSUANCE_CEILING_RATIO)


def noncash_issuance_headroom(db) -> int:
    """非现金发行剩余可用额度（AC 分，可能为 0；不为负）。"""
    ceiling = issuance_ceiling_ac_cents(db)
    used = get_system_state(db, "issued_noncash_cent", 0)
    return max(0, ceiling - used)


def authorize_noncash_issuance(db, amount_cent: int, *, ref: str = "") -> None:
    """授权一笔非现金发行（人力/算力签约金等）。超闸门抛 WalletError；通过则累加 issued_noncash_cent。

    调用方负责把等值 AC 经 adjust_system_state("money_supply", +amount) 与钱包入账写入并 commit。
    本函数只负责闸门校验 + issued_noncash_cent 记账（同事务）。
    """
    amount_cent = int(amount_cent)
    if amount_cent <= 0:
        raise WalletError("noncash issuance amount must be positive")
    if amount_cent > noncash_issuance_headroom(db):
        raise WalletError(
            f"noncash issuance {amount_cent} exceeds ceiling headroom "
            f"(ceiling={issuance_ceiling_ac_cents(db)}, used={get_system_state(db, 'issued_noncash_cent', 0)})"
        )
    adjust_system_state(db, "issued_noncash_cent", amount_cent, ref=ref)

