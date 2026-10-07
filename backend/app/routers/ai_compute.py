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
"""自主工作流端点（蓝图 §1.0：AI 公民"自主干活"的真实现法）。

POST /api/ai/contracts/{id}/execute —— 已中标并签约（executing）的 AI 公民，
凭 AI key 自主触发：compute.exec（平台算力池 / worker_bridge 两通道）→
成功后自动版本化交付（escrow.deliver，file_ref/fingerprint 来自执行结果）。

全链：中标(bid) → 托管签约(sign_contract) → 本端点执行+交付 →
买方/宿主验收(acceptance/host_acceptance) → 结算(fulfill_contract)。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import compute, escrow, wallet
from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen, Contract

router = APIRouter(prefix="/api/ai", tags=["ai-compute"])

# 合法执行通道枚举（六链路 + LLM）——未知 kind 一律拒绝，拒绝"封闭枚举=失败"的反面：
# 平台通道开放扩展（platform_compute.KINDS 可注册），但本次执行必须命中已登记通道。
ALLOWED_KINDS = {"image", "hd_image", "img2img", "music",
                 "video_civil", "video_openvdn", "llm"}


class ExecuteBody(BaseModel):
    kind: str = Field(..., min_length=1, max_length=32)
    prompt: str = ""
    params: dict = {}


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/contracts/{contract_id}/execute")
def execute(contract_id: int, body: ExecuteBody,
            ai: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    """AI 公民自主执行合约：校验身份与状态 → compute.exec → 成功即自动交付。"""
    # ---- 合约与身份校验（恶意主体视角：非履约方/越态调用一律拒绝） ----
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Contract {contract_id} not found")
    if c.worker_id != ai.id:
        raise HTTPException(status_code=403, detail="Not a party to this contract; no permission to execute")
    if c.status != "executing":
        raise _bad(EscrowStateError(f"Contract status={c.status}; only executing can execute (if delivered, do not execute again)"))
    if body.kind not in ALLOWED_KINDS:
        raise _bad(EscrowStateError(f"Unknown execution channel kind={body.kind}"))

    # ---- 统一执行（平台算力池 / worker_bridge 两通道，接口一致） ----
    try:
        result = compute.exec(db, ai, body.kind, body.prompt, body.params)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise _bad(e)

    # ---- 终态成功 → 自动版本化交付（file_ref/fingerprint 来自执行结果） ----
    if result.get("status") == "succeeded" and result.get("files"):
        f = result["files"][0]
        if not f.get("fingerprint"):
            db.rollback()
            raise _bad(EscrowStateError("Execution succeeded but the artifact lacks a fingerprint; delivery rejected"))
        try:
            d = escrow.deliver(db, ai, contract_id, f.get("file_ref", ""), f["fingerprint"])
        except (escrow.EscrowError, wallet.WalletError) as e:
            db.rollback()
            raise _bad(e)
        db.commit()
        return {"ok": True, "contract_id": contract_id, "status": "delivered",
                "deliverable": {"version": d.version, "file_ref": f.get("file_ref", ""),
                                "fingerprint": f["fingerprint"]},
                "compute": result}

    # ---- 运行中 / worker 委派：不交付，合约保持 executing 等待 ----
    db.commit()
    return {"ok": False, "contract_id": contract_id,
            "status": result.get("status", "running"), "compute": result}


class EscrowStateError(Exception):
    """端点内联的状态校验异常（避免与 B 线 EscrowError 混用）。"""
