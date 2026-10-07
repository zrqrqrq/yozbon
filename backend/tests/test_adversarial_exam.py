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
"""对抗测试：按《边界情形登记册》§一 10 视角轰考试/入驻/申报实现（测试名标注视角号）。

只验证"设计外行为不崩"，不重复正向用例。
"""
import json
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import AICitizen, Contract, ExamPaper, SkillCertificate
from app import capability, exam, onboarding


def _h(key):
    return {"X-AI-Key": key}


def _skill_ai(client, host, skill):
    from conftest import new_ai
    return new_ai(client, host["token"], name=f"AI-{skill}",
                  self_decl=json.dumps({"skill": skill}, ensure_ascii=False),
                  occupation=skill, mode="api")


def _obj_paper(skill, pass_score=60):
    s = SessionLocal()
    pj = json.dumps({"title": "t", "duration_minutes": 30, "pass_score": pass_score,
                     "questions": [
                         {"id": "o1", "type": "objective", "stem": "?",
                          "options": ["A", "B"], "answer": "A", "score": 50},
                         {"id": "o2", "type": "objective", "stem": "?",
                          "options": ["A", "B"], "answer": "A", "score": 50}]},
                    ensure_ascii=False)
    p = ExamPaper(skill=skill, level="l1", paper_type="objective",
                  paper_json=pj, active=1)
    s.add(p); s.commit(); s.close()
    return p.id


def _dispatch(client, ai):
    r = client.post("/api/ai/onboard", json={}, headers=_h(ai["api_key"]))
    return r.json()


GOOD = {"o1": "A", "o2": "A"}


# ---------------- 视角 3：女巫刷分（批量同答案 → 全部 anti_cheat fail） ----------------

def test_perspective_03_witch_batch_same_answers(client, host):
    pid = _obj_paper("text")
    ai1 = _skill_ai(client, host, "text")
    ai2 = _skill_ai(client, host, "text")
    ai3 = _skill_ai(client, host, "text")
    for ai in (ai1, ai2, ai3):
        _dispatch(client, ai)

    r1 = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai1["api_key"]))
    assert r1.json()["status"] == "pass"

    # 后两者交卷与第一人完全相同 → 雷同 fail
    r2 = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai2["api_key"]))
    r3 = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai3["api_key"]))
    assert r2.json()["anti_cheat"] == "collusion"
    assert r3.json()["anti_cheat"] == "collusion"


def test_perspective_03_over_quota_proposal_rejected(client, host):
    """超量申报刷目录 → 限流拒绝（pending 封顶 3）。"""
    ai = _skill_ai(client, host, "x1")
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    for i in range(3):
        capability.submit_capability_proposal(s, c.id, f"new_skill_{i}",
                                              "d", "i", "a", "e")
    # 第 4 个 → 限流
    with pytest.raises(capability.ProposalError):
        capability.submit_capability_proposal(s, c.id, "new_skill_3",
                                               "d", "i", "a", "e")
    s.close()


# ---------------- 视角 5：边界数值 ----------------

def test_perspective_05_boundary_values():
    # Brier 全中=0、全错=1
    assert exam.brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert exam.brier_score([1.0, 0.0], [0, 1]) == 1.0


def test_perspective_05_empty_answers_no_crash(client, host):
    pid = _obj_paper("text")
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": {}},
                    headers=_h(ai["api_key"]))
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["status"] == "fail"


def test_perspective_05_decision_zero_module_boundary(client, host):
    """decision 卷 audit_text 为空 → audit 模块 0 分，系统不崩。"""
    pj = {"pass_score": 0,
          "scoring_meta": {"calibration_weight": 0.3, "sandbox_weight": 0.4,
                           "audit_weight": 0.3},
          "calibration": {"items": []},
          "sandbox": {"scenarios": []},
          "audit": {"criteria": ["x"]},
          "adversarial": {"traps": []}}
    s = SessionLocal()
    p = ExamPaper(skill="decision_making", level="l1", paper_type="decision",
                  paper_json=json.dumps(pj, ensure_ascii=False), active=1)
    s.add(p); s.commit(); s.close()
    pid = p.id
    ai = _skill_ai(client, host, "decision_making")
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"audit_text": ""}}, headers=_h(ai["api_key"]))
    assert r.status_code == 200
    assert r.json()["modules"]["audit"] == 0.0


# ---------------- 视角 4：规则冲突 ----------------

def test_perspective_04_performance_overrides_expiry(client, host):
    """绩效达标 + 30 天到期同时成立 → 转正优先（不冻结）。"""
    ai = _skill_ai(client, host, "data_analysis")
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    c.created_at = datetime.utcnow() - timedelta(days=31)
    s.commit()
    for _ in range(20):
        s.add(Contract(worker_id=ai["id"], buyer_id=1, status="accepted"))
    s.commit(); s.close()

    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    onboarding.check_apprentice_expiry(s)
    s.commit(); s.refresh(c)
    assert c.status == "active"
    s.close()


def test_perspective_04_retake_downgrade_then_new_valid_cert(client, host):
    """复考降级(downgraded)之后再通过 → 新证 valid，两者并存不冲突。"""
    pid = _obj_paper("text", pass_score=60)
    # 先持 l2：直接插一张 l2 卷并通过
    s = SessionLocal()
    from app.models import ExamPaper as EP
    pj2 = json.dumps({"title": "l2", "duration_minutes": 30, "pass_score": 60,
                      "questions": [{"id": "o1", "type": "objective", "stem": "?",
                                     "options": ["A", "B"], "answer": "A", "score": 100}]},
                     ensure_ascii=False)
    p2 = EP(skill="text", level="l2", paper_type="objective", paper_json=pj2, active=1)
    s.add(p2); s.commit(); s.close()
    pid2 = p2.id

    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)
    # 通过 l2
    client.post(f"/api/ai/exam/{pid2}/submit", json={"answers": {"o1": "A"}},
                headers=_h(ai["api_key"]))
    s = SessionLocal()
    assert s.query(SkillCertificate).filter_by(citizen_id=ai["id"], level="l2",
                                               status="valid").count() == 1
    s.close()

    # 复考失败 → l2 downgraded
    _dispatch(client, ai)
    client.post(f"/api/ai/exam/{pid2}/submit", json={"answers": {"o1": "B"}},
                headers=_h(ai["api_key"]))
    s = SessionLocal()
    assert s.query(SkillCertificate).filter_by(citizen_id=ai["id"], level="l2",
                                               status="downgraded").count() == 1
    s.close()

    # 再通过 l1 卷 → 新 valid 证与 downgraded 历史并存
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai["api_key"]))
    assert r.json()["status"] == "pass"
    s = SessionLocal()
    assert s.query(SkillCertificate).filter_by(citizen_id=ai["id"], status="valid").count() >= 1
    s.close()


# ---------------- 视角 3：恶意主体 ----------------

def test_perspective_03_fake_paper_id_rejected(client, host):
    ai = _skill_ai(client, host, "text")
    r = client.post("/api/ai/exam/99999/submit", json={"answers": GOOD},
                    headers=_h(ai["api_key"]))
    assert r.status_code == 404


def test_perspective_03_duplicate_submit_idempotent(client, host):
    """重复交卷不崩、不重复发证炸库（第二次复考走复考逻辑）。"""
    pid = _obj_paper("text")
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)
    r1 = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai["api_key"]))
    assert r1.status_code == 200
    _dispatch(client, ai)
    r2 = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": GOOD},
                    headers=_h(ai["api_key"]))
    assert r2.status_code == 200  # 复考不崩


# ---------------- 视角 6：故障恢复（LLM 通道失败 → echo 兜底不崩） ----------------

def test_perspective_06_llm_openai_failure_echo_fallback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compat")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://127.0.0.1:1")  # 不可达
    out = exam.llm_complete("probe")
    assert out.startswith("[echo]")  # 网络失败 echo 兜底，不抛


def test_perspective_06_llm_echo_still_works():
    out = exam.llm_complete("x")
    assert "[echo]" in out


# ---------------- 视角 1：新物种（未知 paper_type 拒绝但不崩；未知 skill 走申报） ----------------

def test_perspective_01_unknown_paper_type_rejected_no_crash(client, host):
    s = SessionLocal()
    pj = json.dumps({"title": "weird", "questions": []})
    p = ExamPaper(skill="text", level="l1", paper_type="telepathy",
                  paper_json=pj, active=1)
    s.add(p); s.commit(); s.close()
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{p.id}/submit", json={"answers": {}},
                    headers=_h(ai["api_key"]))
    # 服务端报错但不崩（400，未抛 500）
    assert r.status_code == 400


def test_perspective_01_unknown_skill_proposal_flow(client, host):
    ai = _skill_ai(client, host, "telekinesis")
    body = _dispatch(client, ai)
    assert body["stage"] == "proposal"
