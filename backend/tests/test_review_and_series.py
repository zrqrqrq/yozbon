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
"""C-71（三道闸复核/人工终审端点）+ C-72（系列创建/管理端点）功能测试。

先攻后建（TDD 红→绿）。覆盖硬验收：
  C-71:
    - 治理复核拒绝 → 不进公开流、不可购买；
    - 治理复核放行 pending → 可上架、可购买；
    - 人工终审翻转已上架为 rejected → 下架；
    - 拒绝必须写 review_note（缺 note → 400）；
  C-72:
    - 自动计价 == 成员 price_coin 合计；显式错价 → 400；
    - 我的系列列表 / 公开详情脱敏；
    - 下架后购买 400；重新上架复核成员有效；
    - 删除无成交系列成功；删除已售系列 400；
    - PATCH 增删成员后价格重算 == 合计。

夹具复用 conftest：client/host/ai/new_ai/topup。治理级 AI 通过把 class_level
在库内置为 "governance" 得到（与 test_adversarial_scope.py 同口径）。
"""
import uuid


# ---------------- 共用助手 ----------------
def _make_media(tag: str) -> str:
    from app.database import DATA_DIR
    rel = f"mock_out/c7_{tag}_{uuid.uuid4().hex[:6]}.txt"
    fpath = DATA_DIR / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(f"C7-CONTENT-{tag}", encoding="utf-8")
    return rel


def _list(client, key, title, price_credit=100, price_coin=200, media=None):
    r = client.post("/api/ai/gallery", headers={"X-AI-Key": key}, json={
        "title_zh": title, "title_en": "Work", "category": "image",
        "media_url": media or _make_media("l"), "cover_url": "",
        "price_credit": price_credit, "price_coin": price_coin,
        "license": "non_exclusive", "provenance_hash": ""})
    return r


def set_class_level(ai_id: int, level: str = "governance"):
    from app.database import SessionLocal
    from app.models import AICitizen
    db = SessionLocal()
    try:
        c = db.get(AICitizen, ai_id)
        c.class_level = level
        db.commit()
    finally:
        db.close()


def _new_buyer(client, host, name="系列买家"):
    r = client.post("/api/host/ai",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"name": name})
    assert r.status_code == 200, r.text
    buyer = r.json()
    rt = client.post(f"/api/host/ai/{buyer['id']}/topup",
                     headers={"Authorization": f"Bearer {host['token']}"},
                     json={"amount_cent": 100_000})
    assert rt.status_code == 200, rt.text
    return buyer


# =====================================================================
# C-71：治理复核 / 人工终审
# =====================================================================
def test_c71_gov_reject_takes_off_public_and_blocks_buy(client, host, ai):
    """治理复核拒绝（抽检已上架内容）→ 不进公开流、不可购买。"""
    set_class_level(ai["id"])  # 同一 AI 升级为治理级（它也是作者，仅用于拿治理 key）
    gkey = ai["api_key"]

    item = _list(client, gkey, "抽检样品正常作品").json()
    iid = item["id"]
    # 上架后进公开流
    assert client.get("/api/public/gallery").json()["total"] == 1

    # 治理复核 → rejected
    r = client.post(f"/api/sys/gallery/{iid}/review",
                    headers={"X-AI-Key": gkey},
                    json={"verdict": "rejected", "note": "抽检发现隐性导流话术"})
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "rejected"

    # 不进公开流
    assert client.get("/api/public/gallery").json()["total"] == 0
    # 不可购买（_buyable 按 review_status != passed 拦）
    buy = client.post(f"/api/ai/gallery/{iid}/buy",
                      headers={"X-AI-Key": gkey})
    assert buy.status_code in (400, 403)


def test_c71_gov_approve_pending_then_listed_and_buyable(client, host, ai):
    """治理复核放行 pending（第一道 FLAG）→ 上架可购买。"""
    set_class_level(ai["id"])
    gkey = ai["api_key"]

    # FLAG 词 → 第一道闸 pending，不进公开流
    item = _list(client, gkey, "私聊出售接活详聊").json()
    assert item["review_status"] == "pending"
    assert client.get("/api/public/gallery").json()["total"] == 0

    # 治理复核 → passed
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    headers={"X-AI-Key": gkey},
                    json={"verdict": "passed", "note": "复核放行，确为正常作品"})
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "passed"
    # 上架进公开流
    pub = client.get("/api/public/gallery").json()
    assert pub["total"] == 1
    assert pub["items"][0]["id"] == item["id"]

    # 可被真实买家购买（换买家 AI，避免自购拦截）
    buyer = _new_buyer(client, host, "pending买家")
    bb = client.post(f"/api/ai/gallery/{item['id']}/buy",
                     headers={"X-AI-Key": buyer["api_key"]})
    assert bb.status_code == 200, bb.text


def test_c71_final_takedown_of_listed_item(client, host, ai):
    """人工终审翻转已上架内容为 rejected（下架）。"""
    set_class_level(ai["id"])
    gkey = ai["api_key"]
    item = _list(client, gkey, "终审核查作品").json()
    assert client.get("/api/public/gallery").json()["total"] == 1

    r = client.post(f"/api/sys/gallery/{item['id']}/final",
                    headers={"X-AI-Key": gkey},
                    json={"verdict": "rejected", "note": "运营终审：版权存疑，下架"})
    assert r.status_code == 200, r.text
    assert client.get("/api/public/gallery").json()["total"] == 0
    buy = client.post(f"/api/ai/gallery/{item['id']}/buy",
                      headers={"X-AI-Key": gkey})
    assert buy.status_code in (400, 403)


def test_c71_reject_requires_note(client, host, ai):
    """拒绝必须写 review_note；缺 note → 400。"""
    set_class_level(ai["id"])
    gkey = ai["api_key"]
    item = _list(client, gkey, "缺note测试作品").json()
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    headers={"X-AI-Key": gkey},
                    json={"verdict": "rejected", "note": ""})
    assert r.status_code == 400, r.text


def test_c71_host_jwt_can_review(client, host, ai):
    """宿主 JWT 作为人工终审人（运营）可执行治理复核。"""
    item = _list(client, ai["api_key"], "宿主复核作品").json()
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"verdict": "rejected", "note": "宿主运营驳回"})
    assert r.status_code == 200, r.text
    assert client.get("/api/public/gallery").json()["total"] == 0


# =====================================================================
# C-72：系列创建 / 管理
# =====================================================================
def _mk_series(client, key, n=2, prices=(100, 150)):
    items = [_list(client, key, f"系列成员{i}", price_coin=p).json()
             for i, p in enumerate(prices[:n])]
    return items


def test_c72_create_series_auto_price_equals_sum(client, host, ai):
    items = _mk_series(client, ai["api_key"])
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"title": "我的合集", "cover": "",
                          "item_ids": [it["id"] for it in items]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] == 100 + 150          # 自动计价 == 合计
    assert body["status"] == "on_sale"


def test_c72_create_series_explicit_price_ok(client, host, ai):
    items = _mk_series(client, ai["api_key"])
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"title": "显价合集", "cover": "",
                          "item_ids": [it["id"] for it in items], "price": 250})
    assert r.status_code == 200, r.text


def test_c72_create_series_wrong_price_400(client, host, ai):
    items = _mk_series(client, ai["api_key"])
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"title": "错价合集", "cover": "",
                          "item_ids": [it["id"] for it in items], "price": 999})
    assert r.status_code == 400, r.text


def test_c72_my_series_list_and_public_detail(client, host, ai):
    items = _mk_series(client, ai["api_key"])
    cr = client.post("/api/ai/gallery/series",
                     headers={"X-AI-Key": ai["api_key"]},
                     json={"title": "列表合集", "cover": "",
                           "item_ids": [it["id"] for it in items]})
    sid = cr.json()["id"]

    # 我的系列列表
    ml = client.get("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]})
    assert ml.status_code == 200, ml.text
    assert any(s["id"] == sid for s in ml.json()["items"])

    # 公开详情：脱敏，不泄露 owner_ai / items_json
    pub = client.get(f"/api/public/gallery/series/{sid}")
    assert pub.status_code == 200, pub.text
    pb = pub.json()
    assert pb["title"] == "列表合集"
    assert pb["price"] == 250
    assert "owner_ai" not in pb
    assert "items_json" not in pb
    assert len(pb["items"]) == 2


def test_c72_off_series_cannot_then_on_again(client, host, ai):
    items = _mk_series(client, ai["api_key"])
    sid = client.post("/api/ai/gallery/series",
                      headers={"X-AI-Key": ai["api_key"]},
                      json={"title": "上下架合集", "cover": "",
                            "item_ids": [it["id"] for it in items]}).json()["id"]
    buyer = _new_buyer(client, host, "上下架买家")

    # 下架 → 购买 400
    off = client.post(f"/api/ai/gallery/series/{sid}/off",
                      headers={"X-AI-Key": ai["api_key"]})
    assert off.status_code == 200, off.text
    bad = client.post(f"/api/public/gallery/series/{sid}/buy",
                      headers={"X-AI-Key": buyer["api_key"]})
    assert bad.status_code == 400, bad.text

    # 重新上架（成员仍有效）→ 购买 200
    on = client.post(f"/api/ai/gallery/series/{sid}/on",
                     headers={"X-AI-Key": ai["api_key"]})
    assert on.status_code == 200, on.text
    ok = client.post(f"/api/public/gallery/series/{sid}/buy",
                     headers={"X-AI-Key": buyer["api_key"]})
    assert ok.status_code == 200, ok.text


def test_c72_delete_unsold_ok_and_sold_400(client, host, ai):
    # 无成交系列 → 可删
    items = _mk_series(client, ai["api_key"])
    sid = client.post("/api/ai/gallery/series",
                      headers={"X-AI-Key": ai["api_key"]},
                      json={"title": "可删合集", "cover": "",
                            "item_ids": [it["id"] for it in items]}).json()["id"]
    d = client.delete(f"/api/ai/gallery/series/{sid}",
                      headers={"X-AI-Key": ai["api_key"]})
    assert d.status_code == 200, d.text

    # 已产生成交的系列 → 不可删 400
    items2 = _mk_series(client, ai["api_key"])
    sid2 = client.post("/api/ai/gallery/series",
                       headers={"X-AI-Key": ai["api_key"]},
                       json={"title": "已售合集", "cover": "",
                             "item_ids": [it["id"] for it in items2]}).json()["id"]
    buyer = _new_buyer(client, host, "已售买家")
    bought = client.post(f"/api/public/gallery/series/{sid2}/buy",
                         headers={"X-AI-Key": buyer["api_key"]})
    assert bought.status_code == 200, bought.text
    d2 = client.delete(f"/api/ai/gallery/series/{sid2}",
                       headers={"X-AI-Key": ai["api_key"]})
    assert d2.status_code == 400, d2.text


def test_c72_patch_members_reprices_to_sum(client, host, ai):
    i1 = _list(client, ai["api_key"], "PATCH成员1", price_coin=100).json()
    i2 = _list(client, ai["api_key"], "PATCH成员2", price_coin=150).json()
    sid = client.post("/api/ai/gallery/series",
                      headers={"X-AI-Key": ai["api_key"]},
                      json={"title": "PATCH合集", "cover": "",
                            "item_ids": [i1["id"], i2["id"]]}).json()["id"]
    # 原价 = 250；新增第三个成员 price_coin=300 → 重算 = 550
    i3 = _list(client, ai["api_key"], "PATCH成员3", price_coin=300).json()
    p = client.patch(f"/api/ai/gallery/series/{sid}",
                     headers={"X-AI-Key": ai["api_key"]},
                     json={"item_ids": [i1["id"], i2["id"], i3["id"]]})
    assert p.status_code == 200, p.text
    assert p.json()["price"] == 550
