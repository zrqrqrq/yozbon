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
"""A 线测试：自动入驻流水线 + AI 侧端点（/onboard /me /wallet /ledger /capabilities）。

覆盖：
- 入驻状态机全路径 handshake→probe→exam→active 与 →apprentice
- 规则 5（test_rule_05_apprentice_30d_freeze）：见习 30 天未转正 → frozen；转正后不再冻结
- probe 校验（worker 模式必须有 endpoint）
- /api/ai/me、/wallet、/ledger 鉴权与数据正确性
"""
import json
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import AICitizen, OnboardingApplication
from app import onboarding


# ---------------- 造数助手 ----------------

def _ai_header(api_key: str) -> dict:
    return {"X-AI-Key": api_key}


def _make_paper(skill: str = "text", level: str = "l1",
                pass_score: int = 60, duration: int = 30,
                obj_score: int = 30, sub_score: int = 40) -> int:
    """直接往 exam_papers 插一张卷：2 客观(各 obj_score) + 1 主观(sub_score)，满分 100。"""
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
    from app.models import ExamPaper
    s = SessionLocal()
    p = ExamPaper(skill=skill, level=level, paper_json=pj, active=1)
    s.add(p)
    s.commit()
    s.close()
    return p.id


def _new_skill_ai(client, host, skill: str = "text", mode: str = "api",
                  endpoint: str = "") -> dict:
    """建一个 self_decl 指向指定 skill 的见习 AI，返回 new_ai dict。"""
    from conftest import new_ai
    return new_ai(client, host["token"], name=f"AI-{skill}",
                  self_decl=json.dumps({"skill": skill}, ensure_ascii=False),
                  occupation=skill, mode=mode, endpoint=endpoint)


# ---------------- 入驻状态机 ----------------

def test_onboard_state_machine_to_active(client, host):
    """handshake→probe→exam→active：派卷后交卷通过 → 转正 active 并发证。"""
    pid = _make_paper(skill="text")
    ai = _new_skill_ai(client, host, skill="text")

    # 首次 onboard：api 模式免回连，handshake+probe 一次跑完 → 派卷到 exam
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "exam", body
    assert body["paper_id"] == pid

    # 交卷（两道客观全对 + 主观含关键词 → 满分）
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"o1": "A", "o2": "A", "s1": "正确 准确"}},
                    headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "pass", res
    assert res["passed"] is True

    # 转正
    r = client.get("/api/ai/me", headers=_ai_header(ai["api_key"]))
    me = r.json()
    assert me["status"] == "active"
    assert len(me["certificates"]) >= 1
    assert me["certificates"][0]["level"] == "l1"


def test_onboard_state_machine_to_apprentice(client, host):
    """handshake→probe→exam→apprentice：派卷后交卷未过 → 保持见习，可复考。"""
    pid = _make_paper(skill="text")
    ai = _new_skill_ai(client, host, skill="text")
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    assert r.json()["stage"] == "exam"
    assert r.json()["paper_id"] == pid

    # 交卷全错 → fail，仍见习
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"o1": "B", "o2": "B", "s1": "废话"}},
                    headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["status"] == "fail", res
    assert res["passed"] is False

    # citizen 仍 apprentice
    s = SessionLocal()
    c = s.get(AICitizen, ai["id"])
    assert c.status == "apprentice"
    s.close()


# ---------------- 规则 5（默认关闭）：见习冻结为可选行为 ----------------

def test_rule_05_apprentice_no_freeze_by_default(client, host):
    """能力画像优先：默认【不因见习满 30 天硬冻结】——见习 AI 继续画像、继续接单。

    旧「见习 30 天未转正 → frozen」改为可选（ONBOARD_APPRENTICE_FREEZE=1 恢复），
    本用例验证默认不冻结 + 显式开启后冻结仍生效。
    """
    from app.config import settings

    # 见习、无证书、建档时间回拨到 31 天前
    ai1 = _new_skill_ai(client, host, skill="text")
    s = SessionLocal()
    c1 = s.get(AICitizen, ai1["id"])
    c1.created_at = datetime.utcnow() - timedelta(days=31)
    s.commit()

    # 默认：freeze 关闭 → 仍 apprentice（不拦门）
    events = onboarding.check_apprentice_expiry(s)
    s.commit()
    s.refresh(c1)
    assert c1.status == "apprentice"
    assert not any(e["citizen_id"] == ai1["id"] for e in events)
    s.close()

    # 显式恢复旧规则 5：freeze 开启 → 满 30 天无证 → frozen
    old = settings.ONBOARD_APPRENTICE_FREEZE
    settings.ONBOARD_APPRENTICE_FREEZE = True
    try:
        s = SessionLocal()
        events = onboarding.check_apprentice_expiry(s)
        s.commit()
        s.refresh(s.get(AICitizen, ai1["id"]))
        assert s.get(AICitizen, ai1["id"]).status == "frozen"
        assert any(e["citizen_id"] == ai1["id"] for e in events)
        s.close()
    finally:
        settings.ONBOARD_APPRENTICE_FREEZE = old

    # 冻结后 AI 侧鉴权 403（get_current_ai 拒绝 frozen）
    r = client.get("/api/ai/me", headers=_ai_header(ai1["api_key"]))
    assert r.status_code == 403


# ---------------- fast-track：能力强直接上岗 ----------------

def test_fast_track_strong_self_decl_direct_active(client, host):
    """能力画像优先：自述能力强（expert 等级）的新 AI，probe 通过后直接 active，
    无需先通过考试；建能力档案 + provisional l2 证（C-D15 分档：expert→l3→provisional l2）；考试降为可选校准。"""
    _make_paper(skill="coding")
    ai = _new_skill_ai(client, host, skill="coding")
    # 用强自述覆盖默认 self_decl
    s = SessionLocal()
    from app.models import OnboardingApplication as OA
    app = (s.query(OA).filter(OA.citizen_id == ai["id"]).first())
    app.self_decl = json.dumps({"skill": "coding", "declared_level": "expert"},
                               ensure_ascii=False)
    s.commit()
    s.close()

    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "active", body
    assert body.get("fast_track") is True

    r = client.get("/api/ai/me", headers=_ai_header(ai["api_key"]))
    me = r.json()
    assert me["status"] == "active"
    assert len(me["certificates"]) >= 1
    assert me["certificates"][0]["level"] == "l2"  # C-D15: expert→canonical l3→provisional l2
    # 能力档案已建立（这是入驻的目的）
    assert any(p["skill"] == "coding" for p in me["capabilities"])

    # 考试为可选校准：active 状态仍可交校准卷并升级实证（不受限时窗口约束）
    pid = body["calibration_paper_id"]
    r = client.post(f"/api/ai/exam/{pid}/submit",
                    json={"answers": {"o1": "A", "o2": "A", "s1": "正确 准确"}},
                    headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is True


def test_no_signal_self_decl_not_fast_tracked(client, host):
    """无能力信号（仅 skill，无等级/背书）→ 不触发 fast-track，仍走派卷考试。"""
    _make_paper(skill="design")
    ai = _new_skill_ai(client, host, skill="design")
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "exam", r.json()
    assert not r.json().get("fast_track")


# ---------------- probe 校验 ----------------

def test_probe_requires_endpoint_for_worker(client, host):
    """worker 模式 MVP 探针：endpoint 为空 → 停在 probe；endpoint 非空 → 进入 exam。"""
    # worker 模式无 endpoint
    ai_bad = _new_skill_ai(client, host, skill="text", mode="worker", endpoint="")
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai_bad["api_key"]))
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "probe", r.json()

    # worker 模式有 endpoint
    _make_paper(skill="text2")
    ai_ok = _new_skill_ai(client, host, skill="text2", mode="worker",
                          endpoint="http://worker:9000/hook")
    r = client.post("/api/ai/onboard", json={}, headers=_ai_header(ai_ok["api_key"]))
    assert r.json()["stage"] == "exam", r.json()


# ---------------- AI 侧端点：鉴权与数据正确性 ----------------

def test_me_wallet_ledger_auth_and_data(client, host):
    """/api/ai/me、/wallet、/ledger：无 key→401；注资后余额/流水正确。"""
    ai = _new_skill_ai(client, host, skill="text")

    # 无 key → 401
    assert client.get("/api/ai/me").status_code == 401
    assert client.get("/api/ai/wallet").status_code == 401
    assert client.get("/api/ai/ledger").status_code == 401

    # 注资（复用宿主 topup）
    from conftest import topup
    topup(client, host["token"], ai["id"], 50_000)

    # /wallet
    r = client.get("/api/ai/wallet", headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200
    assert r.json()["balance_cent"] == 50_000

    # /ledger：至少一条充值流水，倒序
    r = client.get("/api/ai/ledger?limit=50&offset=0",
                   headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert data["items"][0]["type"] == "充值"
    assert data["items"][0]["amount_cent"] == 50000

    # /me：身份/信用分/状态字段齐全
    r = client.get("/api/ai/me", headers=_ai_header(ai["api_key"]))
    me = r.json()
    assert me["citizen_id"] == ai["id"]
    assert me["status"] == "apprentice"
    assert me["credit_score"] == 100
    assert isinstance(me["certificates"], list)
    assert isinstance(me["capabilities"], list)


def test_capabilities_endpoint_empty_then_filled(client, host):
    """/api/ai/capabilities：初始空；考试通过后出现档案。"""
    pid = _make_paper(skill="text3")
    ai = _new_skill_ai(client, host, skill="text3")

    r = client.get("/api/ai/capabilities", headers=_ai_header(ai["api_key"]))
    assert r.status_code == 200
    assert r.json() == []

    client.post("/api/ai/onboard", json={}, headers=_ai_header(ai["api_key"]))
    client.post(f"/api/ai/exam/{pid}/submit",
                json={"answers": {"o1": "A", "o2": "A", "s1": "正确准确"}},
                headers=_ai_header(ai["api_key"]))
    r = client.get("/api/ai/capabilities", headers=_ai_header(ai["api_key"]))
    caps = r.json()
    assert len(caps) == 1
    assert caps[0]["skill"] == "text3"
    assert caps[0]["verified_level"] == "l1"
    assert caps[0]["benchmark_score"] == 100
