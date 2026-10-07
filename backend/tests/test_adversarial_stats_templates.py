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
"""N3/N4 对抗测试（登记册 §一 10 类攻击视角自攻）。

逐条自攻结论（写进 docstring，纪律 §三「先攻后建」）：
  #1 新物种进场/维度注入：category、required_fields、metric/dimension 全部白名单封闭枚举，
     注入未知值（hack / evil_field / SQL 串）一律 400，不落库；
  #2 极端规模：列表/趋势分页钳制 page_size≤50、days∈[1,365]，空库返回空结构不崩；
  #3 恶意主体/刷模板：POST 写操作非 host JWT 且非 governance AI key → 403；
  #4 规则冲突：模板 upsert 按 (category,name_zh) 幂等，重复名不刷行只 version+1；
  #5 边界数值：days=0/-5/超大、budget_min>max → 400/422，不产生脏数据；
  #6 故障恢复/幂等：daily_snapshot 同日重复调用自查返回 0，不重复建行；
  #7 新供应商：不涉及（统计只读既有表，无外部通道）；
  #8 人为滥用：平台视图 /api/stats/platform 非管理员人类一律 403，AI 一律不可达（T5 红线）；
  #9 经济失衡：GMV 口径=accepted 合约 escrow（非充值），不把充值误计为成交；
  #10 法律合规：模板/统计端点无密钥回显，不触碰 .env。
"""
import pytest

from app import stats
from app.config import settings
from app.database import SessionLocal
from app.models import StatSnapshot
from tests.conftest import new_host, new_ai


def _admin_host(client):
    """注册平台运营管理员人类宿主（email == PLATFORM_HOST_EMAIL）。"""
    email = settings.PLATFORM_HOST_EMAIL
    r = client.post("/api/host/register", json={
        "email": email, "password": "admin123456", "nickname": "运营",
        "region": "CN", "seat_tier": "premium"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 维度注入 / 枚举白名单 ----------------

def test_category_injection_rejected(client):
    h = new_host(client)
    # 列表侧注入
    r = client.get("/api/templates?category=hack;DROP TABLE--")
    assert r.status_code == 400
    # 写入侧注入
    r2 = client.post("/api/templates",
                     json={"category": "hack", "name_zh": "x", "required_fields": []},
                     headers={"Authorization": f"Bearer {h['token']}"})
    assert r2.status_code == 400


def test_required_fields_injection_rejected(client):
    h = new_host(client)
    r = client.post("/api/templates",
                    json={"category": "promo", "name_zh": "x",
                          "required_fields": ["goal", "evil", "__import__('os')"]},
                    headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 400


# ---------------- days 边界 ----------------

@pytest.mark.parametrize("bad_days", [0, -1, -30, 99999, 1000])
def test_trends_days_bounds_rejected(client, bad_days):
    h = new_host(client)
    r = client.get(f"/api/stats/trends?days={bad_days}",
                   headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 422        # FastAPI Query ge/le 拒绝


def test_trends_valid_days_ok(client):
    h = new_host(client)
    r = client.get("/api/stats/trends?days=7",
                   headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    assert r.json()["items"] == []      # 空库不崩


# ---------------- 平台视图越权（管理类=管理员人类，AI 一律不可达） ----------------

def test_platform_view_requires_admin_human(client):
    # 无凭证 → 403
    assert client.get("/api/stats/platform").status_code == 403
    # 普通宿主 JWT → 403
    h = new_host(client)
    assert client.get("/api/stats/platform",
                     headers={"Authorization": f"Bearer {h['token']}"}).status_code == 403


def test_platform_view_ai_key_forbidden(client):
    h = new_host(client)
    ai = new_ai(client, h["token"], name="AI")
    # AI key 落到 host 解析 → typ 不是 host → 403
    r = client.get("/api/stats/platform", headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 403


def test_platform_view_admin_host_ok(client):
    admin = _admin_host(client)
    r = client.get("/api/stats/platform",
                   headers={"Authorization": f"Bearer {admin['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("gmv_cent", "gmv_txns", "active_ai", "tax_pool_cent",
              "money_supply_cent", "class_dist"):
        assert k in body


def test_overview_requires_host_token(client):
    # 无凭证 → 401/403（get_current_host 拦截）
    assert client.get("/api/stats/overview").status_code in (401, 403)
    h = new_host(client)
    r = client.get("/api/stats/overview",
                   headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200


# ---------------- 快照同日幂等 ----------------

def test_snapshot_same_day_idempotent(client):
    db = SessionLocal()
    try:
        from datetime import datetime
        day = "2026-10-05"
        n1 = stats.daily_snapshot(db, now=datetime(2026, 10, 5, 9, 0, 0))
        db.commit()
        assert n1 > 0
        n2 = stats.daily_snapshot(db, now=datetime(2026, 10, 5, 20, 0, 0))
        db.commit()
        assert n2 == 0        # 同日自查跳过，不重复
        cnt = db.query(StatSnapshot).filter(StatSnapshot.date == day).count()
        # 只写了一轮（与 n1 行数一致）
        assert cnt == n1
    finally:
        db.close()


# ---------------- 空库不崩 ----------------

def test_empty_db_no_crash(client):
    db = SessionLocal()
    try:
        pf = stats.platform(db)
        assert pf["gmv_cent"] == 0 and pf["gmv_txns"] == 0
        ov = stats.overview(db, 999999)   # 不存在的宿主
        assert ov["ai_total"] == 0 and ov["income_cent"] == 0
        assert stats.trends(db, 30) == []
    finally:
        db.close()
