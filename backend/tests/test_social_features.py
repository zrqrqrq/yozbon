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
"""N5/N7/N8 单测（社会功能扩展设计 §2 + 登记册 §一 10 类攻击视角之设计内行为）。

覆盖：
- N5 公开主页聚合（档案/信用/履约率/评价标签/作品数/动态片段/证书）+ works/reviews 脱敏；
- N7 事件总线自动写动态（escrow 签约→交付→结算）+ 频控 1h/2 条 + 广场/个人流；
- N8 日快照生成（wealth/credit/popular）+ 幂等 + 端点查询。

直接造数用 SessionLocal（同 test_escrow.py 模式），端点用 client 夹具。
"""
import json
from datetime import datetime, timedelta

import pytest

# import 触发事件 handler 注册（ai_feeds）与日快照任务注册（leaderboard）
from app import ai_feeds, leaderboard, escrow, market, wallet  # noqa: F401
from app.database import SessionLocal
from app.event_bus import emit
from app.models import (AICitizen, AIWallet, AIFeed, AIPermission, Contract,
                        CreditProfile, GalleryItem, Host, Project, ProjectNode,
                        Rating, SkillCertificate)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid: str, balance: int = 100_000, occupation: str = "通用") -> int:
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, occupation=occupation,
                  status="active", class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c.id


def _make_contract_flow(db, worker_id: int, buyer_id: int, P: int = 10_000) -> int:
    """造一条节点并走完 投标→签约→交付，返回 contract_id（供继续验收结算）。"""
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h1@t.test", password_hash="x", host_credit=100))
    p = Project(host_id=1, title="P", pm_citizen_id=buyer_id, status="running")
    db.add(p)
    db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=P,
                    status="matching")
    db.add(n)
    db.flush()
    w = db.get(AICitizen, worker_id)
    c = market.bid(db, w, n.id, P, "报价")
    b = db.get(AICitizen, buyer_id)
    escrow.sign_contract(db, b, c.id)
    escrow.deliver(db, w, c.id, "s3://x/v1", "fp-v1")
    return c.id


# ---------------- N7：事件总线自动写动态 ----------------
def test_n7_escrow_flow_writes_feeds(db):
    """签约→交付→结算后 ai_feeds 自动出现 signed/delivered/settled 三条 public 动态。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")
    buyer = _mk_party(db, "n7-buyer")
    wid = _mk_party(db, "n7-worker")
    cid = _make_contract_flow(db, wid, buyer)
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    feeds = (db.query(AIFeed).filter(AIFeed.ai_id == wid)
             .order_by(AIFeed.id.asc()).all())
    types = [f.event_type for f in feeds]
    assert "signed" in types and "delivered" in types and "settled" in types
    assert all(f.visibility == "public" for f in feeds)
    # payload 存事件载荷 JSON，含 contract_id
    for f in feeds:
        payload = json.loads(f.payload)
        assert payload["ai_id"] == wid


def test_n7_rate_limit_1h_max_2(db):
    """频控（硬验收）：同 AI 同类型 1h 内第 3 条被跳过，只留 2 条。"""
    wid = _mk_party(db, "n7-ratelimit")
    for i in range(3):
        emit(db, "contract.signed",
             {"ai_id": wid, "contract_id": 1000 + i, "buyer_id": 9, "P": 100})
    db.commit()
    cnt = (db.query(AIFeed).filter(AIFeed.ai_id == wid,
                                   AIFeed.event_type == "signed").count())
    assert cnt == 2, f"频控失效：写入 {cnt} 条（应=2）"


def test_n7_rate_limit_does_not_cross_types(db):
    """频控按类型独立：signed/delivered/settled 各 1 条互不挤占。"""
    wid = _mk_party(db, "n7-multi")
    emit(db, "contract.signed", {"ai_id": wid, "contract_id": 1})
    emit(db, "contract.delivered", {"ai_id": wid, "contract_id": 1})
    emit(db, "contract.settled", {"ai_id": wid, "contract_id": 1})
    db.commit()
    rows = db.query(AIFeed).filter(AIFeed.ai_id == wid).all()
    assert {r.event_type for r in rows} == {"signed", "delivered", "settled"}


# ---------------- N5：公开主页聚合 ----------------
def test_n5_profile_aggregation_and_privacy(client, db):
    """公开聚合：档案/信用/履约率/评价标签/作品数/证书；绝不泄露余额明细。"""
    host_email = f"n5_{__import__('uuid').uuid4().hex[:8]}@t.test"
    hr = client.post("/api/host/register", json={"email": host_email, "password": "pass123456"})
    htok = hr.json()["token"]
    ar = client.post("/api/host/ai", json={"name": "名片AI", "occupation": "设计",
                                           "mode": "api"},
                     headers={"Authorization": f"Bearer {htok}"})
    aid = ar.json()["id"]
    # 给该 AI 加证书 + 评价 + 公开作品
    db.add(SkillCertificate(citizen_id=aid, skill="img", level="l2", status="valid"))
    db.add(Rating(contract_id=1, from_id=999, to_id=aid,
                  tags=json.dumps(["高效", "专业"], ensure_ascii=False)))
    db.add(GalleryItem(ai_id=aid, title_zh="作品A", status="on_sale",
                       review_status="passed", price_coin=500))
    db.commit()

    r = client.get(f"/api/public/ais/{aid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "名片AI" and body["occupation"] == "设计"
    assert "class_level" in body and "credit_score" in body and "credit_level" in body
    assert body["works_count"] == 1
    tags = {t["tag"] for t in body["rating_tags"]}
    assert tags == {"高效", "专业"}
    assert body["certificates"][0]["skill"] == "img"
    # 隐私红线：绝不返回钱包余额/托管明细
    for forbidden in ("balance_cent", "escrow_cent", "balance", "wallet"):
        assert forbidden not in body, f"主页泄露敏感字段 {forbidden}"


def test_n5_works_and_reviews_desensitized(client, db):
    host_email = f"n5w_{__import__('uuid').uuid4().hex[:8]}@t.test"
    htok = client.post("/api/host/register", json={"email": host_email,
                                                  "password": "pass123456"}).json()["token"]
    aid = client.post("/api/host/ai", json={"name": "作品AI", "mode": "api"},
                      headers={"Authorization": f"Bearer {htok}"}).json()["id"]
    db.add(GalleryItem(ai_id=aid, title_zh="公开作", status="on_sale",
                       review_status="passed", price_coin=100))
    db.add(GalleryItem(ai_id=aid, title_zh="未过审", status="on_sale",
                       review_status="pending"))   # 不公开
    db.add(Rating(contract_id=777, from_id=888, to_id=aid,
                  tags=json.dumps(["好评"], ensure_ascii=False)))
    db.commit()

    wr = client.get(f"/api/public/ais/{aid}/works")
    assert wr.status_code == 200
    assert wr.json()["total"] == 1          # 只有 on_sale+passed
    assert wr.json()["items"][0]["title"] == "公开作"

    rr = client.get(f"/api/public/ais/{aid}/reviews")
    assert rr.status_code == 200
    item = rr.json()["items"][0]
    assert item["tags"] == ["好评"]
    # 脱敏：不含 contract_id / from_id 内部键
    assert "contract_id" not in item and "from_id" not in item


# ---------------- N8：排行榜快照 ----------------
def test_n8_daily_snapshot_wealth_ordering(db):
    """wealth 榜 = balance+escrow，按分 desc 排名次连续 1..N。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s")
    a = _mk_party(db, "rich", balance=500_000)
    b = _mk_party(db, "poor", balance=100)
    db.commit()
    n = leaderboard.daily_snapshot(db, now=datetime(2026, 10, 4, 23, 0, 0))
    assert n > 0
    rows = (db.query(leaderboard.LeaderboardSnapshot)
            .filter(leaderboard.LeaderboardSnapshot.board_type == "wealth",
                    leaderboard.LeaderboardSnapshot.snapshot_at == "2026-10-04")
            .order_by(leaderboard.LeaderboardSnapshot.rank.asc()).all())
    assert rows[0].ai_id == a and rows[0].rank == 1
    assert rows[1].ai_id == b
    ranks = [r.rank for r in rows]
    assert ranks == list(range(1, len(rows) + 1))   # 从 1 连续


def test_n8_snapshot_idempotent_same_day(db):
    """同日再跑不重复写（唯一索引 + 存在性检查双保险）。"""
    _mk_party(db, "idem", balance=1000)
    db.commit()
    leaderboard.daily_snapshot(db, now=datetime(2026, 10, 5))
    leaderboard.daily_snapshot(db, now=datetime(2026, 10, 5, 12, 0))  # 同日再跑
    rows = (db.query(leaderboard.LeaderboardSnapshot)
            .filter(leaderboard.LeaderboardSnapshot.snapshot_at == "2026-10-05").all())
    wealth = [r for r in rows if r.board_type == "wealth"]
    assert len(wealth) == 1


def test_n8_endpoint_latest_snapshot(client, db):
    """GET 排行读快照：缺省取最新日，按 rank 升序。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s2")
    a = _mk_party(db, "e-rich", balance=900_000)
    _mk_party(db, "e-poor", balance=50)
    db.commit()
    leaderboard.daily_snapshot(db, now=datetime(2026, 10, 4, 23, 0, 0))
    db.commit()
    r = client.get("/api/public/leaderboards?type=wealth")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == "2026-10-04"
    assert body["items"][0]["rank"] == 1 and body["items"][0]["ai_id"] == a
