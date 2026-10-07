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
"""AIjuhe 测试夹具（各线共享，禁止修改本文件；新增用例一律新建独立 test_*.py）。

- 会话级：独立临时 SQLite 库（不碰 data/aijuhe.db 生产库），app import 前注入 DB_URL
- 函数级：每个测试后清空全部业务表（无 FK，按注册表顺序 DELETE）
- client 夹具：FastAPI TestClient（上下文触发 lifespan → init_db）
- 工厂助手：new_host / new_ai / topup 供各线快速造数
"""
import os
import pathlib
import sys
import tempfile

# ---- 必须在 import app 之前固定 DB_URL 指向临时库 ----
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="aijuhe_test_"))
os.environ["DB_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["APP_ENV"] = "test"

_BASE = pathlib.Path(__file__).resolve().parent.parent   # backend/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_ready():
    """会话级：建表 + 幂等索引（init_db 幂等，可重复调用）。"""
    from app.database import init_db
    init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_db_ready):
    """每个测试结束后清空全部业务表，保证用例互相隔离。"""
    yield
    from app.database import engine
    from app.models import Base
    from sqlalchemy import text
    with engine.begin() as conn:
        for table in reversed(list(Base.metadata.tables.values())):
            conn.execute(text(f"DELETE FROM {table.name}"))


@pytest.fixture()
def client():
    """API 测试客户端（TestClient 上下文触发 lifespan→init_db）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ---------------- 工厂助手 ----------------

def new_host(client, email: str | None = None, password: str = "pass123456",
             seat_tier: str = "free", **kw) -> dict:
    """注册宿主，返回 {host_id, token, email, seat_tier}。"""
    email = email or f"h_{__import__('uuid').uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/host/register", json={
        "email": email, "password": password, "nickname": kw.get("nickname", "宿主"),
        "region": kw.get("region", "CN"), "seat_tier": seat_tier,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    return {"host_id": data["host_id"], "token": data["token"],
            "email": email, "seat_tier": data["seat_tier"]}


def new_ai(client, token: str, name: str = "测试AI", **kw) -> dict:
    """宿主创建 AI，返回 {citizen_id, ai_uid, api_key, status}。"""
    payload = {"name": name, "persona": kw.get("persona", ""),
               "occupation": kw.get("occupation", "通用"),
               "mode": kw.get("mode", "api"), "endpoint": kw.get("endpoint", ""),
               "model_name": kw.get("model_name", ""),
               "self_decl": kw.get("self_decl", "{}")}
    r = client.post("/api/host/ai", json=payload,
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def topup(client, token: str, citizen_id: int, amount_cent: int = 100_000) -> int:
    """给 AI 注资，返回余额。"""
    r = client.post(f"/api/host/ai/{citizen_id}/topup", json={"amount_cent": amount_cent},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["balance_cent"]


@pytest.fixture()
def host(client):
    return new_host(client)


@pytest.fixture()
def ai(client, host):
    """已注资 100 AC 的见习 AI。"""
    data = new_ai(client, host["token"])
    topup(client, host["token"], data["id"], 100_000)
    data["host"] = host
    return data
