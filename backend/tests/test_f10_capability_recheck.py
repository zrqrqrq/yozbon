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
"""G10 能力定时强制复核 job 测试（capability_recheck）。

覆盖：降档映射、过期扫描（expires_at 到期 / issued_at 兜底线 / 未过期忽略）、
强制复核原语（降级+过期证书+治理任务+审计）、下限幂等、日级汇总、scheduler 注册。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import capability
from app import capability_recheck as cr
from app.database import SessionLocal
from app.models import (AuditLog, GovernanceTask, SkillCertificate)


@pytest.fixture()
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _new_cid():
    # 用大随机 id 规避外键/冲突（本模块不校验 AICitizen 存在性）
    return 900000 + (uuid.uuid4().int % 99999)


# ==================== 1. 降档映射 ====================

def test_demote_one_step_levels():
    assert cr._demote_one_step("l3") == "l2"
    assert cr._demote_one_step("l2") == "l1"
    assert cr._demote_one_step("l1") == "unverified"
    assert cr._demote_one_step("unverified") == "unverified"  # 下限保持
    assert cr._demote_one_step("bogus") == "unverified"        # 未知按 unverified


# ==================== 2. 过期扫描 ====================

def test_scan_detects_expired_by_expires_at(db_session):
    cid, skill = _new_cid(), "coding"
    db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level="l2",
                                    status="valid",
                                    issued_at=datetime.utcnow() - timedelta(days=400),
                                    expires_at=datetime.utcnow() - timedelta(days=1)))
    db_session.commit()
    out = cr.scan_expired_certificates(db_session)
    assert (cid, skill) in out


def test_scan_detects_stale_by_issued_at_fallback(db_session):
    cid, skill = _new_cid(), "research"
    # 无 expires_at；issued_at 超过 CAPABILITY_RECHECK_DAYS
    db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level="l1",
                                    status="valid",
                                    issued_at=datetime.utcnow()
                                    - timedelta(days=cr.settings.CAPABILITY_RECHECK_DAYS + 10),
                                    expires_at=None))
    db_session.commit()
    out = cr.scan_expired_certificates(db_session)
    assert (cid, skill) in out


def test_scan_ignores_current_and_nonvalid(db_session):
    cid = _new_cid()
    # 未到期有效证书：不应命中
    db_session.add(SkillCertificate(citizen_id=cid, skill="fresh", level="l2",
                                    status="valid",
                                    issued_at=datetime.utcnow(),
                                    expires_at=datetime.utcnow() + timedelta(days=100)))
    # 已 downgraded：不复核（只扫 valid）
    db_session.add(SkillCertificate(citizen_id=cid, skill="old", level="l1",
                                    status="downgraded",
                                    issued_at=datetime.utcnow() - timedelta(days=500),
                                    expires_at=datetime.utcnow() - timedelta(days=100)))
    db_session.commit()
    out = cr.scan_expired_certificates(db_session)
    assert (cid, "fresh") not in out
    assert (cid, "old") not in out


def test_scan_dedupes_same_skill(db_session):
    cid, skill = _new_cid(), "audit"
    for _ in range(3):
        db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level="l2",
                                        status="valid",
                                        issued_at=datetime.utcnow() - timedelta(days=400),
                                        expires_at=datetime.utcnow() - timedelta(days=1)))
    db_session.commit()
    out = cr.scan_expired_certificates(db_session)
    assert out.count((cid, skill)) == 1


# ==================== 3. 强制复核原语 ====================

def test_request_reexam_downgrades_and_expires(db_session):
    cid, skill = _new_cid(), "coding"
    capability.set_verified_level(db_session, cid, skill, "l2")
    cap = SkillCertificate(citizen_id=cid, skill=skill, level="l2",
                           status="valid",
                           issued_at=datetime.utcnow() - timedelta(days=400),
                           expires_at=datetime.utcnow() - timedelta(days=1))
    db_session.add(cap)
    db_session.commit()

    res = cr.request_capability_reexam(db_session, cid, skill,
                                       reason="到期", source="expiry")
    db_session.commit()

    assert res["from_level"] == "l2"
    assert res["to_level"] == "l1"
    assert res["demoted"] is True
    assert res["certs_expired"] == 1
    assert capability.get_profile(db_session, cid, skill).verified_level == "l1"
    db_session.refresh(cap)
    assert cap.status == "expired"
    # 治理任务生成（compliance / stage=capability_reexam）
    assert res["task_id"]
    assert db_session.get(GovernanceTask, res["task_id"]).type == "compliance"
    # 审计留痕
    assert db_session.query(AuditLog).filter_by(
        action="capability.recheck_downgrade").count() >= 1


def test_request_reexam_idempotent_at_floor(db_session):
    cid, skill = _new_cid(), "tts"
    capability.set_verified_level(db_session, cid, skill, "unverified")
    db_session.commit()
    res = cr.request_capability_reexam(db_session, cid, skill,
                                       reason="到期", source="expiry")
    db_session.commit()
    assert res["demoted"] is False
    assert res["to_level"] == "unverified"
    assert res["certs_expired"] == 0  # 无有效证书


def test_request_reexam_arbitration_source_seam(db_session):
    """验证 3.5 复用接缝：source=arbitration 走同一原语（本批次不接入 submit_verdict）。"""
    cid, skill = _new_cid(), "arbitration"
    capability.set_verified_level(db_session, cid, skill, "l3")
    db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level="l3",
                                    status="valid", issued_at=datetime.utcnow(),
                                    expires_at=datetime.utcnow() + timedelta(days=100)))
    db_session.commit()
    res = cr.request_capability_reexam(db_session, cid, skill,
                                       reason="仲裁败诉", source="arbitration", severity=2)
    db_session.commit()
    assert res["source"] == "arbitration"
    assert res["from_level"] == "l3" and res["to_level"] == "l2"
    assert res["certs_expired"] == 1


# ==================== 4. 日级汇总 ====================

def test_run_capability_recheck_processes(db_session):
    base = _new_cid()
    for i, lvl in enumerate(["l2", "l1"]):
        cid, skill = base + i, f"skill_{i}"
        capability.set_verified_level(db_session, cid, skill, lvl)
        db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level=lvl,
                                        status="valid",
                                        issued_at=datetime.utcnow() - timedelta(days=400),
                                        expires_at=datetime.utcnow() - timedelta(days=1)))
    db_session.commit()

    summary = cr.run_capability_recheck(db_session)
    db_session.commit()
    assert summary["scanned"] >= 2
    assert summary["processed"] >= 2
    assert len(summary["tasks"]) >= 2
    # 复核后无残留 valid 过期证书
    assert cr.scan_expired_certificates(db_session) is not None
    remaining = [c for c in cr.scan_expired_certificates(db_session)
                 if c[0] >= base and c[0] < base + 2]
    assert remaining == []


def test_daily_job_returns_zero_and_no_raise(db_session):
    cid, skill = _new_cid(), "coding"
    db_session.add(SkillCertificate(citizen_id=cid, skill=skill, level="l2",
                                    status="valid",
                                    issued_at=datetime.utcnow() - timedelta(days=400),
                                    expires_at=datetime.utcnow() - timedelta(days=1)))
    db_session.commit()
    rc = cr.capability_recheck_daily_job(db_session)
    db_session.commit()
    assert rc == 0


# ==================== 5. scheduler 注册 ====================

def test_registered_as_daily_job():
    from app import scheduler
    assert any(jt == "capability_recheck" for jt, _ in scheduler._EXTRA_DAILY_JOBS)
