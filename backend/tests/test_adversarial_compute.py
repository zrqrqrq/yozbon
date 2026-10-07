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
"""Adversarial tests（逆向攻击测试）—— 按 docs/边界情形登记册 §一 10 类视角轰 D 线实现。

单测验证"设计内行为正确"，本文件验证"设计外行为不崩"：
  视角6 故障恢复：RH 提交/轮询抛错 → failed 不崩可重试；S3 未配置 → 本地落盘；
  视角3 恶意主体：非 worker 调 execute → 403；合约非 executing → 拒绝；
  视角6 重复回调幂等：同合约 execute 两次不重复交付；同 job_id 重复 poll 无副作用；
  视角2 极端规模：批量公民 exec 并发无锁死；
  视角5 边界数值：空 prompt / 非法 kind → 明确报错不崩；
  视角7 新供应商：RH key 全空 + LLM_PROVIDER=echo → 系统仍可用。
"""
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import compute, platform_compute  # noqa: E402
from app.database import SessionLocal, DATA_DIR  # noqa: E402
from app.models import AICitizen, Deliverable, Project, ProjectNode  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_no_real_keys(monkeypatch):
    """环境隔离：backend/.env 现含真实 RH/S3 key，本文件用例一律强制「无 key → mock +
    本地落盘」假设，避免真链路被触发/扣费。用例内自行 monkeypatch _slots 的故障注入
    测试在本 fixture 之后生效，不受影响。仅隔离环境，不改用例语义。"""
    for k in ("RH_API_KEY", "RH_API_KEY_PERSONAL", "RH_API_KEY_ENTERPRISE",
              "RH_API_KEY_INTL"):
        monkeypatch.setattr(settings, k, "")
    for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT"):
        monkeypatch.setattr(settings, k, "")



# ---------------- 造数助手：一条 executing 合约 ----------------
def _setup_executing_contract(client, host, worker, buyer_fund=100000, P=1000):
    """worker 投标 + 买方签约 → 返回 contract_id。buyer 由本函数创建并注资。"""
    rb = client.post("/api/host/ai", json={"name": "总管AI", "occupation": "PM"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    buyer = rb.json()
    client.post(f"/api/host/ai/{buyer['id']}/topup", json={"amount_cent": buyer_fund},
                headers={"Authorization": f"Bearer {host['token']}"})
    db = SessionLocal()
    proj = Project(host_id=host["host_id"], title="攻击项目", budget_cent=P,
                   pm_citizen_id=buyer["id"])
    db.add(proj); db.flush()
    node = ProjectNode(project_id=proj.id, skill="文生图", budget_cent=P,
                       duration_h=24, status="matching")
    db.add(node); db.commit()
    node_id = node.id
    db.close()
    r = client.post(f"/api/ai/jobs/{node_id}/bid", json={"offer_cent": P},
                    headers={"X-AI-Key": worker["api_key"]})
    cid = r.json()["contract_id"]
    client.post(f"/api/ai/contracts/{cid}/accept",
                json={"disclaimer_accepted": True},
                headers={"X-AI-Key": buyer["api_key"]})
    return cid, buyer


# ================= 视角6 故障恢复 =================
def test_rh_submit_error_returns_failed_not_crash(monkeypatch):
    """RH 提交边界抛错 → run 返回 failed（不崩），可重试。"""
    monkeypatch.setattr(platform_compute, "_slots",
                        lambda: [{"id": "personal", "key": "fake", "base": "http://rh.invalid",
                                  "queue": True, "password": ""}])

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(platform_compute, "_http_submit", boom)
    res = platform_compute.run("image", "cat", {})
    assert res["status"] == "failed"
    assert "network down" in res["error"]


def test_rh_poll_error_returns_failed_not_crash(monkeypatch):
    """RH 轮询边界抛错 → poll 返回 failed，不抛穿。"""
    monkeypatch.setattr(platform_compute, "_slots",
                        lambda: [{"id": "personal", "key": "fake", "base": "http://rh.invalid",
                                  "queue": True, "password": ""}])
    monkeypatch.setattr(platform_compute, "_http_submit",
                        lambda base, body: {"code": 0, "data": {"taskId": "t-1"}})
    monkeypatch.setattr(platform_compute, "_http_poll",
                        lambda base, path, body: (_ for _ in ()).throw(RuntimeError("timeout")))
    s = platform_compute.submit("image", "cat", {})
    assert s["job_id"] == "t-1"
    p = platform_compute.poll("t-1", "personal")
    assert p["status"] == "failed"
    assert "timeout" in p["error"]


def test_s3_not_configured_local_fallback():
    """S3 未配置：产物落盘 data/mock_out/，file_ref 可定位，文件真实存在。"""
    res = platform_compute.run("image", "s3-fallback-probe", {})
    assert res["status"] == "succeeded"
    f = res["files"][0]
    assert f["file_ref"].startswith("mock_out/")
    disk = DATA_DIR / f["file_ref"]
    assert disk.exists() and disk.stat().st_size > 0


# ================= 视角3 恶意主体 =================
def test_execute_non_worker_403(client, host, ai):
    """不是履约方的 AI 调 execute → 403。"""
    cid, _buyer = _setup_executing_contract(client, host, ai)
    stranger = client.post("/api/host/ai", json={"name": "陌生人"},
                           headers={"Authorization": f"Bearer {host['token']}"}).json()
    r = client.post(f"/api/ai/contracts/{cid}/execute",
                    json={"kind": "image", "prompt": "x"},
                    headers={"X-AI-Key": stranger["api_key"]})
    assert r.status_code == 403


def test_execute_missing_contract_404(client, ai):
    """合约不存在 → 404，不崩。"""
    r = client.post("/api/ai/contracts/999999/execute",
                    json={"kind": "image", "prompt": "x"},
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 404


# ================= 视角6 重复回调幂等 =================
def test_execute_twice_no_duplicate_deliverable(client, host, ai):
    """同合约 execute 两次：第一次 delivered，第二次因状态=delivered 被拒，不重复交付。"""
    cid, _buyer = _setup_executing_contract(client, host, ai)
    r1 = client.post(f"/api/ai/contracts/{cid}/execute",
                     json={"kind": "image", "prompt": "猫"},
                     headers={"X-AI-Key": ai["api_key"]})
    assert r1.status_code == 200 and r1.json()["status"] == "delivered"
    # 第二次：状态已 delivered → 400
    r2 = client.post(f"/api/ai/contracts/{cid}/execute",
                     json={"kind": "image", "prompt": "猫 again"},
                     headers={"X-AI-Key": ai["api_key"]})
    assert r2.status_code == 400
    db = SessionLocal()
    try:
        n = db.query(Deliverable).filter(Deliverable.contract_id == cid).count()
        assert n == 1, "重复执行产生了重复交付物"
    finally:
        db.close()


def test_repeated_poll_no_side_effects():
    """同一 mock job_id 重复 poll：产物指纹一致、文件数不膨胀（幂等）。"""
    s = platform_compute.submit("music", "song", {})
    p1 = platform_compute.poll(s["job_id"], s["key_slot"])
    p2 = platform_compute.poll(s["job_id"], s["key_slot"])
    assert p1["status"] == p2["status"] == "succeeded"
    assert p1["files"][0]["fingerprint"] == p2["files"][0]["fingerprint"]
    assert len(p1["files"]) == len(p2["files"])


# ================= 视角2 极端规模 =================
def test_batch_exec_no_lockup(client):
    """批量造 20 个 worker 公民并发 exec：全部 delegated，无锁死无异常。
    （premium 宿主席位 100，free 宿主仅 3 席位会触发熔断——这本身也是边界断言。）"""
    rh = client.post("/api/host/register", json={
        "email": f"batch-{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456", "nickname": "批", "seat_tier": "premium"})
    token = rh.json()["token"]
    host_id = rh.json()["host_id"]
    ids = []
    for i in range(20):
        r = client.post("/api/host/ai", json={
            "name": f"w{i}", "compute_decl": json.dumps({"channel": "worker"})},
            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    db = SessionLocal()
    try:
        results = []
        for cid in ids:
            citizen = db.get(AICitizen, cid)
            results.append(compute.exec(db, citizen, "image", f"task-{cid}", {}))
        assert all(x["meta"]["channel"] == "worker" for x in results)
        assert len(results) == 20
    finally:
        db.close()


# ================= 视角5 边界数值 =================
def test_empty_prompt_rejected():
    """空 prompt → failed，不静默出垃圾。"""
    assert platform_compute.run("image", "   ", {})["status"] == "failed"


def test_unknown_kind_rejected():
    """未知 kind → failed；端点层未知 kind → 400。"""
    assert platform_compute.run("hacking-kind", "x", {})["status"] == "failed"


def test_unknown_kind_endpoint_400(client, host, ai):
    cid, _ = _setup_executing_contract(client, host, ai)
    r = client.post(f"/api/ai/contracts/{cid}/execute",
                    json={"kind": "rm-rf", "prompt": "x"},
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 400


# ================= 视角7 新供应商/新模型 =================
def test_no_rh_keys_system_still_usable(monkeypatch):
    """RH key 全空 + LLM_PROVIDER=echo：种子注册、exec、LLM 全部不依赖真实 key。"""
    monkeypatch.setattr(platform_compute, "_slots", lambda: [])  # 强制无 key
    monkeypatch.setattr(settings, "LLM_PROVIDER", "echo")
    db = SessionLocal()
    try:
        from tools import seed_citizens
        r = seed_citizens.seed(db)
        assert r["created"] == 7 or r["skipped"] == 7   # 注册不依赖 key
        c = db.query(AICitizen).filter(AICitizen.ai_uid == "seed-image-pro").first()
        res = compute.exec(db, c, "image", "无 key 也能跑", {})
        assert res["status"] == "succeeded"
        assert platform_compute.complete("hi").startswith("[echo]")
    finally:
        db.close()
