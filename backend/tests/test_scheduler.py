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
"""M3+M4 调度器 + 平台运营四岗位 常规单测（契约 §4.2/§4.3）。

覆盖：
- run_due_jobs 生成 4 类岗位任务、同日幂等、跨日重跑；
- trigger 端点 + 看板端点；
- 四岗位合法结论集；
- 平台岗位竞标/报告/复核/结算全链（税池预充）；
- seed-text（governance 级）可竞标 platform_*；
- platform_facts 采集函数确定性与形态。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import (AICitizen, FileRegistry, GovernanceTask,  # noqa: E402
                        SchedulerRun)
from app import governance, platform_facts, scheduler, wallet  # noqa: E402
from app.governance import GovError  # noqa: E402
from tools import seed_citizens  # noqa: E402


def _db():
    return SessionLocal()


def _host_hdr(host: dict) -> dict:
    """C-57：platform-jobs trigger/看板需 host JWT。"""
    return {"Authorization": f"Bearer {host['token']}"}


def _seed_text_id(db) -> int:
    """跑种子并返回 seed-text（class_level=governance）的 citizen_id。"""
    seed_citizens.seed(db)
    db.commit()
    return db.query(AICitizen).filter(AICitizen.ai_uid == "seed-text").first().id


# ---------------- 调度核心：run_due_jobs ----------------
def test_run_due_jobs_creates_four_platform_tasks(client):
    """一次 run_due_jobs 生成四岗位任务，预算 300，写四条 scheduler_runs。

    （N 轮插件适配：stat_snapshot/leaderboard_snapshot 等注册日任务会额外跑，
    这里只校验 M3 关心的 4 个平台岗位任务本身，不锁死返回列表总长度。）"""
    db = _db()
    now = datetime(2026, 10, 4, 9, 0, 0)
    ids = scheduler.run_due_jobs(db, now=now)
    db.commit()
    ptypes = set(scheduler.PLATFORM_JOBS.keys())
    platform_ids = [i for i in ids if i and db.get(GovernanceTask, i)
                    and db.get(GovernanceTask, i).type in ptypes]
    assert len(platform_ids) == 4
    types = {db.get(GovernanceTask, i).type for i in platform_ids}
    assert types == ptypes
    assert all(db.get(GovernanceTask, i).budget_cent == 300 for i in platform_ids)
    runs = (db.query(SchedulerRun)
            .filter(SchedulerRun.job_type.in_(ptypes)).all())
    assert len(runs) == 4
    assert {r.job_type for r in runs} == ptypes
    assert {r.run_key for r in runs} == {"2026-10-04"}
    db.close()


def test_run_due_jobs_idempotent_same_day(client):
    """同一 run_key 再跑一次 → 不生成任何任务（日级幂等）。"""
    db = _db()
    now = datetime(2026, 10, 4, 9, 0, 0)
    scheduler.run_due_jobs(db, now=now)
    db.commit()
    second = scheduler.run_due_jobs(db, now=now)
    db.commit()
    assert second == []
    db.close()


def test_run_due_jobs_new_day_reruns(client):
    """跨日（新 run_key）→ 重新生成四岗位任务。"""
    db = _db()
    ptypes = set(scheduler.PLATFORM_JOBS.keys())
    scheduler.run_due_jobs(db, now=datetime(2026, 10, 4))
    db.commit()
    scheduler.run_due_jobs(db, now=datetime(2026, 10, 5))
    db.commit()
    runs_04 = (db.query(SchedulerRun)
               .filter(SchedulerRun.run_key == "2026-10-04",
                       SchedulerRun.job_type.in_(ptypes)).all())
    runs_05 = (db.query(SchedulerRun)
               .filter(SchedulerRun.run_key == "2026-10-05",
                       SchedulerRun.job_type.in_(ptypes)).all())
    assert {r.job_type for r in runs_04} == ptypes
    assert {r.job_type for r in runs_05} == ptypes
    db.close()


# ---------------- trigger / 看板端点 ----------------
def test_trigger_endpoint_then_dashboard(client, host):
    """POST trigger 立即生成；当日再 trigger → already=true；GET 看板反映今日状态。"""
    hdr = _host_hdr(host)  # C-57：trigger/看板需 host JWT
    r = client.post("/api/sys/platform-jobs/trigger",
                    json={"job_type": "platform_security"}, headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already"] is False
    assert body["task_id"] > 0
    task_id = body["task_id"]

    r2 = client.post("/api/sys/platform-jobs/trigger",
                     json={"job_type": "platform_security"}, headers=hdr)
    assert r2.status_code == 200, r2.text
    assert r2.json()["already"] is True
    assert r2.json()["task_id"] is None

    d = client.get("/api/sys/platform-jobs", headers=hdr)
    assert d.status_code == 200, d.text
    dash = d.json()
    assert dash["today"]["platform_security"] == task_id
    assert dash["today"]["platform_code"] == 0
    assert len(dash["recent"]) == 1
    assert dash["recent"][0]["task_status"] == "open"


def test_trigger_invalid_job_type_400(client, host):
    hdr = _host_hdr(host)
    r = client.post("/api/sys/platform-jobs/trigger",
                    json={"job_type": "platform_hack"}, headers=hdr)
    assert r.status_code == 400


# ---------------- 四岗位结论集 ----------------
def test_platform_conclusion_sets(client):
    """每个 platform_* 类型的合法结论集可通过提交；非法/空结论拒绝（C-19）。"""
    db = _db()
    sid = _seed_text_id(db)
    for jt, legal in governance.TASK_CONCLUSIONS.items():
        if not jt.startswith("platform_"):
            continue
        # 合法结论可提交（open 任务自动中标）
        t = governance.publish_task(db, jt, {}, budget_cent=300)
        rep = governance.submit_task_report(db, sid, t.id, sorted(legal)[0],
                                           {"note": "ok"})
        assert rep.status == "submitted"
        db.refresh(t)
        assert t.assignee_id == sid and t.status == "assigned"
        # 非法结论
        t2 = governance.publish_task(db, jt, {}, budget_cent=300)
        with pytest.raises(GovError):
            governance.submit_task_report(db, sid, t2.id, "bogus_conclusion", {})
        # 空结论
        t3 = governance.publish_task(db, jt, {}, budget_cent=300)
        with pytest.raises(GovError):
            governance.submit_task_report(db, sid, t3.id, "", {})
    db.commit()
    db.close()


# ---------------- 平台岗位全链（竞标→指派→报告→复核→结算） ----------------
def test_platform_job_full_chain_taxpool(client):
    """税池预充后：调度生成任务 → seed-text 竞标/中标/报告/复核/结算，
    报酬 300 分从税池出，中标 AI 入账。"""
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 50000, ref="test:seed")
    sid = _seed_text_id(db)
    ids = scheduler.run_due_jobs(db, now=datetime(2026, 10, 4))
    # ids[0] 是首个平台岗位治理任务（平台任务在 extras 之前 append）
    task_id = ids[0]
    t = db.get(GovernanceTask, task_id)
    governance.bid_task(db, sid, task_id, price_cent=280, message="我来做安全巡检")
    governance.assign_task(db, task_id, sid)
    governance.submit_task_report(db, sid, task_id, "safe", {"via": "rule"})
    governance.review_task(db, task_id, "pass", quality_score=1.0)

    bal_before = wallet.balance(db, sid)
    pool_before = wallet.get_system_state(db, "tax_pool")
    governance.settle_task(db, task_id)
    db.refresh(t)
    assert t.status == "paid"
    assert wallet.balance(db, sid) == bal_before + 300
    assert wallet.get_system_state(db, "tax_pool") == pool_before - 300
    db.commit()
    db.close()


def test_seed_text_eligible_to_bid_platform(client):
    """seed-text（class_level=governance）满足岗位门槛，可竞标 platform_*。"""
    db = _db()
    sid = _seed_text_id(db)
    t = governance.publish_task(db, "platform_security", {}, budget_cent=300)
    bid = governance.bid_task(db, sid, t.id, price_cent=300)
    assert bid.status == "bid"
    db.commit()
    db.close()


# ---------------- platform_facts 采集 ----------------
def test_platform_facts_shapes_and_determinism(client):
    """四采集函数返回契约形态；security/intel 确定性（同输入同输出）。"""
    db = _db()
    sec1 = platform_facts.collect_security_facts(db)
    sec2 = platform_facts.collect_security_facts(db)
    assert sec1 == sec2
    assert set(sec1) == {"ports", "services", "dep_vulns", "login_anomalies"}

    code = platform_facts.collect_code_facts(db)
    assert set(code) == {"errors", "slow_queries", "log_warnings"}

    intel1 = platform_facts.collect_intel_facts(db)
    intel2 = platform_facts.collect_intel_facts(db)
    assert intel1 == intel2 and len(intel1) >= 1
    assert all(set(i) == {"type", "title", "summary", "source_url",
                          "capability_tags"} for i in intel1)
    db.close()


def test_collect_file_facts_writes_registry(client, tmp_path):
    """扫描目录 → 按路径约定归类 → 写 file_registry（幂等 upsert）。"""
    (tmp_path / "tmp_a.log").write_text("x", encoding="utf-8")
    (tmp_path / "deliverable_out.zip").write_text("y", encoding="utf-8")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    db = _db()
    items = platform_facts.collect_file_facts(db, scan_root_dir=tmp_path)
    db.commit()
    by_name = {Path(i["path"]).name: i for i in items}
    assert by_name["tmp_a.log"]["category"] == "temp"
    assert by_name["deliverable_out.zip"]["category"] == "deliverable"
    assert by_name["pic.png"]["category"] == "media"
    for i in items:
        assert i["status"] in ("active", "expired")
    # 已写入 file_registry
    reg = (db.query(FileRegistry)
           .filter(FileRegistry.path.like("%deliverable_out.zip%")).first())
    assert reg is not None and reg.category == "deliverable"
    # 再扫一次幂等：不重复建行
    n = db.query(FileRegistry).count()
    platform_facts.collect_file_facts(db, scan_root_dir=tmp_path)
    db.commit()
    assert db.query(FileRegistry).count() == n
    db.close()


def test_platform_handlers_rule_determinism(client):
    """规则版执行体：默认空事实 → safe/no_issues/clean/collected。"""
    db = _db()
    for jt, expected in [("platform_security", "safe"),
                          ("platform_code", "no_issues"),
                          ("platform_file", "clean"),
                          ("platform_intel", "collected")]:
        t = governance.publish_task(db, jt, {}, budget_cent=300)
        out = governance.TASK_HANDLERS[jt](db, t, {})
        assert out["conclusion"] == expected
    db.commit()
    db.close()
