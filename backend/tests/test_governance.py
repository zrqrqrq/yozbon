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
"""C2 线治理任务市场 + 专家评审 + 仲裁 测试（蓝图 §四 L11/L12 / 规则 11）。

覆盖：
- 治理市场 open→bidding→assigned→reviewed→paid 全路径、竞标唯一、税池收支；
- 规则 11：败诉方付仲裁费入税池（余额/税池断言）；
- 规则 11：同一申请人累计败诉≥3 → 双倍扣信用；
- 仲裁 verdict → escrow 折算释放（B 线 release_refund 已落地，真实联动，不跳过）；
- events 聚合正确性。
"""
import json

import pytest

from app.database import SessionLocal
from app.models import (AICitizen, ArbitrationCase, Contract, CreditEvent,
                        Escrow, Project, ProjectNode)
from app import governance, wallet
from app.governance import GovError, ARBITRATION_FEE_CENT
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


# ---------------- 治理任务市场状态机 ----------------
def test_gov_market_lifecycle_and_taxpool(client, ai):
    """open→bidding→assigned→reviewed→paid 全路径；报酬从税池出。"""
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 100000, ref="seed:test")
    pool_before = wallet.get_system_state(db, "tax_pool")

    t = governance.publish_task(db, "audit", {"scope": "女巫识别"},
                                 budget_cent=500, deadline=None)
    assert t.status == "open"

    # 竞标
    governance.bid_task(db, ai["id"], t.id, price_cent=400, message="我能做")
    db.refresh(t)
    assert t.status == "bidding"

    # 指派 + 执行 + 复核 + 结算
    governance.assign_task(db, t.id, ai["id"])
    db.refresh(t)
    assert t.status == "assigned"
    governance.submit_task_report(db, ai["id"], t.id, "clean", {"note": "无异常"})
    governance.review_task(db, t.id, "pass", quality_score=1.0)
    db.refresh(t)
    assert t.status == "reviewed"

    bal_before = wallet.balance(db, ai["id"])
    governance.settle_task(db, t.id)
    db.refresh(t)
    assert t.status == "paid"
    # 中标 AI 拿到 500 分报酬
    assert wallet.balance(db, ai["id"]) == bal_before + 500
    # 税池支出 500
    assert wallet.get_system_state(db, "tax_pool") == pool_before - 500
    db.close()


def test_gov_bid_unique(client, ai):
    """同一 AI 对同一任务只能竞标一次。"""
    db = _db()
    t = governance.publish_task(db, "credit", {}, budget_cent=100, deadline=None)
    governance.bid_task(db, ai["id"], t.id, price_cent=90)
    with pytest.raises(GovError):
        governance.bid_task(db, ai["id"], t.id, price_cent=95)  # 重复竞标
    db.close()


# ---------------- 仲裁 setup（走 B 线真实签约+托管+开案） ----------------
def _setup_dispute(host_token, buyer, worker, P=2000):
    """造一个真实的「已签约托管 + 已开仲裁案」：buyer 申诉 worker。

    返回 (contract_id, case_id)。钱书：buyer 签约时 debit P 锁定托管。
    """
    from app import escrow
    db = _db()
    # B 线 C-16：合约必须绑定一个 matching 节点才可签约——先造项目+节点
    proj_row = Project(host_id=db.get(AICitizen, buyer["id"]).host_id,
                       title="仲裁测试项目", budget_cent=P, status="running")
    db.add(proj_row)
    db.flush()
    node = ProjectNode(project_id=proj_row.id, skill="dev", budget_cent=P,
                       status="matching", seq=1)
    db.add(node)
    db.flush()
    c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                 terms_json=json.dumps({"offer_cent": P}), escrow_cent=P,
                 project_id=proj_row.id, node_id=node.id)
    db.add(c)
    db.flush()
    cid = c.id
    buyer_obj = db.get(AICitizen, buyer["id"])
    escrow.sign_contract(db, buyer_obj, cid)          # buyer debit P，escrow locked=1
    buyer_obj = db.get(AICitizen, buyer["id"])
    case = escrow.open_dispute(db, buyer_obj, cid, type_="delivery")  # buyer 申诉
    case_id = case.id
    db.commit()
    db.close()
    return cid, case_id


def _arbiter_setup(client):
    """建一名仲裁 AI（独立宿主/职业）。"""
    h = new_host(client)
    arb = new_ai(client, h["token"], name="仲裁员", occupation="治理")
    return arb


def test_rule_11_loser_pays_arbitration(client, ai):
    """规则 11：败诉方付仲裁费入税池（余额/税池断言）。

    buyer 申诉 worker，仲裁支持 worker（support_worker）→ 申诉方 buyer 败诉：
      - buyer 钱包 debit 仲裁费 ARBITRATION_FEE_CENT；
      - tax_pool += 仲裁费；
      - escrow 折算释放（ratio=1.0 → worker 拿全额 P）。
    """
    h = ai["host"]
    buyer = ai
    # worker（另一宿主）
    wh = new_host(client)
    worker = new_ai(client, wh["token"], name="工人", occupation="开发")
    topup(client, h["token"], buyer["id"], 100000)
    arb = _arbiter_setup(client)

    cid, case_id = _setup_dispute(h["token"], buyer, worker, P=2000)

    db = _db()
    pool0 = wallet.get_system_state(db, "tax_pool")   # 用例间表已清空，初始为 0
    buyer_bal0 = wallet.balance(db, buyer["id"])
    governance.form_arbitration_panel(db, case_id, [arb["id"]])
    db.commit()
    db.close()

    db = _db()
    out = governance.submit_verdict(db, arb["id"], case_id, "support_worker",
                                     ratio=1.0, reason="申诉不成立")
    db.commit()
    db.close()

    assert out["loser_id"] == buyer["id"]
    assert out["arbitration_fee_cent"] == ARBITRATION_FEE_CENT
    assert out["escrow_released"] is True

    db = _db()
    # buyer 余额 = 签约后余额 - 仲裁费（release_refund ratio=1.0 不退 buyer）
    assert wallet.balance(db, buyer["id"]) == buyer_bal0 - ARBITRATION_FEE_CENT
    # 税池 + 仲裁费
    assert wallet.get_system_state(db, "tax_pool") == pool0 + ARBITRATION_FEE_CENT
    # escrow 已释放
    esc = db.get(Escrow, cid)
    assert esc.locked == 0
    c = db.get(Contract, cid)
    assert c.status == "refunded"
    db.close()


def test_rule_11_abuse_double_penalty(client, ai):
    """规则 11：同一申请人累计败诉 ≥3 → 第 3 次信用扣分双倍（-20）。"""
    h = ai["host"]
    buyer = ai
    wh = new_host(client)
    worker = new_ai(client, wh["token"], name="工人", occupation="开发")
    topup(client, h["token"], buyer["id"], 200000)
    arb = _arbiter_setup(client)

    deltas = []
    for i in range(3):
        cid, case_id = _setup_dispute(h["token"], buyer, worker, P=1000)
        db = _db()
        governance.form_arbitration_panel(db, case_id, [arb["id"]])
        db.commit()
        db.close()
        db = _db()
        out = governance.submit_verdict(db, arb["id"], case_id, "support_worker",
                                         ratio=1.0, reason="滥用申诉")
        db.commit()
        db.close()
        deltas.append(out["credit_events"][0]["delta"])

    # 前两次 -10，第三次累计败诉达 3 → 双倍 -20
    assert deltas[0] == -10
    assert deltas[1] == -10
    assert deltas[2] == -20

    db = _db()
    evs = (db.query(CreditEvent)
           .filter(CreditEvent.citizen_id == buyer["id"],
                   CreditEvent.event == "malicious_arbitration")
           .order_by(CreditEvent.id.asc()).all())
    assert [e.delta for e in evs] == [-10, -10, -20]
    db.close()


# ---------------- LLM 执行体接线（蓝图 §1.0，echo 确定性） ----------------
def test_llm_executor_echo(client, ai):
    """LLM_PROVIDER=echo：治理任务执行体走 LLM 通道，返回确定性结构化结论（不发网络）。"""
    db = _db()
    t = governance.publish_task(db, "audit", {"scope": "女巫识别"},
                               budget_cent=500, deadline=None)
    out = governance.run_task_handler(db, t)
    db.close()
    # echo 通道确定性返回 + 标注 via_llm
    assert out["evidence"].get("via_llm") is True
    assert out["conclusion"] == "ok"


def test_llm_complete_echo_deterministic(client, ai):
    """llm_complete 在 echo 下返回可解析 JSON，且不触发真实网络。"""
    raw = governance.llm_complete("[gov:review] test prompt")
    parsed = json.loads(raw)
    assert parsed["evidence"]["echo"] is True


# ---------------- 规则执行体结论合法性（不变量：handler 结论 ∈ TASK_CONCLUSIONS） ----------------
def test_rule_handlers_return_legal_conclusions(client, ai):
    """G1/G2 回归护栏：每个规则占位执行体产出的 conclusion 必须落入该类型合法集。

    历史缺陷：_h_arbitrate 返回 'pending_verdict'、_h_credit 返回 'no_adjust'，
    均不在 TASK_CONCLUSIONS 合法集内，违反第 66 行声明的「执行体产出与合法集一致」不变量。
    """
    db = _db()
    try:
        offenders = []
        for type_, allowed in governance.TASK_CONCLUSIONS.items():
            handler = governance.TASK_HANDLERS.get(type_)
            if handler is None:
                continue
            t = governance.publish_task(db, type_, {}, budget_cent=300, deadline=None)
            db.flush()
            try:
                out = handler(db, t, {})
            except Exception:  # noqa: BLE001
                # 执行体本身报错（缺依赖事实）不算结论违规，跳过
                db.rollback()
                continue
            concl = out.get("conclusion")
            if concl not in allowed:
                offenders.append(f"{type_}: {concl!r} not in {sorted(allowed)}")
        assert not offenders, "非法结论：\n" + "\n".join(offenders)
    finally:
        db.close()



# ---------------- events 聚合 ----------------
def test_events_aggregation(client, ai):
    """GET /api/ai/events：聚合本 AI 的治理任务/合约/税务事件，分页倒序。"""
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 100000, ref="seed:ev")
    t = governance.publish_task(db, "audit", {}, budget_cent=300, deadline=None)
    governance.bid_task(db, ai["id"], t.id, price_cent=250)
    governance.assign_task(db, t.id, ai["id"])
    governance.submit_task_report(db, ai["id"], t.id, "clean", {})
    governance.review_task(db, t.id, "pass")
    governance.settle_task(db, t.id)
    db.commit()
    db.close()

    r = client.get("/api/ai/events", headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200, r.text
    body = r.json()
    types = [e["type"] for e in body["items"]]
    assert "gov_task" in types
    assert body["total"] >= 1
