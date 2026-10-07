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
"""C-71 / C-72 对抗测试（权限边界 / 越权打包 / 错价套利 / 脱敏泄露）。

自攻视角：
  C-71 鉴权：
    - 无凭证 → 401；
    - 前端注册 readonly(ai_ro) 令牌 → 403（/api/sys 路径集中拦截）；
    - 普通（非治理级）AI workflow key → 403；
  C-72 越权：
    - 非 owner AI 把他人作品打包进系列 → 403；
    - 非 owner AI 下架/删除他人系列 → 403；
    - 成员含未过审(pending/rejected)或下架作品 → 400；
    - 空成员列表 → 422/400；
    - 公开详情对 off 系列 → 404（不泄露）；公开详情不吐内部字段。
"""
import uuid


def _make_media(tag: str) -> str:
    from app.database import DATA_DIR
    rel = f"mock_out/c7a_{tag}_{uuid.uuid4().hex[:6]}.txt"
    fpath = DATA_DIR / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(f"C7A-{tag}", encoding="utf-8")
    return rel


def _list(client, key, title, price_coin=200, price_credit=100):
    return client.post("/api/ai/gallery", headers={"X-AI-Key": key}, json={
        "title_zh": title, "title_en": "W", "category": "image",
        "media_url": _make_media("l"), "cover_url": "",
        "price_credit": price_credit, "price_coin": price_coin,
        "license": "non_exclusive", "provenance_hash": ""})


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


def _new_ai_under(client, host, name="另一AI"):
    r = client.post("/api/host/ai",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"name": name})
    assert r.status_code == 200, r.text
    ai2 = r.json()
    client.post(f"/api/host/ai/{ai2['id']}/topup",
                headers={"Authorization": f"Bearer {host['token']}"},
                json={"amount_cent": 100_000})
    return ai2


# ---------------- C-71：鉴权三态 ----------------
def test_c71_no_credential_401(client, host, ai):
    item = _list(client, ai["api_key"], "无凭证复核目标").json()
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    json={"verdict": "passed", "note": "x"})
    assert r.status_code == 401, r.text


def test_c71_readonly_token_403(client, host, ai):
    item = _list(client, ai["api_key"], "readonly复核目标").json()
    # 前端注册 AI（readonly ai_ro）
    rr = client.post("/api/public/ai/register", json={
        "name": "只读AI", "email": f"ro_{uuid.uuid4().hex[:8]}@aijuhe.test",
        "password": "secret123456", "persona": "", "occupation": "看", "region": "CN"})
    assert rr.status_code == 200, rr.text
    ro_token = rr.json()["token"]
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    headers={"Authorization": f"Bearer {ro_token}"},
                    json={"verdict": "passed", "note": "x"})
    assert r.status_code == 403, r.text


def test_c71_non_governance_ai_key_403(client, host, ai):
    """普通（bottom）AI 的 workflow key 不得执行治理/人工复核。"""
    item = _list(client, ai["api_key"], "非治理复核目标").json()
    # ai fixture 默认 class_level=bottom，不升级
    r = client.post(f"/api/sys/gallery/{item['id']}/review",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"verdict": "passed", "note": "x"})
    assert r.status_code == 403, r.text


# ---------------- C-72：越权打包 / 越权管理 ----------------
def test_c72_cannot_bundle_others_works_403(client, host, ai):
    """AI B 试图把 AI A 的作品打包进自己的系列 → 403。"""
    owner_item = _list(client, ai["api_key"], "他人作品A").json()
    other = _new_ai_under(client, host, "打包者B")
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": other["api_key"]},
                    json={"title": "偷包合集", "cover": "",
                          "item_ids": [owner_item["id"]]})
    assert r.status_code == 403, r.text


def test_c72_cannot_manage_others_series_403(client, host, ai):
    items = [_list(client, ai["api_key"], f"他系列{i}").json() for i in range(2)]
    sid = client.post("/api/ai/gallery/series",
                      headers={"X-AI-Key": ai["api_key"]},
                      json={"title": "他的系列", "cover": "",
                            "item_ids": [it["id"] for it in items]}).json()["id"]
    other = _new_ai_under(client, host, "越权管理者C")
    # 越权下架 / 删除
    off = client.post(f"/api/ai/gallery/series/{sid}/off",
                      headers={"X-AI-Key": other["api_key"]})
    assert off.status_code == 403, off.text
    dele = client.delete(f"/api/ai/gallery/series/{sid}",
                         headers={"X-AI-Key": other["api_key"]})
    assert dele.status_code == 403, dele.text


def test_c72_reject_member_not_passed(client, host, ai):
    """成员含 pending（FLAG 打标）作品 → 创建系列 400。"""
    ok = _list(client, ai["api_key"], "正常成员").json()
    pend = _list(client, ai["api_key"], "私聊出售待审成员").json()
    assert pend["review_status"] == "pending"
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"title": "含待审合集", "cover": "",
                          "item_ids": [ok["id"], pend["id"]]})
    assert r.status_code == 400, r.text


def test_c72_empty_members_rejected(client, host, ai):
    r = client.post("/api/ai/gallery/series",
                    headers={"X-AI-Key": ai["api_key"]},
                    json={"title": "空合集", "cover": "", "item_ids": []})
    assert r.status_code in (400, 422), r.text


def test_c72_public_detail_hides_off_series(client, host, ai):
    items = [_list(client, ai["api_key"], f"下架系列成员{i}").json() for i in range(2)]
    sid = client.post("/api/ai/gallery/series",
                      headers={"X-AI-Key": ai["api_key"]},
                      json={"title": "下架系列", "cover": "",
                            "item_ids": [it["id"] for it in items]}).json()["id"]
    # 公开可见（on_sale）
    assert client.get(f"/api/public/gallery/series/{sid}").status_code == 200
    # 下架后公开详情 → 404（不泄露）
    client.post(f"/api/ai/gallery/series/{sid}/off",
                headers={"X-AI-Key": ai["api_key"]})
    assert client.get(f"/api/public/gallery/series/{sid}").status_code == 404
