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
"""M5+M6 攻击测试（契约 §5.4 + 边界登记册攻击视角）。

视角：越权提单 / 坏输入 / 状态机逆行 / 重复执行 / 钱包攻击 / 重复采集 / 唯一约束 / 越界参数。
"""
import pathlib
import tempfile

import pytest

from app.database import SessionLocal
from app.models import AICitizen, GovernanceTask
import app.file_governance as fg


def _set_class_level(ai_id, level):
    db = SessionLocal()
    db.get(AICitizen, ai_id).class_level = level
    db.commit()
    db.close()


def _new_ai_via_host(client, host, name):
    r = client.post("/api/host/ai", json={
        "name": name, "persona": "", "occupation": "通用",
        "mode": "api", "endpoint": "", "model_name": "", "self_decl": "{}"},
        headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    return r.json()


def _host_hdr(host: dict) -> dict:
    """C-57：sys 复核端点需 host JWT 或治理级 AI key。"""
    return {"Authorization": f"Bearer {host['token']}"}


@pytest.fixture()
def gov_ai(client, ai):
    _set_class_level(ai["id"], "governance")
    return ai


@pytest.fixture()
def tmp_scan():
    d = tempfile.mkdtemp(prefix="aijuhe_adv_")
    fg.set_scan_root(d)
    return d


def _order(client, ai, path):
    r = client.post("/api/sys/cleanup/orders",
                    json={"items": [{"path": path}]},
                    headers={"X-AI-Key": ai["api_key"]})
    return r


# ---- A1: 非 platform_file 中标 AI / 非治理级提单 → 403 ----
def test_a1_non_assignee_cannot_submit(client, ai, tmp_scan):
    # 默认 ai：class_level=bottom，无 platform_file 中标记录
    f = pathlib.Path(tmp_scan) / "x.txt"
    f.write_text("x", encoding="utf-8")
    r = _order(client, ai, str(f))
    assert r.status_code == 403, r.text


# ---- A1b: 是 platform_file 中标者则可提单（对照组，验证资格校验不误伤）----
def test_a1b_platform_file_assignee_can_submit(client, ai, tmp_scan):
    db = SessionLocal()
    db.add(GovernanceTask(type="platform_file", status="assigned",
                          assignee_id=ai["id"], budget_cent=300))
    db.commit()
    db.close()
    f = pathlib.Path(tmp_scan) / "y.txt"
    f.write_text("x", encoding="utf-8")
    r = _order(client, ai, str(f))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"


# ---- A2: items 空 / 含不存在路径 → 400 ----
def test_a2_empty_items_bad_path(client, gov_ai, tmp_scan):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/cleanup/orders", json={"items": []}, headers=hdr)
    assert r.status_code == 400
    r = client.post("/api/sys/cleanup/orders",
                    json={"items": [{"path": "/no/such/file.bin"}]}, headers=hdr)
    assert r.status_code == 400


# ---- A3: 未 review 直接 execute → 400（状态机拦截，红线）----
def test_a3_execute_without_review(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "z.txt"
    f.write_text("x", encoding="utf-8")
    oid = _order(client, gov_ai, str(f)).json()["id"]
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 400
    # 红线：文件仍在
    assert f.exists()


# ---- A4a: rejected 订单 execute → 400 ----
def test_a4_rejected_execute(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "w.txt"
    f.write_text("x", encoding="utf-8")
    oid = _order(client, gov_ai, str(f)).json()["id"]
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "reject"}, headers=_host_hdr(gov_ai["host"]))
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 400
    assert f.exists()


# ---- A4b: 已 executed 再 execute → 幂等 200，不重复删 ----
def test_a4b_executed_idempotent(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "v.txt"
    f.write_text("x", encoding="utf-8")
    oid = _order(client, gov_ai, str(f)).json()["id"]
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "approve"}, headers=_host_hdr(gov_ai["host"]))
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 200
    assert r.json()["executed_items"] == []


# ---- A5: 技能调用钱包不足 → 400 且 usage_count 不变 ----
def test_a5_insufficient_wallet_no_count(client, gov_ai):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    client.post("/api/sys/skills", json={
        "skill_id": "sk-paid", "entrypoint": "e", "doc": "付费",
        "owner_id": gov_ai["id"], "royalty_rate": 0.2}, headers=hdr)  # fee=20
    poor = _new_ai_via_host(client, gov_ai["host"], "穷AI")  # 不注资，余额 0
    r = client.post("/api/skills/sk-paid/invoke",
                    headers={"X-AI-Key": poor["api_key"]})
    assert r.status_code == 400, r.text
    # usage_count 仍为 0
    r = client.get("/api/skills")
    sk = [s for s in r.json()["items"] if s["skill_id"] == "sk-paid"][0]
    assert sk["usage_count"] == 0


# ---- A6: 情报重复采集 → skipped 不重复入库 ----
def test_a6_intel_duplicate_collect(client, host):
    hdr = _host_hdr(host)  # C-57：intel/collect 需 host JWT（或治理 AI key）
    client.post("/api/sys/intel/collect", json={"source": "github"}, headers=hdr)
    r = client.post("/api/sys/intel/collect", json={"source": "github"}, headers=hdr)
    assert r.json()["collected"] == 0
    assert r.json()["skipped"] == 3
    r = client.get("/api/intel")
    assert r.json()["total"] == 3


# ---- A7: skill_id 重复创建 → 409 ----
def test_a7_duplicate_skill_id(client, gov_ai):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    body = {"skill_id": "sk-dup", "entrypoint": "e", "doc": "d",
            "owner_id": gov_ai["id"], "royalty_rate": 0.1}
    assert client.post("/api/sys/skills", json=body, headers=hdr).status_code == 200
    r = client.post("/api/sys/skills", json=body, headers=hdr)
    assert r.status_code == 409, r.text


# ---- A8: royalty_rate 越界（1.5）→ 400 ----
def test_a8_royalty_out_of_range(client, gov_ai):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/skills", json={
        "skill_id": "sk-bad-rate", "entrypoint": "e", "doc": "d",
        "owner_id": gov_ai["id"], "royalty_rate": 1.5}, headers=hdr)
    assert r.status_code == 400, r.text


# ---- A9: 非治理级 AI 建技能 → 403 ----
def test_a9_non_governance_create_skill(client, ai):
    r = client.post("/api/sys/skills", json={
        "skill_id": "sk-nongov", "entrypoint": "e", "doc": "d",
        "owner_id": ai["id"], "royalty_rate": 0.1},
        headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 403


# ---- A10: 非法 collect source → 400 ----
def test_a10_bad_intel_source(client, host):
    r = client.post("/api/sys/intel/collect", json={"source": "hack"},
                   headers=_host_hdr(host))
    assert r.status_code == 400
