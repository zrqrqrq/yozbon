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
"""N18 邀请推荐奖励测试（先攻后建）。

覆盖：
- 生成/列表邀请码（HTTP）
- 注册/建 AI 绑定 invite_code（pending，不发奖）
- 防羊毛：条件未达成不发奖；批量空注册无奖励；一码一人；自邀拒绝
- 结算：受邀 AI 充值后日巡检发 bootstrap；受邀宿主名下 AI 结算后发 bounty 到邀请方积分
- 幂等：重复结算不重复发
"""
import pytest

from app import escrow, invites as svc, market, wallet
from app.database import SessionLocal
from app.models import (AICitizen, AIWallet, AIPermission, Contract,
                        CreditProfile, Host, Invite, Project, ProjectNode,
                        SystemState)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_host(db, hid=None, credit=100):
    h = Host(id=hid, email=f"h{hid or 'x'}@t.test", password_hash="x",
             host_credit=credit, nickname="h")
    db.add(h)
    db.flush()
    return h


def _mk_ai(db, host_id, uid, balance=0):
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status="active")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    if balance:
        wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}")
    return c


def _settle_a_contract(db, worker_id, buyer_id, P=10_000):
    """跑通 bid→sign→deliver→accept 结算，返回 contract。"""
    buyer = db.get(AICitizen, buyer_id)
    worker = db.get(AICitizen, worker_id)
    proj = Project(host_id=worker.host_id, title="P", pm_citizen_id=buyer_id,
                   status="running", budget_cent=P)
    db.add(proj)
    db.flush()
    node = ProjectNode(project_id=proj.id, skill="文案", spec="s", budget_cent=P,
                       status="matching")
    db.add(node)
    db.flush()
    c = market.bid(db, worker, node.id, P, "offer")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "ref", "fp-x")
    escrow.acceptance(db, buyer, c.id, "accept")
    return c


# ---------------- HTTP：生成/列表 ----------------
def test_create_and_list_invites(client, host):
    r = client.post("/api/host/invites",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert code and r.json()["status"] == "pending"
    r2 = client.get("/api/host/invites",
                    headers={"Authorization": f"Bearer {host['token']}"})
    codes = [x["code"] for x in r2.json()["items"]]
    assert code in codes


# ---------------- 绑定：注册用邀请码 → pending 不发奖 ----------------
def test_register_bind_invite_no_reward_yet(client, host, db):
    code = client.post("/api/host/invites",
                       headers={"Authorization": f"Bearer {host['token']}"}
                       ).json()["code"]
    # 新宿主注册带邀请码
    r = client.post("/api/host/register", json={
        "email": f"new_{code}@t.test", "password": "pass123456",
        "invite_code": code})
    assert r.status_code == 200, r.text
    inv = db.query(Invite).filter(Invite.code == code).first()
    assert inv.invitee_id != 0 and inv.status == "pending"
    # 条件未达成 → 不发奖
    paid = svc.settle_pending(db)
    assert paid == 0
    db.refresh(inv)
    assert inv.status == "pending"


# ---------------- adversarial：假码/自邀/重复绑定不发奖 ----------------
def test_adversarial_fake_code_self_invite_no_reward(db):
    inviter = _mk_host(db, hid=101)
    code = svc.create_invite(db, inviter.id).code
    db.commit()
    # 假码
    assert svc.bind_invite(db, "NOPE", 999) is None
    # 自邀
    own = _mk_host(db, hid=102)
    assert svc.bind_invite(db, code, inviter.id) is None
    # 一个 AI 绑定
    ai = _mk_ai(db, 102, "inv-ai-1")
    bound = svc.bind_invite(db, code, ai.id)
    assert bound is not None
    # 已绑定的码再绑给别人 → None
    other = _mk_ai(db, 102, "inv-ai-2")
    assert svc.bind_invite(db, code, other.id) is None
    # 无充值无结算 → 不发奖
    assert svc.settle_pending(db) == 0


# ---------------- 受邀 AI 充值后结算 bootstrap + 幂等 ----------------
def test_ai_invitee_topup_settles_bootstrap_idempotent(db):
    inviter = _mk_host(db, hid=201)
    code = svc.create_invite(db, inviter.id).code
    ai = _mk_ai(db, 202, "boot-ai")
    svc.bind_invite(db, code, ai.id)
    db.commit()
    before = wallet.balance(db, ai.id)
    # 到账充值
    wallet.credit(db, ai.id, 1000, "充值", ref="order:boot")
    db.commit()
    paid = svc.settle_pending(db)
    assert paid == 1
    db.refresh(db.query(Invite).filter(Invite.code == code).first())
    inv = db.query(Invite).filter(Invite.code == code).first()
    assert inv.status == "accepted" and inv.reward_credit == svc.AI_BOOTSTRAP_CENT
    assert wallet.balance(db, ai.id) == before + 1000 + svc.AI_BOOTSTRAP_CENT
    # 幂等：再巡检不重复发
    assert svc.settle_pending(db) == 0
    assert wallet.balance(db, ai.id) == before + 1000 + svc.AI_BOOTSTRAP_CENT


# ---------------- 受邀宿主名下 AI 结算 → 邀请方积分 bounty ----------------
def test_host_invitee_settlement_bounty_to_inviter(db):
    inviter = _mk_host(db, hid=301)
    code = svc.create_invite(db, inviter.id).code
    invited = _mk_host(db, hid=302)
    svc.bind_invite(db, code, invited.id)
    # 受邀宿主名下 buyer/worker
    buyer = _mk_ai(db, 302, "h-buyer", balance=100_000)
    worker = _mk_ai(db, 302, "h-worker", balance=0)
    db.commit()
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")
    wallet.adjust_system_state(db, "money_supply", 100_000, ref="seed:ms")
    before_cred = wallet.get_system_state(db, f"host_cred:{inviter.id}", 0)
    # 名下 AI 完成一次真实结算
    _settle_a_contract(db, worker.id, buyer.id, P=10_000)
    db.commit()
    paid = svc.settle_pending(db, only_invitee_id=invited.id)
    assert paid == 1
    after_cred = wallet.get_system_state(db, f"host_cred:{inviter.id}", 0)
    assert after_cred == before_cred + svc.HOST_BOUNTY_CRED
    inv = db.query(Invite).filter(Invite.code == code).first()
    assert inv.status == "accepted"
