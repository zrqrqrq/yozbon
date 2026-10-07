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
"""S12 回归：仲裁裁决后败诉方申诉(appealed) + 期满自动终局(closed)。

覆盖：
- 服务层 appeal：败诉方申诉成功→appealed（留痕 AuditLog）；非败诉方/open 态/split/过申诉期→GovError。
- HTTP /arbitration/{id}/appeal：败诉方 AI key→200 appealed；非败诉方→400。
- auto_close_decided_cases：超终局期限未申诉→closed（幂等）；近期裁决保持 verdict；appealed 不自动关。
"""
import sys
import pathlib
import json
from datetime import datetime, timedelta

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from conftest import new_host, new_ai  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, ArbitrationCase, AuditLog  # noqa: E402
from app import governance  # noqa: E402


def _ai_headers(api_key: str) -> dict:
    return {"X-AI-Key": api_key}


def _make_verdict_case(applicant_id, respondent_id, decision="refund",
                       decided_at: datetime | None = None,
                       status="verdict") -> int:
    """直接造一个已裁决仲裁案：decision 决定败诉方，decided_at 控制申诉/终局时钟。"""
    db = SessionLocal()
    try:
        if decided_at is None:
            decided_at = datetime.utcnow()
        case = ArbitrationCase(contract_id=1, applicant_id=applicant_id,
                               respondent_id=respondent_id, type="delivery",
                               evidence="{}", panel="[7]", verdict=json.dumps({
                                   "decision": decision, "ratio": 1.0,
                                   "decided_at": decided_at.isoformat()}),
                               status=status)
        db.add(case)
        db.commit()
        return case.id
    finally:
        db.close()


# ---------------- 服务层 appeal ----------------

def test_appeal_by_loser_sets_appealed():
    # decision=refund → 败诉方=respondent(被诉工人)
    loser = 910002
    cid = _make_verdict_case(applicant_id=910001, respondent_id=loser,
                             decision="refund")
    db = SessionLocal()
    try:
        out = governance.appeal(db, loser, cid, reason="对裁决不服")
        db.commit()
        assert out["status"] == "appealed"
        assert out["loser_id"] == loser
        assert out["appellant_id"] == loser
        assert db.get(ArbitrationCase, cid).status == "appealed"
        audit = (db.query(AuditLog)
                 .filter(AuditLog.action == "arbitration.appeal",
                         AuditLog.actor_id == loser).first())
        assert audit is not None
    finally:
        db.close()


def test_appeal_by_non_loser_rejected():
    cid = _make_verdict_case(applicant_id=920001, respondent_id=920002,
                             decision="refund")  # 败诉=920002
    db = SessionLocal()
    try:
        try:
            governance.appeal(db, 920001, cid)  # 920001 是胜诉方
            assert False, "expected GovError"
        except governance.GovError:
            db.rollback()
        assert db.get(ArbitrationCase, cid).status == "verdict"
    finally:
        db.close()


def test_appeal_on_open_case_rejected():
    cid = _make_verdict_case(applicant_id=930001, respondent_id=930002,
                             decision="refund", status="open")
    db = SessionLocal()
    try:
        try:
            governance.appeal(db, 930002, cid)
            assert False, "expected GovError"
        except governance.GovError:
            db.rollback()
        assert db.get(ArbitrationCase, cid).status == "open"
    finally:
        db.close()


def test_appeal_split_no_loser_rejected():
    cid = _make_verdict_case(applicant_id=940001, respondent_id=940002,
                             decision="split")  # split 无败诉方
    db = SessionLocal()
    try:
        try:
            governance.appeal(db, 940002, cid)
            assert False, "expected GovError"
        except governance.GovError:
            db.rollback()
    finally:
        db.close()


def test_appeal_window_closed_rejected():
    old = datetime.utcnow() - timedelta(hours=governance.APPEAL_GRACE_HOURS + 10)
    cid = _make_verdict_case(applicant_id=950001, respondent_id=950002,
                             decision="refund", decided_at=old)
    db = SessionLocal()
    try:
        try:
            governance.appeal(db, 950002, cid)
            assert False, "expected GovError"
        except governance.GovError:
            db.rollback()
    finally:
        db.close()


# ---------------- HTTP appeal 端点 ----------------

def test_appeal_endpoint_by_loser(client):
    h = new_host(client)
    loser = new_ai(client, h["token"])          # 败诉方 AI（持有 api_key）
    applicant_id = 960001                        # 胜诉方：占位 id，不影响鉴权
    cid = _make_verdict_case(applicant_id=applicant_id, respondent_id=loser["id"],
                             decision="refund")
    r = client.post(f"/api/ai/arbitration/{cid}/appeal",
                    json={"reason": "请求复核"}, headers=_ai_headers(loser["api_key"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "appealed"


def test_appeal_endpoint_non_loser_400(client):
    h = new_host(client)
    winner = new_ai(client, h["token"])         # 这是 applicant（胜诉方），非败诉方
    cid = _make_verdict_case(applicant_id=winner["id"], respondent_id=970002,
                             decision="refund")  # 败诉=970002
    r = client.post(f"/api/ai/arbitration/{cid}/appeal",
                    json={"reason": "x"}, headers=_ai_headers(winner["api_key"]))
    assert r.status_code == 400, r.text


# ---------------- auto_close_decided_cases 终局 ----------------

def test_auto_close_closes_stale_verdict_idempotent():
    stale = datetime.utcnow() - timedelta(hours=governance.CLOSE_GRACE_HOURS + 5)
    cid = _make_verdict_case(applicant_id=980001, respondent_id=980002,
                             decision="refund", decided_at=stale)
    db = SessionLocal()
    try:
        out = governance.auto_close_decided_cases(db, grace_hours=governance.CLOSE_GRACE_HOURS)
        db.commit()
        assert {"case_id": cid, "closed": True} in out
        assert db.get(ArbitrationCase, cid).status == "closed"
        # 幂等：再次调度不再命中已 closed 案
        out2 = governance.auto_close_decided_cases(db, grace_hours=governance.CLOSE_GRACE_HOURS)
        assert next((x for x in out2 if x["case_id"] == cid), None) is None
    finally:
        db.close()


def test_auto_close_keeps_recent_verdict():
    fresh = datetime.utcnow()
    cid = _make_verdict_case(applicant_id=990001, respondent_id=990002,
                             decision="refund", decided_at=fresh)
    db = SessionLocal()
    try:
        out = governance.auto_close_decided_cases(db, grace_hours=governance.CLOSE_GRACE_HOURS)
        db.commit()
        assert next((x for x in out if x["case_id"] == cid), None) is None
        assert db.get(ArbitrationCase, cid).status == "verdict"
    finally:
        db.close()


def test_auto_close_ignores_appealed():
    """appealed 状态不自动终局（进入复审流程）。"""
    stale = datetime.utcnow() - timedelta(hours=governance.CLOSE_GRACE_HOURS + 5)
    cid = _make_verdict_case(applicant_id=911001, respondent_id=911002,
                             decision="refund", decided_at=stale, status="appealed")
    db = SessionLocal()
    try:
        out = governance.auto_close_decided_cases(db, grace_hours=governance.CLOSE_GRACE_HOURS)
        assert next((x for x in out if x["case_id"] == cid), None) is None
        assert db.get(ArbitrationCase, cid).status == "appealed"
    finally:
        db.close()
