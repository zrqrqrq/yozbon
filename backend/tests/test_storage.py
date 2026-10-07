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
"""storage 对象存储模块 + 接入点单测（环境无关，绝不打真实 S3）。

口径：.env 里有真实 S3 key，但本文件一律 monkeypatch settings 把 S3 四件套置空
（或伪造 enabled），测的是分支逻辑而非真实连通；真实连通由 tools/s3_smoke.py 负责。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import compute, file_governance, storage
from app.config import settings


@pytest.fixture
def s3_snap(monkeypatch):
    """快照并隔离 S3 相关配置，测试结束还原。"""
    keys = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
            "S3_CDN_HOST", "S3_KEY_PREFIX", "S3_REGION", "S3_ADDRESSING_STYLE",
            "MEDIA_DELIVERY", "MEDIA_URL_TTL", "APP_ENV")
    snap = {k: getattr(settings, k) for k in keys}
    yield snap
    for k, v in snap.items():
        setattr(settings, k, v)


def _configure_real_like(monkeypatch, s3_snap, cdn: str = ""):
    """伪造一套「配置齐备且非 proxy、非 test」的环境。"""
    monkeypatch.setattr(settings, "S3_ENDPOINT", "https://s3.example.test")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY", "ak-fake")
    monkeypatch.setattr(settings, "S3_SECRET_KEY", "sk-fake")
    monkeypatch.setattr(settings, "S3_BUCKET", "bucket-fake")
    monkeypatch.setattr(settings, "S3_CDN_HOST", cdn)
    monkeypatch.setattr(settings, "S3_KEY_PREFIX", "aijuhe")
    monkeypatch.setattr(settings, "MEDIA_DELIVERY", "direct")
    monkeypatch.setattr(settings, "APP_ENV", "dev")


# ---------------- enabled() 闸口 ----------------
class TestEnabled:
    def test_disabled_when_keys_missing(self, monkeypatch, s3_snap):
        monkeypatch.setattr(settings, "S3_ENDPOINT", "")
        monkeypatch.setattr(settings, "S3_ACCESS_KEY", "")
        monkeypatch.setattr(settings, "S3_SECRET_KEY", "")
        monkeypatch.setattr(settings, "MEDIA_DELIVERY", "direct")
        monkeypatch.setattr(settings, "APP_ENV", "dev")
        assert storage.enabled() is False

    def test_disabled_when_proxy(self, monkeypatch, s3_snap):
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(settings, "MEDIA_DELIVERY", "proxy")
        assert storage.enabled() is False

    def test_disabled_in_test_env(self, monkeypatch, s3_snap):
        # 即使四件套齐备，APP_ENV=test 也强制关闭（pytest 环境隔离）
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(settings, "APP_ENV", "test")
        assert storage.enabled() is False

    def test_enabled_when_configured_dev(self, monkeypatch, s3_snap):
        _configure_real_like(monkeypatch, s3_snap)
        assert storage.enabled() is True


# ---------------- key_for / content_type_of ----------------
class TestKeyAndMime:
    def test_key_for_strips_mock_out_and_uses_prefix(self, monkeypatch, s3_snap):
        monkeypatch.setattr(settings, "S3_KEY_PREFIX", "aijuhe")
        key = storage.key_for("deliverables", "mock_out/image/job1.png")
        assert key == "aijuhe/deliverables/image/job1.png"

    def test_key_for_namespace(self, monkeypatch, s3_snap):
        monkeypatch.setattr(settings, "S3_KEY_PREFIX", "aijuhe")
        assert storage.key_for("smoke", "x.bin") == "aijuhe/smoke/x.bin"

    @pytest.mark.parametrize("ext,expected", [
        ("png", "image/png"), (".mp4", "video/mp4"), ("mp3", "audio/mpeg"),
        (".txt", "text/plain; charset=utf-8"), ("", "application/octet-stream"),
        ("bin", "application/octet-stream"),
    ])
    def test_content_type(self, ext, expected):
        assert storage.content_type_of(ext) == expected


# ---------------- put_file / presign 失败不抛 ----------------
class TestPutAndPresign:
    def test_put_file_false_when_disabled(self, monkeypatch, s3_snap, tmp_path):
        monkeypatch.setattr(settings, "S3_ENDPOINT", "")
        monkeypatch.setattr(settings, "APP_ENV", "dev")
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        assert storage.put_file("k", f, "png") is False

    def test_put_file_false_when_file_missing(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        # enabled() True 但本地文件不存在 → 返回 False 不抛
        assert storage.put_file("k", tmp_path / "nope.png", "png") is False

    def test_presign_none_when_disabled(self, monkeypatch, s3_snap):
        monkeypatch.setattr(settings, "S3_ENDPOINT", "")
        monkeypatch.setattr(settings, "APP_ENV", "dev")
        assert storage.presign("any/key.png") is None

    def test_presign_cdn_then_fallback(self, monkeypatch, s3_snap):
        _configure_real_like(monkeypatch, s3_snap, cdn="bucket.cdn.example.com")
        origin = MagicMock()
        origin.generate_presigned_url.return_value = "https://s3.example/bucket/k?sig=origin"
        cdn = MagicMock()
        cdn.generate_presigned_url.return_value = "https://cdn.example/bucket/k?sig=cdn"
        monkeypatch.setattr(storage, "_get_client", lambda: origin)
        monkeypatch.setattr(storage, "_get_read_client", lambda: cdn)
        # CDN 正常 → 用 CDN 签名
        url = storage.presign("deliverables/a/b.png", ttl=120)
        assert url == "https://cdn.example/bucket/k?sig=cdn"
        cdn.generate_presigned_url.assert_called_once()
        # CDN 异常 → 回退原端点
        cdn.generate_presigned_url.side_effect = RuntimeError("cdn not support s3 api")
        url2 = storage.presign("deliverables/a/b.png")
        assert url2 == "https://s3.example/bucket/k?sig=origin"

    def test_presign_none_when_client_none(self, monkeypatch, s3_snap):
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(storage, "_get_client", lambda: None)
        monkeypatch.setattr(storage, "_get_read_client", lambda: None)
        assert storage.presign("k") is None


# ---------------- 接入点：compute._mirror_local_files ----------------
class TestComputeMirror:
    def _local_result(self, tmp_path, ref="mock_out/image/job1.png"):
        workdir = tmp_path / "data" / "mock_out"
        workdir.mkdir(parents=True)
        rel = Path(ref).relative_to("mock_out") if ref.startswith("mock_out/") else Path(ref)
        f = workdir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"PNGDATA")
        return {"job_id": "job1", "status": "succeeded",
                "files": [{"file_ref": ref, "fingerprint": "fp1", "size": 7}],
                "error": "", "meta": {"channel": "image"}}

    def test_disabled_keeps_local_ref(self, monkeypatch, s3_snap, tmp_path):
        # 未启用（默认 test 环境即关闭）→ 纯透传，file_ref 不动
        monkeypatch.setattr(settings, "S3_ENDPOINT", "")
        res = self._local_result(tmp_path)
        out = compute._mirror_local_files(res)
        assert out["files"][0]["file_ref"] == "mock_out/image/job1.png"
        assert out["files"][0]["storage"] == "local"
        assert out["meta"]["storage_channel"] == "local"

    def test_enabled_upload_success_rewrites_ref(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(storage, "put_file", lambda key, path, ext="": True)
        res = self._local_result(tmp_path)
        monkeypatch.setattr(compute, "DATA_DIR", tmp_path / "data")
        out = compute._mirror_local_files(res)
        f0 = out["files"][0]
        assert f0["storage"] == "s3"
        assert f0["file_ref"].startswith("aijuhe/deliverables/")
        assert f0["file_ref"].endswith("image/job1.png")
        assert out["meta"]["storage_channel"] == "s3"

    def test_enabled_upload_failure_falls_back_local(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(storage, "put_file", lambda key, path, ext="": False)
        res = self._local_result(tmp_path)
        monkeypatch.setattr(compute, "DATA_DIR", tmp_path / "data")
        out = compute._mirror_local_files(res)
        f0 = out["files"][0]
        assert f0["storage"] == "local"
        assert f0["file_ref"] == "mock_out/image/job1.png"
        assert out["meta"]["storage_channel"] == "local"

    def test_remote_url_ref_untouched(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        called = []
        monkeypatch.setattr(storage, "put_file",
                            lambda key, path, ext="": called.append(key) or True)
        res = {"job_id": "r", "status": "succeeded",
               "files": [{"file_ref": "https://rh.example/out/x.png",
                          "fingerprint": "", "size": 1}],
               "error": "", "meta": {}}
        out = compute._mirror_local_files(res)
        assert out["files"][0]["storage"] == "remote"
        assert out["files"][0]["file_ref"] == "https://rh.example/out/x.png"
        assert called == []  # 远端直链不回传、不二次上传


# ---------------- 接入点：file_governance.mirror_registered_path ----------------
class TestFileGovMirror:
    def test_temp_category_never_uploads(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        called = []
        monkeypatch.setattr(storage, "put_file",
                            lambda key, path, ext="": called.append(key) or True)
        f = tmp_path / "tmp.bin"
        f.write_bytes(b"x")
        assert file_governance.mirror_registered_path(str(f), "temp") == ""
        assert called == []

    def test_deliverable_category_uploads_when_enabled(self, monkeypatch, s3_snap, tmp_path):
        _configure_real_like(monkeypatch, s3_snap)
        monkeypatch.setattr(file_governance, "_SCAN_ROOT", tmp_path)
        monkeypatch.setattr(storage, "put_file", lambda key, path, ext="": True)
        f = tmp_path / "out.png"
        f.write_bytes(b"png")
        # 传相对文件名：函数内部按 _SCAN_ROOT(=tmp_path) 解析
        key = file_governance.mirror_registered_path("out.png", "deliverable")
        assert key == "aijuhe/deliverable/out.png"

    def test_disabled_returns_empty(self, monkeypatch, s3_snap, tmp_path):
        monkeypatch.setattr(settings, "S3_ENDPOINT", "")
        monkeypatch.setattr(settings, "APP_ENV", "dev")
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        assert file_governance.mirror_registered_path(str(f), "media") == ""
