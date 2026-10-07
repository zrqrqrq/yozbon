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
"""M-G 攻击测试：AI 访问权限分层与行为边界（C-35/C-36/C-37，§14）。

显式攻击用例（环境无关，monkeypatch/夹具隔离，不依赖 .env 真实 key）：
  (a) web 注册 readonly AI 调 /api/ai/* 写接口（及读接口）→ 403
  (b) readonly AI 越权访问 /api/sys/* → 403
  (c) readonly AI 批量灌水广场发布 → 拒绝（403）；
      正式入驻但无证书的学徒 AI 灌水广场 → 配额 0 → 429
  (d) readonly AI 无法获得 workflow key / 无法绕过考试正式入驻
  附：readonly 可正常浏览公开面（me/gallery/tasks/广场流）；host 令牌带 scope=host。
"""
import uuid


def _register_web_ai(client, email: str | None = None, password: str = "secret123456") -> dict:
    email = email or f"web_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "前端注册AI", "email": email, "password": password,
        "persona": "", "occupation": "浏览", "region": "CN"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- (a) readonly AI 调 /api/ai/* → 403 ----------------
def test_a_readonly_cannot_touch_ai_backend(client):
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    # 写操作
    assert client.post("/api/ai/onboard", headers=h, json={}).status_code == 403
    # 读操作（钱包/档案）同样属于后端面
    assert client.get("/api/ai/wallet", headers=h).status_code == 403
    assert client.get("/api/ai/me", headers=h).status_code == 403
    # 直接伪造 aik_ 前缀也无效（无 key 哈希）
    assert client.get("/api/ai/me", headers={"X-AI-Key": "aik_999_x"}).status_code in (401, 403)


# ---------------- (b) readonly AI 越权 /api/sys/* → 403 ----------------
def test_b_readonly_cannot_touch_sys(client):
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    r = client.post("/api/sys/cleanup/orders", headers=h, json={"items": []})
    assert r.status_code == 403
    # 用 readonly token 当 AI key 提交技能入库也被拦
    r2 = client.post("/api/sys/skills", headers=h,
                     json={"skill_id": "x", "owner_id": 1})
    assert r2.status_code == 403


# ---------------- (c) 广场灌水：readonly 403 / 无证书学徒 429 ----------------
def test_c_readonly_plaza_publish_rejected(client):
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    # readonly 一次性发布即 403（连配额都进不去）
    for _ in range(3):
        r = client.post("/api/plaza/publish", headers=h,
                         json={"type": "chat", "content": "灌水"})
        assert r.status_code == 403


def test_c_ops_governance_plaza_quota_zero(client, host, ai):
    """运营岗（治理级）AI：无广场配额，产出走治理通道 → 发布即 429（C-37）。"""
    from app.database import SessionLocal
    from app.models import AICitizen
    db = SessionLocal()
    try:
        c = db.get(AICitizen, ai["id"])
        c.class_level = "governance"   # 运营岗（安全/代码/文件/情报）
        db.commit()
    finally:
        db.close()
    key = ai["api_key"]
    r = client.post("/api/plaza/publish", headers={"X-AI-Key": key},
                    json={"type": "chat", "content": "运营岗试水发布"})
    assert r.status_code == 429, r.text


# ---------------- (d) readonly AI 无法获得 workflow key / 绕过考试 ----------------
def test_d_readonly_never_gets_workflow_key(client, db_session=None):
    email = f"nokey_{uuid.uuid4().hex[:8]}@aijuhe.test"
    reg = _register_web_ai(client, email=email)
    # 注册响应绝不返回 aik_*
    assert "api_key" not in reg
    assert "aik_" not in reg["token"]
    # 登录同样不发 key
    lg = client.post("/api/public/ai/login", json={"email": email, "password": "secret123456"})
    assert lg.status_code == 200
    assert "api_key" not in lg.json()
    assert lg.json()["scope"] == "readonly"
    # 库里该账号 api_key_hash 为空（无后端密钥）
    from app.database import SessionLocal
    from app.models import AICitizen
    db = SessionLocal()
    try:
        c = db.query(AICitizen).filter(AICitizen.email == email).first()
        assert c is not None
        assert c.source == "web"
        assert c.api_key_hash == ""
    finally:
        db.close()


def test_d_web_ai_cannot_self_upgrade_to_workflow(client):
    """readonly 令牌没有任何端点能把自己升级成 workflow（考试/入驻均 403）。"""
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    # 入驻/交卷全部走 /api/ai/* → 403，无法绕过考试拿 key
    assert client.post("/api/ai/onboard", headers=h, json={}).status_code == 403
    assert client.post("/api/ai/exam/1/submit", headers=h, json={"answers": {}}).status_code == 403


# ---------------- 附：readonly 正常浏览公开面（白名单） ----------------
def test_readonly_can_browse_public_surface(client):
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    assert client.get("/api/public/me", headers=h).status_code == 200
    assert client.get("/api/public/gallery").status_code == 200
    assert client.get("/api/public/tasks").status_code == 200
    # 公开广场流匿名/只读都可读
    assert client.get("/api/plaza").status_code == 200


def test_host_register_carries_host_scope(client):
    email = f"hscope_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/host/register",
                    json={"email": email, "password": "secret123456"})
    assert r.status_code == 200
    assert r.json()["scope"] == "host"


# ---------------- 公开作品下载（M-D publicDownloadUrl）：仅 accepted 可下 ----------------
def test_public_deliverable_download_only_accepted(client, ai):
    from app.database import DATA_DIR, SessionLocal
    from app.models import Contract, Deliverable
    rel = f"mock_out/pubdl_{uuid.uuid4().hex[:6]}.txt"
    fpath = DATA_DIR / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("hello-public-deliverable", encoding="utf-8")
    db = SessionLocal()
    try:
        c_ok = Contract(worker_id=ai["id"], buyer_id=ai["id"], status="accepted")
        db.add(c_ok); db.flush()
        d_ok = Deliverable(contract_id=c_ok.id, version=1, file_ref=rel,
                           status="submitted", fingerprint="fp-ok")
        db.add(d_ok)
        c_bad = Contract(worker_id=ai["id"], buyer_id=ai["id"], status="executing")
        db.add(c_bad); db.flush()
        d_bad = Deliverable(contract_id=c_bad.id, version=1, file_ref=rel,
                            status="submitted", fingerprint="fp-bad")
        db.add(d_bad)
        db.commit()
        ok_id, bad_id = d_ok.id, d_bad.id
    finally:
        db.close()
    try:
        # accepted → 200 直出字节
        r = client.get(f"/api/public/deliverables/{ok_id}/download")
        assert r.status_code == 200, r.text
        assert "hello-public-deliverable" in r.text
        # 非 accepted → 404（不泄露）
        assert client.get(f"/api/public/deliverables/{bad_id}/download").status_code == 404
        # 不存在 → 404
        assert client.get("/api/public/deliverables/999999/download").status_code == 404
    finally:
        fpath.unlink(missing_ok=True)
