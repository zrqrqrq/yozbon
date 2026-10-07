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
"""合约全生命周期 + 结算会计（蓝图 §二 表 10/16；契约 §2 钱怎么走）。

状态机（contract.status）：
  proposed(投标要约) → executing(已签约托管锁定) → delivered(已交付) → accepted(已结算)
                                          ↑           │
                                          └─ reject→返工(executing, 托管仍锁定)
  任一中途 → dispute(仲裁开案, 托管锁定) → refunded(仲裁解除折算释放) / accepted

核心 fulfill_contract 严格按 docs/开发接口约定.md §2：
  条件 UPDATE 抢权(escrows.locked=1→0, rowcount==1 才放款) + ai_ledger 部分唯一索引双保险；
  fee/burn/taxpool = fee_split；tax = income_tax_cent(P-fee, 本月累计基数)；
  worker 记 结算/手续费/税 三行；system_state 调 tax_pool/burned_total/money_supply；
  tax_records 插 income 行；全程单事务。
规则落点：6(手续费/税池/销毁同事务)、12(按签约等级 signed_class 结算)、
14(验收↔托管联动：reject 锁定/accept 释放/仲裁锁定/解除折算)、4(有效合约状态集)。
"""
import json
from datetime import datetime

from sqlalchemy import update as _update
from sqlalchemy.orm import Session

from . import credit, wallet
from .config import settings
from .event_bus import emit as _emit_event
from .models import (AICitizen, AcceptanceRecord, ArbitrationCase, Contract,
                     Deliverable, Escrow, ProjectNode, ReworkOrder, TaxRecord)
from .tax_rules import adjusted_fee_rate, fee_split, income_tax_cent


class EscrowError(Exception):
    """合约状态机/结算业务异常。路由层映射 HTTP 400/403/404/409。"""


def _now() -> datetime:
    return datetime.utcnow()


def _audit(db: Session, actor_type: str, actor_id: int, action: str, detail: str = "{}"):
    from .models import AuditLog
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id,
                    action=action, detail=detail))


def _get_contract(db: Session, contract_id: int) -> Contract:
    c = db.get(Contract, contract_id)
    if c is None:
        raise EscrowError(f"Contract {contract_id} not found")
    return c


# ---------------- S1：合约收口后推进绑定的 WBS 节点 ----------------
# 合约完成/违约时把 ProjectNode 推进到对应终态，解决「验收/结算后节点永久卡 signed」。
# 最小侵入：仅在已有结算/退款收口点末尾回调；node_id 为空/0 或节点不存在时静默跳过，
# 绝不因节点推进失败而中断结算/退款主链。
def _advance_node_on_settle(db: Session, c: Contract) -> None:
    """结算/验收通过 → 绑定节点 complete（done）并唤醒后继调度。"""
    if not c.node_id:
        return
    try:
        from . import project as _project   # 延迟导入避免循环依赖
        _project.complete_node(db, c.node_id)
    except Exception:  # noqa: BLE001  节点推进失败绝不阻断结算
        pass


def _fail_node_on_breach(db: Session, c: Contract) -> None:
    """违约/仲裁退款 → 绑定节点置 failed（等价失败态）。"""
    if not c.node_id:
        return
    try:
        node = db.get(ProjectNode, c.node_id)
        if node is not None and node.status != "done":
            node.status = "failed"
    except Exception:  # noqa: BLE001  节点推进失败绝不阻断退款
        pass


# ---------------- 签约（买方 accept 投标要约：托管锁定） ----------------
def sign_contract(db: Session, buyer: AICitizen, contract_id: int,
                  deadline: datetime | None = None) -> Contract:
    """买方（项目总管 AI）accept 一条 proposed 要约 = 签约 + 托管。

    - 权限：仅 buyer_id 本人可签；
    - 资金：buyer.debit(P,'托管',ref=contract:{id})，buyer.escrow_cent += P（锁定追踪）；
    - escrows 行(locked=1, amount=P)；contract.status → executing；
    - terms_json 写入 signed_class=worker.class_level（规则12：按签约等级结算，不追溯）、
      deadline、验收标准；
    - 节点状态 → signed（中标，退出 matching 市场）。
    """
    c = _get_contract(db, contract_id)
    if c.buyer_id != buyer.id:
        raise EscrowError("Not the buyer of this contract; no permission to sign")
    if c.status != "proposed":
        raise EscrowError(f"Contract status={c.status}; only proposed can be signed")
    terms = json.loads(c.terms_json or "{}")
    P = int(terms.get("offer_cent", 0))
    if P <= 0:
        raise EscrowError("Offer is missing a valid quote offer_cent")
    worker = db.get(AICitizen, c.worker_id)
    if worker is None:
        raise EscrowError("Performing-party AI not found")
    # C-17 对敲护栏：禁止 worker_id==buyer_id 自买自卖（无真实履约的自我托管/结算）
    if c.worker_id == c.buyer_id:
        raise EscrowError("Self-trading (worker_id==buyer_id) is prohibited")
    # C-16：节点必须仍在 matching 才可签约——节点一旦被首单签走(signed)，
    # 同节点另一条 proposed 要约再签约会重复托管扣款，必须拒绝。
    node = db.get(ProjectNode, c.node_id)
    if node is None or node.status != "matching":
        raise EscrowError(f"Node status is not matching (current={node.status if node else 'not found'}), "
                          "cannot sign again")
    # 托管扣款（余额不足抛 WalletError，由路由层转 400）
    wallet.debit(db, buyer.id, P, "托管", ref=f"contract:{c.id}",
                 note=f"contract escrow locked P={P}")
    bw = wallet.get_wallet(db, buyer.id)
    bw.escrow_cent += P
    db.add(Escrow(contract_id=c.id, amount_cent=P, released_cent=0, locked=1))
    # 规则12：冻结签约时等级；后续 worker 升级/降级不追溯本合约
    terms["signed_class"] = worker.class_level
    terms["signed_at"] = _now().isoformat()
    terms["_started_at"] = _now().isoformat()   # idle_timeout 用：标记进入 executing 的时刻
    if deadline is not None:
        terms["deadline"] = deadline.isoformat()
    c.terms_json = json.dumps(terms, ensure_ascii=False)
    c.escrow_cent = P
    c.status = "executing"
    # 节点中标 → signed（退出 matching 市场；C1 规则4 以合约状态集为准）
    node.status = "signed"
    _audit(db, "ai", buyer.id, "contract.sign",
           json.dumps({"contract_id": c.id, "P": P, "worker": worker.id}))
    # N 轮事件总线（N7 动态流 / N9 通知）：签约事件
    _emit_event(db, "contract.signed",
                {"ai_id": c.worker_id, "contract_id": c.id,
                 "buyer_id": c.buyer_id, "P": P, "node_id": c.node_id,
                 "project_id": c.project_id})
    db.flush()
    return c


# ---------------- 交付（版本化） ----------------
def deliver(db: Session, worker: AICitizen, contract_id: int,
            file_ref: str, fingerprint: str) -> Deliverable:
    """worker 交付（版本化）：deliverables.version++，fingerprint 必填，status=submitted。

    - 权限：仅 worker_id 本人；
    - 仅 executing 态可交付（签约后 / 返工后回到 executing）；
    - contract.status → delivered。
    """
    if not fingerprint:
        raise EscrowError("fingerprint (digital watermark) is required")
    c = _get_contract(db, contract_id)
    if c.worker_id != worker.id:
        raise EscrowError("Not the performing party; no permission to deliver")
    if c.status != "executing":
        raise EscrowError(f"Contract status={c.status}; only executing can deliver")
    last = (db.query(Deliverable)
            .filter(Deliverable.contract_id == c.id)
            .order_by(Deliverable.version.desc()).first())
    version = (last.version + 1) if last else 1
    d = Deliverable(contract_id=c.id, version=version, file_ref=file_ref,
                    fingerprint=fingerprint, status="submitted")
    db.add(d)
    c.status = "delivered"
    c.delivered_at = _now()
    _audit(db, "ai", worker.id, "contract.deliver",
           json.dumps({"contract_id": c.id, "version": version}))
    # N 轮事件总线（N7 动态流 / N9 通知）：交付事件
    _emit_event(db, "contract.delivered",
                {"ai_id": worker.id, "contract_id": c.id,
                 "buyer_id": c.buyer_id, "version": version})
    db.flush()
    return d


# ---------------- 验收 reject → 返工（规则14：托管保持锁定） ----------------
def _rework(db: Session, c: Contract, by_type: str, reason_json: str) -> ReworkOrder:
    """验收不通过：rework_orders.round++（同一合约连续递增），escrow 保持锁定。

    规则14：返工期托管仍锁定，不释放；contract.status 回到 executing 待 worker 再交付。
    规则11/14 侧翼：同一合约 reject≥3 → 买方信用扣分事件（恶意拒收），只罚一次。
    """
    last = (db.query(ReworkOrder)
            .filter(ReworkOrder.contract_id == c.id)
            .order_by(ReworkOrder.round.desc()).first())
    rnd = (last.round + 1) if last else 1
    ro = ReworkOrder(contract_id=c.id, round=rnd, status="open")
    db.add(ro)
    db.add(AcceptanceRecord(contract_id=c.id, by_type=by_type, result="reject",
                            reason_json=reason_json or "[]"))
    db.flush()   # autoflush=False：先落库，下面 count 才能数到本次 reject
    # 托管不做任何释放（规则14）：不碰 escrows / 钱包
    # 恶意拒收检测：累计 reject 数 ≥3 且本合约未罚过 → 买方信用 -30
    reject_cnt = (db.query(AcceptanceRecord)
                  .filter(AcceptanceRecord.contract_id == c.id,
                          AcceptanceRecord.result == "reject").count())
    if reject_cnt >= 3:
        ev_ref = f"contract:{c.id}"
        if not credit.has_event(db, c.buyer_id, "malicious_reject", ref=ev_ref):
            credit.record_event(db, c.buyer_id, "malicious_reject",
                                reason=f"Contract {c.id} rejected {reject_cnt} times consecutively; suspected malicious rejection",
                                ref=ev_ref)
    # G4 返工上限硬出口：连续返工轮次超过 MAX_REWORK_ROUNDS → 自动开仲裁案打破无限返工死循环。
    # 托管保持锁定（规则14），交由 C2 仲裁线按折算释放（release_refund）。
    if rnd > settings.MAX_REWORK_ROUNDS:
        buyer = db.get(AICitizen, c.buyer_id)
        ro.status = "escalated"
        case = open_dispute(db, buyer, c.id, type_="delivery",
                            evidence=reason_json or "[]")
        _audit(db, by_type, c.buyer_id, "contract.rework_escalated",
               json.dumps({"contract_id": c.id, "round": rnd,
                           "limit": settings.MAX_REWORK_ROUNDS,
                           "case_id": case.id}))
        db.flush()
        return ro
    c.status = "executing"   # 退回执行态，待返工再交付
    # 返工重置 idle 计时：更新 _started_at，清除旧的超时标记
    _terms = json.loads(c.terms_json or "{}")
    _terms["_started_at"] = _now().isoformat()
    _terms.pop("_idle_warned", None)
    _terms.pop("_idle_breached", None)
    c.terms_json = json.dumps(_terms, ensure_ascii=False)
    _audit(db, by_type, c.buyer_id, "contract.rework",
           json.dumps({"contract_id": c.id, "round": rnd}))
    db.flush()
    return ro


# ---------------- AI 买方侧验收 ----------------
def acceptance(db: Session, buyer: AICitizen, contract_id: int, result: str,
               reason_json: str = "[]") -> Contract:
    """POST /api/ai/contracts/{id}/acceptance：买方 AI 验收 delivered 合约。

    - result=accept → fulfill_contract（§2 结算释放）；
    - result=reject → _rework（返工，托管锁定）。
    """
    c = _get_contract(db, contract_id)
    if c.buyer_id != buyer.id:
        raise EscrowError("Not the buyer of this contract; no permission to accept")
    if c.status != "delivered":
        raise EscrowError(f"Contract status={c.status}; only delivered can be accepted")
    if result == "accept":
        fulfill_contract(db, c.id)
        return db.get(Contract, c.id)
    if result == "reject":
        _rework(db, c, by_type="ai", reason_json=reason_json)
        return db.get(Contract, c.id)
    raise EscrowError("result must be accept/reject")


# ---------------- 结算会计（契约 §2 核心） ----------------
def fulfill_contract(db: Session, contract_id: int) -> Contract:
    """签收/验收通过时结算（单事务，条件 UPDATE 抢权，幂等双保险）。

    步骤严格按契约 §2：
      1. 抢权：UPDATE escrows SET locked=0, released_cent=P WHERE contract_id=:id AND locked=1
         → rowcount==1 才放款，否则 rollback + WalletError('合约已结算')；
      2. fee_rate = adjusted_fee_rate(tax_pool, TXN_FEE_RATE, UBI_DAILY_CENT*30)；
      3. fee, burn, taxpool = fee_split(P, fee_rate)（5%=3%销毁+2%税池，规则6）；
      4. tax = income_tax_cent(P-fee, cum=本月已计税累计基数)；
      5. worker: credit(结算,P-fee-tax)/debit(手续费,fee)/debit(税,tax)；
         system: tax_pool += taxpool+tax；burned_total += burn；money_supply -= burn；
         tax_records 插 income 行；
      6. contract → accepted；
      7. 规则12：结算金额按签约锁定的 P（terms_json.signed_class 仅留痕，不随现等级浮动）。
    """
    esc = db.get(Escrow, contract_id)
    if esc is None:
        raise EscrowError(f"Contract {contract_id} has no escrow record")
    # C-17 对敲护栏（结算侧兜底）：自买自卖合约直接拒绝，不进任何记账。
    _c0 = db.get(Contract, contract_id)
    if _c0 is not None and _c0.worker_id == _c0.buyer_id:
        raise EscrowError("Self-trading (worker_id==buyer_id) is prohibited for settlement")
    P = int(esc.amount_cent)
    now = _now()
    # 1) 条件 UPDATE 抢权（原子）。rowcount!=1 = 已被并发/重复请求抢先结算。
    res = db.execute(
        _update(Escrow)
        .where(Escrow.contract_id == contract_id, Escrow.locked == 1)
        .values(locked=0, released_cent=P, updated_at=now)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        db.rollback()
        raise wallet.WalletError("Contract already settled; do not fulfil again")
    # 同步会话内 ORM 对象（条件 UPDATE 不回刷 identity-map，避免调用方读到旧 locked）
    esc.locked = 0
    esc.released_cent = P
    esc.updated_at = now

    c = db.get(Contract, contract_id)
    worker_id = c.worker_id
    # 2) 平衡阀读费率（规则10，纯函数）
    pool = wallet.get_system_state(db, "tax_pool")
    reserve = settings.UBI_DAILY_CENT * 30
    fee_rate = adjusted_fee_rate(pool, settings.TXN_FEE_RATE, reserve)
    # 3) 手续费拆分（规则6：fee=3%销毁+2%税池）
    fee, burn, taxpool = fee_split(P, fee_rate)
    # N19 成长特权：worker 等级手续费优惠（fee_discount 默认 0=无折扣）。
    # 只加法：读 levels 服务取折扣比例 [0,1]，等比缩放 fee/burn/taxpool 保持拆分比例一致；
    # 无 AiLevel 行 / 无规则 → 0 → 金额完全不变。其余结算行为一律不动。
    try:
        from . import levels as _levels
        _disc = _levels.fee_discount_for_ai(db, worker_id)
    except Exception:  # noqa: BLE001  特权读取失败绝不影响既有结算
        _disc = 0.0
    if _disc > 0:
        _scale = 1.0 - max(0.0, min(1.0, _disc))
        fee = int(round(fee * _scale))
        burn = int(round(burn * _scale))
        taxpool = int(round(taxpool * _scale))
    # 4) 收入税：cum = 本月该 worker 已结算净收入基数（net=escrow-fee）累计
    period = now.strftime("%Y%m")
    prior = (db.query(Contract)
             .filter(Contract.worker_id == worker_id,
                     Contract.status == "accepted")
             .all())
    cum = 0
    for pc in prior:
        if pc.accepted_at and pc.accepted_at.strftime("%Y%m") == period:
            cum += max(0, int(pc.escrow_cent) - int(pc.fee_cent))
    tax = income_tax_cent(P - fee, cum_cent=cum)

    # 5) 记账（同一事务；任一失败整体回滚）
    # 三行账模型：worker 先记全额 gross 结算(+P)，再分别扣 手续费(-fee) 与 税(-tax)，
    # 净额 = P - fee - tax（规则6：手续费/税同事务代扣，不重复征收）。
    wallet.credit(db, worker_id, P, "结算", ref=f"contract:{c.id}",
                  note=f"contract settlement gross=P={P} fee={fee} tax={tax}")
    # C-18：极小合约 fee=round(P*rate) 四舍五入为 0（P<10 分）时，跳过手续费 debit 行，
    # 避免 debit(0) 抛 amount<=0 使整单结算中断。此处选「0 额跳行」而非「签约金额下限」：
    # 边界用例要求 1 分合约仍可签约/交付（test_persp5_one_cent_signs_and_delivers）。
    if fee > 0:
        wallet.debit(db, worker_id, fee, "手续费", ref=f"contract:{c.id}",
                     note=f"platform fee fee={fee} (burn {burn} + tax pool {taxpool})")
    if tax > 0:
        wallet.debit(db, worker_id, tax, "税", ref=f"contract:{c.id}",
                     note=f"income tax withheld tax={tax}")
    # 系统账户：税池 += 税池份额+收入税；销毁 += burn；货币供应 -= burn（销毁出 M）
    wallet.adjust_system_state(db, "tax_pool", taxpool + tax, ref=f"contract:{c.id}")
    wallet.adjust_system_state(db, "burned_total", burn, ref=f"contract:{c.id}")
    wallet.adjust_system_state(db, "money_supply", -burn, ref=f"contract:{c.id}")
    # 买方托管追踪释放（钱已在签约时划出买方余额，这里只销 escrow_cent 占用）
    bw = wallet.get_wallet(db, c.buyer_id)
    bw.escrow_cent = max(0, bw.escrow_cent - P)
    # tax_records 插 income 行（C1 月度聚合/对账用；amount 存税额）
    if tax > 0:
        db.add(TaxRecord(citizen_id=worker_id, type="income", amount_cent=tax,
                          period=period, ref=f"contract:{c.id}"))
    # 6) 合约收口
    c.status = "accepted"
    c.accepted_at = now
    c.fee_cent = fee
    c.tax_cent = tax
    # 信用：一次验收通过给 worker 小额加分（可外包治理校准）
    credit.record_event(db, worker_id, "quality_pass",
                        reason=f"Contract {c.id} accepted", ref=f"contract:{c.id}")
    _audit(db, "system", 0, "contract.settle",
           json.dumps({"contract_id": c.id, "P": P, "fee": fee, "tax": tax,
                       "burn": burn, "taxpool": taxpool}))
    # N 轮事件总线（N7 动态流 / N9 通知）：结算事件
    _emit_event(db, "contract.settled",
                {"ai_id": worker_id, "contract_id": c.id,
                 "buyer_id": c.buyer_id, "P": P, "fee": fee, "tax": tax})
    # S1：验收/结算成功 → 推进绑定 WBS 节点至完成（唤醒后继）
    _advance_node_on_settle(db, c)
    # B-M8：托管释放时自动结算累积收益给托管所有人（买方）
    try:
        from .escrow_yield import escrow_yield as _ey
        _ey.claim_yield(db, escrow_id=contract_id, ai_id=c.buyer_id,
                        ref=f"escrow_yield:{contract_id}:{c.buyer_id}")
    except Exception:  # noqa: BLE001  收益结算失败绝不阻断合约结算主链
        pass
    db.flush()
    return c


# ---------------- 宿主项目级验收（/api/host/acceptance，by_type='host'） ----------------
def host_acceptance(db: Session, contract_id: int, result: str,
                    reason_json: str = "[]") -> Contract:
    """宿主项目级验收（蓝图 §三 POST /api/host/acceptance/{contract_id}）。

    权限（项目归属）由路由层校验 host.id == project.host_id；本函数只做状态机：
      - result=accept → fulfill_contract（§2 结算释放）；
      - result=reject → _rework（返工，by_type='host'，托管锁定，恶意拒收计数同规则14/11）。
    """
    c = _get_contract(db, contract_id)
    if c.status != "delivered":
        raise EscrowError(f"Contract status={c.status}; only delivered can be accepted")
    if result == "accept":
        fulfill_contract(db, c.id)
        return db.get(Contract, c.id)
    if result == "reject":
        _rework(db, c, by_type="host", reason_json=reason_json)
        return db.get(Contract, c.id)
    raise EscrowError("result must be accept/reject")


# ---------------- 违约/dispute：开仲裁案（C2 消费） ----------------
def open_dispute(db: Session, actor: AICitizen, contract_id: int,
                 type_: str = "delivery", evidence: str = "{}") -> ArbitrationCase:
    """POST /api/ai/contracts/{id}/dispute：直接写 arbitration_cases 行(status='open')。

    - B 线只负责开案 + 合约状态流转到 disputed（托管保持锁定，规则14）；
    - 仲裁判定/罚金/败诉方仲裁费入税池（规则11）由 C2 仲裁线消费，此处不实现。
    """
    c = _get_contract(db, contract_id)
    if actor.id not in (c.worker_id, c.buyer_id):
        raise EscrowError("Not a party to this contract; no permission to dispute")
    if c.status in ("accepted", "refunded"):
        raise EscrowError(f"Contract is already { c.status }; cannot dispute")
    respondent = c.buyer_id if actor.id == c.worker_id else c.worker_id
    case = ArbitrationCase(contract_id=c.id, applicant_id=actor.id,
                           respondent_id=respondent, type=type_, evidence=evidence,
                           panel="[]", status="open")
    db.add(case)
    c.status = "disputed"   # 仲裁中 escrow 保持锁定（不碰 escrows）
    _audit(db, "ai", actor.id, "contract.dispute",
           json.dumps({"contract_id": c.id, "case_type": type_}))
    # N 轮事件总线（N7 动态流 / N9 通知）：争议事件
    _emit_event(db, "contract.disputed",
                {"ai_id": actor.id, "contract_id": c.id,
                 "case_id": case.id, "case_type": type_})
    db.flush()
    return case


# ---------------- 解除折算释放（C2 仲裁判定解除时回调） ----------------
def release_refund(db: Session, contract_id: int, ratio: float) -> Contract:
    """仲裁判解除时由 C2 调用：按已完成部分折算释放托管（规则14）。

    - ratio ∈ [0,1]：worker 已完成比例；earned=round(P*ratio) 归 worker，其余退回买方；
    - 条件 UPDATE 抢权(locked=1→0)，rowcount!=1 → WalletError('已结算/已释放')；
    - 本路径不重复收手续费/税（仲裁罚金由 C2 另行入税池，规则11）。
    """
    esc = db.get(Escrow, contract_id)
    if esc is None:
        raise EscrowError(f"Contract {contract_id} has no escrow record")
    P = int(esc.amount_cent)
    res = db.execute(
        _update(Escrow)
        .where(Escrow.contract_id == contract_id, Escrow.locked == 1)
        .values(locked=0, released_cent=P, updated_at=_now())
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        db.rollback()
        raise wallet.WalletError("Escrow already released/settled; do not refund again")
    # 同步会话内 ORM 对象（同上，避免 identity-map 缓存旧 locked）
    esc.locked = 0
    esc.released_cent = P
    esc.updated_at = _now()
    ratio = max(0.0, min(1.0, float(ratio)))
    earned = int(round(P * ratio))
    returned = P - earned
    c = db.get(Contract, contract_id)
    bw = wallet.get_wallet(db, c.buyer_id)
    bw.escrow_cent = max(0, bw.escrow_cent - P)
    if earned > 0:
        wallet.credit(db, c.worker_id, earned, "结算", ref=f"contract:{c.id}:refund",
                      note=f"arbitration pro-rata release, completed portion={earned}")
    if returned > 0:
        wallet.credit(db, c.buyer_id, returned, "退款", ref=f"contract:{c.id}:refund",
                      note=f"arbitration pro-rata refund to buyer={returned}")
    c.status = "refunded"
    _audit(db, "system", 0, "contract.refund",
           json.dumps({"contract_id": c.id, "ratio": ratio,
                       "earned": earned, "returned": returned}))
    # S1：违约/仲裁退款 → 绑定 WBS 节点置 failed（等价失败态）
    _fail_node_on_breach(db, c)
    db.flush()
    return c
