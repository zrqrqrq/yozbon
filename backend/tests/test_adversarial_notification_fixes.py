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
"""N 轮修复对抗测试（C-48 / C-53 / C-57；登记册 §一 10 类攻击视角自攻）。

视角自攻结论（写进 docstring，纪律 §三-1）：
- 视角3 恶意主体 / 视角8 人为滥用：无凭证匿名打 sys 运维/复核端点 → 必须 401，
  不能因"历史无鉴权"继续裸奔；前端注册 readonly AI → 必须 403（deps 路径前缀拦截）；
  非治理级 AI key 调人工/治理复核 → 必须 403（二选一门控）。
- 视角6 故障恢复：S3 删除是 best-effort，delete_object 失败/抛错不得阻断本地删除
  与订单状态翻转；已 executed 再 execute 幂等不重复删（含 S3）。
- 视角7 新供应商：S3 key 优先取 file_registry.s3_key（C-53 镜像落库），为空则按
  category+相对扫描根路径推导；任何分支都不得因 S3 不可用而拖垮文件治理主流程。
- 视角2 极端规模 / 视角10 合规：pytest 环境一律 monkeypatch，绝不打真实 S3。
"""
import pathlib
import tempfile

import pytest
from sqlalchemy import text as _sql_text

from app.database import SessionLocal
from app.models import AICitizen, FileRegistry
from app import platform_facts, storage
import app.file_governance as fg


# ---------------- 造数助手 ----------------
@pytest.fixture()
def gov_ai(client, ai):
    """把默认 ai 提为治理级（platform_file 治理任务可用）。"""
    db = SessionLocal()
    c = db.get(AICitizen, ai["id"])
    c.class_level = "governance"
    db.commit()
    db.close()
    return ai


@pytest.fixture()
def tmp_scan():
    d = tempfile.mkdtemp(prefix="aijuhe_nfix_")
    fg.set_scan_root(d)
    return d


def _host_hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _register_web_ai(client) -> dict:
    import uuid
    email = f"nfix_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "前端注册AI", "email": email, "password": "secret123456",
        "persona": "", "occupation": "浏览", "region": "CN"})
    assert r.status_code == 200, r.text
    return r.json()


def _set_s3_key(path: str, s3_key: str):
    """models.py 未映射 file_registry.s3_key（登记册 C-63），用 raw SQL 写。"""
    db = SessionLocal()
    db.execute(_sql_text("UPDATE file_registry SET s3_key=:k WHERE path=:p"),
               {"k": s3_key, "p": path})
    db.commit()
    db.close()


# ---------------- C-57：无凭证 → 401；health 保持公开 ----------------
def test_c57_no_credential_sys_endpoints_401(client):
    # 加固过的写/读运维端点，无凭证一律 401
    assert client.post("/api/sys/tick").status_code == 401
    assert client.post("/api/sys/recalc").status_code == 401
    assert client.get("/api/sys/economy").status_code == 401
    assert client.get("/api/sys/cleanup/orders").status_code == 401
    assert client.post("/api/sys/cleanup/orders/999/review",
                       json={"action": "approve"}).status_code == 401
    assert client.post("/api/sys/platform-jobs/trigger",
                       json={"job_type": "platform_intel"}).status_code == 401
    assert client.get("/api/sys/platform-jobs").status_code == 401
    assert client.post("/api/sys/intel/collect",
                       json={"source": "github"}).status_code == 401
    assert client.post("/api/sys/plaza/999/review",
                       json={"action": "pass"}).status_code == 401
    # 探活端点保持公开（不破坏健康检查）
    assert client.get("/api/sys/health").status_code == 200


# ---------------- C-57：readonly AI → 403（deps 路径前缀拦截） ----------------
def test_c57_readonly_ai_sys_endpoints_403(client):
    web = _register_web_ai(client)
    h = {"Authorization": f"Bearer {web['token']}"}
    # 双凭证端点（host_or_governance_ai → 落到 get_current_ai）→ /api/sys/ 前缀拦截 403
    assert client.post("/api/sys/cleanup/orders/999/review",
                       headers=h, json={"action": "approve"}).status_code == 403
    assert client.post("/api/sys/intel/collect",
                       headers=h, json={"source": "github"}).status_code == 403
    # host-JWT 专用端点（tick）：ai_ro 非 host token → get_current_host 401（同样拒绝）
    assert client.post("/api/sys/tick", headers=h).status_code == 401


# ---------------- C-57：非治理级 AI key → 403（二选一门控） ----------------
def test_c57_non_governance_ai_review_403(client, ai):
    hdr = {"X-AI-Key": ai["api_key"]}   # 默认 bottom 级
    assert client.post("/api/sys/cleanup/orders/999/review",
                       headers=hdr, json={"action": "approve"}).status_code == 403
    assert client.post("/api/sys/intel/collect",
                       headers=hdr, json={"source": "github"}).status_code == 403


# ---------------- C-57：host JWT / 治理 AI key 放行 ----------------
def test_c57_host_jwt_passes(client, host):
    r = client.get("/api/sys/economy", headers=_host_hdr(host))
    assert r.status_code == 200, r.text


# ---------------- C-48：execute 联动删 S3（显式 s3_key），失败不阻断 ----------------
def test_c48_execute_deletes_s3_by_explicit_key(client, host, gov_ai,
                                                tmp_scan, monkeypatch):
    f = pathlib.Path(tmp_scan) / "deliv.txt"
    f.write_text("x", encoding="utf-8")
    db = SessionLocal()
    db.add(FileRegistry(path=str(f), category="deliverable", status="active"))
    db.commit()
    db.close()
    _set_s3_key(str(f), "aijuhe/deliverables/explicit.bin")

    calls = []

    def fake_delete(key):
        calls.append(key)
        return True

    monkeypatch.setattr(storage, "enabled", lambda: True)   # 过回收闸口
    monkeypatch.setattr(storage, "delete_object", fake_delete)

    aid = {"X-AI-Key": gov_ai["api_key"]}
    oid = client.post("/api/sys/cleanup/orders",
                      json={"items": [{"path": str(f)}]},
                      headers=aid).json()["id"]
    oh = _host_hdr(host)
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "approve"}, headers=oh)
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                   json={"action": "execute"}, headers=oh)
    assert r.status_code == 200, r.text
    # 按登记的 s3_key 调用删除
    assert calls == ["aijuhe/deliverables/explicit.bin"]
    assert r.json()["executed_items"][0]["s3_deleted"] is True
    assert not f.exists()


# ---------------- C-48：s3_key 为空 → 推导 key；delete 失败不阻断本地删除 ----------------
def test_c48_derived_key_and_delete_failure_nonblocking(client, host, gov_ai,
                                                        tmp_scan, monkeypatch):
    f = pathlib.Path(tmp_scan) / "asset.dat"
    f.write_text("y", encoding="utf-8")
    db = SessionLocal()
    db.add(FileRegistry(path=str(f), category="asset", status="active"))
    db.commit()
    db.close()  # s3_key 留空 → 走推导

    calls = []

    def boom(key):
        calls.append(key)
        return False  # best-effort 失败

    monkeypatch.setattr(storage, "enabled", lambda: True)   # 过回收闸口
    monkeypatch.setattr(storage, "delete_object", boom)

    aid = {"X-AI-Key": gov_ai["api_key"]}
    oid = client.post("/api/sys/cleanup/orders",
                      json={"items": [{"path": str(f)}]},
                      headers=aid).json()["id"]
    oh = _host_hdr(host)
    client.post(f"/api/sys/cleanup/orders/{oid}/review",
                json={"action": "approve"}, headers=oh)
    r = client.post(f"/api/sys/cleanup/orders/{oid}/review",
                   json={"action": "execute"}, headers=oh)
    assert r.status_code == 200, r.text
    expected = storage.key_for("asset", "asset.dat")
    assert calls == [expected]
    # 失败不阻断：订单仍 executed、本地文件已删、s3_deleted=False
    assert r.json()["status"] == "executed"
    assert r.json()["executed_items"][0]["s3_deleted"] is False
    assert not f.exists()


# ---------------- C-53：collect_file_facts 后 s3_key 落库 ----------------
def test_c53_collect_file_facts_writes_s3_key(client, tmp_path, monkeypatch):
    (tmp_path / "deliverable_out.zip").write_text("y", encoding="utf-8")
    (tmp_path / "note.log").write_text("z", encoding="utf-8")  # temp 类不镜像

    def fake_mirror(path, category):
        return f"fake/{category}/{pathlib.Path(path).name}"

    monkeypatch.setattr(fg, "mirror_registered_path", fake_mirror)

    db = SessionLocal()
    platform_facts.collect_file_facts(db, scan_root_dir=tmp_path)
    db.commit()
    dk = db.execute(_sql_text(
        "SELECT s3_key FROM file_registry WHERE path LIKE '%deliverable_out.zip%'"
    )).first()
    tk = db.execute(_sql_text(
        "SELECT s3_key FROM file_registry WHERE path LIKE '%note.log%'"
    )).first()
    db.close()
    assert dk[0] == "fake/deliverable/deliverable_out.zip"
    # temp 类不镜像 → s3_key 留空
    assert (tk[0] or "") == ""
