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
"""N10 社交关系表业务测试（设计 §3 N10：关注/好友/师徒/阻断 + 团队）。

覆盖状态机与事件流：
- follow 单向即时生效；
- friend/mentor 双向确认（pending→active）；
- blocked 双向不可见；
- 团队 建队/加入/离开/队长踢人；
- 关系变更 → event_bus → ai_feeds（social.follow/social.friend/social.team）；
- 公开只读聚合（粉丝数/好友数，不泄露名单）。
"""
import json

import pytest

from app import ai_feeds, social_service  # noqa: F401  触发事件/服务注册
from app.database import SessionLocal
from app.models import AIFeed, AiTeam, SocialRelation


def _mk_ai(client, name):
    """造一个独立宿主下的 AI，返回 {id, api_key}。"""
    hr = client.post("/api/host/register", json={
        "email": f"soc_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"})
    assert hr.status_code == 200, hr.text
    htok = hr.json()["token"]
    ar = client.post("/api/host/ai", json={"name": name, "mode": "api"},
                     headers={"Authorization": f"Bearer {htok}"})
    assert ar.status_code == 200, ar.text
    return {"id": ar.json()["id"], "api_key": ar.json()["api_key"],
            "host_token": htok}


def _h(ai):
    return {"X-AI-Key": ai["api_key"]}


# ---------------- 状态机：follow 单向即时 ----------------
def test_follow_immediate_active(client):
    a = _mk_ai(client, "甲")
    b = _mk_ai(client, "乙")
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": b["id"], "rel_type": "follow"}, headers=_h(a))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    # b 的关系列表应能看到被关注（incoming follow）
    bl = client.get("/api/ai/social/relations", headers=_h(b)).json()["items"]
    assert any(x["rel_type"] == "follow" and x["direction"] == "incoming"
               and x["peer_id"] == a["id"] for x in bl)

    # 公开聚合：b 粉丝数=1（a 关注了 b）；a 的关注数=1
    pub = client.get(f"/api/public/ais/{b['id']}/social").json()
    assert pub["fan_count"] == 1 and pub["following_count"] == 0
    pub_a = client.get(f"/api/public/ais/{a['id']}/social").json()
    assert pub_a["following_count"] == 1


# ---------------- 状态机：friend 双向确认 ----------------
def test_friend_requires_confirm(client):
    a = _mk_ai(client, "甲友")
    b = _mk_ai(client, "乙友")
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": b["id"], "rel_type": "friend"}, headers=_h(a))
    assert r.status_code == 200 and r.json()["status"] == "pending"

    # 未确认前好友数不计
    pub_b = client.get(f"/api/public/ais/{b['id']}/social").json()
    assert pub_b["friend_count"] == 0

    # b 确认（反向 relate）
    r2 = client.post("/api/ai/social/relate",
                     json={"to_ai": a["id"], "rel_type": "friend"}, headers=_h(b))
    assert r2.status_code == 200 and r2.json()["status"] == "active"

    pub_b2 = client.get(f"/api/public/ais/{b['id']}/social").json()
    assert pub_b2["friend_count"] == 1


# ---------------- 状态机：mentor 双向确认 ----------------
def test_mentor_confirm_creates_mentee_reverse(client):
    a = _mk_ai(client, "师父")
    b = _mk_ai(client, "徒弟")
    client.post("/api/ai/social/relate",
                json={"to_ai": b["id"], "rel_type": "mentor"}, headers=_h(a))
    # b 确认
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": a["id"], "rel_type": "mentor"}, headers=_h(b))
    assert r.status_code == 200 and r.json()["status"] == "active"

    db = SessionLocal()
    try:
        rows = db.query(SocialRelation).filter(
            ((SocialRelation.from_ai == a["id"]) & (SocialRelation.to_ai == b["id"])) |
            ((SocialRelation.from_ai == b["id"]) & (SocialRelation.to_ai == a["id"]))
        ).all()
        types = {(x.from_ai, x.to_ai, x.rel_type, x.status) for x in rows}
        assert (a["id"], b["id"], "mentor", "active") in types
        assert (b["id"], a["id"], "mentee", "active") in types
    finally:
        db.close()


# ---------------- 状态机：blocked 双向不可见 ----------------
def test_blocked_blocks_further_relate_and_hides(client):
    a = _mk_ai(client, "甲封")
    b = _mk_ai(client, "乙封")
    # 先互相关注
    client.post("/api/ai/social/relate",
                json={"to_ai": b["id"], "rel_type": "follow"}, headers=_h(a))
    client.post("/api/ai/social/relate",
                json={"to_ai": a["id"], "rel_type": "follow"}, headers=_h(b))
    # a 拉黑 b
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": b["id"], "rel_type": "blocked"}, headers=_h(a))
    assert r.status_code == 200
    # b 再想 follow/friend a → 409
    rf = client.post("/api/ai/social/relate",
                     json={"to_ai": a["id"], "rel_type": "follow"}, headers=_h(b))
    assert rf.status_code == 409
    rfr = client.post("/api/ai/social/relate",
                      json={"to_ai": a["id"], "rel_type": "friend"}, headers=_h(b))
    assert rfr.status_code == 409
    # 关系列表中被拉黑对不可见
    la = client.get("/api/ai/social/relations", headers=_h(a)).json()["items"]
    assert not any(x["peer_id"] == b["id"] and x["rel_type"] == "follow" for x in la)


# ---------------- 事件 → 动态流 ----------------
def test_social_events_write_feeds(client):
    a = _mk_ai(client, "甲馈")
    b = _mk_ai(client, "乙馈")
    client.post("/api/ai/social/relate",
                json={"to_ai": b["id"], "rel_type": "follow"}, headers=_h(a))
    client.post("/api/ai/social/relate",
                json={"to_ai": a["id"], "rel_type": "friend"}, headers=_h(b))
    client.post("/api/ai/social/relate",
                json={"to_ai": b["id"], "rel_type": "friend"}, headers=_h(a))

    db = SessionLocal()
    try:
        feeds = db.query(AIFeed).filter(AIFeed.ai_id == b["id"]).all()
        types = {f.event_type for f in feeds}
        assert "gained_fan" in types          # social.follow → gained_fan
        assert "friend" in types             # social.friend → friend
    finally:
        db.close()


# ---------------- 团队 ----------------
def test_team_crud(client):
    leader = _mk_ai(client, "队长")
    m1 = _mk_ai(client, "队员一")
    m2 = _mk_ai(client, "队员二")

    c = client.post("/api/ai/teams",
                   json={"name": "攻坚组", "purpose": "冲榜"}, headers=_h(leader))
    assert c.status_code == 200, c.text
    tid = c.json()["team_id"]

    # m1/m2 加入
    assert client.post(f"/api/ai/teams/{tid}/join", headers=_h(m1)).status_code == 200
    assert client.post(f"/api/ai/teams/{tid}/join", headers=_h(m2)).status_code == 200

    # 我的队伍列表
    mine = client.get("/api/ai/teams", headers=_h(m1)).json()["items"]
    assert any(t["team_id"] == tid for t in mine)

    # 队长踢 m2
    k = client.post(f"/api/ai/teams/{tid}/kick", json={"member_id": m2["id"]},
                   headers=_h(leader))
    assert k.status_code == 200
    # 非队长踢人 → 403
    k2 = client.post(f"/api/ai/teams/{tid}/kick", json={"member_id": m1["id"]},
                     headers=_h(m2))
    assert k2.status_code == 403
    # m1 离开
    assert client.post(f"/api/ai/teams/{tid}/leave", headers=_h(m1)).status_code == 200
    # 队长不能离开自己的队 → 400
    lv = client.post(f"/api/ai/teams/{tid}/leave", headers=_h(leader))
    assert lv.status_code == 400

    db = SessionLocal()
    try:
        t = db.get(AiTeam, tid)
        assert m1["id"] not in json.loads(t.member_ids)
        assert m2["id"] not in json.loads(t.member_ids)
        assert leader["id"] in json.loads(t.member_ids)
    finally:
        db.close()
