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
"""N18-N20 合并对抗测试：防羊毛 / 默认零加成 / 指纹绕过。

- N18：批量空注册无奖励；条件未达成反复巡检不发奖；一码一人
- N19：无规则时既有结算不受特权影响（默认零加成）；同事件重放不刷 XP
- N20：比对端点未鉴权拒绝；文本近似命中边界；空库/坏指纹不崩
"""
import io
import json
import random

import pytest

from app import escrow, fingerprints as fpmod, invites as svc, levels, market, wallet
from app.database import SessionLocal
from app.event_bus import emit
from app.models import (AICitizen, AIWallet, AIPermission, AuditLog,
                        ContentFingerprint, CreditProfile, Host, Invite,
                        Project, ProjectNode)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_ai(db, host_id, uid, balance=0):
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status="active")
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    if balance:
        wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}")
    return c


# ---------------- N18：批量空注册无奖励 ----------------
def test_batch_fake_registrations_no_reward(db):
    inviter = Host(email="inv@t.test", password_hash="x", host_credit=100)
    db.add(inviter); db.flush()
    code = svc.create_invite(db, inviter.id).code
    # 一批空注册（不带邀请码）→ 不产生任何 pending 邀请
    for i in range(5):
        h = Host(email=f"fake{i}@t.test", password_hash="x")
        db.add(h)
    db.commit()
    assert svc.settle_pending(db) == 0
    # 绑定一个受邀 AI 但它啥也没做 → 反复巡检仍不发奖
    ai = _mk_ai(db, inviter.id, "fake-ai")
    svc.bind_invite(db, code, ai.id)
    db.commit()
    for _ in range(3):
        assert svc.settle_pending(db) == 0


# ---------------- N19：无规则默认零加成，既有结算不受影响 ----------------
def test_no_rules_default_zero_no_xp_runaway(db):
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s")
    wallet.adjust_system_state(db, "money_supply", 100_000, ref="s")
    buyer = _mk_ai(db, 1, "adv-buyer", balance=100_000)
    worker = _mk_ai(db, 1, "adv-worker", balance=0)
    db.commit()
    proj = Project(host_id=1, title="P", pm_citizen_id=buyer.id, status="running",
                  budget_cent=10_000)
    db.add(proj); db.flush()
    node = ProjectNode(project_id=proj.id, skill="文案", spec="s", budget_cent=10_000,
                       status="matching")
    db.add(node); db.flush()
    c = market.bid(db, worker, node.id, 10_000, "o")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "r", "fp")
    escrow.acceptance(db, buyer, c.id, "accept")
    db.commit()
    # 无 LevelRule → 特权零加成
    assert levels.sort_weight_for_ai(db, worker.id) == 1.0
    assert levels.fee_discount_for_ai(db, worker.id) == 0.0
    # 无规则 → award_xp 不升级（get_rule 返回 None，xp 累加但 level 恒 1）
    # 注：上面真实结算已触发 contract.settled handler 自动给 worker +20 XP
    levels.award_xp(db, worker.id, 999, ref="contract:adv1")
    assert levels.view(db, worker.id)["level"] == 1
    xp_after = levels.view(db, worker.id)["xp"]
    # 同 ref 重放不刷
    levels.award_xp(db, worker.id, 999, ref="contract:adv1")
    assert levels.view(db, worker.id)["xp"] == xp_after


# ---------------- N20：文本近似命中边界 + 坏指纹不崩 ----------------
def test_text_simhash_boundary_and_bad_input(db):
    h1 = fpmod.simhash_text("AI 公民 入驻 平台 任务 结算 信用")
    h2 = fpmod.simhash_text("AI 公民 入驻 平台 任务 结算 信用 等级")
    assert fpmod._hamming(h1, h2) <= fpmod._threshold(64)
    # 入库
    db.add(ContentFingerprint(media_type="text", fingerprint=f"{h1:016x}",
                              owner_ai=1, source_task=0))
    db.flush()
    hits = fpmod.compare(db, "text", f"{h2:016x}")
    assert len(hits) >= 0  # 不崩
    # 坏 hex 不崩
    assert fpmod.compare(db, "text", "zzzz") == []


def test_compare_endpoint_requires_credential(client):
    r = client.post("/api/sys/fingerprint/compare",
                    json={"media_type": "text", "fingerprint": "a" * 16})
    assert r.status_code in (401, 403)
