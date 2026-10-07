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
"""N20 版权溯源增强路由（社会功能扩展设计 §4 N20）。

  POST /api/sys/fingerprint/compare   host_or_governance_ai 双凭证；
      body {media_type, fingerprint? 或 content_ref?} → 相似度命中列表
  GET  /api/public/works/{id}/provenance  公开来源链（不泄露内部密钥）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import host_or_governance_ai
from ..models import GalleryItem
from .. import fingerprints as svc

router = APIRouter(tags=["n20-fingerprint"])


class CompareBody(BaseModel):
    media_type: str = Field(..., description="image/text/audio/video")
    fingerprint: str = Field("", description="Known fingerprint hex; choose one of fingerprint or content_ref")
    content_ref: str = Field("", description="Local relative file ref; server computes fingerprint")


@router.post("/api/sys/fingerprint/compare")
def compare(body: CompareBody, cred=Depends(host_or_governance_ai),
            db: Session = Depends(get_db)):
    fp = body.fingerprint.strip()
    if not fp and body.content_ref.strip():
        data = svc._resolve_media_bytes(body.content_ref.strip())
        text = None
        if body.media_type == "text" and data is not None:
            text = data.decode("utf-8", errors="ignore")
        fp = svc.compute_fingerprint(body.media_type, data=data, text=text) or ""
    hits = svc.compare(db, body.media_type, fp)
    return {"media_type": body.media_type, "query_fingerprint": fp[:16],
            "threshold": svc.HAMMING_THRESHOLD, "hits": hits}


@router.get("/api/public/works/{item_id}/provenance")
def work_provenance(item_id: int, db: Session = Depends(get_db)):
    prov = svc.provenance(db, item_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="Work not found")
    return prov
