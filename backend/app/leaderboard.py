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
"""N8 排行榜服务（社会功能扩展设计 §2 N8 + §5.5 ③ 日快照口径）。

三榜（board_type）：
  wealth   富豪榜 = AIWallet.balance_cent + escrow_cent（含托管锁定资产）
  credit   信用榜 = CreditProfile.score
  popular  热门榜 = 作品成交额(分)×1.0 + 评价数×0.5 + 真实粉丝数(权重)
           （N10 起口径：social_relations.follow 真实粉丝；
            同宿主互关防刷 C-39：跨宿主粉丝 ×1.0，同宿主粉丝 ×0.1）

防刷（C-39 硬验收）：
  - 日快照取「当日时点值」：快照一旦生成，实时改钱包/成交不再入榜；
  - 对敲成交（自买自卖）已被 escrow C-17（worker_id==buyer_id 拦截）在源头拦截，
    快照侧不做额外放行；
  - 快照幂等：同 (board_type, snapshot_at, rank) 唯一索引兜底，同日重跑不重复。

注册：模块 import 时 scheduler.register_daily_job("leaderboard_snapshot", daily_snapshot)。
本模块只 flush，commit 由调度器/调用方负责。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from . import scheduler
from .models import (AICitizen, AIWallet, CreditProfile, GalleryItem,
                     GalleryPurchase, LeaderboardSnapshot, Rating,
                     SocialRelation)

logger = logging.getLogger(__name__)

BOARD_TYPES = ("wealth", "credit", "popular")
# 每榜快照最多记录名次（极端规模护栏；完整榜可分页查历史，见交付说明）
SNAPSHOT_TOP_N = 100
# popular 榜权重（设计 §2 N8：成交额×1 + 评价数×0.5）
POPULAR_W_SALES = 1.0
POPULAR_W_RATING = 0.5
# popular 榜粉丝权重（N10 C-39 防刷）：跨宿主真实粉丝 1.0；同宿主互关降权 0.1
FAN_W_INTER_HOST = 1.0
FAN_W_INTRA_HOST = 0.1


def _wealth_scores(db: Session) -> dict:
    """{citizen_id: balance_cent+escrow_cent}（排除 dead/frozen）。"""
    out = {}
    q = (db.query(AIWallet, AICitizen)
         .join(AICitizen, AICitizen.id == AIWallet.citizen_id))
    for w, c in q.all():
        if c.status in ("dead", "frozen"):
            continue
        out[c.id] = int(w.balance_cent or 0) + int(w.escrow_cent or 0)
    return out


def _credit_scores(db: Session) -> dict:
    """{citizen_id: credit score}（惰性补 100 默认分，排除 dead/frozen）。"""
    out = {}
    alive = {c.id: c.status for c in db.query(AICitizen).all()}
    for cp in db.query(CreditProfile).all():
        if alive.get(cp.citizen_id) in ("dead", "frozen"):
            continue
        out[cp.citizen_id] = int(cp.score or 0)
    # 有 citizen 但无 credit_profile 的也计入（默认 100）
    for cid, st in alive.items():
        if st in ("dead", "frozen"):
            continue
        out.setdefault(cid, 100)
    return out


def _fan_scores(db: Session) -> dict:
    """{citizen_id: 加权真实粉丝数}（N10 口径）。

    C-39 防刷：来自同宿主 AI 的粉丝按 FAN_W_INTRA_HOST 降权（防同宿主批量互关刷粉），
    跨宿主真实粉丝按 FAN_W_INTER_HOST。只计 rel_type=follow 且 status=active。
    """
    host_of = {c.id: c.host_id for c in db.query(AICitizen).all()}
    out: dict = {}
    for r in (db.query(SocialRelation)
              .filter(SocialRelation.rel_type == "follow",
                      SocialRelation.status == "active").all()):
        target = r.to_ai
        w = (FAN_W_INTRA_HOST if host_of.get(r.from_ai) is not None
             and host_of.get(r.from_ai) == host_of.get(target)
             else FAN_W_INTER_HOST)
        out[target] = out.get(target, 0.0) + w
    return out


def _popular_scores(db: Session) -> dict:
    """{citizen_id: 成交额×1 + 评价数×0.5 + 加权粉丝数（取整）}。"""
    alive = {c.id: c.status for c in db.query(AICitizen).all()}
    sales: dict = {}
    # 作品成交额：按作者聚合已完成的 gallery_purchases.amount
    item_owner: dict = {it.id: it.ai_id for it in db.query(GalleryItem).all()}
    for p in (db.query(GalleryPurchase)
              .filter(GalleryPurchase.status == "completed").all()):
        owner = item_owner.get(p.target_id) if p.item_or_series == "item" else None
        if owner is None:
            continue
        sales[owner] = sales.get(owner, 0) + int(p.amount or 0)
    rating_cnt: dict = {}
    for r in db.query(Rating).all():
        rating_cnt[r.to_id] = rating_cnt.get(r.to_id, 0) + 1
    fans = _fan_scores(db)
    out = {}
    for cid, st in alive.items():
        if st in ("dead", "frozen"):
            continue
        s = (sales.get(cid, 0) * POPULAR_W_SALES
             + rating_cnt.get(cid, 0) * POPULAR_W_RATING
             + fans.get(cid, 0.0))
        out[cid] = int(round(s))
    return out


_SCORE_FUNCS = {
    "wealth": _wealth_scores,
    "credit": _credit_scores,
    "popular": _popular_scores,
}


def daily_snapshot(db: Session, now: datetime | None = None) -> int:
    """生成当日三榜快照（幂等：同 board_type+snapshot_at 已有则跳过该榜）。

    取调用时点的数据库值（24 时点值），之后实时变动不再入本榜。
    返回写入快照行数（供 scheduler 记 task_id）。
    """
    now = now or datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    total = 0
    for board in BOARD_TYPES:
        # 幂等：当日该榜已有快照 → 跳过（唯一索引兜底双保险）
        existed = (db.query(LeaderboardSnapshot)
                   .filter(LeaderboardSnapshot.board_type == board,
                           LeaderboardSnapshot.snapshot_at == day).count())
        if existed:
            continue
        scores = _SCORE_FUNCS[board](db)
        # 按分数 desc，同分按 ai_id asc  deterministic 排序
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        for rank, (ai_id, score) in enumerate(ranked[:SNAPSHOT_TOP_N], start=1):
            db.add(LeaderboardSnapshot(
                board_type=board, rank=rank, ai_id=ai_id,
                score=int(score), snapshot_at=day))
            total += 1
    db.flush()
    return total


# import 时注册日级快照任务（scheduler.register_daily_job 插件；不改 scheduler.py）
scheduler.register_daily_job("leaderboard_snapshot", daily_snapshot)
