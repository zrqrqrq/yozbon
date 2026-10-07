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
"""MVT 楔子服务：宣传视频成片「需求→编排→出片→下载→支付」最短链路最小可跑骨架。

设计原则（刻意与文明/经济体系解耦）：
- 不 import / 不触碰 wallet、credit、market、contract、escrow、citizen 经济/治理模块；
- 仅复用 `platform_compute`（平台内置算力：六链路出片 + LLM 通道）做真实出片；
- 支付为「一次性 mock 解锁下载」：只置 pay_unlocked 位，不入钱包、不发现金、不接经济层。

链路阶段（status）与埋点（events 每条 {ts, stage, request_id, detail}）：
  queued   → 需求受理（提交时）
  scripting→ 编排（LLM 把诉求转分镜脚本）
  rendering→ 出片（platform_compute.run 六链路，mock 通道即时返回）
  done     → 出片完成（产物落盘 + sha256 指纹）
  paid     → 支付解锁（下载闸门打开）
  failed   → 任一步失败（error 记原因）

测试可测性：出片经 platform_compute，无 key 环境自动走 mock（产物落盘 + 指纹，
与真链路同结构）；LLM 经 settings.LLM_PROVIDER=echo 返回确定性文本，无外部请求。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import WedgeJob
from . import platform_compute

logger = logging.getLogger("aijuhe.wedge")

# 楔子成片售价（分）：真实资金入口；默认 1990=¥19.9，可经 settings.WEDGE_PRICE_CENT 覆盖。
_DEFAULT_PRICE_CENT = 1990
# 楔子支持的出片链路（仅宣传视频相关，刻意收窄，不做全能力）。
WEDGE_KINDS = ("video_civil", "video_openvdn", "image")

# 楔子出片默认参数（复用 RunVerseHub 视频链路的口径）：
# duration=8s（H3 标准短片段），16:9 横幅，480p 性价比档。
_WEDGE_DEFAULT_PARAMS = {
    "video_civil":   {"duration": 8, "aspect": "16:9", "quality": "480p"},
    "video_openvdn": {"duration": 8, "aspect": "16:9", "quality": "480p"},
    "image":         {"width": 1024, "height": 1024},
}


def _price_cent() -> int:
    return int(getattr(settings, "WEDGE_PRICE_CENT", _DEFAULT_PRICE_CENT) or _DEFAULT_PRICE_CENT)


def _now() -> datetime:
    return datetime.utcnow()


# ---------------- 埋点 ----------------
def _emit(job: WedgeJob, stage: str, request_id: str = "", detail: str = "") -> None:
    """往 job.events 追加一条里程碑事件（JSON 数组）。

    request_id 与 G-03/G-05 同源（RequestIDMiddleware 注入 request.state.request_id），
    使每次交付的每一跳都能用同一个 id 串起访问日志 / span / 业务里程碑。
    """
    try:
        ev = json.loads(job.events or "[]")
        if not isinstance(ev, list):
            ev = []
    except (json.JSONDecodeError, TypeError):
        ev = []
    ev.append({
        "ts": _now().isoformat(),
        "stage": stage,
        "request_id": request_id or job.request_id or "",
        "detail": detail[:500],
    })
    job.events = json.dumps(ev, ensure_ascii=False)


def _touch(job: WedgeJob, status: str) -> None:
    job.status = status
    job.updated_at = _now()


# ---------------- 编排（scripting）----------------
def _compose_script(brief: str) -> str:
    """编排第一步：用 LLM 把用户诉求转成分镜/画面提示词脚本（楔子的「AI 员工」最小化身）。

    经 platform_compute.complete（echo 通道返回确定性文本），不触达经济/治理 AI。
    """
    prompt = (
        "You are a brand promo-video director. Turn the brief below into 4 shot "
        "descriptions, one per line, format 'Shot N: <visual description>'. Brief:\n"
        + brief
    )
    return platform_compute.complete(prompt, system="Output exactly 4 shots, no extra explanation.")


# ---------------- 主流程：编排 + 出片 ----------------
def run_pipeline(db: Session, job: WedgeJob, request_id: str = "") -> WedgeJob:
    """同步执行「编排→出片」并把产物落到 job。

    MVT 阶段同步执行（mock 通道即时返回，便于端到端可跑）；
    生产可把本函数投递到 async_queue 异步执行，job 先返回 queued、由 worker 推进。
    """
    rid = request_id or job.request_id
    # 1) 编排 scripting
    _touch(job, "scripting")
    _emit(job, "scripting", rid, "开始编排：生成宣传视频分镜脚本")
    db.commit()
    try:
        script = _compose_script(job.brief or "")
    except Exception as exc:  # noqa: BLE001
        job.error = f"script error: {exc}"
        _touch(job, "failed")
        _emit(job, "failed", rid, job.error)
        db.commit()
        return job
    job.script = script[:4000]
    _emit(job, "scripted", rid, "编排完成，分镜脚本已生成")
    db.commit()

    # 2) 出片 rendering（复用平台算力；无 key 环境走 mock）
    #    传入完整视频/图片参数（duration/aspect/quality），使 _node_info_list
    #    能把 {video_width}/{video_height}/{frames} 等 token 正确注入工作流节点。
    _touch(job, "rendering")
    _emit(job, "rendering", rid, f"开始出片：channel={job.kind}")
    db.commit()
    render_params = dict(_WEDGE_DEFAULT_PARAMS.get(job.kind, {}))
    # 支持 job.params 字段覆盖（前端可选传入 duration/quality 等，JSON dict）
    if getattr(job, "params", None):
        try:
            user_params = json.loads(job.params) if isinstance(job.params, str) else job.params
            if isinstance(user_params, dict):
                render_params.update(user_params)
        except (json.JSONDecodeError, TypeError):
            pass
    res = platform_compute.run(job.kind, script or job.brief or "", params=render_params)
    status = res.get("status")
    files = res.get("files") or []
    if status != "succeeded" or not files:
        job.error = (res.get("error") or "出片失败：未返回产物")[:500]
        _touch(job, "failed")
        _emit(job, "failed", rid, job.error)
        db.commit()
        return job
    f0 = files[0]
    job.file_ref = f0.get("file_ref", "")
    job.fingerprint = f0.get("fingerprint", "")
    job.size = int(f0.get("size", 0) or 0)
    _touch(job, "done")
    _emit(job, "done", rid,
          f"出片完成：{job.file_ref} fp={job.fingerprint[:12]} size={job.size}")
    db.commit()
    return job


# ---------------- 创建（queued）----------------
def create_job(db: Session, host_id: int, brief: str,
               kind: str = "video_civil", request_id: str = "",
               params: dict | None = None) -> WedgeJob:
    """受理需求并建单（status=queued），记首条埋点。"""
    if kind not in WEDGE_KINDS:
        kind = "video_civil"
    job = WedgeJob(
        host_id=host_id,
        brief=(brief or "")[:4000],
        kind=kind,
        status="queued",
        price_cent=_price_cent(),
        request_id=request_id,
        events="[]",
        params=json.dumps(params) if params else "",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _emit(job, "queued", request_id, f"需求受理：kind={kind} len(brief)={len(brief or '')}")
    db.commit()
    return job


# ---------------- 支付解锁（paid，一次性 mock，不碰经济层）----------------
def pay(db: Session, job: WedgeJob, request_id: str = "") -> WedgeJob:
    """一次性 mock 支付解锁下载位。

    刻意不调用 wallet/credit：只置 pay_unlocked=1 并记流水号，作为「交付闸门 +
    计费点」的最小验证。真实支付渠道（Creem/MoR）后续接入时，仅替换本函数内部，
    对外的下载闸门语义不变。
    """
    if job.status != "done":
        raise ValueError("成片尚未就绪，无法支付")
    if job.pay_unlocked == 1:
        return job  # 幂等：已解锁直接返回
    job.pay_unlocked = 1
    job.pay_ref = f"wedge_mock_{uuid.uuid4().hex}"
    job.paid_at = _now()
    _emit(job, "paid", request_id or job.request_id, f"支付解锁：{job.pay_ref}")
    db.commit()
    return job


# ---------------- 序列化 ----------------
def to_dict(job: WedgeJob, include_events: bool = False) -> dict:
    d = {
        "id": job.id,
        "status": job.status,
        "kind": job.kind,
        "brief": job.brief,
        "script": job.script,
        "price_cent": job.price_cent,
        "paid": int(job.pay_unlocked or 0),
        "downloadable": (job.status == "done" and int(job.pay_unlocked or 0) == 1),
        "fingerprint": job.fingerprint,
        "size": job.size,
        "error": job.error,
        "request_id": job.request_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "paid_at": job.paid_at.isoformat() if job.paid_at else None,
    }
    if include_events:
        try:
            d["events"] = json.loads(job.events or "[]")
        except (json.JSONDecodeError, TypeError):
            d["events"] = []
    return d
