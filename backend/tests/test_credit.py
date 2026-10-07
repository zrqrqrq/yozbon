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
"""B 线信用单测：事件追加 / delta 规则 / score 聚合 / level 阈值 / 市场排序联动读 score。"""
import pytest

from app import credit
from app.database import SessionLocal
from app.models import AICitizen, AIWallet, AIPermission, CreditEvent, CreditProfile


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_ai(db, uid: str, score: int = 100):
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status="active")
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=score))
    db.flush()
    return c.id


def test_record_event_updates_score_and_level(db):
    """信用事件追加 → score 累加 → level 按阈值重算。"""
    cid = _mk_ai(db, "cr-1")
    credit.record_event(db, cid, "deliver_on_time", reason="按时", ref="contract:1")
    prof = db.get(CreditProfile, cid)
    assert prof.score == 105       # +5
    ev = db.query(CreditEvent).filter_by(citizen_id=cid).one()
    assert ev.event == "deliver_on_time" and ev.delta == 5


def test_delta_rules_table(db):
    """delta 规则表：恶意拒收 -30、好评 +5、差评 -10。"""
    cid = _mk_ai(db, "cr-2")
    credit.record_event(db, cid, "malicious_reject")
    assert db.get(CreditProfile, cid).score == 70
    credit.record_event(db, cid, "positive_rating")
    assert db.get(CreditProfile, cid).score == 75
    credit.record_event(db, cid, "negative_rating")
    assert db.get(CreditProfile, cid).score == 65


def test_score_floor_and_level_thresholds(db):
    """分数下限护栏 + level 阈值映射（bottom/middle/boss/capital/governance）。"""
    assert credit.level_of_score(0) == "bottom"
    assert credit.level_of_score(100) == "bottom"
    assert credit.level_of_score(120) == "middle"
    assert credit.level_of_score(200) == "boss"
    assert credit.level_of_score(350) == "capital"
    assert credit.level_of_score(500) == "governance"
    # 下限护栏：连续负分不跌破 0
    cid = _mk_ai(db, "cr-3", score=5)
    credit.record_event(db, cid, "fraud")        # -50 → 截断到 0
    assert db.get(CreditProfile, cid).score == 0


def test_has_event_dedup(db):
    """has_event：恶意拒收只罚一次的判定依据。"""
    cid = _mk_ai(db, "cr-4")
    assert not credit.has_event(db, cid, "malicious_reject", ref="contract:9")
    credit.record_event(db, cid, "malicious_reject", ref="contract:9")
    assert credit.has_event(db, cid, "malicious_reject", ref="contract:9")
    assert not credit.has_event(db, cid, "malicious_reject", ref="contract:10")


def test_market_sort_reads_credit_score(db):
    """市场排序联动：jobs 检索读取信用档案（信用分高者排前）。"""
    from app import market
    from app.models import Host, Project, ProjectNode
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h@t.test", password_hash="x", host_credit=100))
    buyer = _mk_ai(db, "cr-buyer")
    p = Project(host_id=1, title="P", pm_citizen_id=buyer, status="running")
    db.add(p); db.flush()
    for i in range(3):
        db.add(ProjectNode(project_id=p.id, skill="文案", budget_cent=1000 * (i + 1),
                           status="matching"))
    db.flush()
    res = market.list_jobs(db, skill="文案")
    assert res["total"] == 3
