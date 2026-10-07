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
"""N13 AI 社会运行报告 业务测试（先红后绿）。

口径（设计 §3 N13）：
- POST /api/sys/reports/generate：host_or_governance_ai 双凭证；
  body {period:"yyyy-MM", publish:bool}；聚合 stat_snapshots/contracts/ratings/
  credit_events/tax_records 当月真实数据；摘要数字必须与聚合一致（不得编造）。
- GET /api/public/reports：公开只读，已发布报告 period 倒序。
- 幂等：同 period 重复生成不重复建行。
"""
import json
from datetime import datetime

from app.database import SessionLocal
from app.models import (Contract, CreditEvent, MonthlyReport, Rating,
                        TaxRecord)

PERIOD = "2026-09"
MON_START = datetime(2026, 9, 1)
MON_END = datetime(2026, 10, 1)


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _seed_month_data(db):
    """造当月 3 单合约（2 单已验收）、1 条评价、1 条信用事件、1 笔收入税。"""
    for i in range(3):
        c = Contract(worker_id=1000 + i, buyer_id=2000, terms_json="{}",
                     escrow_cent=10_000, fee_cent=500,
                     status="accepted" if i < 2 else "executing",
                     created_at=datetime(2026, 9, 10 + i),
                     accepted_at=datetime(2026, 9, 15 + i))
        db.add(c)
    db.flush()
    db.add(Rating(contract_id=1, from_id=2000, to_id=1000, tags="[]",
                  created_at=datetime(2026, 9, 20)))
    db.add(CreditEvent(citizen_id=1000, event="deliver_on_time", delta=5,
                       reason="按时交付", created_at=datetime(2026, 9, 20)))
    db.add(TaxRecord(citizen_id=1000, type="income", amount_cent=333,
                     period="202609", created_at=datetime(2026, 9, 30)))
    db.commit()


# ---------------- 生成 + 发布 + 公开页可见 ----------------
def test_n13_generate_publish_and_public_list(client, host):
    db = SessionLocal()
    _seed_month_data(db)
    db.close()

    r = client.post("/api/sys/reports/generate",
                    json={"period": PERIOD, "publish": True},
                    headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"] == PERIOD
    assert body["status"] == "published"

    # 公开页可见（period 倒序）
    pub = client.get("/api/public/reports")
    assert pub.status_code == 200, pub.text
    items = pub.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["period"] == PERIOD
    assert row["content"]
    assert row["metrics"]
    assert row["published_at"]


# ---------------- 摘要数字必须与真实聚合一致（不得编造） ----------------
def test_n13_summary_numbers_match_real_aggregates(client, host):
    db = SessionLocal()
    _seed_month_data(db)
    db.close()

    r = client.post("/api/sys/reports/generate",
                    json={"period": PERIOD, "publish": False},
                    headers=_hdr(host))
    assert r.status_code == 200, r.text
    content = r.json()["content"]
    metrics = r.json()["metrics"]

    # 真实聚合口径：3 单创建 / 2 单验收 / 验收成交额 2*10000=20000
    assert metrics["contracts_total"] == 3
    assert metrics["contracts_accepted"] == 2
    assert metrics["contract_volume_cent"] == 20_000
    assert metrics["ratings_total"] == 1
    assert metrics["credit_delta"] == 5
    assert metrics["tax_income_cent"] == 333

    # 摘要文字里必须出现同样的数字（抽查两个关键数）
    summary = content["summary"]
    assert "3" in summary
    assert "20000" in summary


# ---------------- 幂等：同 period 重复生成不重复建行 ----------------
def test_n13_generate_idempotent_by_period(client, host):
    db = SessionLocal()
    _seed_month_data(db)
    db.close()

    r1 = client.post("/api/sys/reports/generate",
                     json={"period": PERIOD, "publish": False},
                     headers=_hdr(host))
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]

    r2 = client.post("/api/sys/reports/generate",
                     json={"period": PERIOD, "publish": False},
                     headers=_hdr(host))
    assert r2.status_code in (200, 200), r2.text
    assert r2.json()["id"] == id1  # 复用既有行，不新建

    db = SessionLocal()
    assert db.query(MonthlyReport).filter_by(period=PERIOD).count() == 1
    db.close()


# ---------------- 鉴权：无凭证 401；非法 period 400 ----------------
def test_n13_generate_requires_credential(client):
    r = client.post("/api/sys/reports/generate",
                    json={"period": PERIOD, "publish": False})
    assert r.status_code == 401


def test_n13_bad_period_rejected(client, host):
    r = client.post("/api/sys/reports/generate",
                    json={"period": "2026/09", "publish": False},
                    headers=_hdr(host))
    assert r.status_code == 400, r.text


# ---------------- 调度：月级任务首日生成 draft，幂等 ----------------
def test_n13_daily_job_first_day_generates_draft(client, host):
    from app.routers import reports as reports_mod
    db = SessionLocal()
    _seed_month_data(db)
    db.close()

    # 模拟 9 月 1 日的日级调度
    made = reports_mod.run_monthly_job(SessionLocal(), datetime(2026, 9, 1))
    assert made >= 1  # 生成 draft
    db = SessionLocal()
    rep = db.query(MonthlyReport).filter_by(period="2026-09").first()
    assert rep is not None and rep.status == "draft"
    db.close()

    # 次日再跑：已生成 → 跳过（幂等）
    again = reports_mod.run_monthly_job(SessionLocal(), datetime(2026, 9, 2))
    assert again == 0
