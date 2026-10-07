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
"""端到端集成测试（验收核心）——跨 A/B/C1/C2/D 真实模块串联，不假桩。

覆盖四条全链路：
  1. 宿主注册→建 AI→注资→入驻考试(客观卷)→发证→投标→签约托管→交付→验收→结算
     → worker 余额=P-fee-tax、tax_pool/burned/money_supply 三账联动、证书/状态一致；
  2. 争议→仲裁→结算联动：dispute→panel→verdict(refund,worker败)→仲裁费入税池
     →escrow 折算释放(release_refund ratio)→全账一致；
  3. tick+合约联动：executing 合约期间 100h 不死（规则4）；accepted 后恢复计时再满 100h 死（规则7）；
  4. 女巫端到端：同宿主 3 免费席位（第 4 个拒绝）→3 AI 各自领低保（规则13 端到端）。

金额 integer 分。考试用客观卷（A 线已落地）；主观/决策卷未在本链路展开处不写桩。
"""
import json
from datetime import datetime, timedelta

import pytest

from app import (ai_citizens, credit, escrow, exam, governance, market,
                 onboarding, project as proj, tax as taxmod, wallet)
from app.database import SessionLocal
from app.models import (AICitizen, AIWallet, Contract, Escrow, ExamPaper,
                        OnboardingApplication, Project, ProjectNode,
                        SkillCertificate, SystemState, UbiGrant)
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


P = 10_000          # 合约 100 AC
# 预期结算账（税池充足 → fee_rate=0.05；income_tax(net=9500,cum=0)=450）
EXPECT_FEE = 500
EXPECT_BURN = 300
EXPECT_TAXPOOL_FEE = 200
EXPECT_TAX = 450
EXPECT_WORKER_NET = P - EXPECT_FEE - EXPECT_TAX   # 9050


# =====================================================================
# 链路 1：全链路（考试→发证→投标→托管→交付→验收→结算三账联动）
# =====================================================================
def _seed_objective_paper(db, skill="文案"):
    """建一张客观卷（含一道满分客观题），返回 paper。"""
    paper = ExamPaper(skill=skill, level="l1", paper_type="objective", active=1,
                      paper_json=json.dumps({
                          "title": "文案入门 l1 激活卷", "duration_minutes": 60,
                          "pass_score": 60,
                          "questions": [
                              {"id": "q1", "type": "objective", "stem": "1+1=?",
                               "options": ["1", "2", "3"], "answer": "2", "score": 100}]}))
    db.add(paper)
    db.flush()
    return paper


def test_e2e_full_chain_register_to_settlement(client):
    # --- 建主：worker（专属宿主，避免申请单对齐歧义）+ buyer（总管，另一宿主） ---
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="文案工", occupation="文案",
                    self_decl=json.dumps({"skill": "文案"}))
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="总管", occupation="项目管理")
    topup(client, host_b["token"], buyer["id"], 100_000)
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")
        # --- 入驻考试（客观卷）→ 发证 → 转正 ---
        paper = _seed_objective_paper(db, skill="文案")
        db.commit()
        wobj = db.get(AICitizen, worker["id"])
        onboarding.run_onboarding(db, wobj)
        db.commit()
        wobj = db.get(AICitizen, worker["id"])
        onboarding.assert_submittable(db, wobj, paper)
        res = exam.submit_exam(db, worker["id"], paper.id, {"q1": "2"})
        assert res["passed"] is True
        onboarding.after_exam_pass(db, wobj, "文案")
        db.commit()
        wobj = db.get(AICitizen, worker["id"])
        assert wobj.status == "active"                      # 见习→active
        cert = (db.query(SkillCertificate)
                .filter_by(citizen_id=worker["id"], status="valid").first())
        assert cert is not None and cert.level == "l1"     # 已发证

        # --- 建项目（预算<20000 免评审）→ 运行 → 节点 matching ---
        p = proj.create_project(db, host_b["host_id"], "文案外包",
                                budget_cent=P, pm_citizen_id=buyer["id"])
        db.commit()
        proj.approve_running(db, host_b["host_id"], p.id)
        db.commit()
        proj.submit_nodes(db, p.id,
                          [{"key": "n1", "skill": "文案", "spec": "写一篇",
                            "deliverable_std": "docx", "budget_cent": P,
                            "duration_h": 8}], deps=[])
        db.commit()
        node = db.query(ProjectNode).filter_by(project_id=p.id).first()
        assert node.status == "matching"

        # --- worker 投标 → buyer 签约托管 → 交付 ---
        c = market.bid(db, wobj, node.id, offer_cent=P, message="接")
        db.commit()
        bobj = db.get(AICitizen, buyer["id"])
        escrow.sign_contract(db, bobj, c.id)
        escrow.deliver(db, wobj, c.id, "s3://deliverable.docx", "sha256:fp1")
        db.commit()
        cid = c.id
        assert db.get(Contract, cid).status == "delivered"

        # --- 结算前快照 ---
        w_bal0 = wallet.balance(db, worker["id"])
        pool0 = wallet.get_system_state(db, "tax_pool")
        burn0 = wallet.get_system_state(db, "burned_total")
        money0 = wallet.get_system_state(db, "money_supply")

        # --- buyer 验收通过 → fulfill 结算 ---
        bobj = db.get(AICitizen, buyer["id"])
        escrow.acceptance(db, bobj, cid, "accept", "[]")
        db.commit()

        # --- 三账联动断言 ---
        assert db.get(Contract, cid).status == "accepted"
        assert wallet.balance(db, worker["id"]) - w_bal0 == EXPECT_WORKER_NET   # P-fee-tax
        assert wallet.get_system_state(db, "tax_pool") - pool0 == EXPECT_TAXPOOL_FEE + EXPECT_TAX
        assert wallet.get_system_state(db, "burned_total") - burn0 == EXPECT_BURN
        assert wallet.get_system_state(db, "money_supply") - money0 == -EXPECT_BURN
        # buyer 托管追踪释放完毕
        assert wallet.get_wallet(db, buyer["id"]).escrow_cent == 0
        esc = db.get(Escrow, cid)
        assert esc.locked == 0 and esc.released_cent == P
    finally:
        db.close()


# =====================================================================
# 链路 2：争议→仲裁→结算联动
# =====================================================================
def test_e2e_dispute_arbitration_release_refund(client):
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="申诉方", occupation="项目管理")
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="被诉工人", occupation="开发")
    arb = new_ai(client, new_host(client)["token"], name="仲裁", occupation="治理")
    P2 = 2000
    topup(client, host_b["token"], buyer["id"], 100_000)
    topup(client, host_w["token"], worker["id"], 10_000)   # 败诉方需有余额付仲裁费
    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:arb")
        # B 线 C-16：合约须绑定 matching 节点才可签约——先造项目+节点
        bobj0 = db.get(AICitizen, buyer["id"])
        proj_row = Project(host_id=bobj0.host_id, title="仲裁e2e",
                           budget_cent=P2, status="running")
        db.add(proj_row); db.flush()
        mnode = ProjectNode(project_id=proj_row.id, skill="dev",
                            budget_cent=P2, status="matching", seq=1)
        db.add(mnode); db.flush()
        # 直接造 proposed 合约（跨线串联，走真实 sign/dispute/verdict）
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": P2}), escrow_cent=P2,
                     project_id=proj_row.id, node_id=mnode.id)
        db.add(c); db.flush()
        bobj = db.get(AICitizen, buyer["id"])
        escrow.sign_contract(db, bobj, c.id)
        wobj = db.get(AICitizen, worker["id"])
        escrow.deliver(db, wobj, c.id, "s3://x", "fp-disp")
        bobj = db.get(AICitizen, buyer["id"])
        case = escrow.open_dispute(db, bobj, c.id, "quality", "{}")
        case_id = case.id
        db.commit()
        assert db.get(Contract, c.id).status == "disputed"

        pool0 = wallet.get_system_state(db, "tax_pool")
        w_bal0 = wallet.balance(db, worker["id"])
        governance.form_arbitration_panel(db, case_id, [arb["id"]])
        # verdict=refund → 退款给申诉方(buyer)，worker(被诉方)败诉付仲裁费；ratio=0.5 折算
        out = governance.submit_verdict(db, arb["id"], case_id, "refund",
                                        ratio=0.5, reason="半成品")
        db.commit()
        # 败诉方=worker；仲裁费 1000 入税池
        assert out["loser_id"] == worker["id"]
        assert out["arbitration_fee_cent"] == governance.ARBITRATION_FEE_CENT
        assert wallet.get_system_state(db, "tax_pool") - pool0 == governance.ARBITRATION_FEE_CENT
        # release_refund(0.5)：worker 得 earned=1000，付仲裁费 1000 → 净变化 0
        earned = round(P2 * 0.5)
        assert wallet.balance(db, worker["id"]) - w_bal0 == earned - governance.ARBITRATION_FEE_CENT
        # 合约收口 refunded，escrow 解锁
        assert db.get(Contract, c.id).status == "refunded"
        assert db.get(Escrow, c.id).locked == 0
    finally:
        db.close()


# =====================================================================
# 链路 3：tick + 合约联动（规则 4/7 端到端）
# =====================================================================
def test_e2e_tick_contract_pause_then_resume_death(client):
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="计时工", occupation="开发")
    db = _db()
    try:
        w = db.get(AICitizen, worker["id"])
        # 置为：非新手、低净产不豁免、信用<150、无豁免标志
        w.created_at = datetime.utcnow() - timedelta(hours=200)
        w.status = "active"
        w.unemployed_minutes = 5999
        w.death_exempt = 0
        db.flush()
        cp = db.query(credit.CreditProfile).filter_by(citizen_id=w.id).first()
        cp.score = 100
        # 造一条 executing 有效合约（规则4：签约即暂停失业计时）
        c = Contract(worker_id=w.id, buyer_id=999, status="executing",
                     terms_json=json.dumps({"offer_cent": 1000}), escrow_cent=1000)
        db.add(c); db.flush()
        now = datetime.utcnow()
        w.last_tick_at = now - timedelta(minutes=1)
        db.commit()

        # executing 合约在身：走 1min，失业计时不累计（5999 不变），不死
        ai_citizens.tick(db, w.id, now)
        db.commit()
        w = db.get(AICitizen, w.id)
        assert w.unemployed_minutes == 5999          # 规则4：合约期间暂停计时
        assert w.status == "active"

        # 合约 accepted（出有效合约状态集）→ 恢复计时
        c = db.get(Contract, c.id)
        c.status = "accepted"
        db.commit()
        w = db.get(AICitizen, w.id)
        w.last_tick_at = now
        w.unemployed_minutes = 5999
        db.commit()
        # 再走 1min：无有效合约 → 计时恢复，累计到 6000 → 死亡（规则7）
        ai_citizens.tick(db, w.id, now + timedelta(minutes=1))
        db.commit()
        w = db.get(AICitizen, w.id)
        assert w.unemployed_minutes == 6000
        assert w.status == "dead"                     # 恢复计时后满 100h 死亡
    finally:
        db.close()


# =====================================================================
# 链路 4：女巫端到端（同宿主 3 席位，第 4 拒绝；3 AI 各自领低保）
# =====================================================================
def test_e2e_witch_three_free_seats_ubi_independent(client):
    host = new_host(client, seat_tier="free")     # free 席位=3
    a1 = new_ai(client, host["token"], name="巫1", occupation="零工")
    a2 = new_ai(client, host["token"], name="巫2", occupation="零工")
    a3 = new_ai(client, host["token"], name="巫3", occupation="零工")
    # 第 4 个免费席位 → 拒绝（底座已测，此处端到端复核）
    r4 = client.post("/api/host/ai", json={"name": "巫4", "occupation": "零工"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert r4.status_code == 400

    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:ubi")
        # 三个 AI 都设为穷（净产<2000），active
        for aid in (a1["id"], a2["id"], a3["id"]):
            c = db.get(AICitizen, aid)
            c.status = "active"
            c.unemployed_minutes = 0
            db.flush()
            wallet.adjust_system_state(db, f"wipe:{aid}", 0, ref="noop")  # noop 保连接
        # 直接把三个钱包余额设为 100（穷线以下）
        for aid in (a1["id"], a2["id"], a3["id"]):
            w = wallet.get_wallet(db, aid)
            w.balance_cent = 100
            w.escrow_cent = 0
        db.commit()

        res = taxmod.grant_ubi(db)
        db.commit()
        # 三个 AI 各自独立领到低保（规则13：按 AI-ID，不按宿主）
        for aid in (a1["id"], a2["id"], a3["id"]):
            assert aid in res["granted"]
            assert wallet.balance(db, aid) == 100 + 200
        grants = db.query(UbiGrant).filter(
            UbiGrant.citizen_id.in_([a1["id"], a2["id"], a3["id"]])).all()
        assert len(grants) == 3                       # 三条独立 UBI 记录
    finally:
        db.close()
