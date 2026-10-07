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
"""C-14 回归：onboarding_applications 直链 citizen_id。

验证：
- 宿主建 AI 时，入驻申请单写入 citizen_id（不再是 0）。
- _find_application 通过 citizen_id 直查到正确申请单。
- 同宿主多个 AI：每个 AI 各自映射到自己的申请单（同序推断的脆弱场景）。
- 老数据（citizen_id=0）仍走同序 1:1 兜底。
"""
from app.database import SessionLocal
from app.models import AICitizen, OnboardingApplication
from app import onboarding


def test_create_ai_backfills_citizen_id(client, host):
    """建 AI 后申请单应直链 citizen_id。"""
    from conftest import new_ai
    ai = new_ai(client, host["token"], name="C14AI")
    cid = ai["id"]
    s = SessionLocal()
    try:
        app = (s.query(OnboardingApplication)
                 .filter(OnboardingApplication.host_id == host["host_id"])
                 .order_by(OnboardingApplication.id.desc()).first())
        assert app is not None
        assert app.citizen_id == cid, (app.citizen_id, cid)
    finally:
        s.close()


def test_find_application_direct_by_citizen_id(client, host):
    """_find_application 通过 citizen_id 命中正确申请单。"""
    from conftest import new_ai
    ai = new_ai(client, host["token"], name="C14Direct")
    cid = ai["id"]
    s = SessionLocal()
    try:
        citizen = s.get(AICitizen, cid)
        app = onboarding._find_application(s, citizen)
        assert app is not None
        assert app.citizen_id == cid
    finally:
        s.close()


def test_two_ais_map_to_own_application(client, host):
    """同宿主两个 AI 各自映射各自的申请单。"""
    from conftest import new_ai
    a1 = new_ai(client, host["token"], name="A1")
    a2 = new_ai(client, host["token"], name="A2")
    s = SessionLocal()
    try:
        c1 = s.get(AICitizen, a1["id"])
        c2 = s.get(AICitizen, a2["id"])
        app1 = onboarding._find_application(s, c1)
        app2 = onboarding._find_application(s, c2)
        assert app1.citizen_id == a1["id"]
        assert app2.citizen_id == a2["id"]
        assert app1.id != app2.id
    finally:
        s.close()


def test_legacy_zero_citizen_id_falls_back(client, host):
    """老数据（citizen_id=0）仍能通过同序 1:1 兜底定位。"""
    from conftest import new_ai
    ai = new_ai(client, host["token"], name="Legacy")
    cid = ai["id"]
    s = SessionLocal()
    try:
        # 模拟迁移前的老数据：把 citizen_id 清 0
        app = (s.query(OnboardingApplication)
                 .filter(OnboardingApplication.host_id == host["host_id"])
                 .order_by(OnboardingApplication.id.desc()).first())
        app.citizen_id = 0
        s.commit()
        citizen = s.get(AICitizen, cid)
        found = onboarding._find_application(s, citizen)
        assert found is not None
        assert found.id == app.id
    finally:
        s.close()
