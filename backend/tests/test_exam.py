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
"""A 线测试：考试引擎（判卷/发证/复考降级/防作弊/限时）。

覆盖：
- 考试通过 → certificate valid + verified_level 提升
- 规则 12（test_rule_12_retake_downgrade_no_retroactive）：复考降级 → 证书 downgraded，
  已发高阶证书历史（行）保留、不追溯；已履行合约（B 线）本模块不动
- 防作弊：同卷雷同 ≥0.9 判 fail；超时交卷拒绝（408）
- 主观题关键词判分（MVP 规则版占位）
"""
import json
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import (AICitizen, Contract, OnboardingApplication,
                        SkillCertificate)


def _ai_header(api_key: str) -> dict:
    return {"X-AI-Key": api_key}


def _make_paper(skill: str = "text", level: str = "l1",
                pass_score: int = 60, duration: int = 30,
                obj_score: int = 30, sub_score: int = 40) -> int:
    """2 客观(各 obj_score) + 1 主观(sub_score)，满分 100。客观标准答案均为 A。"""
    from app.models import ExamPaper
    questions = [
        {"id": "o1", "type": "objective", "stem": "q1?", "options": ["A", "B"],
         "answer": "A", "score": obj_score},
        {"id": "o2", "type": "objective", "stem": "q2?", "options": ["A", "B"],
         "answer": "A", "score": obj_score},
        {"id": "s1", "type": "subjective", "stem": "描述", "keywords": ["正确", "准确"],
         "score": sub_score},
    ]
    pj = json.dumps({"title": "t", "duration_minutes": duration,
                     "pass_score": pass_score, "questions": questions},
                    ensure_ascii=False)
    s = SessionLocal()
    p = ExamPaper(skill=skill, level=level, paper_json=pj, active=1)
    s.add(p)
    s.commit()
    s.close()
    return p.id


def _skill_ai(client, host, skill: str) -> dict:
    from conftest import new_ai
    return new_ai(client, host["token"], name=f"AI-{skill}",
                  self_decl=json.dumps({"skill": skill}, ensure_ascii=False),
                  occupation=skill, mode="api")


def _dispatch(client, ai) -> int:
    """调 /onboard 派卷，返回 paper_id。"""
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "exam", r.json()
    return r.json()["paper_id"]


GOOD = {"o1": "A", "o2": "A", "s1": "正确 准确"}
BAD = {"o1": "B", "o2": "B", "s1": "无关内容"}


# ---------------- 考试发证 ----------------

def test_exam_pass_issues_certificate_and_level_up(client, host):
    """通过 → certificate valid(l1) + verified_level 提升到 l1。"""
    pid = _make_paper(skill="text")
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)

    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": GOOD}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "pass"
    assert res["objective_score"] == 60
    assert res["subjective_score"] == 40
    assert res["total"] == 100
    assert res["anti_cheat"] == ""
    assert res["certificate"]["level"] == "l1"

    # 库内：一张 valid l1 证 + 档案 verified=l1
    s = SessionLocal()
    cert = (s.query(SkillCertificate)
              .filter(SkillCertificate.citizen_id == ai["id"]).first())
    assert cert.status == "valid" and cert.level == "l1"
    from app.models import CapabilityProfile
    prof = (s.query(CapabilityProfile)
              .filter(CapabilityProfile.citizen_id == ai["id"]).first())
    assert prof.verified_level == "l1"
    assert prof.benchmark_score == 100
    assert prof.credibility > 0
    s.close()


# ---------------- 规则 12：复考降级不追溯 ----------------

def test_rule_12_retake_downgrade_no_retroactive(client, host):
    """规则 12：先持 l2 证，复考成绩更低（未过）→ 证书 downgraded。

    断言：
    - 旧 l2 证状态变为 downgraded，但【行仍在】（历史不删）；
    - 能力档案 verified_level 降级（只影响新单评级）；
    - 已履行合约不受影响——本模块从不写 contracts，contracts 表保持空。
    """
    pid = _make_paper(skill="text", level="l2")
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)

    # 第一次：通过 l2 → 持有 l2 valid 证
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": GOOD}, headers=_ai_header(ai["api_key"]))
    assert r.json()["status"] == "pass"
    s = SessionLocal()
    certs = (s.query(SkillCertificate)
               .filter(SkillCertificate.citizen_id == ai["id"]).all())
    assert len(certs) == 1 and certs[0].level == "l2" and certs[0].status == "valid"
    s.close()

    # 复考：再次派卷（刷新限时），交卷全错 → 更低成绩
    _dispatch(client, ai)
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": BAD}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "fail"

    # 旧 l2 证 downgraded，但行保留（历史不追溯删除）
    s = SessionLocal()
    certs = (s.query(SkillCertificate)
               .filter(SkillCertificate.citizen_id == ai["id"])
               .order_by(SkillCertificate.id.asc()).all())
    assert len(certs) == 1, "复考降级不得删除历史证书行"
    assert certs[0].level == "l2"
    assert certs[0].status == "downgraded"

    # 能力档案降级（只影响新单评级）
    from app.models import CapabilityProfile
    prof = (s.query(CapabilityProfile)
              .filter(CapabilityProfile.citizen_id == ai["id"]).first())
    assert prof.verified_level == "unverified"

    # 已履行合约不受影响：A 线从不写 contracts，表内无该 AI 合约
    n_contracts = s.query(Contract).filter(Contract.worker_id == ai["id"]).count()
    assert n_contracts == 0
    s.close()


# ---------------- 防作弊 ----------------

def test_anti_cheat_collusion_marked_fail(client, host):
    """同卷两份答案相似度 ≥0.9 → 第二份 anti_cheat 标记并判 fail。"""
    pid = _make_paper(skill="text")
    ai1 = _skill_ai(client, host, "text")
    ai2 = _skill_ai(client, host, "text")
    _dispatch(client, ai1)

    # ai1 正常交卷（正确答案）
    r1 = client.post(f"/api/ai/exam/{pid}/submit",
                     json={"answers": GOOD}, headers=_ai_header(ai1["api_key"]))
    assert r1.json()["status"] == "pass"

    # ai2 派发同一卷，交卷与 ai1 完全相同 → 雷同判 fail
    _dispatch(client, ai2)
    r2 = client.post(f"/api/ai/exam/{pid}/submit",
                     json={"answers": GOOD}, headers=_ai_header(ai2["api_key"]))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "fail"
    assert body["anti_cheat"] == "collusion"
    assert body["similarity"] >= 0.9
    # 雷同 → 不发证
    assert body["certificate"] == {}


def test_exam_timeout_rejected(client, host):
    """限时交卷：开考时刻回拨到 31 分钟前（限时 30）→ 408 拒绝。"""
    pid = _make_paper(skill="text", duration=30)
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)

    # 回拨开考时刻到 31 分钟前
    s = SessionLocal()
    app = (s.query(OnboardingApplication)
             .filter(OnboardingApplication.host_id == host["host_id"])
             .order_by(OnboardingApplication.id.desc()).first())
    meta = json.loads(app.error)
    meta["exam_started_at"] = (datetime.utcnow() - timedelta(minutes=31)).isoformat()
    app.error = json.dumps(meta)
    s.commit()
    s.close()

    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": GOOD}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 408, r.text


def test_submit_without_dispatch_rejected(client, host):
    """未派卷直接交卷 → 400。"""
    pid = _make_paper(skill="text")
    ai = _skill_ai(client, host, "text")
    # 不调 onboard 直接交卷
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": GOOD}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 400, r.text


# ---------------- 主观题关键词判分（MVP 占位） ----------------

def test_subjective_keyword_scoring(client, host):
    """主观题 MVP 规则版：命中关键词比例给分（可外包给评审 AI 的占位）。"""
    pid = _make_paper(skill="text")
    ai = _skill_ai(client, host, "text")
    _dispatch(client, ai)

    # 只命中 1/2 关键词：主观得一半（20），客观全对 60 → total 80
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"o1": "A", "o2": "A", "s1": "正确"}},
                    headers=_ai_header(ai["api_key"]))
    res = r.json()
    assert res["objective_score"] == 60
    assert res["subjective_score"] == 20
    assert res["total"] == 80
    assert res["status"] == "pass"
