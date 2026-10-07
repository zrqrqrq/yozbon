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
"""平台算力池 / compute.exec / 自主工作流测试。

必含：
  - test_llm_echo_provider：LLM_PROVIDER=echo 返回确定性结果；
  - test_compute_exec_platform_routing：平台公民 exec → platform_compute（mock 成功→succeeded→files）；
  - test_compute_exec_worker_routing：worker 模式公民 exec → worker_bridge 路径；
  - test_autonomous_workflow_end_to_end：中标→签约托管→exec(mock)→自动交付→买方验收→结算，
    断言 worker 余额=净额、tax_pool/burned_total 联动（mock 通道，不真调 RH/S3）。
"""
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import compute, platform_compute, wallet  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, Project, ProjectNode  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_no_real_keys(monkeypatch):
    """环境隔离：backend/.env 现含真实 RH/S3 key，本文件用例一律强制「无 key → mock +
    本地落盘」假设，恢复原有用例语义，避免真链路被触发/扣费。仅隔离环境。"""
    for k in ("RH_API_KEY", "RH_API_KEY_PERSONAL", "RH_API_KEY_ENTERPRISE",
              "RH_API_KEY_INTL"):
        monkeypatch.setattr(settings, k, "")
    for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT"):
        monkeypatch.setattr(settings, k, "")


# ---------------- LLM 通道 ----------------
def test_llm_echo_provider(monkeypatch):
    """LLM_PROVIDER=echo：不发请求，返回确定性结果。"""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "echo")
    out1 = platform_compute.complete("写一句问候", system="你是助手")
    out2 = platform_compute.complete("写一句问候", system="你是助手")
    assert out1 == out2                       # 确定性
    assert out1.startswith("[echo]")
    assert "写一句问候" in out1


# ---------------- compute.exec 路由 ----------------
def _make_worker_citizen(client, host, name="worker-mode-AI", channel="worker"):
    """造一个指定 compute_assets.channel 的公民，返回 (citizen_id, api_key)。"""
    r = client.post("/api/host/ai", json={
        "name": name, "occupation": "测试", "compute_decl": json.dumps({"channel": channel, "provider": "platform"}),
    }, headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["api_key"]


def test_compute_exec_platform_routing(client, host, ai):
    """普通/平台公民 exec(image) → 走 platform_compute mock：succeeded + files 带 sha256 指纹。"""
    db = SessionLocal()
    try:
        citizen = db.get(AICitizen, ai["id"])
        res = compute.exec(db, citizen, "image", "一只猫", {})
        assert res["status"] == "succeeded"
        assert res["files"], "应返回产物文件"
        f = res["files"][0]
        assert len(f["fingerprint"]) == 64       # sha256 hex
        assert f["size"] > 0
        assert res["meta"]["provider"] == "platform"
    finally:
        db.close()


def test_compute_exec_worker_routing(client, host):
    """worker 模式公民 exec → 走 worker_bridge（delegated），不真跑平台算力。"""
    cid, _key = _make_worker_citizen(client, host, name="worker-A", channel="worker")
    db = SessionLocal()
    try:
        citizen = db.get(AICitizen, cid)
        res = compute.exec(db, citizen, "image", "x", {})
        assert res["status"] == "delegated"
        assert res["meta"]["channel"] == "worker"
        assert res["meta"]["provider"] == "worker_bridge"
    finally:
        db.close()


# ---------------- 自主工作流端到端 ----------------
def test_autonomous_workflow_end_to_end(client, host, ai):
    """中标→托管签约→exec(mock 成功)→自动交付→买方验收→结算；断言净额与税/销毁联动。"""
    # 买方 = 项目总管 AI（注资用于托管 P=1000 分=10 AC）
    rb = client.post("/api/host/ai", json={"name": "总管AI", "occupation": "PM"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200, rb.text
    buyer = rb.json()
    rt = client.post(f"/api/host/ai/{buyer['id']}/topup", json={"amount_cent": 100000},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rt.status_code == 200, rt.text

    # 建项目 + matching 节点（PM=buyer）
    db = SessionLocal()
    proj = Project(host_id=host["host_id"], title="猫图项目", budget_cent=20000,
                   pm_citizen_id=buyer["id"])
    db.add(proj); db.flush()
    node = ProjectNode(project_id=proj.id, skill="文生图", spec="画猫",
                       budget_cent=1000, duration_h=24, status="matching")
    db.add(node); db.commit()
    node_id = node.id
    db.close()

    wk = ai["api_key"]
    # 1) worker 投标
    r = client.post(f"/api/ai/jobs/{node_id}/bid", json={"offer_cent": 1000},
                   headers={"X-AI-Key": wk})
    assert r.status_code == 200, r.text
    cid = r.json()["contract_id"]
    # 2) 买方签约（托管锁定，buyer 扣 1000；M8：需确认免责声明）
    r = client.post(f"/api/ai/contracts/{cid}/accept",
                    json={"disclaimer_accepted": True},
                    headers={"X-AI-Key": buyer["api_key"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executing"
    # 3) worker 自主执行（mock 平台算力成功 → 自动版本化交付）
    r = client.post(f"/api/ai/contracts/{cid}/execute",
                    json={"kind": "image", "prompt": "一只橘猫", "params": {}},
                    headers={"X-AI-Key": wk})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "delivered"
    assert body["deliverable"]["fingerprint"]
    # 4) 买方验收通过 → 结算释放
    r = client.post(f"/api/ai/contracts/{cid}/acceptance", json={"result": "accept"},
                    headers={"X-AI-Key": buyer["api_key"]})
    assert r.status_code == 200, r.text

    # 5) 钱账断言：P=1000。税池初始 0 < 储备(UBI*30=6000) → 平衡阀费率 5.5%（规则10），
    #    fee=round(1000*0.055)=55；net=945<免税线5000→tax=0；worker 初始 100000 + 945 = 100945。
    db = SessionLocal()
    try:
        assert wallet.balance(db, ai["id"]) == 100000 + 945
        # 规则6：fee=55 = 销毁33(round(55*0.6)) + 税池22；tax=0
        assert wallet.get_system_state(db, "burned_total") == 33
        assert wallet.get_system_state(db, "tax_pool") >= 22
    finally:
        db.close()
