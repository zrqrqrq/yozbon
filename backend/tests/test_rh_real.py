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
"""RH 真链路产物下载落盘 + 参数透传 + 存储通道 测试（不发任何真实网络请求）。

全部用 monkeypatch 拦截薄边界（_http_submit/_http_poll/_http_download），
离线验证：
  - submit body 透传 instanceType / retainSeconds / usePersonalQueue / workflowId；
  - poll 真链路终态：流式下载字节 → 本地落盘 + sha256，file_ref 为本地相对路径；
  - 远端产物大小上限 _http_download 累计超阈值即中断；
  - 失败/取消终态：透传 failedReason，不假装 running；
  - S3 best-effort：storage 模块缺失 → 回退 local；注入假 storage → 标记 s3。
"""
import hashlib
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import app.platform_compute as pc  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import DATA_DIR  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """强制一个 fake personal 槽位走「真链路代码分支」，但所有 HTTP 边界由用例自行 patch。
    同时清空 S3 字段，默认走 local 落盘（需要 S3 的用例自行恢复）。"""
    monkeypatch.setattr(pc, "_slots",
                        lambda: [{"id": "personal", "key": "fake", "base": "http://rh.invalid",
                                  "queue": True, "password": ""}])
    for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT"):
        monkeypatch.setattr(settings, k, "")


# ---------------- submit 参数透传 ----------------
def test_submit_body_passes_instance_and_retain(monkeypatch):
    """settings.RH_INSTANCE_TYPE 非空 / RH_RETAIN_SECONDS>0 → body 透传。"""
    monkeypatch.setattr(settings, "RH_INSTANCE_TYPE", "default")
    monkeypatch.setattr(settings, "RH_RETAIN_SECONDS", 600)
    captured = {}

    def fake_submit(base, body):
        captured.update(body=body)
        return {"code": 0, "data": {"taskId": "tx-1"}}

    monkeypatch.setattr(pc, "_http_submit", fake_submit)
    pc.submit("image", "cat", {})
    b = captured["body"]
    assert b["instanceType"] == "default"
    assert b["retainSeconds"] == 600
    assert b["usePersonalQueue"] is True
    assert b["workflowId"]                       # 从 settings.RH_WF_* 读到（不打印值）
    assert "nodeInfoList" in b


def test_submit_no_retain_when_zero(monkeypatch):
    """RH_RETAIN_SECONDS=0 时完全不传 retainSeconds（避免按保留时长额外计费）。"""
    monkeypatch.setattr(settings, "RH_INSTANCE_TYPE", "")
    monkeypatch.setattr(settings, "RH_RETAIN_SECONDS", 0)
    captured = {}

    def fake_submit(base, body):
        captured.update(body=body)
        return {"code": 0, "data": {"taskId": "tx-2"}}

    monkeypatch.setattr(pc, "_http_submit", fake_submit)
    pc.submit("image", "cat", {})
    b = captured["body"]
    assert "retainSeconds" not in b
    assert "instanceType" not in b


def test_submit_img2img_uses_denoise_workflow(monkeypatch):
    """img2img kind → 节点映射键 img2img_denoise → workflowId 取 RH_WF_IMG2IMG_DENOISE（非空）。"""
    captured = {}

    def fake_submit(base, body):
        captured.update(body=body)
        return {"code": 0, "data": {"taskId": "tx-3"}}

    monkeypatch.setattr(pc, "_http_submit", fake_submit)
    pc.submit("img2img", "cat", {})
    assert captured["body"]["workflowId"]


# ---------------- poll 真链路下载落盘 ----------------
def test_real_poll_downloads_persists_and_hashes(monkeypatch):
    """终态 SUCCESS：下载远端字节 → 本地相对路径 file_ref + 真实 sha256/size。"""
    monkeypatch.setattr(settings, "RH_RESULT_MAX_BYTES", 50 * 1024 * 1024)
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 128

    def fake_poll(base, path, body):
        if path == "/task/openapi/status":
            return {"code": 0, "data": {"taskStatus": "SUCCESS"}}
        return {"code": 0, "data": [{"fileUrl": "http://rh.invalid/out/t-9.png",
                                     "fileSize": len(fake_bytes)}]}

    monkeypatch.setattr(pc, "_http_poll", fake_poll)
    monkeypatch.setattr(pc, "_http_download", lambda url, mx: fake_bytes)
    pc._REAL_JOBS["t-9"] = {"kind": "image"}

    p = pc.poll("t-9", "personal")
    assert p["status"] == "succeeded"
    f = p["files"][0]
    assert f["file_ref"].startswith("mock_out/image/")
    assert f["file_ref"].endswith(".png")
    assert not f["file_ref"].startswith("http")          # 不是远端直链
    assert f["size"] == len(fake_bytes)
    assert f["fingerprint"] == hashlib.sha256(fake_bytes).hexdigest()
    assert p["meta"]["storage"] == "local"
    # 本地真实落盘
    assert (DATA_DIR / f["file_ref"]).exists()


def test_http_download_enforces_max_bytes(monkeypatch):
    """累计超过 max_bytes 即中断抛错；max_bytes=0 不限制。"""
    import httpx

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size=0):
            for _ in range(10):
                yield b"0123456789"      # 每次 10 字节，共 100 字节

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeResp())
    with pytest.raises(RuntimeError):
        pc._http_download("http://x/y.bin", 25)             # 上限 25 < 100
    out = pc._http_download("http://x/y.bin", 0)           # 0 = 不限制
    assert len(out) == 100


def test_download_failure_marks_failed(monkeypatch):
    """下载抛错（如超上限）→ poll 落 failed，不崩。"""
    def fake_poll(base, path, body):
        if path == "/task/openapi/status":
            return {"code": 0, "data": {"taskStatus": "SUCCESS"}}
        return {"code": 0, "data": [{"fileUrl": "http://rh.invalid/big.mp4"}]}

    monkeypatch.setattr(pc, "_http_poll", fake_poll)

    def boom(url, mx):
        raise RuntimeError("产物超过大小上限 524288000 字节，已中止下载")

    monkeypatch.setattr(pc, "_http_download", boom)
    pc._REAL_JOBS["t-big"] = {"kind": "video_civil"}
    p = pc.poll("t-big", "personal")
    assert p["status"] == "failed"
    assert "大小上限" in p["error"]


# ---------------- 失败/取消终态 ----------------
def test_failed_status_transparently_reasons(monkeypatch):
    """status=FAILED → 拉 outputs 取 failedReason 透传，不假装 running。"""
    def fake_poll(base, path, body):
        if path == "/task/openapi/status":
            return {"code": 0, "data": {"taskStatus": "FAILED"}}
        return {"code": 805, "data": {"failedReason": {
            "exception_type": "ValueError", "node_name": "6",
            "exception_message": "bad input"}}}

    monkeypatch.setattr(pc, "_http_poll", fake_poll)
    pc._REAL_JOBS["t-fail"] = {"kind": "image"}
    p = pc.poll("t-fail", "personal")
    assert p["status"] == "failed"
    assert "bad input" in p["error"]


# ---------------- S3 best-effort 存储通道 ----------------
def test_s3_channel_local_when_storage_missing(monkeypatch, tmp_path):
    """storage_enabled=True 但 storage 模块 import 失败 → 回退 local，不抛。"""
    monkeypatch.setattr(settings, "S3_ENDPOINT", "https://fake.s3")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY", "ak")
    monkeypatch.setattr(settings, "S3_SECRET_KEY", "sk")
    import app as _app
    # 强制 app.storage 不可 import（模拟 M-C 尚未落地）：sys.modules 置 None + 包属性删净，
    # 保证 from . import storage 在任何 pytest 导入顺序下都失败。
    monkeypatch.setitem(sys.modules, "app.storage", None)
    monkeypatch.delattr(_app, "storage", raising=False)
    p = tmp_path / "x.png"
    p.write_bytes(b"1234")
    ch = pc._maybe_upload_s3(p, "image/x.png", "png")
    assert ch == "local"


def test_s3_channel_s3_when_upload_ok(monkeypatch, tmp_path):
    """注入假 storage 模块 put_file 成功 → 通道标记 s3。"""
    monkeypatch.setattr(settings, "S3_ENDPOINT", "https://fake.s3")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY", "ak")
    monkeypatch.setattr(settings, "S3_SECRET_KEY", "sk")
    captured = {}
    fake = types.ModuleType("app.storage")
    fake.enabled = lambda: True
    fake.put_file = lambda key, path, ext: captured.update(
        key=key, ext=ext) or True
    import app as _app
    # from . import storage 既查 sys.modules 又查 app 包属性，两处都注入才确定命中。
    monkeypatch.setitem(sys.modules, "app.storage", fake)
    monkeypatch.setattr(_app, "storage", fake)
    p = tmp_path / "x.png"
    p.write_bytes(b"1234")
    ch = pc._maybe_upload_s3(p, "image/x.png", "png")
    assert ch == "s3"
    assert captured["key"].endswith("image/x.png")
