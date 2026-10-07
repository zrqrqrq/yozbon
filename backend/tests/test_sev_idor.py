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
"""S5 严重级修复测试：二级市场买卖端点 IDOR（越权用他人 AI 钱包付款/挂他人资产）。

要点：buyer_id/seller_id 不可信客户端，必须由鉴权宿主派生并校验归属；
不符 → 403（越权）。归属校验在路由层、业务校验之前执行。

覆盖：
- 宿主用「他人 AI」作为 buyer 购买 → 403；
- 宿主用「他人 AI」作为 seller 上架 → 403；
- 宿主取消「他人 AI」的挂牌 → 403；
- 正向对照：宿主用自己名下 AI 正常上架/购买（不误伤合法流程）。
"""
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import AssetTransferListing  # noqa: E402
from tests.conftest import new_host, new_ai, topup  # noqa: E402


def _jwt(token):
    return {"Authorization": f"Bearer {token}"}


def _list_asset(client, host, seller_id, price_cent=10000):
    r = client.post("/api/marketplace/list", json={
        "seller_id": seller_id, "asset_type": "tool",
        "asset_id": 999, "price_cent": price_cent, "description": "s5"},
        headers=_jwt(host["token"]))
    return r


def test_buy_with_foreign_ai_returns_403(client):
    """宿主 A 试图用宿主 B 的 AI 钱包（buyer_id=B 的 AI）购买 → 403。"""
    hostA = new_host(client)
    hostB = new_host(client)
    aiB = new_ai(client, hostB["token"], name="B的AI")
    topup(client, hostB["token"], aiB["id"], 100_000)

    # B 正常上架自己的资产
    lr = _list_asset(client, hostB, aiB["id"])
    assert lr.status_code == 200, lr.text
    listing_id = lr.json()["id"]

    # A 越权：用 B 的 AI 作为 buyer 去付款
    br = client.post(f"/api/marketplace/{listing_id}/buy",
                     json={"buyer_id": aiB["id"]},
                     headers=_jwt(hostA["token"]))
    assert br.status_code == 403, br.text

    # 挂牌状态不应被越权请求改变
    db = SessionLocal()
    try:
        listing = db.get(AssetTransferListing, listing_id)
        assert listing.status == "listed"
    finally:
        db.close()


def test_list_with_foreign_ai_returns_403(client):
    """宿主 A 试图用自己名下的凭据、却把 seller_id 指向 B 的 AI 上架 → 403。"""
    hostA = new_host(client)
    hostB = new_host(client)
    aiB = new_ai(client, hostB["token"], name="B的AI")

    r = _list_asset(client, hostA, aiB["id"])
    assert r.status_code == 403, r.text


def test_cancel_foreign_listing_returns_403(client):
    """宿主 A 试图取消 B 名下 AI 的挂牌（伪造 seller_id=B 的 AI）→ 403。"""
    hostA = new_host(client)
    hostB = new_host(client)
    aiB = new_ai(client, hostB["token"], name="B的AI")

    lr = _list_asset(client, hostB, aiB["id"])
    assert lr.status_code == 200, lr.text
    listing_id = lr.json()["id"]

    r = client.post(f"/api/marketplace/{listing_id}/cancel",
                    json={"seller_id": aiB["id"]},
                    headers=_jwt(hostA["token"]))
    assert r.status_code == 403, r.text


def test_legitimate_list_and_buy_ok(client):
    """正向对照：A 用自己名下 AI 上架、B 用自己名下 AI 购买 → 合法成功，不误伤。"""
    hostA = new_host(client)
    hostB = new_host(client)
    aiA = new_ai(client, hostA["token"], name="A的AI")
    aiB = new_ai(client, hostB["token"], name="B的AI")
    topup(client, hostB["token"], aiB["id"], 100_000)

    # A 用自己名下 AI 上架
    lr = _list_asset(client, hostA, aiA["id"], price_cent=10000)
    assert lr.status_code == 200, lr.text
    listing_id = lr.json()["id"]

    # B 用自己名下 AI 购买
    br = client.post(f"/api/marketplace/{listing_id}/buy",
                     json={"buyer_id": aiB["id"]},
                     headers=_jwt(hostB["token"]))
    assert br.status_code == 200, br.text
    assert br.json()["status"] == "sold"

    db = SessionLocal()
    try:
        listing = db.get(AssetTransferListing, listing_id)
        assert listing.buyer_id == aiB["id"]
    finally:
        db.close()
