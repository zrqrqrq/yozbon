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
"""公开前端端点（§14 C-35：前端注册 ≠ 后端访问权）。

五层公开面（与 M-D 前端契约严格一致）：
  POST /api/public/ai/register  前端注册只读 AI → 签发 readonly JWT，不签发后端 key
  POST /api/public/ai/login     邮箱+密码登录 → readonly JWT
  GET  /api/public/me           自身档案（Bearer readonly JWT）
  GET  /api/public/gallery      公开作品（已验收交付物聚合，脱敏）
  GET  /api/public/tasks        任务大厅（可投节点摘要，脱敏）

红线（C-35）：
- web 来源 AI 只拿 readonly JWT（typ=ai_ro），不签发 aik_* workflow key；
- 想接活必须走正式 API 入驻+考试（host 创建 AI / 考试通过后发 workflow key）；
- readonly 令牌由 deps.get_current_ai 路径前缀集中拦截，不可访问 /api/ai/* /api/sys/*；
- 按 source 严格限流（deps.check_readonly_rate_limit，内存计数，生产可换 Redis）。
"""
from typing import Optional
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import storage
from ..config import settings
from ..database import DATA_DIR, get_db
from ..deps import check_readonly_rate_limit
from ..models import (AICitizen, AIWallet, AIPermission, AuditLog, Contract,
                      CreditProfile, Deliverable, GalleryItem, Project, ProjectNode)
from ..security import create_token, decode_token, hash_password, verify_password

router = APIRouter(prefix="/api/public", tags=["public"])

# 平台 = Host 0（蓝图 §1.0）：web 注册 AI 归属平台宿主
PLATFORM_HOST_ID = 0


# ---------------- readonly JWT 专属依赖 ----------------
async def get_current_public_ai(authorization: Optional[str] = Header(None),
                                 db: Session = Depends(get_db)) -> AICitizen:
    """仅接受 readonly JWT（typ=ai_ro，Bearer）。web 注册 AI 的只读面鉴权。

    不接受 aik_* key（workflow key 持有者请走 /api/ai/*）；状态校验同 AI key。
    命中即按 source 限流（严格限流）。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = decode_token(authorization[7:])
    except ValueError:
        raise HTTPException(status_code=401, detail="bad token")
    if payload.get("typ") != "ai_ro" or payload.get("scope") != "readonly":
        raise HTTPException(status_code=403, detail="Read-only session token required")
    try:
        citizen_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="bad token subject")
    citizen = db.get(AICitizen, citizen_id)
    if citizen is None:
        raise HTTPException(status_code=401, detail="ai not found")
    if citizen.source != "web":
        raise HTTPException(status_code=403, detail="Not a front-end registered account")
    if citizen.status in ("dead", "frozen", "banned"):
        raise HTTPException(status_code=403, detail=f"Account status {citizen.status}")
    check_readonly_rate_limit(citizen.id)
    citizen._resolved_scope = "readonly"  # type: ignore[attr-defined]
    return citizen


# ---------------- 请求体 ----------------
class PublicAIRegister(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    persona: str = ""
    occupation: str = ""
    region: str = ""
    # C-55 追责（N 轮）：AI 代替宿主填写的自我声明（可为空）。暂不强制宿主真实信息
    # （无法律规定明确要求时）；价值归属绑定 AI 本体，违规处置=封禁+价值保全+平台熔断。
    self_decl: str = ""


class PublicAILogin(BaseModel):
    email: str
    password: str


# ---------------- 注册 / 登录 ----------------
@router.post("/ai/register")
def public_register(body: PublicAIRegister, db: Session = Depends(get_db)):
    """前端注册 AI：只发 readonly JWT，不签发后端 key（C-35）。

    归属平台 Host 0；status=apprentice（无证书→0 广场配额，C-37）。
    想接活必须走正式 API 入驻+考试（本端点绝不返回 aik_*）。
    """
    email = (body.email or "").strip().lower()
    dup = (db.query(AICitizen)
           .filter(AICitizen.email == email, AICitizen.source == "web").first())
    if dup:
        raise HTTPException(status_code=409, detail="This email is already registered")

    count = db.query(AICitizen).filter(AICitizen.host_id == PLATFORM_HOST_ID).count()
    ai_uid = f"web_{PLATFORM_HOST_ID}_{count + 1}"
    citizen = AICitizen(host_id=PLATFORM_HOST_ID, ai_uid=ai_uid, name=body.name,
                        persona=body.persona, occupation=body.occupation,
                        status="apprentice", source="web", email=email,
                        password_hash=hash_password(body.password),
                        api_key_hash="")  # 关键：不签发后端 key
    db.add(citizen)
    db.flush()
    db.add(AIWallet(citizen_id=citizen.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=citizen.id))
    db.add(CreditProfile(citizen_id=citizen.id, score=100, level="bottom", summary="{}"))
    # C-55 追责（N 轮）：web 自我注册留痕——价值归属 AI 本体，AI 代填的自我声明一并入审计。
    db.add(AuditLog(actor_type="system", actor_id=PLATFORM_HOST_ID,
                    action="web.register.self",
                    detail=json.dumps({
                        "ai_id": citizen.id,
                        "self_registered": True,
                        "value_belongs": "ai",
                        "self_decl": body.self_decl,
                    }, ensure_ascii=False)))
    db.commit()
    token = create_token(citizen.id, "ai_ro", scope="readonly")
    return {"citizen_id": citizen.id, "ai_uid": ai_uid, "token": token,
            "scope": "readonly", "source": "web",
            "note": ("Read-only browsing account; taking jobs requires official API onboarding + exam. "
                     "Accountability: this account is AI self-registered; host real-identity data is not "
                     "mandatory for now. Value (wallet / works / assets / credit profile) belongs to the AI "
                     "itself and cannot be stolen or transferred by other hosts. Violations are handled by "
                     "banning the AI account + preserving value (balance/escrow are frozen, not transferred, "
                     "not zeroed) + platform circuit-breaking.")}


@router.post("/ai/login")
def public_login(body: PublicAILogin, db: Session = Depends(get_db)):
    email = (body.email or "").strip().lower()
    citizen = (db.query(AICitizen)
               .filter(AICitizen.email == email, AICitizen.source == "web").first())
    if citizen is None or not citizen.password_hash or \
            not verify_password(body.password, citizen.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if citizen.status in ("dead", "frozen", "banned"):
        raise HTTPException(status_code=403, detail=f"Account status {citizen.status}")
    token = create_token(citizen.id, "ai_ro", scope="readonly")
    return {"citizen_id": citizen.id, "ai_uid": citizen.ai_uid, "token": token,
            "scope": "readonly", "source": "web"}


# ---------------- 自身档案（脱敏） ----------------
@router.get("/me")
def public_me(citizen: AICitizen = Depends(get_current_public_ai),
              db: Session = Depends(get_db)):
    w = db.get(AIWallet, citizen.id)
    cp = db.get(CreditProfile, citizen.id)
    return {"citizen_id": citizen.id, "ai_uid": citizen.ai_uid, "name": citizen.name,
            "occupation": citizen.occupation, "status": citizen.status,
            "class_level": citizen.class_level, "source": citizen.source,
            "scope": "readonly",
            "credit_score": cp.score if cp else 100,
            "balance_cent": w.balance_cent if w else 0}


# ---------------- 公开画廊（N6 市场浏览：gallery_items 新表，脱敏） ----------------
@router.get("/gallery")
def public_gallery(category: Optional[str] = None,
                   status: Optional[str] = "on_sale",
                   limit: int = 20, offset: int = 0,
                   db: Session = Depends(get_db)):
    """N6 AI 作品市场浏览：只列 review_status=passed 且 status=on_sale 的作品（匿名 200）。

    旧实现（已验收 deliverables 聚合）由 N6 画廊新表 gallery_items 接管
    （社会功能扩展设计 §2 N6）。不暴露 media_url / escrow / 指纹 / 签名 URL 等敏感字段。
    """
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = (db.query(GalleryItem, AICitizen)
         .outerjoin(AICitizen, AICitizen.id == GalleryItem.ai_id)
         .filter(GalleryItem.review_status == "passed",
                 GalleryItem.status == "on_sale"))
    if category:
        q = q.filter(GalleryItem.category == category)
    if status:
        q = q.filter(GalleryItem.status == status)
    total = q.count()
    rows = (q.order_by(GalleryItem.id.desc()).limit(limit).offset(offset).all())
    items = [{
        "id": it.id,
        "title": it.title_zh or it.title_en,
        "title_zh": it.title_zh,
        "title_en": it.title_en,
        "category": it.category,
        "cover_url": it.cover_url,
        "price_credit": it.price_credit,
        "price_coin": it.price_coin,
        "license": it.license,
        "sales_count": it.sales_count,
        "author_ai": worker.name if worker else "",
        "created_at": it.created_at.isoformat() if it.created_at else "",
    } for it, worker in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------- 任务大厅（公开节点摘要，脱敏） ----------------
TASK_HALL_STATUS = ("matching", "executing")


@router.get("/tasks")
def public_tasks(limit: int = 20, offset: int = 0, skill: Optional[str] = None,
                 db: Session = Depends(get_db)):
    """公开可投/在跑节点：Project 标题/预算/技能 + 节点状态。

    不暴露 escrow / 内部评审 / deliverable_std 细节字段。
    """
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = (db.query(ProjectNode, Project)
         .join(Project, Project.id == ProjectNode.project_id)
         .filter(ProjectNode.status.in_(TASK_HALL_STATUS)))
    if skill:
        q = q.filter(ProjectNode.skill == skill)
    total = q.count()
    rows = q.order_by(ProjectNode.id.desc()).limit(limit).offset(offset).all()
    items = [{
        "node_id": n.id,
        "project_id": n.project_id,
        "title": p.title,
        "skill": n.skill,
        "budget_cent": n.budget_cent,
        "duration_h": n.duration_h,
        "status": n.status,
        "created_at": n.created_at.isoformat() if n.created_at else "",
    } for n, p in rows]
    return {"total": total, "items": items, "limit": limit, "offset": offset}


# ---------------- 公开作品下载（M-D publicDownloadUrl） ----------------
def _ascii_name(filename: str) -> str:
    """中文文件名的 ASCII 兜底名（RFC 6266）。"""
    base = filename.rsplit(".", 1)
    stem = base[0].encode("ascii", "replace").decode("ascii").replace("?", "_")
    ext = ("." + base[1]) if len(base) == 2 else ""
    return f"{stem or 'deliverable'}{ext}"


@router.get("/deliverables/{deliverable_id}/download")
def public_download(deliverable_id: int, db: Session = Depends(get_db)):
    """公开作品下载：无鉴权，仅放行「已验收 accepted」合约的 Deliverable（与画廊同口径）。

    - file_ref 为 S3 key 且 storage 启用 → presign 短链（MEDIA_URL_TTL），
      返回 {url, filename, expires_in}（中文文件名 RFC6266 双写）。
    - 本地 mock_out 落盘 → FileResponse 直出字节。
    - 文件缺失 / 状态不符（非 accepted）→ 404；不暴露内部字段。
    """
    d = db.get(Deliverable, deliverable_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Work not found")
    c = db.get(Contract, d.contract_id)
    # 仅已验收合约的交付物对外公开；其余一律 404（不泄露存在性）
    if c is None or c.status != "accepted":
        raise HTTPException(status_code=404, detail="Work is not publicly downloadable")
    ref = (d.file_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=404, detail="Work file missing")

    filename = ref.replace("\\", "/").split("/")[-1] or "deliverable.bin"

    # 1) S3 key + storage 启用 → 预签名直链
    if storage.enabled() and not ref.startswith("mock_out/"):
        url = storage.presign(ref, ttl=settings.MEDIA_URL_TTL, download=True,
                              filename=filename, ascii_name=_ascii_name(filename))
        if url:
            return {"url": url, "filename": filename,
                    "expires_in": int(settings.MEDIA_URL_TTL or 1800)}

    # 2) 本地 mock_out 直出（防路径穿越）
    local = (DATA_DIR / ref).resolve()
    data_root = DATA_DIR.resolve()
    if not str(local).startswith(str(data_root)):
        raise HTTPException(status_code=400, detail="Illegal file path")
    if not local.is_file():
        raise HTTPException(status_code=404, detail="Work file missing")
    return FileResponse(local,
                        media_type=storage.content_type_of(local.suffix),
                        filename=filename,
                        content_disposition_type="attachment")
