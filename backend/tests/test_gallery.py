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
"""N6 AI 作品画廊 + 交易 单元/端到端测试（社会功能扩展设计 §2 N6）。

覆盖真实数据链路（登记册 §六 真实数据验证要求）：
  1. AI 挂售（正常标题）→ 三道闸第一道通过 → on_sale/passed；
  2. 人类积分购买（host JWT）→ 扣积分 → 本地 FileResponse 下载 200 且正文命中；
  3. AI 货币购买（workflow key）→ escrow 托管 → 结算 → 卖家到账 95%（5% 平台）；
  4. 系列打包价 == 成员单件 price_coin 合计（一致性）；
  5. 三道闸：违禁词挂售 → rejected，不进公开流。

夹具复用 conftest：client/host/ai/new_host/new_ai/topup。
人类积分（host_cred）经 system_state 直接 seed（生产充值走 Creem 回调，待接）。
"""
import json
import uuid

import pytest


def _seed_host_cred(host_id: int, amount: int):
    """直接往 system_state 写人类积分余额（host_cred:<id>）。"""
    from app.database import SessionLocal
    from app import wallet
    db = SessionLocal()
    try:
        wallet.adjust_system_state(db, f"host_cred:{host_id}", amount)
        db.commit()
    finally:
        db.close()


def _make_media(tag: str) -> str:
    """在 DATA_DIR/mock_out 下落一个文件，返回本地 ref。"""
    from app.database import DATA_DIR
    rel = f"mock_out/n6_{tag}_{uuid.uuid4().hex[:6]}.txt"
    fpath = DATA_DIR / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(f"GALLERY-CONTENT-{tag}", encoding="utf-8")
    return rel


def _list_item(client, api_key: str, title: str, media_url: str,
               price_credit: int = 100, price_coin: int = 200,
               provenance_hash: str = "", series_id: int = 0) -> dict:
    r = client.post("/api/ai/gallery", headers={"X-AI-Key": api_key}, json={
        "title_zh": title, "title_en": "Work", "category": "image",
        "media_url": media_url, "cover_url": "",
        "price_credit": price_credit, "price_coin": price_coin,
        "license": "non_exclusive", "provenance_hash": provenance_hash,
        "series_id": series_id})
    return r


# ---------------- 1. 挂售 → 公开流可见 ----------------
def test_list_item_enters_public_flow(client, host, ai):
    rel = _make_media("list")
    r = _list_item(client, ai["api_key"], "江南水墨风景图", rel)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_status"] == "passed"
    assert body["status"] == "on_sale"
    # 公开流（匿名）能看到
    pub = client.get("/api/public/gallery").json()
    assert pub["total"] == 1
    assert pub["items"][0]["id"] == body["id"]
    assert pub["items"][0]["author_ai"]


# ---------------- 2. 人类积分购买 → 下载 200 ----------------
def test_human_credit_buy_then_download(client, host, ai):
    rel = _make_media("human")
    r = _list_item(client, ai["api_key"], "人类买家作品", rel,
                   price_credit=100, price_coin=200)
    item_id = r.json()["id"]

    # seed 人类积分
    _seed_host_cred(host["host_id"], 1000)

    # 人类积分购买（host JWT）
    rb = client.post(f"/api/public/gallery/{item_id}/buy",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["price_type"] == "credit"
    assert rb.json()["amount"] == 100

    # 下载 → 200 且正文命中（S3 关闭回退本地 FileResponse）
    rd = client.get(f"/api/gallery/{item_id}/download",
                   headers={"Authorization": f"Bearer {host['token']}"})
    assert rd.status_code == 200, rd.text
    assert f"GALLERY-CONTENT-human" in rd.text


# ---------------- 3. AI 货币购买 → escrow → 结算 95% ----------------
def test_ai_coin_buy_settles_95pct(client, host, ai):
    from app.database import SessionLocal
    from app import wallet
    # 卖家 = ai（已注资 100000 分）；买家 = 另一个 AI
    buyer = {
        "id": None, "api_key": None,
    }
    r = client.post("/api/host/ai", headers={"Authorization": f"Bearer {host['token']}"},
                    json={"name": "买家AI", "occupation": "收藏"})
    assert r.status_code == 200, r.text
    buyer = r.json()
    # 给买家注资
    rt = client.post(f"/api/host/ai/{buyer['id']}/topup",
                     headers={"Authorization": f"Bearer {host['token']}"},
                     json={"amount_cent": 100_000})
    assert rt.status_code == 200, rt.text

    rel = _make_media("coin")
    rl = _list_item(client, ai["api_key"], "AI货币作品", rel,
                    price_credit=100, price_coin=1000)
    item_id = rl.json()["id"]

    db = SessionLocal()
    try:
        seller_before = wallet.balance(db, ai["id"])
        buyer_before = wallet.balance(db, buyer["id"])
    finally:
        db.close()

    rb = client.post(f"/api/ai/gallery/{item_id}/buy",
                    headers={"X-AI-Key": buyer["api_key"]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["price_type"] == "coin"
    assert rb.json()["amount"] == 1000
    assert rb.json()["status"] == "completed"

    db = SessionLocal()
    try:
        seller_after = wallet.balance(db, ai["id"])
        buyer_after = wallet.balance(db, buyer["id"])
    finally:
        db.close()

    # 买家 -1000；卖家 +950（95%，50 平台分成）
    assert buyer_after == buyer_before - 1000
    assert seller_after == seller_before + 950


# ---------------- 4. 系列打包价 == 单件合计 ----------------
def test_series_bundle_price_consistency(client, host, ai):
    from app.database import SessionLocal
    from app.models import GallerySeries

    rel1 = _make_media("s1")
    rel2 = _make_media("s2")
    i1 = _list_item(client, ai["api_key"], "系列作品一", rel1, price_coin=100).json()
    i2 = _list_item(client, ai["api_key"], "系列作品二", rel2, price_coin=150).json()

    # 建系列（owner=卖家 AI），打包价 = 单件合计 250
    db = SessionLocal()
    try:
        s = GallerySeries(owner_ai=ai["id"], title="合集", cover="",
                           items_json=json.dumps([i1["id"], i2["id"]]),
                           price=250, status="on_sale")
        db.add(s)
        db.commit()
        series_id = s.id
        # 再建一个不一致的系列（price=200 != 250）
        bad = GallerySeries(owner_ai=ai["id"], title="坏合集", cover="",
                            items_json=json.dumps([i1["id"], i2["id"]]),
                            price=200, status="on_sale")
        db.add(bad)
        db.commit()
        bad_id = bad.id
    finally:
        db.close()

    # 买家 AI
    r = client.post("/api/host/ai", headers={"Authorization": f"Bearer {host['token']}"},
                    json={"name": "系列买家"})
    buyer = r.json()
    client.post(f"/api/host/ai/{buyer['id']}/topup",
                headers={"Authorization": f"Bearer {host['token']}"},
                json={"amount_cent": 100_000})

    # 一致系列 → 200
    ok = client.post(f"/api/public/gallery/series/{series_id}/buy",
                     headers={"X-AI-Key": buyer["api_key"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["amount"] == 250

    # 不一致系列 → 400
    badr = client.post(f"/api/public/gallery/series/{bad_id}/buy",
                       headers={"X-AI-Key": buyer["api_key"]})
    assert badr.status_code == 400, badr.text


# ---------------- 5. 三道闸：违禁词挂售 → rejected ----------------
def test_banned_title_rejected(client, host, ai):
    rel = _make_media("banned")
    r = _list_item(client, ai["api_key"], "加微信 刷单 免费领", rel,
                   price_credit=100, price_coin=200)
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "rejected"
    # 不进公开流
    pub = client.get("/api/public/gallery").json()
    assert pub["total"] == 0
    # 不可购买
    buy = client.post(f"/api/ai/gallery/{r.json()['id']}/buy",
                      headers={"X-AI-Key": ai["api_key"]})
    assert buy.status_code in (400, 403)
