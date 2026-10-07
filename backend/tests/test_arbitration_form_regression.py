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
"""阻断④回归：仲裁组庭 HTTP 入口 + 自动组庭调度。

覆盖：
- 组庭端点鉴权：无凭证 → 401；非治理级 AI → 403。
- 治理级 AI 为 open+空庭 案组庭 → formed=True，panel 非空且含仲裁员。
- 幂等：已组庭再次调用 → formed=False。
- 调度入口 auto_form_arbitration_panels 批量组庭 open+空庭 案。
"""
import sys
import pathlib

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from conftest import new_host, new_ai  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, ArbitrationCase  # noqa: E402
from app import governance  # noqa: E402


def _ai_headers(api_key: str) -> dict:
    return {"X-AI-Key": api_key}


def _make_open_case(applicant_id: int, respondent_id: int) -> int:
    db = SessionLocal()
    try:
        case = ArbitrationCase(contract_id=1, applicant_id=applicant_id,
                               respondent_id=respondent_id, type="delivery",
                               evidence="{}", panel="[]", status="open")
        db.add(case)
        db.commit()
        return case.id
    finally:
        db.close()


def _promote_governance(citizen_id: int):
    db = SessionLocal()
    try:
        ai = db.get(AICitizen, citizen_id)
        ai.class_level = "governance"
        db.commit()
    finally:
        db.close()


# ---------------- 鉴权 ----------------

def test_form_panel_requires_auth(client):
    r = client.post("/api/ai/arbitration/1/form_panel")
    assert r.status_code == 401, r.text


def test_form_panel_non_governance_forbidden(client):
    h = new_host(client)
    ai = new_ai(client, h["token"])
    r = client.post("/api/ai/arbitration/1/form_panel",
                    headers=_ai_headers(ai["api_key"]))
    assert r.status_code == 403, r.text


# ---------------- 组庭主流程 + 幂等 ----------------

def test_form_panel_forms_then_idempotent(client):
    h = new_host(client)
    arb = new_ai(client, h["token"], name="仲裁员")
    _promote_governance(arb["id"])
    cid = _make_open_case(applicant_id=900001, respondent_id=900002)

    r = client.post(f"/api/ai/arbitration/{cid}/form_panel",
                    headers=_ai_headers(arb["api_key"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["formed"] is True
    assert arb["id"] in body["panel"]

    # 幂等：已组庭再调用 formed=False
    r2 = client.post(f"/api/ai/arbitration/{cid}/form_panel",
                     headers=_ai_headers(arb["api_key"]))
    assert r2.status_code == 200, r2.text
    assert r2.json()["formed"] is False


# ---------------- 调度批量组庭 ----------------

def test_auto_form_panels_batch(client):
    h = new_host(client)
    arb = new_ai(client, h["token"])
    _promote_governance(arb["id"])
    cid = _make_open_case(applicant_id=900003, respondent_id=900004)

    db = SessionLocal()
    try:
        out = governance.auto_form_arbitration_panels(db, seed_ids=[arb["id"]])
        db.commit()
        formed = next((x for x in out if x["case_id"] == cid), None)
        assert formed is not None
        assert formed["formed"] is True
        assert arb["id"] in formed["panel"]
        # 再次调度幂等：空庭列表不含已组庭案
        out2 = governance.auto_form_arbitration_panels(db, seed_ids=[arb["id"]])
        assert next((x for x in out2 if x["case_id"] == cid), None) is None
    finally:
        db.close()


def test_auto_form_no_arbiter_stays_open(client):
    """无合格治理 AI 时无法组庭，案件保持 open（不误关）。"""
    cid = _make_open_case(applicant_id=900005, respondent_id=900006)
    db = SessionLocal()
    try:
        formed = governance.auto_form_arbitration_panel(db, cid)
        assert formed is None
        case = db.get(ArbitrationCase, cid)
        assert case.status == "open"
        assert case.panel == "[]"
    finally:
        db.close()
