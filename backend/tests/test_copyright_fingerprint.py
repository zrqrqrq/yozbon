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
"""N20 版权溯源增强测试。

覆盖：
- pHash 近似图（改亮度/缩放裁切）命中（海明距离 <= 阈值）
- SimHash 文本相似/不相似
- 比对端点治理岗鉴权（无凭证 401）
- gallery.listed 入库钩子：命中重复 → review_status=rejected + audit
- provenance 公开来源链
- adversarial：无指纹输入比对不崩；空库比对返回空
"""
import io
import json

import pytest

from app import fingerprints as fpmod
from app.database import DATA_DIR, SessionLocal
from app.event_bus import emit
from app.models import AICitizen, AuditLog, ContentFingerprint, GalleryItem


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _png(px):
    """px: (w,h) 像素灰度块 → PNG bytes。"""
    from PIL import Image
    im = Image.new("L", (len(px[0]), len(px)))
    for y, row in enumerate(px):
        for x, v in enumerate(row):
            im.putpixel((x, y), v)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _gradient(w=64, h=64, bright=0):
    return [[min(255, x * 4 + bright) for x in range(w)] for _ in range(h)]


# ---------------- pHash 近似命中 ----------------
def test_phash_similar_images_close():
    a = fpmod.phash_image_bytes(_png(_gradient()))
    # 改亮度（+30）：近似图
    b = fpmod.phash_image_bytes(_png(_gradient(bright=30)))
    assert a is not None and b is not None
    assert fpmod._hamming(a, b) <= fpmod.HAMMING_THRESHOLD


def test_phash_unrelated_far():
    import random
    random.seed(7)
    px1 = [[(x * 4) % 256 for x in range(64)] for _ in range(64)]
    px2 = [[random.randint(0, 255) for _ in range(64)] for _ in range(64)]
    a = fpmod.phash_image_bytes(_png(px1))
    b = fpmod.phash_image_bytes(_png(px2))
    assert fpmod._hamming(a, b) > fpmod.HAMMING_THRESHOLD


# ---------------- SimHash 文本 ----------------
def test_simhash_text_similar_vs_different():
    h1 = fpmod.simhash_text("AI 公民 入驻 平台 任务 结算 信用 等级")
    h2 = fpmod.simhash_text("AI 公民 入驻 平台 任务 结算 信用 等级 加成")
    h3 = fpmod.simhash_text("今天天气很好适合去海边吃海鲜大餐游泳晒太阳")
    assert fpmod._hamming(h1, h2) <= fpmod.HAMMING_THRESHOLD
    assert fpmod._hamming(h1, h3) > fpmod.HAMMING_THRESHOLD


# ---------------- 入库钩子：重复图 → rejected + audit ----------------
def test_listed_duplicate_flagged_rejected(db):
    from app import ai_feeds  # noqa: F401
    owner = AICitizen(host_id=1, ai_uid="fp-owner", name="fp-owner", status="active")
    db.add(owner); db.flush()
    infringer = AICitizen(host_id=1, ai_uid="fp-infringer", name="fp-infringer", status="active")
    db.add(infringer); db.flush()
    base = _png(_gradient())
    # 已有库里指纹（别人的近似图 → 侵权嫌疑）
    existing = fpmod.compute_fingerprint("image", data=base)
    db.add(ContentFingerprint(media_type="image", fingerprint=existing,
                              owner_ai=infringer.id, source_task=0))
    db.flush()
    # 新作品（改亮度的近似图）落到本地文件
    folder = DATA_DIR / "n20"
    folder.mkdir(exist_ok=True)
    f = folder / "dup.png"
    f.write_bytes(_png(_gradient(bright=40)))
    rel = "n20/dup.png"
    item = GalleryItem(ai_id=owner.id, title_zh="疑似重复图", category="image",
                       media_url=rel, price_credit=100, price_coin=100,
                       status="on_sale", review_status="passed")
    db.add(item); db.flush()
    emit(db, "gallery.listed", {"ai_id": owner.id, "item_id": item.id})
    db.flush()
    db.refresh(item)
    assert item.review_status == "rejected"
    audit = (db.query(AuditLog)
             .filter(AuditLog.action == "content.dup.flag",
                     AuditLog.actor_id == owner.id).first())
    assert audit is not None


# ---------------- adversarial：无指纹不崩 ----------------
def test_compare_empty_or_missing_no_crash(db):
    # 空指纹
    assert fpmod.compare(db, "image", "") == []
    # 库空 + 合法指纹
    h = fpmod.compute_fingerprint("image", data=_png(_gradient()))
    assert fpmod.compare(db, "image", h) == []
    # 坏指纹 hex 不崩
    assert fpmod.compare(db, "image", "zzzz") == []


# ---------------- 公开来源链 ----------------
def test_provenance_endpoint(client, db):
    owner = AICitizen(host_id=1, ai_uid="prov-owner", name="p", status="active")
    db.add(owner); db.flush()
    item = GalleryItem(ai_id=owner.id, title_zh="来源作品", category="image",
                       media_url="n20/x.png", provenance_hash="sha-provenance-abc",
                       price_credit=10, price_coin=10, status="on_sale",
                       review_status="passed")
    db.add(item); db.flush()
    db.add(ContentFingerprint(media_type="image",
                              fingerprint=fpmod.compute_fingerprint("image", data=_png(_gradient())),
                              owner_ai=owner.id, source_task=42))
    db.commit()
    r = client.get(f"/api/public/works/{item.id}/provenance")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["provenance_hash"] == "sha-provenance-abc"
    assert d["author_ai"] == owner.id
    assert d["source_task"] == 42
    # 不存在
    assert client.get("/api/public/works/99999/provenance").status_code == 404


# ---------------- 治理岗鉴权 ----------------
def test_compare_endpoint_requires_credential(client):
    # 无凭证 → 401
    r = client.post("/api/sys/fingerprint/compare",
                    json={"media_type": "image", "fingerprint": "a" * 16})
    assert r.status_code in (401, 403), r.status_code
