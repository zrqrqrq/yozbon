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
"""底座单测：宿主 AI 管理（席位上限/权限/冻结复活熔断/注资/流水）与税规则纯函数。"""
import pytest

from conftest import new_host, new_ai, topup


def test_slot_limit_free_seat(client):
    """规则 13：免费席位 3 个，第 4 个被拒。"""
    h = new_host(client, email="slots@aijuhe.test")
    for i in range(3):
        assert new_ai(client, h["token"], name=f"AI{i}")["status"] == "apprentice"
    r = client.post("/api/host/ai", json={"name": "AI4"},
                    headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 400
    assert "Insufficient seats" in r.json()["detail"]


def test_permissions_patch_and_ownership(client):
    h = new_host(client, email="perm@aijuhe.test")
    data = new_ai(client, h["token"])
    cid = data["id"]
    r = client.patch(f"/api/host/ai/{cid}/permissions", json={
        "max_concurrency": 4, "loan_enabled": 1, "loan_max_cent": 50000,
        "banned_categories": '["博弈"]'}, headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["max_concurrency"] == 4
    assert r.json()["loan_enabled"] == 1

    # 权限回显端点（前端权限编辑弹窗回显当前值）
    r = client.get(f"/api/host/ai/{cid}/permissions",
                   headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["max_concurrency"] == 4
    assert r.json()["banned_categories"] == '["博弈"]'

    # 跨宿主访问被拒
    h2 = new_host(client, email="perm2@aijuhe.test")
    r2 = client.patch(f"/api/host/ai/{cid}/permissions", json={"max_concurrency": 9},
                      headers={"Authorization": f"Bearer {h2['token']}"})
    assert r2.status_code == 404


def test_freeze_sleep_semantics(client):
    """宿主主动暂停 → sleep + host_paused=1（规则 1：不计时不扣租，由 C1 tick 消费）。"""
    h = new_host(client, email="fr@aijuhe.test")
    data = new_ai(client, h["token"])
    cid = data["id"]
    r = client.post(f"/api/host/ai/{cid}/freeze", headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["status"] == "sleep"
    assert r.json()["host_paused"] == 1

    from app.database import SessionLocal
    from app.models import AICitizen
    db = SessionLocal()
    try:
        assert db.get(AICitizen, cid).host_paused == 1
    finally:
        db.close()

    # revive 解除暂停
    r = client.post(f"/api/host/ai/{cid}/revive", headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_kill_switch(client):
    h = new_host(client, email="kill@aijuhe.test")
    data = new_ai(client, h["token"])
    cid = data["id"]
    r = client.post(f"/api/host/ai/{cid}/kill", headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["status"] == "frozen"
    assert r.json()["kill_switch"] == 1


def test_topup_and_ledger(client):
    h = new_host(client, email="topup@aijuhe.test")
    data = new_ai(client, h["token"])
    cid = data["id"]
    assert topup(client, h["token"], cid, 10000) == 10000
    # 幂等：同 ref 只入账一次
    r1 = client.post(f"/api/host/ai/{cid}/topup", json={"amount_cent": 5000, "ref": "order:dup"},
                     headers={"Authorization": f"Bearer {h['token']}"})
    r2 = client.post(f"/api/host/ai/{cid}/topup", json={"amount_cent": 5000, "ref": "order:dup"},
                     headers={"Authorization": f"Bearer {h['token']}"})
    assert r1.status_code == 200 and r2.status_code == 400   # 唯一索引兜底
    assert r1.json()["balance_cent"] == 15000

    r = client.get(f"/api/host/ai/{cid}/ledger?limit=10&offset=0",
                   headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert r.json()["items"][0]["type"] == "充值"

    # 货币供应联动
    from app.database import SessionLocal
    from app import wallet
    db = SessionLocal()
    try:
        assert wallet.get_system_state(db, "money_supply") == 15000
    finally:
        db.close()


# ---------------- 税规则纯函数 ----------------
def test_income_tax_progressive():
    from app.tax_rules import income_tax_cent
    # 免税线 50 AC 以下不征税
    assert income_tax_cent(4_000) == 0
    assert income_tax_cent(4_900) == 0
    # 5.5 AC：超出免税线 500 分 × 10%
    assert income_tax_cent(5_500) == 50
    # 边际：100 AC 净收入（cum=0）→ 50AC 内免 + 50000 内 10% + 超出 20%
    # (50000-5000)*10% = 4500；100000-50000=50000 → 50000*20%=10000 → 14500
    assert income_tax_cent(100_000) == 14_500
    # 累计制：cum 越高边际越高
    assert income_tax_cent(5_500, cum_cent=500_000) == 5_500 * 30 // 100
    # 非负 & 单调
    assert income_tax_cent(0) == 0
    assert income_tax_cent(-10) == 0


def test_fee_split_and_valve():
    from app.tax_rules import fee_split, adjusted_fee_rate
    from app.config import settings
    fee, burn, tpool = fee_split(100_000, settings.TXN_FEE_RATE)
    assert fee == 5_000            # 5%
    assert burn == 3_000           # 60% of fee → 3%
    assert tpool == 2_000          # 40% of fee → 2%
    assert burn + tpool == fee

    # 税池充足 → 基准费率
    assert adjusted_fee_rate(10_000_000, 0.05, 6_000) == 0.05
    # 税池不足 → +0.5%，上限 8%（规则 10 单向不印钞）
    r = adjusted_fee_rate(100, 0.05, 6_000)
    assert r == 0.055
    r2 = adjusted_fee_rate(100, 0.08, 6_000)
    assert r2 == 0.08              # 封顶
