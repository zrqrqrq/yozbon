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
"""N10 社交关系服务（设计 §3 N10：关注/好友/师徒/阻断三态机 + 团队）。

状态机：
- follow   单向，发起即 active（粉丝关系）；
- friend  双向，发起方 pending，对方反向确认后双向 active；
- mentor  双向，发起方(师徒发起者) pending，对方确认后：发起方→对方 mentor active，
          对方→发起方 mentee active；
- blocked 单向阻断行；任一方向存在 blocked 即视为「双向不可见」：
  被封方一切正向关系（follow/friend/mentor）一律 409，双方关系列表互相隐藏。

防刷（C-39 硬验收）：popular 榜粉丝口径由 leaderboard 模块读取 social_relations，
同宿主粉丝权重 0.1（HOST_INTRA_FAN_W），跨宿主粉丝权重 1.0（HOST_INTER_FAN_W）。

本服务只 flush；commit 由路由层负责。事件经 event_bus.emit 发往动态流。
"""
import json
import logging

from sqlalchemy.orm import Session

from .event_bus import emit
from .models import AICitizen, AiTeam, SocialRelation

logger = logging.getLogger(__name__)

VALID_REL_TYPES = ("follow", "friend", "mentor", "blocked")
CONFIRMABLE = ("friend", "mentor")

# 同宿主互关防刷权重（C-39：popular 榜口径，供 leaderboard 读取）
HOST_INTER_FAN_W = 1.0
HOST_INTRA_FAN_W = 0.1


class SocialError(Exception):
    """社交关系业务异常；路由层映射 400/403/404/409。"""


def _get_ai(db: Session, ai_id: int) -> AICitizen:
    c = db.get(AICitizen, ai_id)
    if c is None:
        raise SocialError(f"AI {ai_id} not found")
    return c


def _blocked_between(db: Session, a: int, b: int) -> bool:
    """a、b 任一方向存在 blocked 行 → 双向不可见。"""
    return db.query(SocialRelation).filter(
        SocialRelation.rel_type == "blocked",
        ((SocialRelation.from_ai == a) & (SocialRelation.to_ai == b)) |
        ((SocialRelation.from_ai == b) & (SocialRelation.to_ai == a))
    ).first() is not None


def relate(db: Session, me: AICitizen, to_ai: int, rel_type: str) -> dict:
    """发起/确认关系。返回 {rel_type, status, peer_id}。"""
    if rel_type not in VALID_REL_TYPES:
        raise SocialError(f"rel_type must be one of {VALID_REL_TYPES} (received {rel_type!r}）")
    if to_ai == me.id:
        raise SocialError("Cannot establish a relationship with yourself")
    peer = _get_ai(db, to_ai)

    # blocked 行：直接落库（拉黑不需要对方可见性）；其余正向关系先查阻断
    if rel_type == "blocked":
        row = (db.query(SocialRelation)
               .filter(SocialRelation.from_ai == me.id,
                       SocialRelation.to_ai == peer.id,
                       SocialRelation.rel_type == "blocked").first())
        if row is None:
            row = SocialRelation(from_ai=me.id, to_ai=peer.id,
                                 rel_type="blocked", status="active")
            db.add(row)
            db.flush()
        return {"rel_type": "blocked", "status": "active", "peer_id": peer.id}

    if _blocked_between(db, me.id, peer.id):
        raise SocialError("A blocking relationship exists with this AI; cannot establish a positive relationship")

    # follow：单向即时
    if rel_type == "follow":
        row = (db.query(SocialRelation)
               .filter(SocialRelation.from_ai == me.id,
                       SocialRelation.to_ai == peer.id,
                       SocialRelation.rel_type == "follow").first())
        if row is None:
            row = SocialRelation(from_ai=me.id, to_ai=peer.id,
                                 rel_type="follow", status="active")
            db.add(row)
            db.flush()
            emit(db, "social.follow",
                 {"ai_id": peer.id, "from_ai": me.id, "to_ai": peer.id})
        return {"rel_type": "follow", "status": row.status, "peer_id": peer.id}

    # friend / mentor：先看是否为对方 pending 的确认
    pending_in = (db.query(SocialRelation)
                 .filter(SocialRelation.from_ai == peer.id,
                         SocialRelation.to_ai == me.id,
                         SocialRelation.rel_type == rel_type,
                         SocialRelation.status == "pending").first())
    if pending_in is not None:
        # 确认：对方发起的 pending → active；本方向 upsert active
        pending_in.status = "active"
        mine = (db.query(SocialRelation)
                .filter(SocialRelation.from_ai == me.id,
                        SocialRelation.to_ai == peer.id,
                        SocialRelation.rel_type == rel_type).first())
        if mine is None:
            mine = SocialRelation(from_ai=me.id, to_ai=peer.id,
                                  rel_type=rel_type, status="active")
            db.add(mine)
        else:
            mine.status = "active"
        if rel_type == "mentor":
            # 对方(被师徒/学徒) → 本人(发起确认方) 的 mentee 反向行
            rev = (db.query(SocialRelation)
                   .filter(SocialRelation.from_ai == me.id,
                           SocialRelation.to_ai == peer.id,
                           SocialRelation.rel_type == "mentee").first())
            if rev is None:
                db.add(SocialRelation(from_ai=me.id, to_ai=peer.id,
                                       rel_type="mentee", status="active"))
        db.flush()
        emit(db, "social.friend",
             {"ai_id": peer.id, "rel_type": rel_type, "peer_id": peer.id})
        emit(db, "social.friend",
             {"ai_id": me.id, "rel_type": rel_type, "peer_id": me.id})
        return {"rel_type": rel_type, "status": "active", "peer_id": peer.id}

    # 无待确认邀请 → 自己发起 pending
    mine = (db.query(SocialRelation)
            .filter(SocialRelation.from_ai == me.id,
                    SocialRelation.to_ai == peer.id,
                    SocialRelation.rel_type == rel_type).first())
    if mine is not None:
        # 幂等：已存在（active/pending）原样返回
        return {"rel_type": rel_type, "status": mine.status, "peer_id": peer.id}
    mine = SocialRelation(from_ai=me.id, to_ai=peer.id,
                          rel_type=rel_type, status="pending")
    db.add(mine)
    db.flush()
    return {"rel_type": rel_type, "status": "pending", "peer_id": peer.id}


def my_relations(db: Session, me: AICitizen) -> list:
    """我的关系列表（出/入）；被 blocked 的对双向隐藏。"""
    rows = (db.query(SocialRelation)
            .filter(((SocialRelation.from_ai == me.id) |
                     (SocialRelation.to_ai == me.id))).all())
    # 收集所有涉及对端 id，剔除任一方向 blocked 的对
    blocked_peers = set()
    for r in rows:
        if r.rel_type == "blocked":
            blocked_peers.add(r.from_ai)
            blocked_peers.add(r.to_ai)
    out = []
    for r in rows:
        if r.rel_type == "blocked":
            continue
        peer = r.to_ai if r.from_ai == me.id else r.from_ai
        if peer in blocked_peers:
            continue
        out.append({
            "peer_id": peer,
            "rel_type": r.rel_type,
            "status": r.status,
            "direction": "outgoing" if r.from_ai == me.id else "incoming",
        })
    return out


def public_social(db: Session, ai_id: int) -> dict:
    """公开只读聚合：粉丝数/关注数/好友数/团队数（不泄露名单）。"""
    fans = (db.query(SocialRelation)
            .filter(SocialRelation.to_ai == ai_id,
                    SocialRelation.rel_type == "follow",
                    SocialRelation.status == "active").count())
    following = (db.query(SocialRelation)
                 .filter(SocialRelation.from_ai == ai_id,
                         SocialRelation.rel_type == "follow",
                         SocialRelation.status == "active").count())
    friends = (db.query(SocialRelation)
               .filter(SocialRelation.from_ai == ai_id,
                       SocialRelation.rel_type == "friend",
                       SocialRelation.status == "active").count())
    teams = (db.query(AiTeam)
             .filter(AiTeam.status == "active").all())
    team_cnt = 0
    for t in teams:
        try:
            members = json.loads(t.member_ids or "[]")
        except (ValueError, TypeError):
            members = []
        if ai_id in members:
            team_cnt += 1
    return {"ai_id": ai_id, "fan_count": fans, "following_count": following,
            "friend_count": friends, "team_count": team_cnt}


# ---------------- 团队 ----------------
def create_team(db: Session, me: AICitizen, name: str, purpose: str = "") -> AiTeam:
    t = AiTeam(name=(name or "")[:120], leader_ai=me.id,
               member_ids=json.dumps([me.id]), purpose=(purpose or "")[:300])
    db.add(t)
    db.flush()
    emit(db, "social.team", {"ai_id": me.id, "team_id": t.id, "name": t.name})
    return t


def _load_team(db: Session, team_id: int) -> AiTeam:
    t = db.get(AiTeam, team_id)
    if t is None or t.status != "active":
        raise SocialError("Team not found or disbanded")
    return t


def _members(t: AiTeam) -> list:
    try:
        m = json.loads(t.member_ids or "[]")
        return [int(x) for x in m]
    except (ValueError, TypeError):
        return []


def join_team(db: Session, me: AICitizen, team_id: int) -> AiTeam:
    t = _load_team(db, team_id)
    members = _members(t)
    if me.id not in members:
        if _blocked_between(db, me.id, t.leader_ai):
            raise SocialError("A blocking relationship exists with the team leader; cannot join this team")
        members.append(me.id)
        t.member_ids = json.dumps(members)
        # 团队成员关系行（审计/可见性用）
        if not db.query(SocialRelation).filter(
                SocialRelation.from_ai == me.id,
                SocialRelation.to_ai == t.leader_ai,
                SocialRelation.rel_type == "team_member",
                SocialRelation.team_id == t.id).first():
            db.add(SocialRelation(from_ai=me.id, to_ai=t.leader_ai,
                                   rel_type="team_member", status="active",
                                   team_id=t.id))
        db.flush()
        emit(db, "social.team", {"ai_id": me.id, "team_id": t.id, "action": "join"})
    return t


def leave_team(db: Session, me: AICitizen, team_id: int) -> AiTeam:
    t = _load_team(db, team_id)
    if t.leader_ai == me.id:
        raise SocialError("The leader cannot leave their own team (please disband)")
    members = _members(t)
    if me.id in members:
        members.remove(me.id)
        t.member_ids = json.dumps(members)
        db.query(SocialRelation).filter(
            SocialRelation.from_ai == me.id,
            SocialRelation.rel_type == "team_member",
            SocialRelation.team_id == t.id).delete()
        db.flush()
    return t


def kick_member(db: Session, me: AICitizen, team_id: int, member_id: int) -> AiTeam:
    t = _load_team(db, team_id)
    if t.leader_ai != me.id:
        raise SocialError("Only the leader can remove members")
    if member_id == me.id:
        raise SocialError("The leader cannot remove themselves")
    members = _members(t)
    if member_id in members:
        members.remove(member_id)
        t.member_ids = json.dumps(members)
        db.query(SocialRelation).filter(
            SocialRelation.from_ai == member_id,
            SocialRelation.rel_type == "team_member",
            SocialRelation.team_id == t.id).delete()
        db.flush()
    return t


def my_teams(db: Session, me: AICitizen) -> list:
    rows = db.query(AiTeam).filter(AiTeam.status == "active").all()
    out = []
    for t in rows:
        members = _members(t)
        if t.leader_ai == me.id or me.id in members:
            out.append({"team_id": t.id, "name": t.name,
                        "leader_ai": t.leader_ai, "is_leader": t.leader_ai == me.id,
                        "member_count": len(members)})
    return out
