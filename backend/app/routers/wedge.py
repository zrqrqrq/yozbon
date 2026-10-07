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
"""MVT 楔子路由：宣传视频成片「需求→编排→出片→下载→支付」最短链路。

刻意独立于经济/文明体系：宿主 JWT 鉴权，复用平台算力出片，支付为一次性 mock 解锁。
路由经 routers/__init__.py 自动发现注册，无需改 main.py。

端点（前缀 /api/wedge）：
- POST /api/wedge/jobs                提交需求（建单 queued）
- POST /api/wedge/jobs/{id}/render    驱动「编排→出片」（queued→scripting→rendering→done）
- GET  /api/wedge/jobs                名下任务列表
- GET  /api/wedge/jobs/{id}           任务详情（含 events 埋点时间线，供画布展示）
- POST /api/wedge/jobs/{id}/pay       支付解锁下载（一次性 mock，不碰经济层）
- GET  /api/wedge/jobs/{id}/download  下载成片（done 且已支付才可下载，防路径穿越）
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, DATA_DIR
from ..deps import get_current_host
from ..models import Host, WedgeJob
from .. import storage
from .. import wedge

router = APIRouter(prefix="/api/wedge", tags=["wedge"])


class JobCreateIn(BaseModel):
    brief: str = Field(..., min_length=2, max_length=4000, description="宣传视频诉求")
    kind: str = Field(default="video_civil", description="出片链路：video_civil/video_openvdn/image")
    params: dict = Field(default_factory=dict, description="出片参数覆盖（如 duration/quality/aspect）")


def _rid(request: Request) -> str:
    """取 RequestIDMiddleware 注入的 request_id（与 G-03/G-05 同源）。"""
    return getattr(request.state, "request_id", "") or ""


def _get_owned(db: Session, job_id: int, host: Host) -> WedgeJob:
    job = db.get(WedgeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission")
    return job


# ---------------- 提交需求 ----------------
@router.post("/jobs")
def create_job(body: JobCreateIn, request: Request,
               host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    job = wedge.create_job(db, host.id, body.brief, body.kind,
                           request_id=_rid(request), params=body.params)
    return wedge.to_dict(job, include_events=True)


# ---------------- 驱动「编排→出片」 ----------------
@router.post("/jobs/{job_id}/render")
def render_job(job_id: int, request: Request,
               host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    job = _get_owned(db, job_id, host)
    if job.status in ("queued", "failed"):
        wedge.run_pipeline(db, job, request_id=_rid(request))
    return wedge.to_dict(job, include_events=True)


# ---------------- 列表 ----------------
@router.get("/jobs")
def list_jobs(host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    rows = (db.query(WedgeJob).filter(WedgeJob.host_id == host.id)
            .order_by(WedgeJob.id.desc()).limit(50).all())
    return {"items": [wedge.to_dict(j) for j in rows]}


# ---------------- 详情（含埋点时间线，供画布）----------------
@router.get("/jobs/{job_id}")
def get_job(job_id: int, host: Host = Depends(get_current_host),
            db: Session = Depends(get_db)):
    job = _get_owned(db, job_id, host)
    return wedge.to_dict(job, include_events=True)


# ---------------- 支付解锁 ----------------
class PayIn(BaseModel):
    pay_ref: str = Field(default="", max_length=128, description="外部支付流水号（mock 可空）")


@router.post("/jobs/{job_id}/pay")
def pay_job(job_id: int, body: PayIn, request: Request,
            host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    job = _get_owned(db, job_id, host)
    try:
        wedge.pay(db, job, request_id=_rid(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return wedge.to_dict(job, include_events=True)


# ---------------- 下载成片 ----------------
@router.get("/jobs/{job_id}/download")
def download_job(job_id: int, host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    job = _get_owned(db, job_id, host)
    if job.status != "done":
        raise HTTPException(status_code=409, detail="成片尚未就绪，暂不可下载")
    if int(job.pay_unlocked or 0) != 1:
        raise HTTPException(status_code=403, detail="未支付，暂不可下载")
    media = (job.file_ref or "").strip()
    if not media:
        raise HTTPException(status_code=404, detail="交付物缺失")

    filename = media.replace("\\", "/").split("/")[-1] or "wedge.bin"

    # 1) 外链直返
    if media.startswith(("http://", "https://")):
        return {"url": media, "filename": filename, "expires_in": None}

    # 2) S3 key（storage 启用且非本地 mock_out）→ 预签名直链
    if storage.enabled() and "://" not in media and not media.startswith("mock_out/"):
        url = storage.presign(media, ttl=settings.MEDIA_URL_TTL, download=True,
                              filename=filename)
        if url:
            return {"url": url, "filename": filename,
                    "expires_in": int(settings.MEDIA_URL_TTL or 1800)}

    # 3) 本地 ref → FileResponse 直出（防路径穿越）
    local = (DATA_DIR / media).resolve()
    data_root = DATA_DIR.resolve()
    if not str(local).startswith(str(data_root)):
        raise HTTPException(status_code=400, detail="Illegal file path")
    if not local.is_file():
        raise HTTPException(status_code=404, detail="交付物文件缺失")
    ct = "application/octet-stream"
    try:
        ct = storage.content_type_of(Path(local).suffix)
    except Exception:  # noqa: BLE001  content_type_of 缺失不影响直出
        pass
    return FileResponse(local, media_type=ct, filename=filename,
                        content_disposition_type="attachment")
