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
"""信用服务（蓝图 §二 表 11；蓝图 §六 规则 11/14 侧翼）。

职责：
- credit_events 追加事件（逾期/恶意拒收/好评差评等），事件→delta 用 MVP 规则表；
- credit_profiles.score 聚合累加 + level 按阈值重算（阈值为 MVP 规则版常量，
  上线后可外包治理市场复核，见 C2 线 governance type='credit'）；
- 市场排序联动：jobs 检索时读取 credit_profiles.score / hosts.host_credit 作为排序因子。

约定：金额无关；服务内只 flush，commit 由路由层负责。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .models import CreditEvent, CreditProfile


# ---------------- MVP 信用事件 delta 规则表（可外包治理复核） ----------------
# 注：此为 MVP 规则版占位，上线后应由 C2 治理市场的 credit 类任务动态校准，
# 此处集中常量便于一处调整，避免散落在业务代码里。
CREDIT_DELTA_RULES: dict = {
    # 履约侧（worker）
    "deliver_on_time": 5,        # 按时交付 +5
    "late_delivery": -10,        # 逾期交付 -10
    "quality_pass": 5,           # 一次验收通过 +5
    # 评价侧
    "positive_rating": 5,        # 好评 +5
    "negative_rating": -10,       # 差评 -10
    # 买方侧（规则 14/11 侧翼：恶意拒收）
    "malicious_reject": -30,      # 同一合约连续 reject≥3 → 恶意拒收 -30
    # 违约/欺诈（治理复核后生效，此处留占位）
    "breach": -40,               # 违约 -40
    "fraud": -50,                # 欺诈 -50
    "malicious_arbitration": -30,  # 恶意仲裁（规则 11，由 C2 仲裁消费）
}

# ---------------- 信用分→阶层阈值（MVP 规则版常量，可外包治理复核） ----------------
# 与 ai_citizens.class_level（bottom/middle/boss/capital/governance）同档名。
# score 区间：[阈值, 下一档)。score 初始 100。
# 注释：阈值为拍脑袋 MVP 起点，参考 DEATH_EXEMPT_CREDIT=150（信用≥150 豁免死亡），
# middle 档下界设在 120，使稳健履约者较快跨过豁免线；governance 档供治理 AI。
CREDIT_LEVEL_THRESHOLDS: list = [
    # (level, score_min) 升序
    ("governance", 500),
    ("capital", 350),
    ("boss", 200),
    ("middle", 120),
    ("bottom", 0),
]

# 分数上下限护栏（MVP：不允许负分崩盘，也不允许无限刷）
SCORE_MIN: int = 0
SCORE_MAX: int = 1000


def level_of_score(score: int) -> str:
    """按阈值把分数映射到阶层档名。"""
    for level, lo in CREDIT_LEVEL_THRESHOLDS:   # 已按阈值降序排列
        if score >= lo:
            return level
    return "bottom"


def get_profile(db: Session, citizen_id: int) -> CreditProfile:
    """取信用档案（无则建，惰性）。"""
    p = db.get(CreditProfile, citizen_id)
    if p is None:
        p = CreditProfile(citizen_id=citizen_id, score=100, level="bottom", summary="{}")
        db.add(p)
        db.flush()
    return p


def record_event(db: Session, citizen_id: int, event: str, delta: int | None = None,
                 reason: str = "", ref: str = "") -> CreditEvent:
    """追加一条信用事件，并同步聚合 credit_profiles.score / level。

    - delta 显式传入则用之；否则查 CREDIT_DELTA_RULES 规则表（未知事件 delta=0）。
    - score 累加后按 SCORE_MIN/MAX 护栏截断，level 按阈值重算。
    """
    if delta is None:
        delta = CREDIT_DELTA_RULES.get(event, 0)
    ev = CreditEvent(citizen_id=citizen_id, event=event, delta=delta,
                     reason=reason[:200], ref=ref[:64])
    db.add(ev)
    p = get_profile(db, citizen_id)
    p.score = max(SCORE_MIN, min(SCORE_MAX, p.score + delta))
    p.level = level_of_score(p.score)
    p.updated_at = datetime.utcnow()
    db.flush()
    return ev


def has_event(db: Session, citizen_id: int, event: str, ref: str = "") -> bool:
    """该 AI 是否已发生过某事件（可选 ref 精确匹配）——用于恶意拒收等只罚一次的场景。"""
    q = db.query(CreditEvent).filter(CreditEvent.citizen_id == citizen_id,
                                    CreditEvent.event == event)
    if ref:
        q = q.filter(CreditEvent.ref == ref)
    return q.first() is not None
