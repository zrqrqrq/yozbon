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
"""阻断②③回归：弹劾宿主鉴权 + 投票去重 + 双重阈值；*-g33 路由族统一鉴权。

覆盖：
- 弹劾三端点无凭证 → 401（路由级 host_or_governance_ai + 端点级 get_current_host）。
- 同一宿主重复投票 → 400 already voted（voters_json 去重）。
- 参与票 < IMPEACHMENT_MIN_VOTES → resolve 判 dismissed（杜绝单票罢黜城主）。
- 达最小法定人数且赞成比例达 QUORUM → resolve 判 removal。
- security-g33 健康探针匿名 200；其余受保护端点无凭证 → 401。
- gov-g33 非弹劾治理端点无凭证 → 401。
"""
import sys
import pathlib

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from conftest import new_host  # noqa: E402


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ======================== 鉴权 ========================

def test_impeachment_requires_auth(client):
    """弹劾端点无任何凭证 → 401（路由级 + 端点级双重 host_or_governance_ai/get_current_host）。"""
    r = client.post("/api/gov-g33/impeachment", json={
        "target_type": "delegate", "target_id": 1, "charges": "滥用职权",
    })
    assert r.status_code == 401, r.text


def test_gov_g33_protected_requires_auth(client):
    """gov-g33 非弹劾治理端点无凭证 → 401。"""
    r = client.get("/api/gov-g33/sunset/active")
    assert r.status_code == 401, r.text


def test_security_g33_health_anonymous_ok(client):
    """安全探针公开：/health 匿名 200。"""
    r = client.get("/api/security-g33/health")
    assert r.status_code == 200, r.text


def test_security_g33_protected_requires_auth(client):
    """安全路由受保护端点无凭证 → 401；带宿主凭证放行。"""
    r = client.get("/api/security-g33/cors/policies")
    assert r.status_code == 401, r.text
    h = new_host(client)
    r2 = client.get("/api/security-g33/cors/policies",
                    headers=_bearer(h["token"]))
    assert r2.status_code == 200, r2.text


# ======================== 投票去重 ========================

def test_impeachment_vote_dedup(client):
    """同一宿主二次投票 → 400 already voted。"""
    h = new_host(client)
    r = client.post("/api/gov-g33/impeachment",
                    headers=_bearer(h["token"]),
                    json={"target_type": "delegate", "target_id": 99,
                          "charges": "越权"})
    assert r.status_code == 200, r.text
    cid = r.json()["case_id"]

    r1 = client.post(f"/api/gov-g33/impeachment/{cid}/vote",
                     headers=_bearer(h["token"]), json={"vote": "for"})
    assert r1.status_code == 200, r1.text

    r2 = client.post(f"/api/gov-g33/impeachment/{cid}/vote",
                     headers=_bearer(h["token"]), json={"vote": "for"})
    assert r2.status_code == 400, r2.text
    assert "already voted" in r2.text


# ======================== 双重阈值 ========================

def test_impeachment_below_min_votes_dismissed(client):
    """仅单票参与（< MIN_VOTES）→ resolve 判 dismissed，城主不受罢黜。"""
    h = new_host(client)
    r = client.post("/api/gov-g33/impeachment",
                    headers=_bearer(h["token"]),
                    json={"target_type": "governor", "target_id": 1,
                          "charges": "独裁"})
    cid = r.json()["case_id"]
    client.post(f"/api/gov-g33/impeachment/{cid}/vote",
                headers=_bearer(h["token"]), json={"vote": "for"})

    rr = client.post(f"/api/gov-g33/impeachment/{cid}/resolve",
                     headers=_bearer(h["token"]))
    assert rr.status_code == 200, rr.text
    assert rr.json()["status"] == "dismissed"
    assert rr.json()["vote_total"] == 1


def test_impeachment_quorum_removal(client):
    """达最小法定人数且赞成比例达 QUORUM → resolve 判 removal。"""
    hosts = [new_host(client) for _ in range(3)]
    initiator = hosts[0]
    r = client.post("/api/gov-g33/impeachment",
                    headers=_bearer(initiator["token"]),
                    json={"target_type": "delegate", "target_id": 555,
                          "charges": "严重渎职"})
    cid = r.json()["case_id"]
    for h in hosts:
        rv = client.post(f"/api/gov-g33/impeachment/{cid}/vote",
                         headers=_bearer(h["token"]), json={"vote": "for"})
        assert rv.status_code == 200, rv.text

    rr = client.post(f"/api/gov-g33/impeachment/{cid}/resolve",
                     headers=_bearer(initiator["token"]))
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["status"] == "removal"
    assert body["vote_total"] == 3
    assert body["vote_for"] == 3
