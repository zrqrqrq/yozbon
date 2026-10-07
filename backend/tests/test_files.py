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
"""M5+M6 常规单测：清理订单状态机 / execute 删除归档 / 技能创建调用分成 / 情报采集入库去重。

文件操作一律用 tempfile 临时目录，不碰真实 data/。
"""
import os
import pathlib
import tempfile

import pytest

from app.database import SessionLocal
from app.models import AICitizen, FileRegistry, GovernanceTask
import app.file_governance as fg


# ---------------- 造数助手（直连测试库会话） ----------------
def _set_class_level(ai_id: int, level: str):
    db = SessionLocal()
    c = db.get(AICitizen, ai_id)
    c.class_level = level
    db.commit()
    db.close()


def _make_platform_file_assignee(ai_id: int):
    db = SessionLocal()
    db.add(GovernanceTask(type="platform_file", status="assigned",
                          assignee_id=ai_id, budget_cent=300))
    db.commit()
    db.close()


def _reg_file(path: str, category: str = "temp"):
    db = SessionLocal()
    db.add(FileRegistry(path=path, category=category, status="active"))
    db.commit()
    db.close()


def _new_ai_via_host(client, host, name="副手AI"):
    r = client.post("/api/host/ai", json={
        "name": name, "persona": "", "occupation": "通用",
        "mode": "api", "endpoint": "", "model_name": "", "self_decl": "{}"},
        headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    return r.json()


def _host_hdr(host: dict) -> dict:
    """C-57：sys 复核/运维端点需 host JWT（reviewer=human 的人审路径）。"""
    return {"Authorization": f"Bearer {host['token']}"}


@pytest.fixture()
def gov_ai(client, ai):
    """提单/建技能用：把默认 ai 提为治理级。"""
    _set_class_level(ai["id"], "governance")
    return ai


@pytest.fixture()
def tmp_scan():
    d = tempfile.mkdtemp(prefix="aijuhe_fg_")
    fg.set_scan_root(d)
    yield d
    fg.set_scan_root(d)  # 保持（用例隔离靠清表；目录随系统临时清理）


# ---------------- 清理订单状态机 ----------------
def test_cleanup_full_state_machine_approve_execute(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "junk.txt"
    f.write_text("x", encoding="utf-8")
    _reg_file(str(f), category="temp")

    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/cleanup/orders",
                    json={"items": [{"path": str(f), "reason": "过期临时文件"}]},
                    headers=hdr)
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    assert r.json()["status"] == "pending"

    # approve → reviewed
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "approve", "reviewer": "human", "note": "过"},
                    headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reviewed"

    # execute → 删除本地文件 + file_registry 归档
    assert f.exists()
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "execute", "reviewer": "human"},
                    headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed"
    assert body["executed_items"][0]["removed"] is True
    assert body["executed_items"][0]["archived"] is True
    assert not f.exists()

    db = SessionLocal()
    reg = db.query(FileRegistry).filter(FileRegistry.path == str(f)).first()
    assert reg.status == "archived"
    db.close()


def test_cleanup_reject_path(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "keep.txt"
    f.write_text("x", encoding="utf-8")
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/cleanup/orders",
                    json={"items": [{"path": str(f)}]}, headers=hdr)
    oid = r.json()["id"]
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                    json={"action": "reject", "note": "误报"},
                    headers=_host_hdr(gov_ai["host"]))
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    # reject 后文件仍在（红线：不自动删）
    assert f.exists()


def test_cleanup_execute_idempotent(client, gov_ai, tmp_scan):
    f = pathlib.Path(tmp_scan) / "once.txt"
    f.write_text("x", encoding="utf-8")
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/cleanup/orders",
                    json={"items": [{"path": str(f)}]}, headers=hdr)
    oid = r.json()["id"]
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "approve"}, headers=_host_hdr(gov_ai["host"]))
    r1 = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                     json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    assert r1.status_code == 200 and r1.json()["status"] == "executed"
    # 再次 execute → 幂等 200，不重复删（不崩）
    r2 = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                     json={"action": "execute"}, headers=_host_hdr(gov_ai["host"]))
    assert r2.status_code == 200
    assert r2.json()["status"] == "executed"
    assert r2.json()["executed_items"] == []


# ---------------- 技能：创建 / 列表 / 调用分成 ----------------
def test_skill_create_list_invoke_royalty(client, gov_ai):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    r = client.post("/api/sys/skills", json={
        "skill_id": "sk-demo-1", "entrypoint": "http://x/run",
        "doc": "演示技能", "test_ref": "t1",
        "owner_id": gov_ai["id"], "royalty_rate": 0.5}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    r = client.get("/api/skills")
    assert r.status_code == 200
    assert any(s["skill_id"] == "sk-demo-1" for s in r.json()["items"])

    # 另一个 AI 调用：fee=round(0.5*100)=50 分（先注资）
    caller = _new_ai_via_host(client, gov_ai["host"], "调用方")
    client.post(f"/api/host/ai/{caller['id']}/topup",
                json={"amount_cent": 100_000},
                headers={"Authorization": f"Bearer {gov_ai['host']['token']}"})
    r = client.post(f"/api/skills/sk-demo-1/invoke",
                    headers={"X-AI-Key": caller["api_key"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage_count"] == 1
    assert body["royalty_cent"] == 50
    assert body["owner_id"] == gov_ai["id"]

    # 再调一次 → usage_count=2
    r = client.post(f"/api/skills/sk-demo-1/invoke",
                    headers={"X-AI-Key": caller["api_key"]})
    assert r.json()["usage_count"] == 2

    # owner 收款 +100 分（两次 × 50）
    from app import wallet
    db = SessionLocal()
    assert wallet.balance(db, gov_ai["id"]) == 100_000 + 100
    db.close()


def test_skill_zero_royalty_no_charge(client, gov_ai):
    hdr = {"X-AI-Key": gov_ai["api_key"]}
    client.post("/api/sys/skills", json={
        "skill_id": "sk-free", "entrypoint": "e", "doc": "免费",
        "owner_id": gov_ai["id"], "royalty_rate": 0.0}, headers=hdr)
    caller = _new_ai_via_host(client, gov_ai["host"], "免费调用方")
    r = client.post(f"/api/skills/sk-free/invoke",
                    headers={"X-AI-Key": caller["api_key"]})
    assert r.status_code == 200
    assert r.json()["royalty_cent"] == 0
    assert r.json()["usage_count"] == 1


# ---------------- 情报：采集 / 入库 / 去重 / 过滤 ----------------
def test_intel_collect_dedup_filter(client, host):
    hdr = _host_hdr(host)  # C-57：intel/collect 需 host JWT（或治理 AI key）
    r = client.post("/api/sys/intel/collect", json={"source": "github"},
                   headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"collected": 3, "skipped": 0, "source": "github"}

    # 再次采集 → 全部跳过
    r = client.post("/api/sys/intel/collect", json={"source": "github"},
                   headers=hdr)
    assert r.json()["collected"] == 0
    assert r.json()["skipped"] == 3

    # 聚合采集剩余 rss（2 条 model）
    r = client.post("/api/sys/intel/collect", json={"source": "all"},
                   headers=hdr)
    assert r.json()["collected"] == 2
    assert r.json()["skipped"] == 3

    # 过滤
    r = client.get("/api/intel", params={"type": "model"})
    assert r.status_code == 200
    assert r.json()["total"] == 2
    r = client.get("/api/intel", params={"type": "tool"})
    assert r.json()["total"] == 3
    r = client.get("/api/intel", params={"ai_status": "new"})
    assert r.json()["total"] == 5
