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
"""M3+M4 攻击测试（契约 §4.4 + 边界登记册 §一 10 类攻击视角）。

视角映射：
- A1 同日重复 trigger   → 故障恢复（幂等/重复回调）
- A2 bottom 竞标被拒    → 恶意主体（绕过岗位门槛薅税池）
- A3 空/非法结论        → C-19 延续（空报告套税池报酬）
- A4 未中标 AI 交报告   → 恶意主体（冒领中标者报酬）
- A5 税池不足结算       → 经济失衡（流动性枯竭不印钞护栏）
- A6 非法 job_type     → 新物种进场（封闭枚举输入）
- A7 重复竞标           → 恶意主体（刷竞标记录）
- A8/A9 L2+证书豁免     → 规则冲突（门槛例外分支正确性）
"""
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, SchedulerRun, SkillCertificate  # noqa: E402
from app import governance, scheduler, wallet  # noqa: E402
from app.governance import GovError  # noqa: E402
from app.wallet import WalletError  # noqa: E402
from tools import seed_citizens  # noqa: E402


def _db():
    return SessionLocal()


def _host_hdr(host: dict) -> dict:
    """C-57：platform-jobs trigger/看板需 host JWT。"""
    return {"Authorization": f"Bearer {host['token']}"}


def _seed_text_id(db) -> int:
    seed_citizens.seed(db)
    db.commit()
    return db.query(AICitizen).filter(AICitizen.ai_uid == "seed-text").first().id


# A1：同日重复 trigger 同类型 → 第二次不生成（幂等）
def test_a1_same_day_trigger_idempotent(client, host):
    hdr = _host_hdr(host)
    r1 = client.post("/api/sys/platform-jobs/trigger",
                     json={"job_type": "platform_intel"}, headers=hdr)
    assert r1.status_code == 200, r1.text
    assert r1.json()["already"] is False
    task_id = r1.json()["task_id"]

    r2 = client.post("/api/sys/platform-jobs/trigger",
                     json={"job_type": "platform_intel"}, headers=hdr)
    assert r2.status_code == 200
    assert r2.json()["already"] is True
    assert r2.json()["task_id"] is None
    assert r2.json()["task_ids"] == []

    db = _db()
    n = (db.query(SchedulerRun)
         .filter(SchedulerRun.job_type == "platform_intel").count())
    assert n == 1
    assert db.query(SchedulerRun).count() == 1
    db.close()


# A2：bottom 级 AI（无 L2+ 证书）竞标 platform_security → 400（服务层+端点层双验）
def test_a2_bottom_ai_blocked(client, ai):
    db = _db()
    t = governance.publish_task(db, "platform_security", {}, budget_cent=300)
    db.commit()
    # 服务层
    with pytest.raises(GovError):
        governance.bid_task(db, ai["id"], t.id, price_cent=300)
    db.close()
    # 端点层（ai_gov.py 映射 GovError→400）
    r = client.post(f"/api/ai/gov/tasks/{t.id}/bid",
                   json={"price_cent": 300, "message": "我能做"},
                   headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 400, r.text


# A3：空/非法结论提交 platform_file 报告 → 400（C-19 延续）
def test_a3_empty_or_invalid_conclusion_rejected(client):
    db = _db()
    sid = _seed_text_id(db)
    t = governance.publish_task(db, "platform_file", {}, budget_cent=300)
    governance.bid_task(db, sid, t.id, price_cent=300)
    governance.assign_task(db, t.id, sid)
    for bad in ("", "delete_all", "cleaned_up", "CLEAN", " Clean "):
        with pytest.raises(GovError):
            governance.submit_task_report(db, sid, t.id, bad, {})
    # 任务仍 assigned，报告未入
    db.commit()
    db.close()


# A4：未中标 AI 提交报告 → 400
def test_a4_non_assignee_cannot_report(client, ai):
    db = _db()
    sid = _seed_text_id(db)
    t = governance.publish_task(db, "platform_security", {}, budget_cent=300)
    governance.bid_task(db, sid, t.id, price_cent=300)
    governance.assign_task(db, t.id, sid)
    # bottom AI 未中标：门槛先拒（等价 400，且绝不自动中标）
    with pytest.raises(GovError):
        governance.submit_task_report(db, ai["id"], t.id, "safe", {})
    db.refresh(t)
    assert t.assignee_id == sid
    assert t.status == "assigned"
    db.commit()
    db.close()


# A5：税池不足结算 → WalletError 护栏抛错不崩（不印钞）
def test_a5_taxpool_insufficient_no_print_money(client):
    db = _db()
    sid = _seed_text_id(db)
    assert wallet.get_system_state(db, "tax_pool") == 0  # 未预充
    t = governance.publish_task(db, "platform_code", {}, budget_cent=300)
    governance.bid_task(db, sid, t.id, price_cent=300)
    governance.assign_task(db, t.id, sid)
    governance.submit_task_report(db, sid, t.id, "no_issues", {})
    governance.review_task(db, t.id, "pass")
    with pytest.raises(WalletError):
        governance.settle_task(db, t.id)
    db.close()


# A6：非法 job_type trigger → 400
def test_a6_invalid_job_type(client, host):
    hdr = _host_hdr(host)
    r = client.post("/api/sys/platform-jobs/trigger",
                    json={"job_type": "platform_ransom"}, headers=hdr)
    assert r.status_code == 400
    r2 = client.post("/api/sys/platform-jobs/trigger",
                     json={"job_type": ""}, headers=hdr)
    assert r2.status_code == 400
    # 未产生任何调度记录
    db = _db()
    assert db.query(SchedulerRun).count() == 0
    db.close()


# A7：同一 AI 重复竞标同一任务 → 400
def test_a7_duplicate_bid_rejected(client):
    db = _db()
    sid = _seed_text_id(db)
    t = governance.publish_task(db, "platform_intel", {}, budget_cent=300)
    governance.bid_task(db, sid, t.id, price_cent=300)
    with pytest.raises(GovError):
        governance.bid_task(db, sid, t.id, price_cent=290)
    db.commit()
    db.close()


# A8：bottom AI 持 l2 valid 证书 → 门槛豁免可竞标（例外分支正确放行）
def test_a8_l2_valid_cert_exemption(client, ai):
    db = _db()
    db.add(SkillCertificate(citizen_id=ai["id"], skill="secops",
                             level="l2", status="valid"))
    db.commit()
    t = governance.publish_task(db, "platform_security", {}, budget_cent=300)
    bid = governance.bid_task(db, ai["id"], t.id, price_cent=300)
    assert bid.status == "bid"
    db.close()


# A9：l2 证书 revoked → 不豁免，仍被拒（防用吊销证书钻空子）
def test_a9_revoked_cert_no_exemption(client, ai):
    db = _db()
    db.add(SkillCertificate(citizen_id=ai["id"], skill="secops",
                             level="l2", status="revoked"))
    db.commit()
    t = governance.publish_task(db, "platform_security", {}, budget_cent=300)
    with pytest.raises(GovError):
        governance.bid_task(db, ai["id"], t.id, price_cent=300)
    db.close()
