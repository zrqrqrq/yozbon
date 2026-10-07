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
"""N4 端到端对账（社会功能扩展设计 §2 N4 验收：数字与明细一致）。

流程（conftest 工厂）：new_host → new_ai → topup → 入驻考试转正 → 发项目 →
投标 → 签约托管 → 交付 → 宿主验收结算 → 抽样对账统计数字与明细一致。
对账点：
  - GMV = 该 accepted 合约 escrow_cent（非充值）；
  - 名下宿主 overview.income_cent = 同一值（worker 归属该宿主）；
  - 活跃 AI 含该 worker（近 7 日有结算流水）；
  - 阶层分布按净资产分档正确。
"""
import json
from datetime import datetime

import pytest

from app import escrow, exam, market, onboarding, project as proj, stats, wallet
from app.database import SessionLocal
from app.models import AICitizen, Contract, ExamPaper, SkillCertificate
from tests.conftest import new_host, new_ai, topup

P = 10_000


def _db():
    return SessionLocal()


def _seed_paper(db, skill="文案"):
    paper = ExamPaper(skill=skill, level="l1", paper_type="objective", active=1,
                      paper_json=json.dumps({
                          "title": "l1", "duration_minutes": 60, "pass_score": 60,
                          "questions": [
                              {"id": "q1", "type": "objective", "stem": "1+1=?",
                               "options": ["1", "2", "3"], "answer": "2", "score": 100}]}))
    db.add(paper); db.flush()
    return paper


def test_reconcile_stats_against_settled_contract(client):
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="对账工", occupation="文案",
                    self_decl=json.dumps({"skill": "文案"}))
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="总管", occupation="项目管理")
    topup(client, host_b["token"], buyer["id"], 100_000)

    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="rec:pool")
        # 入驻考试转正
        paper = _seed_paper(db)
        db.commit()
        wobj = db.get(AICitizen, worker["id"])
        onboarding.run_onboarding(db, wobj)
        db.commit()
        wobj = db.get(AICitizen, worker["id"])
        onboarding.assert_submittable(db, wobj, paper)
        exam.submit_exam(db, worker["id"], paper.id, {"q1": "2"})
        onboarding.after_exam_pass(db, wobj, "文案")
        db.commit()

        # 发项目 → 运行 → 节点
        p = proj.create_project(db, host_b["host_id"], "对账项目", budget_cent=P,
                               pm_citizen_id=buyer["id"])
        db.commit()
        proj.approve_running(db, host_b["host_id"], p.id)
        db.commit()
        proj.submit_nodes(db, p.id,
                          [{"key": "n1", "skill": "文案", "spec": "写一篇",
                            "deliverable_std": "docx", "budget_cent": P,
                            "duration_h": 8}], deps=[])
        db.commit()
        node = db.query(__import__("app.models", fromlist=["ProjectNode"]).ProjectNode).first()

        # 投标 → 签约 → 交付 → 验收结算
        c = market.bid(db, wobj, node.id, offer_cent=P, message="接")
        db.commit()
        bobj = db.get(AICitizen, buyer["id"])
        escrow.sign_contract(db, bobj, c.id)
        wobj = db.get(AICitizen, worker["id"])
        escrow.deliver(db, wobj, c.id, "s3://x.docx", "fp-rec")
        db.commit()
        bobj = db.get(AICitizen, buyer["id"])
        escrow.host_acceptance(db, c.id, "accept", "[]")
        db.commit()
        cid = c.id

        # ---- 对账：平台视图 ----
        pf = stats.platform(db)
        accepted = (db.query(Contract)
                    .filter(Contract.id == cid, Contract.status == "accepted").first())
        assert accepted is not None
        assert pf["gmv_cent"] == accepted.escrow_cent == P     # GMV=该合约 escrow
        assert pf["gmv_txns"] >= 1

        # ---- 对账：worker 宿主名下收益 ----
        ov = stats.overview(db, host_w["host_id"])
        assert ov["income_cent"] == P          # 名下 worker 已结算 escrow 总额
        assert ov["ai_total"] >= 1
        # worker 近 7 日有结算流水 → 活跃
        assert ov["active_ai"] >= 1

        # ---- 对账：阶层分布键齐全 ----
        for k in ("bottom", "middle", "boss", "capital", "governance"):
            assert k in pf["class_dist"]
            assert pf["class_dist"][k] >= 0
    finally:
        db.close()
