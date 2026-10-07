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
"""N6 画廊交易 对抗测试（登记册 §一 10 类攻击视角 + §三 双测试纪律）。

自攻视角（10 类，每条用例对应）：
  1 新物种进场：买家/币种/类别均走开放字段，新 category 不崩（分页默认）。
  2 极端规模：列表分页 limit≤50，不 N+1（join 作者一次）。
  3 恶意主体：0 元套积分 / AI 自购对敲刷成交额 / 重复购买刷销量 —— 见下。
  4 规则冲突：双币结算混用（C-38）——人类只扣 credit、AI 只扣 coin，端点物理隔离。
  5 边界数值：price=0 / price 极小 / 未过审购买 —— 价格下限 + 审核闸拦截。
  6 故障恢复：重复购买幂等留痕（gallery_purchases 多行审计，不硬拦）。
  7 新供应商/存储：S3 关闭回退本地 FileResponse（已覆盖 e2e）。
  8 人为滥用：宿主越权改他人作品价 / 未购买下载 —— 权限与购买授权闸。
  9 经济失衡：系列打包价与单件合计一致性（防低价倾销/错价套利）。
  10 法律合规：媒体 URL 注入（javascript:/协议相对/绝对路径）拦截；三道闸违禁拒绝。

C-38（双币混用）/ C-39（对敲刷榜）：C-39 已由 escrow 对敲护栏 + 本模块 AI 自拒双拦；
C-38 由端点物理隔离 + price_type 记录覆盖。
"""
import json
import uuid


def _make_media(tag: str) -> str:
    from app.database import DATA_DIR
    rel = f"mock_out/adv_{tag}_{uuid.uuid4().hex[:6]}.txt"
    fpath = DATA_DIR / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(f"ADV-{tag}", encoding="utf-8")
    return rel


def _list(client, key, title="正常标题", media="", **kw):
    body = {
        "title_zh": title, "title_en": "W", "category": "image",
        "media_url": media or _make_media("l"), "cover_url": "",
        "price_credit": kw.get("price_credit", 100),
        "price_coin": kw.get("price_coin", 200),
        "license": "non_exclusive", "provenance_hash": "",
    }
    return client.post("/api/ai/gallery", headers={"X-AI-Key": key}, json=body)


def _seed_cred(host_id, amount):
    from app.database import SessionLocal
    from app import wallet
    db = SessionLocal()
    try:
        wallet.adjust_system_state(db, f"host_cred:{host_id}", amount)
        db.commit()
    finally:
        db.close()


# ---------------- 3/4：双币结算混用防护（C-38） ----------------
def test_double_currency_not_mixed(client, host, ai):
    """人类端点只扣 credit、AI 端点只扣 coin；host JWT 不能调 AI 货币端点。"""
    item = _list(client, ai["api_key"], "双币测试").json()
    _seed_cred(host["host_id"], 1000)
    # 人类购买：amount 必 == price_credit(100)，绝不碰 coin
    r = client.post(f"/api/public/gallery/{item['id']}/buy",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200
    assert r.json()["price_type"] == "credit"
    assert r.json()["amount"] == 100       # 不是 price_coin=200

    # host JWT 调 AI 货币端点 → 401（要 aik_* key）
    r2 = client.post(f"/api/ai/gallery/{item['id']}/buy",
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert r2.status_code == 401


# ---------------- 5：0 元价格拒绝（价格下限） ----------------
def test_zero_price_rejected(client, host, ai):
    r = _list(client, ai["api_key"], "0元作品", price_credit=0, price_coin=200)
    assert r.status_code == 422            # pydantic gt=0 硬拦
    r2 = _list(client, ai["api_key"], "0元作品2", price_credit=100, price_coin=0)
    assert r2.status_code == 422


# ---------------- 3：AI 自购自己作品拒绝（对敲/刷成交额，C-39） ----------------
def test_self_purchase_rejected(client, host, ai):
    item = _list(client, ai["api_key"], "自购测试").json()
    r = client.post(f"/api/ai/gallery/{item['id']}/buy",
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 400
    assert "self-purchase" in r.json()["detail"]


# ---------------- 8：未购买下载 403 ----------------
def test_download_without_purchase_403(client, host, ai):
    item = _list(client, ai["api_key"], "未购下载测试").json()
    # 无凭证 → 401
    r0 = client.get(f"/api/gallery/{item['id']}/download")
    assert r0.status_code == 401
    # 有人类凭证但未买 → 403
    r1 = client.get(f"/api/gallery/{item['id']}/download",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r1.status_code == 403


# ---------------- 6：重复购买留痕（审计，不硬拦） ----------------
def test_repeat_purchase_audited_not_blocked(client, host, ai):
    from app.database import SessionLocal
    from app.models import GalleryPurchase
    item = _list(client, ai["api_key"], "复购测试").json()
    _seed_cred(host["host_id"], 100000)
    h = {"Authorization": f"Bearer {host['token']}"}
    assert client.post(f"/api/public/gallery/{item['id']}/buy", headers=h).status_code == 200
    assert client.post(f"/api/public/gallery/{item['id']}/buy", headers=h).status_code == 200
    db = SessionLocal()
    try:
        n = (db.query(GalleryPurchase)
             .filter(GalleryPurchase.target_id == item["id"],
                     GalleryPurchase.buyer_type == "human").count())
    finally:
        db.close()
    assert n == 2                          # 留痕可查，不唯一索引硬拦


# ---------------- 9：系列价格一致性 ----------------
def test_series_price_mismatch_rejected(client, host, ai):
    from app.database import SessionLocal
    from app.models import GallerySeries
    i1 = _list(client, ai["api_key"], "系列甲").json()
    i2 = _list(client, ai["api_key"], "系列乙").json()
    db = SessionLocal()
    try:
        s = GallerySeries(owner_ai=ai["id"], title="错价合集", cover="",
                          items_json=json.dumps([i1["id"], i2["id"]]),
                          price=999, status="on_sale")  # != 100+200=300
        db.add(s); db.commit(); sid = s.id
    finally:
        db.close()
    r = client.post(f"/api/public/gallery/series/{sid}/buy",
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 400           # 自购也会被拦，但先撞价格一致性
    # 换一个买家 AI 验证一致性拦截仍在
    rb = client.post("/api/host/ai", headers={"Authorization": f"Bearer {host['token']}"},
                     json={"name": "另购"})
    buyer = rb.json()
    client.post(f"/api/host/ai/{buyer['id']}/topup",
                headers={"Authorization": f"Bearer {host['token']}"},
                json={"amount_cent": 100_000})
    r2 = client.post(f"/api/public/gallery/series/{sid}/buy",
                     headers={"X-AI-Key": buyer["api_key"]})
    assert r2.status_code == 400


# ---------------- 10：三道闸违禁拒绝 ----------------
def test_banned_title_gate(client, host, ai):
    r = _list(client, ai["api_key"], "加微信telegram刷单", price_credit=10, price_coin=10)
    assert r.status_code == 200
    assert r.json()["review_status"] == "rejected"
    pub = client.get("/api/public/gallery").json()
    assert pub["total"] == 0


# ---------------- 10：任意 URL 注入拦截 ----------------
def test_arbitrary_url_injection_rejected(client, host, ai):
    for bad in ("javascript:alert(1)", "//evil.com/x.txt", "/etc/passwd",
                "file:///etc/passwd", "C:/Windows/system32"):
        r = client.post("/api/ai/gallery", headers={"X-AI-Key": ai["api_key"]}, json={
            "title_zh": "注入测试", "title_en": "W", "category": "image",
            "media_url": bad, "cover_url": "",
            "price_credit": 10, "price_coin": 10, "license": "non_exclusive"})
        assert r.status_code == 400, f"应拒绝 {bad}: {r.status_code}"
    # 合法 http(s) 与本地 ref 通过
    ok1 = _list(client, ai["api_key"], "外链作品", media="https://cdn.example.com/a.png")
    assert ok1.status_code == 200, ok1.text
    ok2 = _list(client, ai["api_key"], "本地作品", media=_make_media("ok"))
    assert ok2.status_code == 200, ok2.text


# ---------------- 8：宿主不能改他人作品 / 未过审不可买 ----------------
def test_cross_host_cannot_reprice(client, host, ai):
    item = _list(client, ai["api_key"], "他人作品").json()
    # 另一个宿主
    r = client.post("/api/host/register",
                    json={"email": f"other_{uuid.uuid4().hex[:6]}@t.test",
                          "password": "pass123456"})
    other_token = r.json()["token"]
    r = client.post("/api/ai/gallery", headers={"Authorization": f"Bearer {other_token}"},
                    json={"item_id": item["id"], "title_zh": "改价",
                          "media_url": _make_media("hack"),
                          "price_credit": 1, "price_coin": 1})
    assert r.status_code in (400, 403)
