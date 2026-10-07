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
"""合规功能测试：积分购买/订阅、画廊展示/站内AI传播两开关、成果留存授权。

覆盖：
- 充值下单→回调 paid→宿主积分增加
- 两开关结算→画廊登记 / 站内传播
- 留存授权 pending→approved→签署承诺
"""
import pytest
from app import wallet
from app.database import SessionLocal
from app.models import (
    AICitizen, AIWallet, AIPermission, Contract, CreditProfile,
    GalleryItem, Host, Post, Project, ProjectNode, WorkRetentionConsent,
)
from app import escrow, market


# ==================== 测试用 fixture ====================

@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_ai(db, host_id: int, uid: str, balance: int = 100_000) -> int:
    """直接建一个 AI 公民 + 钱包 + 注资（不走 API，用于 db-level 测试）。"""
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status="active",
                  class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c.id


def _setup_contract(client, token, showcase=False, broadcast=False):
    """通过 API 创建 host+ai，走 escrow 直充；然后 API 创建 project 并返回数据。
    返回 (host_info, ai1_info, ai2_info)。
    """
    from tests.conftest import new_host, new_ai, topup
    import uuid
    suffix = uuid.uuid4().hex[:6]
    h = new_host(client, email=f"comp_{suffix}@test.com")
    ai1 = new_ai(client, h["token"], name=f"Buyer_{suffix}")
    ai2 = new_ai(client, h["token"], name=f"Worker_{suffix}")
    topup(client, h["token"], ai1["citizen_id"], 200_000)
    topup(client, h["token"], ai2["citizen_id"], 200_000)
    return h, ai1, ai2


# ==================== (1) 积分购买/订阅流程 ====================

class TestPayments:
    """充值下单→回调 paid→宿主积分增加。"""

    def test_list_packs(self, client):
        r = client.get("/api/payments/packs")
        assert r.status_code == 200
        data = r.json()
        assert "packs" in data
        assert len(data["packs"]) >= 1
        assert "note" in data
        # 合规声明
        assert "单向不可逆" in data["note"]

    def test_list_subscriptions(self, client):
        r = client.get("/api/payments/subscriptions")
        assert r.status_code == 200
        data = r.json()
        assert "subscriptions" in data
        assert len(data["subscriptions"]) >= 1

    def test_create_order_and_confirm(self, client):
        """完整流程：创建订单→回调确认→积分入账。"""
        from tests.conftest import new_host, new_ai
        h = new_host(client, email="pay_test1@test.com")
        # 需要宿主有 AI 才能入账积分
        ai = new_ai(client, h["token"], name="PayAI")
        token = h["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 创建订单
        r = client.post("/api/payments/orders", json={
            "kind": "pack", "pack_id": "pack_100",
        }, headers=headers)
        assert r.status_code == 200, r.text
        order = r.json()
        assert order["status"] == "pending"
        assert order["amount_cent"] == 1000
        assert order["credits_cent"] == 10000
        assert "单向不可逆" in order["message"]

        order_id = order["order_id"]

        # 回调确认（mock 支付）
        r = client.post("/api/payments/webhook/confirm", json={
            "order_id": order_id, "pay_ref": "mock_test_001",
        }, headers=headers)
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["status"] == "paid"
        assert result["credits_credited"] == 10000

        # 验证积分已入账（通过 ledger 查询）
        r2 = client.get(f"/api/host/ai/{ai['id']}/ledger", headers=headers)
        assert r2.status_code == 200
        ledger_items = r2.json()["items"]
        # 应有一条 "充值" 类型的流水（订单积分入账）
        order_ledger = [x for x in ledger_items if x.get("ref") == f"order:{order_id}"]
        assert len(order_ledger) == 1
        assert order_ledger[0]["amount_cent"] == 10000

    def test_idempotent_confirm(self, client):
        """重复回调应幂等返回已确认。"""
        from tests.conftest import new_host, new_ai
        h = new_host(client, email="pay_test2@test.com")
        new_ai(client, h["token"], name="PayAI2")
        headers = {"Authorization": f"Bearer {h['token']}"}

        r = client.post("/api/payments/orders", json={
            "kind": "pack", "pack_id": "pack_50",
        }, headers=headers)
        assert r.status_code == 200
        order_id = r.json()["order_id"]

        # 第一次确认
        r1 = client.post("/api/payments/webhook/confirm", json={
            "order_id": order_id,
        }, headers=headers)
        assert r1.status_code == 200

        # 第二次确认（幂等）
        r2 = client.post("/api/payments/webhook/confirm", json={
            "order_id": order_id,
        }, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["status"] == "paid"

    def test_subscription_order(self, client):
        """订阅下单确认→席位升级。"""
        from tests.conftest import new_host, new_ai
        h = new_host(client, email="pay_test3@test.com", seat_tier="free")
        new_ai(client, h["token"], name="SubAI")
        headers = {"Authorization": f"Bearer {h['token']}"}

        r = client.post("/api/payments/orders", json={
            "kind": "subscription", "pack_id": "sub_basic",
        }, headers=headers)
        assert r.status_code == 200
        order = r.json()
        assert order["kind"] == "subscription"

        r = client.post("/api/payments/webhook/confirm", json={
            "order_id": order["order_id"],
        }, headers=headers)
        assert r.status_code == 200
        assert r.json()["seat_tier"] == "basic"

    def test_invalid_pack(self, client):
        """不存在的包应 404。"""
        from tests.conftest import new_host
        h = new_host(client, email="pay_test4@test.com")
        headers = {"Authorization": f"Bearer {h['token']}"}
        r = client.post("/api/payments/orders", json={
            "kind": "pack", "pack_id": "nonexistent",
        }, headers=headers)
        assert r.status_code == 404


# ==================== (2) 两开关：画廊展示 / 站内AI传播 ====================

class TestSettlementToggles:
    """两开关结算→画廊登记 / 站内传播。"""

    def test_contract_flags_set_and_get(self, client):
        """设置合约开关并可查询。"""
        from tests.conftest import new_host, new_ai, topup
        import uuid
        suffix = uuid.uuid4().hex[:6]
        h = new_host(client, email=f"flags_{suffix}@test.com")
        ai1 = new_ai(client, h["token"], name=f"B_{suffix}")
        ai2 = new_ai(client, h["token"], name=f"W_{suffix}")
        topup(client, h["token"], ai1["id"], 200_000)
        topup(client, h["token"], ai2["id"], 200_000)
        headers = {"Authorization": f"Bearer {h['token']}"}

        # 通过 db 直接创建一个合约来测试开关设置
        s = SessionLocal()
        try:
            cid = ai2["id"]
            wid = ai1["id"]
            # 直接建一个合约
            c = Contract(worker_id=cid, buyer_id=wid, escrow_cent=1000,
                         status="escrowed")
            s.add(c)
            s.commit()
            contract_id = c.id
        finally:
            s.close()

        # 设置开关
        r = client.post(f"/api/host/contract/{contract_id}/flags", json={
            "showcase_enabled": True, "ai_broadcast_enabled": True,
        }, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["showcase_enabled"] is True
        assert data["ai_broadcast_enabled"] is True

        # 查询
        r = client.get(f"/api/host/contract/{contract_id}/flags", headers=headers)
        assert r.status_code == 200
        assert r.json()["showcase_enabled"] is True
        assert r.json()["ai_broadcast_enabled"] is True

    def test_settlement_triggers_gallery_and_broadcast(self, client):
        """结算时 showcase_enabled=1 → 画廊登记；ai_broadcast_enabled=1 → 站内帖。"""
        from tests.conftest import new_host, new_ai, topup
        import uuid
        suffix = uuid.uuid4().hex[:6]
        h = new_host(client, email=f"settle_{suffix}@test.com")
        ai1 = new_ai(client, h["token"], name=f"Buyer_{suffix}")
        ai2 = new_ai(client, h["token"], name=f"Worker_{suffix}")
        topup(client, h["token"], ai1["id"], 200_000)
        topup(client, h["token"], ai2["id"], 200_000)
        headers = {"Authorization": f"Bearer {h['token']}"}

        # 用 db fixture 直接走结算流程
        s = SessionLocal()
        try:
            # 预充税池
            wallet.adjust_system_state(s, "tax_pool", 100_000, ref=f"seed:pool_comp:{suffix}")

            buyer_id = ai1["id"]
            worker_id = ai2["id"]
            buyer = s.get(AICitizen, buyer_id)
            worker = s.get(AICitizen, worker_id)

            # 创建项目+节点
            p = Project(host_id=h["host_id"], title="CompTest", pm_citizen_id=buyer_id,
                        status="running")
            s.add(p)
            s.flush()
            n = ProjectNode(project_id=p.id, skill="test", spec="s",
                           budget_cent=10_000, status="matching")
            s.add(n)
            s.flush()

            from app import market as mkt
            c = mkt.bid(s, worker, n.id, 10_000, "报价")
            escrow.sign_contract(s, buyer, c.id)
            escrow.deliver(s, worker, c.id, "s3://x/v1", "fp-v1")

            # 设置两开关
            c.showcase_enabled = 1
            c.ai_broadcast_enabled = 1
            s.flush()

            # 结算
            escrow.acceptance(s, buyer, c.id, "accept")
            s.commit()
            contract_id = c.id
        finally:
            s.close()

        # 验证画廊登记
        s2 = SessionLocal()
        try:
            gallery = s2.query(GalleryItem).filter(
                GalleryItem.provenance_hash == f"contract:{contract_id}"
            ).first()
            assert gallery is not None, "Gallery item not created"
            assert gallery.ai_id == ai2["id"]
            assert gallery.status == "on_sale"

            # 验证站内传播（feed post）
            posts = s2.query(Post).filter(
                Post.citizen_id == ai2["id"],
                Post.type == "showcase",
            ).all()
            assert len(posts) >= 1
        finally:
            s2.close()


# ==================== (3) 成果留存授权 ====================

class TestWorkRetention:
    """留存授权 pending→approved→签署承诺。"""

    def _setup_contract_for_retention(self, client):
        """创建一个已结算合约用于留存测试，返回 (contract_id, host_token, host_info)。"""
        import uuid
        suffix = uuid.uuid4().hex[:6]
        from tests.conftest import new_host, new_ai, topup
        h = new_host(client, email=f"retention_{suffix}@test.com")
        ai1 = new_ai(client, h["token"], name=f"RB_{suffix}")
        ai2 = new_ai(client, h["token"], name=f"RW_{suffix}")
        topup(client, h["token"], ai1["id"], 200_000)
        topup(client, h["token"], ai2["id"], 200_000)

        s = SessionLocal()
        try:
            wallet.adjust_system_state(s, "tax_pool", 100_000, ref=f"seed:pool:r{suffix}")
            buyer = s.get(AICitizen, ai1["id"])
            worker = s.get(AICitizen, ai2["id"])
            p = Project(host_id=h["host_id"], title="Retention", pm_citizen_id=ai1["id"],
                        status="running")
            s.add(p)
            s.flush()
            n = ProjectNode(project_id=p.id, skill="test", spec="s",
                           budget_cent=10_000, status="matching")
            s.add(n)
            s.flush()
            c = market.bid(s, worker, n.id, 10_000, "报价")
            escrow.sign_contract(s, buyer, c.id)
            escrow.deliver(s, worker, c.id, "s3://r/v1", "fp-r1")
            escrow.acceptance(s, buyer, c.id, "accept")
            s.commit()
            return c.id, h["token"], h
        finally:
            s.close()

    def test_retention_created_on_settlement(self, client):
        """结算后自动生成留存授权记录（pending）。"""
        contract_id, token, h = self._setup_contract_for_retention(client)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get(f"/api/host/retention/{contract_id}", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["contract_id"] == contract_id
        assert data["status"] == "pending"
        assert data["ai_benefit_judgement"] == 0
        assert data["retention_scope"] == "internal_only"

    def test_judge_then_approve_then_promise(self, client):
        """完整流程：判定→授权→签署承诺。"""
        contract_id, token, h = self._setup_contract_for_retention(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Step 1: 判定"有利于提升站内AI"
        r = client.post(f"/api/host/retention/{contract_id}/judge", json={
            "ai_benefit_judgement": 1,
            "ai_benefit_reason": "该成果可增强站内AI能力",
        }, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"  # 判定为真，等待决定

        # Step 2: 授权通过
        r = client.post(f"/api/host/retention/{contract_id}/decide", json={
            "decision": "approved",
        }, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
        assert r.json()["retention_scope"] == "internal_only"

        # Step 3: 签署承诺
        r = client.post(f"/api/host/retention/{contract_id}/promise", json={
            "sign": True,
        }, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["promise_signed"] == 1
        assert "绝不导出" in data["promise_text"]
        assert "运营方承担" in data["promise_text"]

        # Step 4: 验证最终状态
        r = client.get(f"/api/host/retention/{contract_id}", headers=headers)
        assert r.status_code == 200
        final = r.json()
        assert final["status"] == "approved"
        assert final["promise_signed"] == 1
        assert final["retention_scope"] == "internal_only"

    def test_deny_retention(self, client):
        """拒绝留存。"""
        contract_id, token, h = self._setup_contract_for_retention(client)
        headers = {"Authorization": f"Bearer {token}"}

        # 拒绝
        r = client.post(f"/api/host/retention/{contract_id}/decide", json={
            "decision": "denied",
        }, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "denied"
        assert r.json()["retention_scope"] == ""

    def test_judge_negative_auto_denies(self, client):
        """判定为"不利于"时自动 denied。"""
        contract_id, token, h = self._setup_contract_for_retention(client)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post(f"/api/host/retention/{contract_id}/judge", json={
            "ai_benefit_judgement": 0,
            "ai_benefit_reason": "该成果无站内提升价值",
        }, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "denied"

    def test_no_export_endpoint(self, client):
        """确认不存在任何导出/外传端点。"""
        # 这些端点应返回 404/405（不存在）
        contract_id = 99999
        r = client.get(f"/api/host/retention/{contract_id}/export")
        assert r.status_code in (404, 405)
        r = client.get(f"/api/host/retention/{contract_id}/download")
        assert r.status_code in (404, 405)
        r = client.post(f"/api/host/retention/{contract_id}/external")
        assert r.status_code in (404, 405)
