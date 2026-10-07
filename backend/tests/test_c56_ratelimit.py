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
"""C-56 攻击/业务测试：readonly 限流双后端（Redis 优先 + 内存降级）。

覆盖（先攻后建：先写失败用例再实现）：
  (a) 默认无 REDIS_URL：内存滑窗路径——超限 429、窗口过期后恢复 200；
  (b) REDIS_URL 配置 + Redis 可用（fake 客户端）：走 Redis 固定窗口，key 带前缀+分钟桶，
      超限 429，INCR/EXPIRE 均被调用；
  (c) Redis 异常/超时：自动降级内存，请求绝不 500，内存路径仍执行限流；
  (d) readonly 403 语义不受限流改动影响（/api/ai/* 仍 403）；
  (e) 配置为空时绝不触碰 redis 库（_redis_get 直接返回 None）。

注：SQLite 空表后 rowid 复用，各用例新注册 AI 可能同 id；内存计数为模块级持久字典，
每用例开头清空（_reset）保证隔离（生产语义：同 citizen 同窗口计数，行为不变）。
"""
import uuid


def _reset(client=None):
    """用例隔离：清空内存限流计数（Redis 路径测试不依赖它，也一并清空防串扰）。"""
    from app import deps
    deps._READONLY_HITS.clear()


def _register_web_ai(client, email: str | None = None, password: str = "secret123456") -> dict:
    email = email or f"web_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "前端注册AI", "email": email, "password": password,
        "persona": "", "occupation": "浏览", "region": "CN"})
    assert r.status_code == 200, r.text
    return r.json()


def _h(web: dict) -> dict:
    return {"Authorization": f"Bearer {web['token']}"}


# ---------------- (a) 内存路径：默认行为 + 窗口过期恢复 ----------------
def test_a_memory_path_limit_and_recovery(client, monkeypatch):
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 2)
    monkeypatch.setattr(settings, "REDIS_URL", "")   # 确保走内存
    web = _register_web_ai(client)
    h = _h(web)
    assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/me", headers=h).status_code == 429   # 超限
    # 窗口过期（等价 60s 后）：计数清空 → 恢复
    deps._READONLY_HITS[str(web["citizen_id"])] = []
    assert client.get("/api/public/me", headers=h).status_code == 200
    # 不同 citizen 互不串扰
    web2 = _register_web_ai(client)
    assert client.get("/api/public/me", headers=_h(web2)).status_code == 200


# ---------------- (b) Redis 路径：fake 客户端 ----------------
class _FakeRedis:
    """最小 fake：INCR 计数 + EXPIRE 记录。"""

    def __init__(self):
        self.store = {}
        self.expired = []

    def incr(self, key: str):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, ttl: int):
        self.expired.append((key, ttl))
        return True

    def ping(self):
        return True


def test_b_redis_path_used_when_configured(client, monkeypatch):
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 2)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(deps, "_redis_get", lambda: fake)
    web = _register_web_ai(client)
    h = _h(web)
    for _ in range(2):
        assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/me", headers=h).status_code == 429
    # Redis key 带前缀 + citizen + 分钟桶；EXPIRE 已设置（ttl=120）
    keys = [k for k in fake.store if str(web["citizen_id"]) in k and k.startswith("aijuhe:rl:")]
    assert keys, "Redis 路径未使用带前缀的 key"
    assert fake.expired, "INCR 首次后未设置 EXPIRE"


def test_b_redis_limit_per_citizen(client, monkeypatch):
    """Redis 路径下不同 citizen 独立计数（互不串扰）。"""
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 2)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(deps, "_redis_get", lambda: fake)
    w1 = _register_web_ai(client)
    w2 = _register_web_ai(client)
    for _ in range(2):
        assert client.get("/api/public/me", headers=_h(w1)).status_code == 200
    assert client.get("/api/public/me", headers=_h(w1)).status_code == 429
    assert client.get("/api/public/me", headers=_h(w2)).status_code == 200


# ---------------- (c) Redis 异常：自动降级内存，绝不 500 ----------------
class _BoomRedis:
    def incr(self, key):
        raise ConnectionError("redis down")

    def expire(self, key, ttl):
        raise ConnectionError("redis down")


def test_c_redis_error_falls_back_to_memory(client, monkeypatch):
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 2)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(deps, "_redis_get", lambda: _BoomRedis())
    web = _register_web_ai(client)
    h = _h(web)
    # Redis 抛错 → 降级内存：请求不 500，内存限流仍生效
    for _ in range(2):
        assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/me", headers=h).status_code == 429


def test_c_redis_get_connection_failure_no_crash(client, monkeypatch):
    """_redis_get 连接失败（ping 抛错）→ 返回 None → 内存路径照常，不 500。"""
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 5)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # 不可达端口
    monkeypatch.setattr(deps, "_redis_get", lambda: None)   # 模拟连接失败返回 None
    web = _register_web_ai(client)
    h = _h(web)
    assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/me", headers=h).status_code == 200


# ---------------- (d) readonly 403 语义不变 ----------------
def test_d_readonly_403_semantics_unchanged(client, monkeypatch):
    from app.config import settings
    from app import deps
    _reset()
    monkeypatch.setattr(settings, "PLAZA_READONLY_RPM", 2)
    monkeypatch.setattr(settings, "REDIS_URL", "")   # 内存路径
    web = _register_web_ai(client)
    h = _h(web)
    # 403 先于限流：readonly 令牌访问 /api/ai/* 一律 403（不走计数）
    for _ in range(3):
        assert client.get("/api/ai/me", headers=h).status_code == 403
        assert client.post("/api/ai/onboard", headers=h, json={}).status_code == 403
        assert client.post("/api/sys/cleanup/orders", headers=h,
                           json={"items": []}).status_code == 403


# ---------------- (e) 未配置 REDIS_URL 时不 import redis 依赖路径 ----------------
def test_e_unconfigured_never_uses_redis(client, monkeypatch):
    from app.config import settings
    _reset()
    monkeypatch.setattr(settings, "REDIS_URL", "")
    web = _register_web_ai(client)
    h = _h(web)
    assert client.get("/api/public/me", headers=h).status_code == 200
    # 未配置时 deps._redis_get 直接返回 None（不建连接）
    from app import deps
    assert deps._redis_get() is None
