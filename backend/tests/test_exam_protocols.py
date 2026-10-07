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
"""A 线扩展：三类考试路由（objective/subjective/decision）+ 决策四模块 + 能力申报 +
见习绩效转正 + LLM echo 全链（能力评估 §3.6/§3.7）。
"""
import json
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import (AICitizen, Contract, ExamPaper, OnboardingApplication,
                        SkillCertificate)
from app import capability, exam, onboarding


def _h(key):
    return {"X-AI-Key": key}


def _skill_ai(client, host, skill):
    from conftest import new_ai
    return new_ai(client, host["token"], name=f"AI-{skill}",
                  self_decl=json.dumps({"skill": skill}, ensure_ascii=False),
                  occupation=skill, mode="api")


def _dispatch(client, ai):
    r = client.post("/api/ai/onboard", json={}, headers=_h(ai["api_key"]))
    assert r.status_code == 200, r.text
    return r.json()


def _make_paper(skill, ptype="objective", level="l1", pj=None, pass_score=60):
    pj = pj or {"title": "t", "duration_minutes": 30, "pass_score": pass_score,
                "questions": [{"id": "o1", "type": "objective", "stem": "q?",
                               "options": ["A", "B"], "answer": "A", "score": 100}]}
    s = SessionLocal()
    p = ExamPaper(skill=skill, level=level, paper_type=ptype,
                  paper_json=json.dumps(pj, ensure_ascii=False), active=1)
    s.add(p)
    s.commit()
    s.close()
    return p.id


# ---------------- brier_score 数值断言 ----------------

def test_brier_score_values():
    # 全中（概率与结果完全一致）→ 0
    assert exam.brier_score([1.0, 0.0], [1, 0]) == 0.0
    # 全错（概率与结果完全相反）→ 1
    assert exam.brier_score([1.0, 0.0], [0, 1]) == 1.0
    # 一半对一半错（第一题对、第二题错）→ 0.5
    assert exam.brier_score([1.0, 0.0], [1, 1]) == 0.5
    # 空输入 → 1（最保守）
    assert exam.brier_score([], []) == 1.0


# ---------------- 决策沙盘确定性收益 ----------------

def test_sandbox_deterministic_revenue():
    scen = {"id": "s1", "payout": {"保守": 50, "激进": 90}}
    assert exam.run_sandbox(scen, {"s1": "激进"}) == 90.0
    assert exam.run_sandbox(scen, {"s1": "保守"}) == 50.0
    assert exam.run_sandbox(scen, {"s1": "不存在"}) == 0.0


def test_decision_paper_routing_and_modules(client, host):
    """decision 卷：四模块加权 + 对抗一票否决；通过 → 发证。"""
    pj = {
        "pass_score": 50,
        "scoring_meta": {"calibration_weight": 0.30, "sandbox_weight": 0.40,
                          "audit_weight": 0.30},
        "calibration": {"items": [{"prob": 0.9, "outcome": 1},
                                  {"prob": 0.1, "outcome": 0}]},
        "sandbox": {"scenarios": [{"id": "s1", "payout": {"保守": 50, "激进": 90}}]},
        "audit": {"criteria": ["信息收集", "风险识别"]},
        "adversarial": {"traps": [{"id": "t1", "bad_answer": "全仓激进"}]},
    }
    pid = _make_paper("decision_making", ptype="decision", pj=pj)
    ai = _skill_ai(client, host, "decision_making")
    body = _dispatch(client, ai)
    assert body["stage"] == "exam"

    answers = {
        "calibration": {"0": 0.9, "1": 0.1},
        "sandbox": {"s1": "激进"},
        "audit_text": "我收集了信息并识别了风险，做了多方案权衡",
        "adversarial": {"t1": "稳健对冲"},
    }
    r = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": answers},
                    headers=_h(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["paper_type"] == "decision"
    assert res["vetoed"] is False
    assert res["modules"]["calibration"] == 99.0     # brier=0.01 → 99
    assert res["modules"]["sandbox"] == 90.0
    assert res["passed"] is True
    # 转正 + 发证
    r = client.get("/api/ai/me", headers=_h(ai["api_key"]))
    assert r.json()["status"] == "active"


def test_decision_adversarial_one_vote_veto(client, host):
    """对抗陷阱被踩中 → 整卷 fail（一票否决），不发证。"""
    pj = {
        "pass_score": 50,
        "scoring_meta": {"calibration_weight": 0.3, "sandbox_weight": 0.4,
                         "audit_weight": 0.3},
        "calibration": {"items": [{"prob": 0.9, "outcome": 1}]},
        "sandbox": {"scenarios": [{"id": "s1", "payout": {"x": 90}}]},
        "audit": {"criteria": ["风险识别"]},
        "adversarial": {"traps": [{"id": "t1", "bad_answer": "全仓激进"}]},
    }
    pid = _make_paper("decision_making", ptype="decision", pj=pj)
    ai = _skill_ai(client, host, "decision_making")
    _dispatch(client, ai)
    answers = {"calibration": {"0": 0.9}, "sandbox": {"s1": "x"},
               "audit_text": "ok 信息收集", "adversarial": {"t1": "全仓激进"}}
    r = client.post(f"/api/ai/exam/{pid}/submit", json={"answers": answers},
                    headers=_h(ai["api_key"]))
    res = r.json()
    assert res["status"] == "fail"
    assert res["vetoed"] is True
    assert res["certificate"] == {}


# ---------------- 主观参考答案卷路由 ----------------

def test_subjective_paper_routing(client, host):
    """subjective 卷：自动比对 + LLM 盲评补分。"""
    pj = {"pass_score": 50, "reference": "AIjuhe 是劳动力市场",
          "questions": [{"id": "s1", "type": "subjective", "stem": "?",
                         "keywords": ["劳动力"], "score": 60}]}
    pid = _make_paper("summarization", ptype="subjective", pj=pj)
    ai = _skill_ai(client, host, "summarization")
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"s1": "AIjuhe 是劳动力市场平台"}},
                    headers=_h(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["paper_type"] == "subjective"
    assert res["subjective_score"] > 0


# ---------------- 能力申报状态机全路径 ----------------

def test_capability_proposal_state_machine(client, host):
    """四要素申报 pending → 初审/终审 approved → 入目录；缺字段拒绝；rejected 留痕。"""
    ai = _skill_ai(client, host, "warp_drive")  # 不在目录
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])

    # 缺字段 → 拒绝
    try:
        capability.submit_capability_proposal(s, c.id, "warp_drive", "", "{}", "x", "y")
        assert False, "应抛 ProposalError"
    except capability.ProposalError as e:
        assert "definition" in str(e)

    # 完整四要素 → pending
    p = capability.submit_capability_proposal(
        s, c.id, "warp_drive",
        definition="超光速调度", io_schema='{"in":"task"}',
        acceptance_metrics="收益>0", eval_protocol="decision",
        market_demand="{}")
    assert p.declared == 0
    assert not capability.is_in_catalog(s, "warp_drive")

    # 重复 pending → 拒绝
    try:
        capability.submit_capability_proposal(s, c.id, "warp_drive", "d", "i", "a", "e")
        assert False
    except capability.ProposalError:
        pass

    # 治理初审 + 平台终审 → approved 入目录
    capability.review_capability_proposal(s, c.id, "warp_drive", approve=True,
                                          note="reviewed")
    assert capability.is_in_catalog(s, "warp_drive")
    s.close()


def test_proposal_rejected_keeps_row(client, host):
    ai = _skill_ai(client, host, "cold_fusion")
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    capability.submit_capability_proposal(s, c.id, "cold_fusion", "d", "i", "a", "e")
    capability.review_capability_proposal(s, c.id, "cold_fusion", approve=False,
                                          note="无需求")
    p = capability.get_profile(s, c.id, "cold_fusion")
    assert "rejected" in p.profile_json
    assert not capability.is_in_catalog(s, "cold_fusion")
    s.close()


# ---------------- 见习绩效转正（§3.6 兜底） ----------------

def _seed_accepted_contracts(citizen_id, n=20, bad=0):
    s = SessionLocal()
    for i in range(n):
        s.add(Contract(worker_id=citizen_id, buyer_id=1, status="accepted"))
    for i in range(bad):
        s.add(Contract(worker_id=citizen_id, buyer_id=1, status="breached"))
    s.commit()
    s.close()


def test_apprentice_performance_promotion(client, host):
    """见习绩效达标（20 accepted，0 违约）→ 治理复核 → 转正 active + 发 l1 证。"""
    ai = _skill_ai(client, host, "data_analysis")
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    c.created_at = datetime.utcnow() - timedelta(days=31)  # 同时满 30 天
    s.commit()
    s.close()

    _seed_accepted_contracts(ai["id"], n=20, bad=0)

    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    events = onboarding.check_apprentice_expiry(s)
    s.commit()
    s.refresh(c)
    # 规则冲突（视角4）：绩效达标 + 30 天同时成立 → 转正优先，不冻结
    assert c.status == "active"
    assert any(e["event"] == "promote_by_performance" for e in events)
    cert = s.query(SkillCertificate).filter(SkillCertificate.citizen_id == ai["id"]).first()
    assert cert is not None and cert.level == "l1" and cert.status == "valid"
    s.close()


# ---------------- LLM echo 全链 ----------------

def test_llm_echo_channel():
    # 默认 LLM_PROVIDER=echo → 确定性返回，不发网络
    out = exam.llm_complete("hello")
    assert out.startswith("[echo]")


# ---------------- 未知 skill → 申报流（视角1） ----------------

def test_unknown_skill_enters_proposal_flow(client, host):
    ai = _skill_ai(client, host, "quantum_forecasting")  # 不在目录、无卷
    body = _dispatch(client, ai)
    assert body["stage"] == "proposal", body
