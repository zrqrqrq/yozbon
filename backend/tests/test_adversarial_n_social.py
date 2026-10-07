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
"""N5/N7/N8 对抗测试（登记册 §一 10 类攻击视角自攻 + §三 C-39 刷榜硬验收）。

自攻结论（10 类攻击视角，对应设计 §6.2）：
  1 新物种进场：event_type 走封闭映射表 EVENT_TO_FEED，未知事件不写 feed（开放扩展由新映射值入表，不崩）。
  2 极端规模：feeds/leaderboards 全部分页 limit≤50，快照每榜截 top100；快照查询走索引。
  3 恶意主体（刷榜/刷动态）：①对敲成交已被 escrow C-17（worker_id==buyer_id）源头拦截；
    ②快照取日时点值，实时刷钱/改分不入榜；③动态频控 1h/2 条防刷屏。
  4 规则冲突：快照幂等（同 board+day 存在即跳过），与 scheduler 日级 SchedulerRun 幂等双保险。
  5 边界数值：无合约 AI 履约率=null；空库排行返回空数组不崩；非法 type/date 400。
  6 故障恢复：重复触发 daily_snapshot 同日不重复写；事件 handler 异常不外抛污染业务事务。
  7 新供应商/新模型：本模块无外部通道依赖，不受影响。
  8 人为滥用：host/AI 无手动发动态入口（只经事件总线），公开主页不暴露管理面。
  9 经济失衡：排行只读快照，不发币不改钱包，无经济外部性。
 10 法律合规：公开主页脱敏（不返回余额/合同内部字段），404 不泄露存在性。

新洞：见交付说明（本轮未发现需登记 C-63+ 的新洞；沿用 C-17/C-39 既有机制）。
"""
import json
from datetime import datetime

import pytest

from app import ai_feeds, leaderboard, escrow, market, wallet  # noqa: F401
from app.database import SessionLocal
from app.event_bus import emit
from app.models import (AICitizen, AIWallet, AIFeed, AIPermission, Contract,
                        CreditProfile, GalleryItem, Host, Project, ProjectNode)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid: str, balance: int = 100_000) -> int:
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status="active", class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c.id


# ---- 视角3：对敲成交（自买自卖）被源头拦截，不入榜 ----
def test_atk_self_deal_blocked_by_c17(db):
    """AI 对自己发布的节点投标并签约 → C-17 拒绝（worker_id==buyer_id），无法对敲刷成交额。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s")
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h@t.test", password_hash="x", host_credit=100))
    same = _mk_party(db, "self-deal", balance=100_000)
    p = Project(host_id=1, title="P", pm_citizen_id=same, status="running")
    db.add(p)
    db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=5000,
                    status="matching")
    db.add(n)
    db.flush()
    c = market.bid(db, db.get(AICitizen, same), n.id, 5000, "报价")
    with pytest.raises(escrow.EscrowError):
        escrow.sign_contract(db, db.get(AICitizen, same), c.id)


# ---- 视角3/6：实时刷分不改快照（快照是时点值） ----
def test_atk_realtime_change_does_not_alter_snapshot(client, db):
    """先快照，再实时把钱包/信用改高，已生成快照不变（C-39 防刷）。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="s")
    a = _mk_party(db, "snap-a", balance=1000)
    _mk_party(db, "snap-b", balance=500)
    db.commit()
    leaderboard.daily_snapshot(db, now=datetime(2026, 10, 4, 23, 0, 0))
    db.commit()
    # 实时给 a 猛充 100 万（刷钱）
    wallet.credit(db, a, 1_000_000, "充值", ref="order:atk:cheat")
    db.commit()
    r = client.get("/api/public/leaderboards?type=wealth&date=2026-10-04")
    assert r.status_code == 200
    body = r.json()
    top = body["items"][0]
    # 快照时点 a 只有 1000；实时充值不回写快照
    assert top["ai_id"] == a
    assert top["score"] == 1000, f"快照被实时改动污染: score={top['score']}"


# ---- 视角3：频控 1h/2 条（第 3 条同型被跳过） ----
def test_atk_rate_limit_third_same_type_dropped(db):
    wid = _mk_party(db, "atk-fl")
    for i in range(5):   # 连发 5 条同型，只能留 2
        emit(db, "gallery.sold", {"ai_id": wid, "target_id": i, "amount": 1})
    db.commit()
    cnt = db.query(AIFeed).filter(AIFeed.ai_id == wid,
                                  AIFeed.event_type == "sold").count()
    assert cnt == 2


# ---- 视角3/10：private 动态一律不对外 ----
def test_atk_private_feed_not_exposed(client, db):
    aid = _mk_party(db, "priv-ai")
    db.add(AIFeed(ai_id=aid, event_type="signed", payload="{}",
                  visibility="private"))
    db.add(AIFeed(ai_id=aid, event_type="settled", payload="{}",
                  visibility="public"))
    db.commit()
    personal = client.get(f"/api/public/ais/{aid}/feeds").json()
    assert personal["total"] == 1
    assert personal["items"][0]["event_type"] == "settled"
    plaza = client.get("/api/public/feeds").json()
    # 私人那条（aid 的 signed）不得出现在广场；公开那条（aid 的 settled）应出现
    assert not any(f["ai_id"] == aid and f["event_type"] == "signed"
                   for f in plaza["items"])
    assert any(f["ai_id"] == aid and f["event_type"] == "settled"
               for f in plaza["items"])


# ---- 视角5：非法 type/date 边界 ----
def test_atk_invalid_type_and_date(client, db):
    _mk_party(db, "edge")
    db.commit()
    assert client.get("/api/public/leaderboards?type=bogus").status_code == 400
    assert client.get("/api/public/leaderboards?type=wealth&date=10-04-2026").status_code == 400
    # 合法 date 但当日无快照 → 空数组 + latest_date 提示，不崩
    r = client.get("/api/public/leaderboards?type=credit&date=2020-01-01")
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---- 视角10：主页不泄露余额明细 ----
def test_atk_profile_no_balance_leak(client, db):
    aid = _mk_party(db, "leak-ai", balance=123_456)
    db.commit()
    body = client.get(f"/api/public/ais/{aid}").json()
    assert "balance_cent" not in body
    assert "escrow_cent" not in body
    assert "balance" not in body
    # 公开层只见阶层与信用等级
    assert "class_level" in body and "credit_level" in body


# ---- 视角5/6：空库/不存在不崩，404 不泄露存在性 ----
def test_atk_empty_and_notfound(client, db):
    # 不存在的 AI：主页/作品/评价/动态一律 404，不泄露存在性
    for path in ("/api/public/ais/999999", "/api/public/ais/999999/works",
                 "/api/public/ais/999999/reviews", "/api/public/ais/999999/feeds"):
        assert client.get(path).status_code == 404, path
    # 空库排行
    r = client.get("/api/public/leaderboards?type=popular")
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["latest_date"] is None
    # 空广场流
    assert client.get("/api/public/feeds").json()["items"] == []


# ---- 视角4：未知事件类型不写 feed（封闭映射，不崩） ----
def test_atk_unknown_event_ignored(db):
    wid = _mk_party(db, "unknown-ev")
    emit(db, "contract.hacked", {"ai_id": wid})     # 未注册/未映射的事件
    emit(db, "weird.event", {})                       # 无 ai_id
    db.commit()
    assert db.query(AIFeed).filter(AIFeed.ai_id == wid).count() == 0
