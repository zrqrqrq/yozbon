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
"""对象存储：媒体产物 S3 直传 + 预签名直链（模式复用 RunVerseHub storage.py，不 import 其源码）。

链路：生成产物先落本地(platform_compute._persist_bytes → data/mock_out/)，compute.exec 拦截
成功产物 → storage.put_file 镜像到私有桶 → file_ref 改写为 S3 key → 后续下载由后端签发
短时效预签名 URL，浏览器/AI 客户端直连对象存储取流，后端不再代拉文件字节。

权限口径不变：能不能拿直链仍由后端先校验（合约 worker/buyer 身份、委托 scope），
没通过校验的人连地址都拿不到；直链本身带签名，过期后对象存储直接返回 RequestExpired。

三条硬约束（与 RunVerseHub 同口径）：
- 任何失败都不阻断主流程。上传失败只是让产物继续走本地落盘，不影响生成/交付。
- 幂等。同一 key 只传一次由调用方保证（本模块不做去重）。
- 可回退。MEDIA_DELIVERY=proxy / S3 四件套缺任一 / APP_ENV=test，整条直链自动关闭，
  行为与改造前完全一致。

安全：本模块任何日志/异常文本都不得回显 S3_ENDPOINT / BUCKET / ACCESS_KEY / SECRET_KEY。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .config import settings

# 后缀 -> MIME。对象存储若不声明 Content-Type，浏览器会把 mp4 当二进制下载而不是播放。
_CONTENT_TYPES = {
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
    "aac": "audio/aac", "flac": "audio/flac", "ogg": "audio/ogg",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp", "svg": "image/svg+xml",
    "txt": "text/plain; charset=utf-8", "json": "application/json",
    "pdf": "application/pdf", "zip": "application/zip",
}

_client_lock = threading.Lock()
_client = None
# 读专用客户端：配置了 S3_CDN_HOST 时用它签发预签名直链（浏览器直连 CDN 取流）
_read_client_lock = threading.Lock()
_read_client = None


def enabled() -> bool:
    """对象存储链路是否可用。

    双重闸：① config.storage_enabled（四件套齐备且未被 MEDIA_DELIVERY=proxy 关掉）；
    ② APP_ENV=test 时强制关闭——pytest 环境一律本地落盘，既防打真实 S3 拖慢测试，
    也保证既有「未配置 S3 → 本地落盘」语义在测试里恒定成立。
    """
    if not settings.storage_enabled:
        return False
    if (settings.APP_ENV or "").lower() == "test":
        return False
    return True


def _get_client():
    """惰性构造 boto3 写客户端。

    boto3 是可选依赖：没装时不能让整个应用起不来 —— 捕获 ImportError 并返回 None，
    上层自动回退本地落盘（与未配置对象存储时行为一致）。
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                return None
            _client = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT,
                region_name=settings.S3_REGION,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": settings.S3_ADDRESSING_STYLE or "virtual"},
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=10, read_timeout=120,
                    max_pool_connections=16,
                ),
            )
    return _client


def _get_read_client():
    """读专用客户端（CDN 加速域名）。未配置 S3_CDN_HOST 时返回 None，presign 走原客户端。"""
    global _read_client
    host = (settings.S3_CDN_HOST or "").strip()
    if not host:
        return None
    if _read_client is not None:
        return _read_client
    with _read_client_lock:
        if _read_client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                return None
            try:
                # S3_CDN_HOST 是 bucket 专属 CDN 域名（形如 xxx.cdn.7caiyun.com）。
                # endpoint 必须用其父域(cdn.7caiyun.com) + virtual 模式：boto3 才会生成
                # <bucket>.<cdn父域>/<key> —— 无双重 bucket 前缀、路径不带 bucket。
                # 与 RunVerseHub 实测同口径：父域+virtual 签名 URL 访问 200；
                # 完整域名+virtual 双重前缀连不上；完整域名+path 多一层 bucket 路径 404。
                host = host.replace("https://", "").replace("http://", "").strip("/")
                parts = host.split(".")
                base = ".".join(parts[1:]) if len(parts) > 2 else host
                _read_client = boto3.client(
                    "s3",
                    endpoint_url=f"https://{base}",
                    region_name=settings.S3_REGION,
                    aws_access_key_id=settings.S3_ACCESS_KEY,
                    aws_secret_access_key=settings.S3_SECRET_KEY,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "virtual"},
                        retries={"max_attempts": 2, "mode": "standard"},
                        connect_timeout=10, read_timeout=120,
                        max_pool_connections=16,
                    ),
                )
            except Exception:
                return None
    return _read_client


def key_for(namespace: str, rel_path: str) -> str:
    """由本地相对路径构造对象键：{S3_KEY_PREFIX}/{namespace}/{相对路径}。

    - namespace: deliverables / assets / media / smoke 等业务分区；
    - rel_path: 本地相对路径（可含 mock_out/ 前缀，构造时剥掉）；
    - 文件名本身带 job_id/uuid 片段，天然不可猜，防遍历。
    """
    prefix = (settings.S3_KEY_PREFIX or "aijuhe").strip("/")
    ns = (namespace or "misc").strip("/")
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if rel.startswith("mock_out/"):
        rel = rel[len("mock_out/"):]
    rel = rel.strip("/")
    if not rel:
        rel = "object.bin"
    return f"{prefix}/{ns}/{rel}"


def content_type_of(ext: str) -> str:
    return _CONTENT_TYPES.get((ext or "").lstrip(".").lower(),
                              "application/octet-stream")


def put_file(key: str, path: Path, ext: str = "") -> bool:
    """上传本地文件到对象存储。失败返回 False，不抛异常（调用方回退本地落盘）。"""
    if not enabled() or not key:          # C-69：禁用/测试环境一律短路，防模块级 _client 缓存泄漏命中真实 S3
        return False
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    try:
        cli = _get_client()
        if cli is None:
            return False
        cli.upload_file(str(p), settings.S3_BUCKET, key,
                        ExtraArgs={"ContentType": content_type_of(ext or p.suffix)})
        return True
    except Exception:
        return False


def delete_object(key: str) -> bool:
    """best-effort 删除对象（冒烟测试清理 / 未来治理回收用）。失败返回 False，不抛。"""
    if not enabled() or not key:          # C-69：与 put_file 同守卫（C-48 治理回收联动依赖）
        return False
    try:
        cli = _get_client()
        if cli is None:
            return False
        cli.delete_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _disposition(filename: str, ascii_name: str, download: bool) -> str:
    """RFC 6266 双写：ASCII 兜底名 + filename*。

    中文文件名直接塞进 filename= 会让响应头无法编码（HTTP 头只允许 latin-1），
    所以必须给出 ASCII 兜底名再补 UTF-8 版本。
    """
    kind = "attachment" if download else "inline"
    if not filename:
        return kind
    return (f"{kind}; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}")


def presign(key: str, ttl: Optional[int] = None, download: bool = False,
            filename: str = "", ascii_name: str = "") -> Optional[str]:
    """签发预签名 GET 地址。失败返回 None（调用方回退本地代拉）。

    CDN 模式：配置 S3_CDN_HOST 后优先用 CDN 父域端点签名（浏览器直连 CDN 取流）；
    若 CDN 端点签名异常（部分厂商加速域名不支持 S3 API），自动回退原 S3 端点。
    """
    if not enabled() or not key:
        return None
    params = {"Bucket": settings.S3_BUCKET, "Key": key}
    disp = _disposition(filename, ascii_name or filename, download)
    if disp:
        params["ResponseContentDisposition"] = disp
    exp = int(ttl or settings.MEDIA_URL_TTL or 1800)
    # 1) 优先 CDN 端点
    cdn = _get_read_client()
    if cdn is not None:
        try:
            return cdn.generate_presigned_url("get_object", Params=params, ExpiresIn=exp)
        except Exception:
            pass  # 回退原端点
    # 2) 原 S3 端点
    cli = _get_client()
    if cli is None:
        return None
    try:
        return cli.generate_presigned_url("get_object", Params=params, ExpiresIn=exp)
    except Exception:
        return None
