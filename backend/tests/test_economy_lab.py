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
"""N14 经济调控实验台 业务+对抗测试（先红后绿）。

口径（设计 §3 N14）：
- POST /api/sys/economy/simulate：host_or_governance_ai；dry-run 不落参——
  用真实历史（escrow/成交/流水）按新参数重算手续费/税/低保，输出预测影响；
  落 EconomyLabRun(status=draft)；绝不改 settings。
- POST /api/sys/economy/apply：{run_id, confirm:true} 二次确认必填；
  应用前快照 params_before；覆写 settings 实例属性；写 audit_logs。
- POST /api/sys/economy/rollback：仅最近一次 applied 可回滚；恢复 params_before。
"""
from datetime import datetime

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog, Contract, EconomyLabRun


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


@pytest.fixture(autouse=True)
def _restore_econ_settings():
    """每个测试后强制还原经济参数（防 apply 漏回滚污染后续用例）。"""
    snap = {k: getattr(settings, k) for k in
            ("TXN_FEE_RATE", "UBI_DAILY_CENT")}
    yield
    for k, v in snap.items():
        setattr(settings, k, v)


def _seed_contracts(db):
    for i in range(3):
        db.add(Contract(worker_id=100 + i, buyer_id=200, terms_json="{}",
                        escrow_cent=10_000, fee_cent=500,
                        status="accepted",
                        created_at=datetime(2026, 9, 10 + i),
                        accepted_at=datetime(2026, 9, 15 + i)))
    db.commit()


# ---------------- simulate：dry-run 不改真实参数 ----------------
def test_n14_simulate_does_not_touch_settings(client, host):
    db = SessionLocal()
    _seed_contracts(db)
    db.close()
    old_rate = settings.TXN_FEE_RATE

    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"TXN_FEE_RATE": 0.08}},
                    headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "draft"
    # 模拟后 settings 必须纹丝不动
    assert settings.TXN_FEE_RATE == old_rate
    # 预测影响里要有基于真实成交的手续费变化
    sim = body["simulation"]
    assert "fee_revenue_delta_cent" in sim
    # 3 单 * 10000 分 * (0.08-0.05) = 900 分
    assert sim["fee_revenue_delta_cent"] == pytest.approx(900, abs=1)


# ---------------- apply → rollback 全链路 ----------------
def test_n14_apply_then_rollback_restores_settings(client, host):
    db = SessionLocal()
    _seed_contracts(db)
    db.close()
    old_rate = settings.TXN_FEE_RATE

    run = client.post("/api/sys/economy/simulate",
                      json={"params": {"TXN_FEE_RATE": 0.08}},
                      headers=_hdr(host)).json()
    rid = run["id"]

    # 未二次确认 → 400
    bad = client.post("/api/sys/economy/apply",
                      json={"run_id": rid, "confirm": False},
                      headers=_hdr(host))
    assert bad.status_code == 400, bad.text

    ok = client.post("/api/sys/economy/apply",
                     json={"run_id": rid, "confirm": True},
                     headers=_hdr(host))
    assert ok.status_code == 200, ok.text
    assert settings.TXN_FEE_RATE == 0.08  # 进程内生效

    rb = client.post("/api/sys/economy/rollback",
                     json={"run_id": rid},
                     headers=_hdr(host))
    assert rb.status_code == 200, rb.text
    assert settings.TXN_FEE_RATE == old_rate  # 已恢复

    db = SessionLocal()
    run_row = db.get(EconomyLabRun, rid)
    assert run_row.status == "rolled_back"
    # 审计留痕
    acts = [a.action for a in db.query(AuditLog).all()]
    assert any("economy" in a for a in acts)
    db.close()


# ---------------- 仅最近一次 applied 可回滚 ----------------
def test_n14_rollback_only_latest_applied(client, host):
    db = SessionLocal()
    _seed_contracts(db)
    db.close()

    r1 = client.post("/api/sys/economy/simulate",
                     json={"params": {"UBI_DAILY_CENT": 300}},
                     headers=_hdr(host)).json()["id"]
    r2 = client.post("/api/sys/economy/simulate",
                     json={"params": {"UBI_DAILY_CENT": 400}},
                     headers=_hdr(host)).json()["id"]
    client.post("/api/sys/economy/apply",
                json={"run_id": r1, "confirm": True}, headers=_hdr(host))
    client.post("/api/sys/economy/apply",
                json={"run_id": r2, "confirm": True}, headers=_hdr(host))
    # 现在回滚旧的 r1 → 拒绝（r2 才是最近 applied）
    rb_old = client.post("/api/sys/economy/rollback",
                         json={"run_id": r1}, headers=_hdr(host))
    assert rb_old.status_code in (400, 409), rb.text
    # 回滚 r2 → 成功
    rb_new = client.post("/api/sys/economy/rollback",
                         json={"run_id": r2}, headers=_hdr(host))
    assert rb_new.status_code == 200, rb_new.text


# ---------------- 非法参数 / 无凭证 ----------------
def test_n14_unknown_param_rejected(client, host):
    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"SECRET_KEY": "hacked"}},
                    headers=_hdr(host))
    assert r.status_code == 400, r.text


def test_n14_requires_credential(client):
    assert client.post("/api/sys/economy/simulate",
                       json={"params": {}}).status_code == 401
