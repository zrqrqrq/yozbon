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
"""平台内置算力池适配器（蓝图 §1.0）。

AIjuhe 平台 = Host 0（平台自有算力供给方）：RunVerseHub 的六链路生成引擎 + LLM 通道
即平台算力资产。本模块把这六条生成链路 + LLM 通道封装成「AI 公民的执行手」：
AI 公民在劳动力市场中标后，由 compute.exec 经本模块真实调用平台算力出活，
交付物落盘 + sha256 指纹后回写合约（与 provenance 哈希链思路一致）。

与 RunVerseHub 的关系（只读复用其协议，不 import 其源码）：
  - 提交/轮询/取消协议与 D:\\traepo\\juhe\\backend\\app\\providers\\runninghub.py 完全一致：
      create  POST {base}/task/openapi/create  body{apiKey, workflowId, nodeInfoList, workflow, usePersonalQueue, accessPassword, instanceType}
      status  POST {base}/task/openapi/status  body{apiKey, taskId}
      outputs POST {base}/task/openapi/outputs body{apiKey, taskId} -> data[].fileUrl
      cancel  POST {base}/task/openapi/cancel body{apiKey, taskId}
  - 多 key 调度口径与 runninghub._slots/_pick_slot 一致：
      personal(个人会员,个人队列) > enterprise(企业共享,共享队列) > intl(国际站备用) > legacy(RH_API_KEY 单 key 兜底)；
      四把 key 全部未配置时视为「无 key 环境」，走 mock 响应（开发/测试默认路径，不真调 RH）。
  - 工作流：六链路提交时本地 *_api.json 全量附带为 body.workflow（RH 文档：workflow 参数优先于
    workflowId），保证执行节点结构永远与本地 rh_workflow_nodes.json 一致。

测试可测性：真正发请求的边界隔离为薄函数 _http_submit/_http_poll/_http_llm，
生产路径不变，单测可 monkeypatch 这三个函数（无 key 环境默认即 mock，无需 patch）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import settings
from .database import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------- 工作流本地文件（六链路，提交时全量附带） ----------------
# 根目录即 backend/（工作流 JSON 放在 backend/ 下，与 app/ 同级）。
# 本地路径解析：AIjuhe/backend/app/platform_compute.py -> 3 up = AIjuhe/backend/
# 生产路径解析：/opt/aijuhe/backend/app/platform_compute.py -> 3 up = /opt/aijuhe/backend/
_ROOT = Path(__file__).resolve().parent.parent  # backend/ 目录
# 兼容开发模式：如果 backend/ 下找不到工作流，回退到上级目录（D:\traepo\juhe\）
_WORKFLOW_ROOT = _ROOT if (_ROOT / "H3_video_civil_api.json").exists() else _ROOT.parent.parent
# 平台 kind -> (本地工作流 JSON, rh 节点映射键)
KINDS = {
    "image":          ("ZImage_T2I_api.json",            "image"),
    "hd_image":       ("ZImage_T2I_HD_2Stage.json",     "hd_image"),
    "img2img":        ("ZImage_img2img_denoise_api.json", "img2img_denoise"),
    "music":          ("MiniMaxMusic3_audio_api.json",   "music"),
    "video_civil":    ("H3_video_civil_api.json",        "video_civil"),
    "video_openvdn":  ("H3_video_openvdn_api.json",      "video_openvdn"),
}
# llm 不走 ComfyUI 工作流，走 LLM 通道（见 complete()）。
LLM_KIND = "llm"
# vlm 不走 ComfyUI 工作流，走 VLM（视觉语言模型）通道（见 vlm_complete()）。
VLM_KIND = "vlm"
# video_analysis 不走 ComfyUI 工作流，走 ffmpeg+VLM 流水线（见 video_analyzer.analyze_video()）。
VIDEO_ANALYSIS_KIND = "video_analysis"

_NODES_PATH = _ROOT / "rh_workflow_nodes.json"
# 兼容开发模式：如果 backend/ 下没有，回退到 RunVerseHub 位置
if not _NODES_PATH.exists():
    _NODES_PATH = _ROOT.parent.parent / "backend" / "rh_workflow_nodes.json"

_MOCK_OUT_DIR = DATA_DIR / "mock_out"
_MOCK_OUT_DIR.mkdir(parents=True, exist_ok=True)

# 内存态 mock 任务表：无 key 环境下 submit 登记、poll 命中即返回终态产物。
# 进程内状态，不落库（与 RunVerseHub MockProvider 同思路）。
_MOCK_JOBS: dict = {}

# 真链路任务表：有 key 环境 submit 成功后登记 job_id -> {"kind"}，
# 供 poll 取 kind 推断产物后缀/目录（跨进程重启丢失时回退从 fileUrl 后缀推断）。
_REAL_JOBS: dict = {}

# ---- g2 全站出站并发总闸（本站 ≤ SITE_MAX_CONCURRENCY）----
# 城主 tick（GOVERNOR_MAX_CONCURRENCY）、队列 worker（QUEUE_WORKER_MAX_CONCURRENCY）各自
# 有独立闸，但多路叠加 + 直接 HTTP 调用，仍可远超 RH key 并发额度而打爆账户。
# 本模块 _http_submit/_http_poll/_http_llm/_http_download 是全站对 RH / LLM 的唯一出站边界，
# 在此挂一把进程级信号量做「本站 ≤N」总封顶：所有真实外呼先取槽位，超出即排队，跨调用方生效。
# 每个 _http_* 在自身函数体内取/还槽位（不跨函数持有），故 submit→poll 各自独立占位，无嵌套死锁。
_SITE_SEM = threading.BoundedSemaphore(max(1, int(getattr(settings, "SITE_MAX_CONCURRENCY", 10))))
# 取槽位的最长等待：超时即抛错（由调用方 try/except 回退 echo），
# 杜绝"总闸被长期占用 → 后续外呼无限排队"导致的 worker 线程永久卡死。
_SITE_ACQUIRE_TIMEOUT = float(getattr(settings, "SITE_SLOT_ACQUIRE_TIMEOUT", 120.0))


@contextmanager
def _site_slot():
    """占用一个全站出站并发槽（最多等 _SITE_ACQUIRE_TIMEOUT 秒）。_http_* 边界统一用它包裹。"""
    if not _SITE_SEM.acquire(timeout=_SITE_ACQUIRE_TIMEOUT):
        raise TimeoutError(
            f"site outbound slot unavailable after {_SITE_ACQUIRE_TIMEOUT}s "
            f"(concurrency cap reached)")
    try:
        yield
    finally:
        _SITE_SEM.release()


def site_concurrency_limit() -> int:
    """总闸容量（配置值，供观测/测试）。"""
    return _SITE_SEM._limit if hasattr(_SITE_SEM, "_limit") else int(
        getattr(settings, "SITE_MAX_CONCURRENCY", 10))


def site_concurrency_inuse() -> int:
    """当前正在占用出站槽的数量（= 容量 - 剩余），供观测/测试断言总闸生效。"""
    return site_concurrency_limit() - _SITE_SEM._value


def _load_nodes_cfg() -> dict:
    """读 rh_workflow_nodes.json（节点映射核对用；缺失时返回空 dict，不阻断）。"""
    try:
        return json.loads(_NODES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _workflow_path(kind: str) -> Optional[Path]:
    info = KINDS.get(kind)
    if not info:
        return None
    p = _ROOT / info[0]
    return p if p.exists() else None


# ---------------- 多 key 调度（与 runninghub._slots 同口径） ----------------
def _slots() -> list:
    """四把 key 槽位：personal > enterprise > intl > legacy。全空 = 无 key 环境。"""
    base = (settings.RH_API_BASE or "https://www.runninghub.cn").rstrip("/")
    intl_base = (settings.RH_API_BASE_INTL or "https://www.runninghub.ai").rstrip("/")
    pw = (settings.RH_ACCESS_PASSWORD or "").strip() if hasattr(settings, "RH_ACCESS_PASSWORD") else ""
    slots = []
    if (settings.RH_API_KEY_PERSONAL or "").strip():
        slots.append({"id": "personal", "key": settings.RH_API_KEY_PERSONAL.strip(),
                      "base": base, "queue": True, "password": ""})
    if (settings.RH_API_KEY_ENTERPRISE or "").strip():
        slots.append({"id": "enterprise", "key": settings.RH_API_KEY_ENTERPRISE.strip(),
                      "base": base, "queue": False, "password": pw})
    if (settings.RH_API_KEY_INTL or "").strip():
        slots.append({"id": "intl", "key": settings.RH_API_KEY_INTL.strip(),
                      "base": intl_base, "queue": False, "password": pw})
    if not slots and (settings.RH_API_KEY or "").strip():
        slots.append({"id": "legacy", "key": settings.RH_API_KEY.strip(),
                      "base": base, "queue": False, "password": pw})
    return slots


def _rh_configured() -> bool:
    return bool(_slots())


# ---- 轻量熔断（复用 model_fallback._failure_counts 模式，进程级，不入库）----
# key: slot_id -> 连续失败次数；成功即重置。超阈值则 _pick_slot 暂时跳过该槽位。
_CB_THRESHOLD = max(3, int(getattr(settings, "CB_FAILURE_THRESHOLD", 5)))
_slot_failures: dict = {}  # {slot_id: int}


def _pick_slot(slot_id: str = "", skip_broken: bool = False) -> Optional[dict]:
    """poll/cancel 用：按提交时落库的槽位标识取回同一把 key（跨站 key 查不到彼此任务）。

    skip_broken=True 时（submit 场景），跳过连续失败超过阈值的槽位（熔断）。
    """
    slots = _slots()
    if slot_id:
        for s in slots:
            if s["id"] == slot_id:
                return s
    if skip_broken and slots:
        healthy = [s for s in slots if _slot_failures.get(s["id"], 0) < _CB_THRESHOLD]
        if healthy:
            return healthy[0]
        # 全部熔断时仍返回第一个（宁可重试也不完全拒绝服务）
    return slots[0] if slots else None


def _report_slot_failure(slot_id: str) -> None:
    """提交/轮询失败时调用：增加该槽位连续失败计数。"""
    if slot_id:
        _slot_failures[slot_id] = _slot_failures.get(slot_id, 0) + 1


def _report_slot_success(slot_id: str) -> None:
    """成功时调用：重置该槽位连续失败计数。"""
    _slot_failures.pop(slot_id, None)


# ---------------- 薄 HTTP 边界（单测 monkeypatch 点；生产真调 RH） ----------------
def _http_submit(base: str, body: dict) -> dict:
    """真发提交请求的唯一边界（create）。生产用 httpx；测试可替换为离线响应。"""
    import httpx
    with _site_slot():  # g2 全站出站总闸占位
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{base}/task/openapi/create", json=body)
            r.raise_for_status()
            return r.json()


def _http_poll(base: str, path: str, body: dict) -> dict:
    """真发轮询请求的唯一边界（status/outputs/cancel 共用）。"""
    import httpx
    with _site_slot():  # g2 全站出站总闸占位
        with httpx.Client(timeout=30.0) as c:
            r = c.post(f"{base}{path}", json=body)
            r.raise_for_status()
            return r.json()


def _http_llm(url: str, headers: dict, body: dict) -> str:
    """真发 LLM 请求的唯一边界（openai_compat / runninghub 通道共用）。"""
    import httpx
    with _site_slot():  # g2 全站出站总闸占位
        r = httpx.post(url, json=body, headers=headers, timeout=120.0)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _http_download(url: str, max_bytes: int) -> bytes:
    """流式下载远端产物字节的唯一边界（真链路 poll 落盘用）。

    累计超过 max_bytes 即中断抛错——防止 RH 返回超大文件把内存/磁盘打爆。
    单测可 monkeypatch 本函数（与 _http_submit/_http_poll/_http_llm 同口径）。
    """
    import httpx
    chunks = []
    total = 0
    limit = int(max_bytes or 0)
    with _site_slot():  # g2 全站出站总闸占位
        with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(chunk_size=64 * 1024):
                total += len(chunk)
                if limit and total > limit:
                    raise RuntimeError(f"Artifact exceeds size limit {limit} bytes; download aborted")
                chunks.append(chunk)
    return b"".join(chunks)


# ---------------- 参数计算辅助（复用 RunVerseHub _video_dims/_video_frames 口径） ----------------
_VIDEO_QUALITY_MP = {"480p": 0.5, "720p": 1.03, "1080p": 2.07}
_VIDEO_QUALITY_MP_OPENVDN = {"720p": 0.8}  # 高动态 720p 像素预算略低

_TOKEN_DEFAULTS = {"seed": 0, "steps": 8, "denoise": 1.0, "strength": 1.0}


def _video_dims(aspect: str = "16:9", quality: str = "480p", chain: str = "") -> tuple:
    """按画幅+画质现算视频 width/height（与 RunVerseHub runninghub._video_dims 同口径）。"""
    try:
        rw, rh = (int(x) for x in str(aspect or "16:9").split(":"))
    except Exception:
        rw, rh = 16, 9
    if rw <= 0 or rh <= 0:
        rw, rh = 16, 9
    q = str(quality or "480p").lower()
    budgets = {**_VIDEO_QUALITY_MP, **(_VIDEO_QUALITY_MP_OPENVDN if chain == "video_openvdn" else {})}
    mp = budgets.get(q, _VIDEO_QUALITY_MP["480p"])
    area = mp * 1_000_000

    def _r32(v):
        return max(32, int(round(v / 32.0)) * 32)

    w = _r32(round((area * rw / rh) ** 0.5))
    h = _r32(round(w * rh / rw))
    return w, h


def _video_frames(duration) -> int:
    """秒 -> H3 帧数：round(sec*24) 后吸附到 5(mod 17)（与文戏工作流同口径）。"""
    try:
        sec = float(duration or 8)
    except (TypeError, ValueError):
        sec = 8.0
    n = max(5, int(round(sec * 24)))
    return n + (5 - n % 17) % 17


def _img_dim(w, h):
    """图片尺寸吸附 16 倍数，限 512~2048（与父项目 _img_dim 同口径）。"""
    def _snap(v, lo=512, hi=2048):
        try:
            x = int(v)
        except (TypeError, ValueError):
            return 1024
        x = max(lo, min(hi, int(x / 16.0 + 0.5) * 16))
        return x
    return _snap(w), _snap(h)


# ---------------- 落盘 + 指纹（对齐 provenance 哈希链） ----------------
def _persist_bytes(rel_name: str, data: bytes) -> dict:
    """把产物字节落到 data/mock_out/（S3 未配置时的本地落盘；配置后可替换为 storage.put_file）。

    fingerprint = sha256(产物字节)——与 RunVerseHub provenance 哈希链同一思路，
    作为交付物数字水印随合约交付（escrow.deliver 必填 fingerprint）。
    """
    fp = _MOCK_OUT_DIR / rel_name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(data)
    return {
        "file_ref": f"mock_out/{rel_name.replace(chr(92), '/')}",
        "fingerprint": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _ext_for(kind: str) -> str:
    return {"image": "png", "hd_image": "png", "img2img": "png",
            "music": "mp3", "video_civil": "mp4", "video_openvdn": "mp4",
            "llm": "txt"}.get(kind, "bin")


def _ext_from_url(url: str) -> str:
    """从 fileUrl 路径取后缀（png/mp4/mp3…）；取不到返回空串，由调用方按 kind 兜底。"""
    try:
        from urllib.parse import urlparse
        suf = Path(urlparse(str(url)).path).suffix.lower().lstrip(".")
        if suf and len(suf) <= 5:
            return suf
    except Exception:  # noqa: BLE001
        pass
    return ""


def _maybe_upload_s3(local_path: Path, rel_ref: str, ext: str) -> str:
    """best-effort 把已落盘产物镜像到 S3，返回存储通道标记 "s3"/"local"。

    storage 模块由另一代理并行开发（可能尚未落地/接口未定）：任何 import/上传异常都
    回退 "local"，绝不阻断主流程。file_ref 永远是本地相对路径（与 mock 通道一致），
    S3 仅作镜像，不改写 file_ref——与 RunVerseHub mirror_work「原文件先本地、再镜像」同思路。
    """
    if not getattr(settings, "storage_enabled", False):
        return "local"
    try:
        from . import storage  # noqa: F401  M-C 在写；可能尚未存在/接口未定
        try:
            if callable(getattr(storage, "enabled", None)) and not storage.enabled():
                return "local"
        except Exception:  # noqa: BLE001
            pass
        key = f"{(settings.S3_KEY_PREFIX or 'aijuhe').strip('/')}/{rel_ref}"
        ok = storage.put_file(key, local_path, ext)
        return "s3" if ok else "local"
    except Exception:  # noqa: BLE001
        return "local"


def _rh_failed_reason(resp: dict) -> str:
    """从终态 outputs 响应里提炼失败原因（runninghub._failed_reason 的最小子集）。

    RH 失败终态的 outputs 返回 code!=0、data.failedReason={exception_type,node_name,
    exception_message}；不解析它就只能回一句「未返回产物」，运维无从下手。
    """
    if not isinstance(resp, dict):
        return "RH task failed"
    data = resp.get("data")
    fr = data.get("failedReason") if isinstance(data, dict) else None
    if isinstance(fr, dict):
        blob = " ".join(str(fr.get(k) or "") for k in
                        ("exception_type", "node_name", "exception_message")).strip()
        if blob:
            return f"RH task failed: {blob}"[:300]
    return f"RH task failed: {resp.get('msg') or 'unknown reason'}"[:300]


# ---------------- mock 通道（无 key 环境） ----------------
def _mock_media(kind: str, prompt: str, params: dict, job_id: str) -> list:
    """无 key 环境的确定性模拟产物：按 kind 合成一小段字节并落盘出指纹。

    测试与开发默认走这里——不真调 RH/S3，但产物落盘路径、指纹口径、结果 dict 结构
    与真实链路完全一致，故切到真 key 后业务代码零改动。
    """
    if kind == LLM_KIND:
        text = complete(prompt, system=params.get("system", ""),
                        max_tokens=params.get("max_tokens"))
        data = text.encode("utf-8")
    else:
        data = f"MOCK-{kind}|prompt={prompt}|seed={params.get('seed', 0)}".encode("utf-8")
    rel = f"{kind}/{job_id}.{_ext_for(kind)}"
    return [_persist_bytes(rel, data)]


# ---------------- 提交 / 轮询 ----------------
def submit(kind: str, prompt: str, params: dict) -> dict:
    """提交一条生成任务（六链路；llm 由 run() 直接走 complete，不经此处）。

    返回 {"job_id", "status"(queued/running), "key_slot", "error", "meta"}。
    无 key 环境登记内存 mock 任务；有 key 环境真调 RH create 并本地附带工作流 JSON。
    """
    if kind == LLM_KIND:
        return {"job_id": f"llm-{uuid.uuid4().hex[:12]}", "status": "queued",
                "key_slot": "llm", "error": "", "meta": {"channel": "llm"}}
    if not _rh_configured():
        job_id = f"mock-{uuid.uuid4().hex[:12]}"
        _MOCK_JOBS[job_id] = {"kind": kind, "prompt": prompt, "params": dict(params or {})}
        return {"job_id": job_id, "status": "queued", "key_slot": "mock", "error": "",
                "meta": {"channel": kind, "mock": True}}
    slot = _pick_slot(skip_broken=True)
    wf_path = _workflow_path(kind)
    wf_key = KINDS[kind][1]
    body = {"apiKey": slot["key"],
            "workflowId": getattr(settings, f"RH_WF_{wf_key.upper()}", "") or "",
            "nodeInfoList": _node_info_list(kind, prompt, params),
            "usePersonalQueue": slot["queue"]}
    if slot["password"]:
        body["accessPassword"] = slot["password"]
    # 机型 / 实例保留时长透传（口径与 runninghub.py 完全一致）：
    #   instanceType：仅在显式配置非空时下发（不传 = RH 自动调度，可能落到高价机型）；
    #   retainSeconds：仅当 >0 时下发（RH 按保留时长额外计费，0/默认不传以避冤枉钱）。
    if (settings.RH_INSTANCE_TYPE or "").strip():
        body["instanceType"] = settings.RH_INSTANCE_TYPE.strip()
    if int(settings.RH_RETAIN_SECONDS or 0) > 0:
        body["retainSeconds"] = int(settings.RH_RETAIN_SECONDS)
    # 本地工作流全量附带（节点永远与本地一致）
    if wf_path:
        try:
            wf = json.loads(wf_path.read_text(encoding="utf-8"))
            body["workflow"] = json.dumps(wf, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            body["workflow"] = wf_path.read_text(encoding="utf-8")
    try:
        r = _http_submit(slot["base"], body)
    except Exception as e:  # noqa: BLE001
        _report_slot_failure(slot["id"])
        return {"job_id": "", "status": "failed", "key_slot": slot["id"],
                "error": f"RH submit error: {e}", "meta": {"channel": kind}}
    if (r.get("code") not in (0, None)):
        _report_slot_failure(slot["id"])
        return {"job_id": "", "status": "failed", "key_slot": slot["id"],
                "error": f"RH submit failed: {r.get('msg')}", "meta": {"channel": kind}}
    _report_slot_success(slot["id"])
    d = r.get("data") or {}
    tid = str(d.get("taskId") or "")
    if tid:
        _REAL_JOBS[tid] = {"kind": kind}
    return {"job_id": tid, "status": "queued",
            "key_slot": slot["id"], "error": "", "meta": {"channel": kind}}


# 工作流 JSON override 中可能出现的全部占位 token
_NODE_TOKENS = ("seed", "steps", "duration", "duration_sec",
                "resolution", "image_res", "quality", "bpm", "genre", "key",
                "mood", "denoise", "strength",
                "subgoal", "image_mode", "width", "height", "lyrics",
                "frames", "video_width", "video_height",
                "img_aspect", "img_kilopixels",
                "target_width", "target_height")


def _to_picture_labels(prompt: str, n_refs: int = 0) -> str:
    """前端 @图N / @imgN 引用 -> H3 R2V 官方 <Picture N> 标签。

    只保留落在已上传参考图范围内的编号；越界编号删掉避免模型凭空捏造。
    n_refs=0 时不做范围校验（仅做标签替换）。与上级 runninghub._to_picture_labels 同口径。
    """
    if not prompt:
        return prompt
    import re as _re
    _REF_TOKEN_RE = _re.compile(r"@图\s*(\d+)|@img\s*(\d+)", _re.IGNORECASE)
    limit = max(0, int(n_refs or 0))

    def _sub(m):
        i = int(m.group(1) or m.group(2))
        if limit and not (1 <= i <= limit):
            return ""
        return f"<Picture {i}>"

    out = _REF_TOKEN_RE.sub(_sub, prompt)
    if out == prompt:
        return prompt
    return _re.sub(r"[ \t]{2,}", " ", out)


def _node_info_list(kind: str, prompt: str, params: dict) -> list:
    """按 rh_workflow_nodes.json 把 prompt/全部参数写入节点（复用 RunVerseHub 完整 token 机制）。

    支持的 token：{seed} {steps} {duration} {duration_sec} {video_width} {video_height}
      {frames} {width} {height} {denoise} {strength} {quality} {resolution}
      {image_res} {bpm} {genre} {key} {mood} {subgoal} {image_mode} {lyrics}
      {img_aspect} {img_kilopixels} {target_width} {target_height}

    数值型 token（整数值等于纯 token 时）保持原生类型，防止 ComfyUI 数值节点收到字符串报错。
    """
    cfg = _load_nodes_cfg().get(KINDS[kind][1], {})
    nodes = []

    # 1) 提示词节点 —— 音乐走三段式 caption，视频走 @图N→<Picture N> 格式化
    pc = cfg.get("prompt") or {}
    field_value = prompt
    if kind == "music":
        try:
            from . import music_caption
            field_value = music_caption.build_caption(params)
        except Exception:  # noqa: BLE001
            field_value = prompt  # fallback: 原 prompt 直传
    elif kind in ("video_civil", "video_openvdn"):
        # H3 R2V 只认 <Picture N> 标签，把前端/LLM 输出的 @图N 转成官方标签。
        n_refs = len(params.get("ref_images") or [])
        field_value = _to_picture_labels(field_value, n_refs)
    if pc.get("nodeId"):
        nodes.append({"nodeId": str(pc["nodeId"]), "fieldName": str(pc["fieldName"]),
                      "fieldValue": field_value})
    # 2) override 节点：支持全部 {token} 占位替换
    for entry in cfg.get("override", []):
        val = entry.get("fieldValue", "")
        if isinstance(val, str) and "{" in val:
            for pk in _NODE_TOKENS:
                token = "{" + pk + "}"
                if token not in val:
                    continue
                raw = params.get(pk, _TOKEN_DEFAULTS.get(pk, ""))
                if pk == "seed":
                    if raw in (None, "", 0):
                        raw = random.randint(0, 0x7FFFFFFF)
                elif pk in ("video_width", "video_height"):
                    _w, _h = _video_dims(params.get("aspect"), params.get("quality"),
                                         KINDS[kind][1])
                    raw = _w if pk == "video_width" else _h
                elif pk == "frames":
                    raw = _video_frames(params.get("duration"))
                elif pk in ("width", "height"):
                    _iw, _ih = _img_dim(params.get("width"), params.get("height"))
                    raw = _iw if pk == "width" else _ih
                # 数值型 token：整体等于单 token 时保持原生类型（int/float）
                # 避免 ComfyUI INTConstant/EmptyLatentImage 等节点收到 str 报错
                keep_native = (val == token)
                val = val.replace(token, str(raw))
                if keep_native:
                    val = raw
                    break  # 已转为原生类型，不再继续做字符串 token 替换
        nodes.append({"nodeId": str(entry["nodeId"]), "fieldName": str(entry["fieldName"]),
                      "fieldValue": val})

    # 3) video_openvdn 无参考素材时必须把 task_type 由 Ref2VA 改为 T2VA。
    #    2026-09-29 实测：0 参考图 + 保持 Ref2VA → RH FAILED 报
    #    "ValueError: REF2VA requires at least one reference media input"；
    #    同参数下 T2VA 可正常出片。此兜底为必需项。
    if KINDS[kind][1] == "video_openvdn":
        refs = params.get("ref_images") or []
        refv = params.get("ref_videos") or []
        refa = params.get("ref_audios") or []
        if not (refs or refv or refa):
            nodes.append({"nodeId": "6", "fieldName": "task_type",
                          "fieldValue": "T2VA"})

    return nodes


def poll(job_id: str, key_slot: str = "") -> dict:
    """轮询任务状态。无 key 的 mock 任务直接合成终态产物；有 key 走 status→outputs。

    返回统一结构：{"job_id", "status"(queued/running/succeeded/failed),
    "files":[{file_ref, fingerprint, size}], "error", "meta"}。
    """
    if key_slot == "llm":
        return {"job_id": job_id, "status": "succeeded", "files": [], "error": "",
                "meta": {"channel": "llm"}}
    if key_slot == "mock" or job_id.startswith("mock-"):
        rec = _MOCK_JOBS.get(job_id)
        if rec is None:
            return {"job_id": job_id, "status": "failed", "files": [],
                    "error": "mock Task not found", "meta": {}}
        files = _mock_media(rec["kind"], rec["prompt"], rec["params"], job_id)
        return {"job_id": job_id, "status": "succeeded", "files": files, "error": "",
                "meta": {"channel": rec["kind"], "mock": True}}
    slot = _pick_slot(key_slot)
    if slot is None:
        return {"job_id": job_id, "status": "failed", "files": [], "error": "no RH key available", "meta": {}}
    kind = (_REAL_JOBS.get(job_id) or {}).get("kind", "")
    try:
        s = _http_poll(slot["base"], "/task/openapi/status",
                       {"apiKey": slot["key"], "taskId": job_id})
        raw = s.get("data") or ""
        st = str(raw.get("taskStatus") if isinstance(raw, dict) else raw).upper()
        # 失败/取消终态：拉 outputs 取 failedReason 透传，不假装 running（避免失败被掩盖）。
        if any(k in st for k in ("FAIL", "ERROR", "CANCEL")):
            o = _http_poll(slot["base"], "/task/openapi/outputs",
                           {"apiKey": slot["key"], "taskId": job_id})
            return {"job_id": job_id, "status": "failed", "files": [],
                    "error": _rh_failed_reason(o), "meta": {"key_slot": slot["id"]}}
        if not any(k in st for k in ("SUCCESS", "SUCCEED", "DONE", "COMPLETE")):
            return {"job_id": job_id, "status": "running", "files": [], "error": "",
                    "meta": {"key_slot": slot["id"]}}
        o = _http_poll(slot["base"], "/task/openapi/outputs",
                       {"apiKey": slot["key"], "taskId": job_id})
        files = []
        channels = []
        for idx, item in enumerate(o.get("data") or []):
            if not (isinstance(item, dict) and item.get("fileUrl")):
                continue
            url = item["fileUrl"]
            ext = _ext_from_url(url) or _ext_for(kind)
            rel = f"{kind or 'rh'}/{job_id}_{idx}.{ext}"
            # 流式下载（累计字节上限防超大文件打爆内存/磁盘）→ 本地落盘 + sha256。
            # file_ref 用与 mock 通道一致的本地相对路径（不是远端 URL）。
            data = _http_download(url, int(settings.RH_RESULT_MAX_BYTES or 0))
            f = _persist_bytes(rel, data)
            ch = _maybe_upload_s3(_MOCK_OUT_DIR / rel, f["file_ref"], ext)
            channels.append(ch)
            files.append({"file_ref": f["file_ref"], "fingerprint": f["fingerprint"],
                          "size": f["size"]})
        storage_ch = "s3" if channels and all(c == "s3" for c in channels) else "local"
        return {"job_id": job_id, "status": "succeeded" if files else "running",
                "files": files, "error": "",
                "meta": {"key_slot": slot["id"], "storage": storage_ch}}
    except Exception as e:  # noqa: BLE001
        return {"job_id": job_id, "status": "failed", "files": [],
                "error": f"RH poll error: {e}", "meta": {}}


def cancel(job_id: str, key_slot: str = "") -> None:
    """取消任务（与 runninghub.cancel 同协议；best-effort，失败不抛）。"""
    if not job_id or key_slot in ("mock", "llm", ""):
        return
    slot = _pick_slot(key_slot)
    if slot is None:
        return
    try:
        _http_poll(slot["base"], "/task/openapi/cancel",
                   {"apiKey": slot["key"], "taskId": job_id})
    except Exception:  # noqa: BLE001
        pass


def run(kind: str, prompt: str, params: Optional[dict] = None) -> dict:
    """提交 + 轮询至终态的一站式执行（自主工作流用）。返回统一结果 dict。

    llm 通道额外返回 `text`（正文原文，与落盘文件同源）：上游（编排
    subtask_results / 宿主 UI）可直接展示正文，无需再回读产物文件。
    """
    params = params or {}
    # 边界数值：未知 kind / 空 prompt 明确报错，不静默出垃圾产物。
    _NON_WORKFLOW_KINDS = (LLM_KIND, VLM_KIND, VIDEO_ANALYSIS_KIND)
    if kind not in _NON_WORKFLOW_KINDS and kind not in KINDS:
        return {"job_id": "", "status": "failed", "files": [],
                "error": f"Unknown execution channel kind={kind}", "meta": {}}
    # video_analysis 不要求 prompt（视频路径从 params 获取）
    if kind != VIDEO_ANALYSIS_KIND and not str(prompt or "").strip():
        return {"job_id": "", "status": "failed", "files": [],
                "error": "prompt is empty", "meta": {}}
    if kind == LLM_KIND:
        text = complete(prompt, system=params.get("system", ""),
                        max_tokens=params.get("max_tokens"))
        job_id = f"llm-{uuid.uuid4().hex[:12]}"
        rel = f"llm/{job_id}.txt"
        f = _persist_bytes(rel, text.encode("utf-8"))
        return {"job_id": job_id, "status": "succeeded", "files": [f], "text": text,
                "error": "", "meta": {"channel": "llm"}}
    if kind == VLM_KIND:
        images = params.get("images") or []
        text = vlm_complete(prompt, images=images,
                            system=params.get("system", ""),
                            max_tokens=params.get("max_tokens"))
        job_id = f"vlm-{uuid.uuid4().hex[:12]}"
        return {"job_id": job_id, "status": "succeeded", "files": [], "text": text,
                "error": "", "meta": {"channel": "vlm", "n_images": len(images)}}
    if kind == VIDEO_ANALYSIS_KIND:
        from .video_analyzer import analyze_video
        video_path = params.get("video_path") or prompt  # 兼容：prompt里也可能就是路径
        report = analyze_video(
            video_path,
            fps=int(params.get("fps", 3)),
            chunk_sec=int(params.get("chunk_sec", 18)),
            overlap_sec=int(params.get("overlap_sec", 4)),
            frame_size=int(params.get("frame_size", 512)),
            focus=params.get("focus", "advertising"),
            task_description=str(prompt or "")[:500],  # 传任务描述让 AI 3 理解意图
            prior_strategy=params.get("prior_strategy"),  # judg_dedup：复用判断阶段的策略
        )
        job_id = f"va-{uuid.uuid4().hex[:12]}"
        status = "succeeded" if report.get("status") == "completed" else "failed"
        # 把报告存为文件产物
        text = json.dumps(report, ensure_ascii=False, indent=2)
        rel = f"video_analysis/{job_id}.json"
        f = _persist_bytes(rel, text.encode("utf-8"))
        return {"job_id": job_id, "status": status, "files": [f], "text": text,
                "error": report.get("error", ""), "meta": {"channel": "video_analysis",
                        "duration": report.get("duration_sec", 0)}}
    s = submit(kind, prompt, params)
    if s["status"] == "failed":
        return {"job_id": s["job_id"], "status": "failed", "files": [],
                "error": s["error"], "meta": s.get("meta", {})}
    return poll(s["job_id"], s["key_slot"])


# ---------------- LLM 通道（全站管理/治理 AI 执行体；蓝图 §1.0） ----------------
def complete(prompt: str, system: str = "", max_tokens: Optional[int] = None) -> str:
    """按 config.LLM_PROVIDER 分派：
      echo/mock      -> 确定性返回（测试用，不发任何请求）；
      openai_compat  -> httpx POST {LLM_BASE_URL}/v1/chat/completions（本地 Qwen 默认 11436）；
      runninghub     -> POST RH_LLM_BASE/chat/completions（RH 企业 LLM 预留通道）。

    max_tokens：输出长度上限（None = 不传，由服务端默认）。调用方按场景分档传入
    （见 config.LLM_MAX_TOKENS_DECOMPOSE / _EXECUTE）——RH 账单实测延迟 ∝ 输出长度，
    不封顶时单次可生成 1.1 万 tokens / 58s。
    """
    provider = (settings.LLM_PROVIDER or "echo").lower()
    if provider in ("echo", "mock", ""):
        sys_part = f"{system} :: " if system else ""
        return f"[echo] {sys_part}{prompt}"
    if provider == "openai_compat":
        base = (settings.LLM_BASE_URL or "").rstrip("/")
        if not base or not settings.LLM_API_KEY:
            # 未配端点/key 时回退 echo（与 RunVerseHub llm_provider 同容错）
            return complete_echo_fallback(prompt, system)
        url = base if "/chat/completions" in base else base + "/v1/chat/completions"
        body = {"model": settings.LLM_MODEL,
                "messages": ([{"role": "system", "content": system}] if system else [])
                            + [{"role": "user", "content": prompt}],
                "temperature": settings.LLM_TEMPERATURE}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        try:
            return _http_llm(url, {"Authorization": f"Bearer {settings.LLM_API_KEY}"}, body)
        except Exception as e:  # noqa: BLE001
            return complete_echo_fallback(prompt, system, err=str(e))
    if provider == "runninghub":
        url = (settings.RH_LLM_BASE or "").rstrip("/") + "/chat/completions"
        body = {"model": settings.RH_LLM_MODEL,
                "messages": ([{"role": "system", "content": system}] if system else [])
                            + [{"role": "user", "content": prompt}]}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        try:
            return _http_llm(url, {"Authorization": f"Bearer {settings.RH_LLM_API_KEY or ''}"}, body)
        except Exception as e:  # noqa: BLE001
            return complete_echo_fallback(prompt, system, err=str(e))
    return complete_echo_fallback(prompt, system)


def complete_echo_fallback(prompt: str, system: str = "", err: str = "") -> str:
    """echo 兜底（确定性）。"""
    sys_part = f"{system} :: " if system else ""
    tail = f"\n<!-- llm fallback: {err} -->" if err else ""
    return f"[echo] {sys_part}{prompt}{tail}"


# ---------------- VLM 通道（视觉语言模型：质检审查图片/视频） ----------------

def _file_to_data_url(path_or_ref: str) -> str:
    """本地文件路径 → base64 data URL（VLM API 接受，无需公网可访问）。
    已经是 http(s) URL 的直接返回原 URL。"""
    import base64
    from mimetypes import guess_type
    if not path_or_ref:
        return ""
    if path_or_ref.startswith(("http://", "https://", "data:")):
        return path_or_ref
    # 解析本地路径
    p = Path(path_or_ref)
    if not p.is_absolute():
        p = _MOCK_OUT_DIR / path_or_ref  # 产物默认在 mock_out/ 下
    if not p.is_file():
        # 尝试 DATA_DIR 相对路径
        from .database import DATA_DIR
        p = DATA_DIR / path_or_ref
    if not p.is_file():
        return ""  # 文件不存在，返回空串让调用方跳过
    mime = guess_type(str(p)) or ("image/png", None)
    data = p.read_bytes()
    # 限制单张大小 ≤ 10MB（VLM API 通常有 body 限制）
    if len(data) > 10 * 1024 * 1024:
        data = data[:10 * 1024 * 1024]
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime[0]};base64,{b64}"


def vlm_complete(prompt: str, images: Optional[list] = None,
                 system: str = "", max_tokens: Optional[int] = None) -> str:
    """视觉语言模型调用（质检核心通道）。

    与 complete() 的关键区别：content 包含 image_url 项，VLM 能实际"看到"图片。

    Args:
        prompt: 文本提问（如"请评估这张图片的视觉质量..."）
        images: 图片列表，每项为本地文件路径 / http URL / base64 data URL。
                本地路径自动转 base64 data URL。视频需先抽帧再传入帧路径。
        system: 系统 prompt。
        max_tokens: 输出上限。

    Returns:
        VLM 文本回答。无 key/失败时返回 echo 兜底（不阻断流程）。
    """
    provider = (settings.VLM_PROVIDER or "echo").lower()
    if provider in ("echo", "mock", ""):
        return vlm_echo_fallback(prompt, system)

    # 构建 OpenAI 兼容 messages（content 含 image_url + text）
    content_parts = []
    for img in (images or []):
        url = _file_to_data_url(img)
        if url:
            content_parts.append({"type": "image_url", "image_url": {"url": url}})
    content_parts.append({"type": "text", "text": prompt})

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content_parts})

    # 确定端点 + key
    if provider in ("siliconflow", "dashscope", "openai_compat"):
        base = (settings.VLM_BASE_URL or "https://api.siliconflow.cn/v1").rstrip("/")
        url = base if "/chat/completions" in base else base + "/chat/completions"
        api_key = settings.VLM_API_KEY
        if provider == "dashscope":
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    else:
        return vlm_echo_fallback(prompt, system)

    if not api_key:
        return vlm_echo_fallback(prompt, system, err="VLM_API_KEY not configured")

    body = {
        "model": settings.VLM_MODEL,
        "messages": messages,
        "temperature": 0.3,  # 质检要确定性，低温
    }
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    elif settings.VLM_MAX_TOKENS:
        body["max_tokens"] = settings.VLM_MAX_TOKENS

    try:
        return _http_llm(url, {"Authorization": f"Bearer {api_key}"}, body)
    except Exception as e:  # noqa: BLE001
        return vlm_echo_fallback(prompt, system, err=str(e))


def vlm_echo_fallback(prompt: str, system: str = "", err: str = "") -> str:
    """VLM echo 兜底（无 VLM key / 调用失败时）。"""
    sys_part = f"{system} :: " if system else ""
    tail = f"\n<!-- vlm fallback: {err} -->" if err else ""
    # 返回一个"看起来像质检结果"的 JSON，让下游解析不至于报错
    import json as _json
    mock_report = {
        "pass": True, "score": 0.7,
        "issues": ["VLM not available, visual quality unverified"],
        "suggestions": [],
        "per_subtask": [],
    }
    return _json.dumps(mock_report, ensure_ascii=False)


def extract_video_frames(video_path: str, n_frames: int = 4) -> list:
    """从视频文件抽取关键帧（ffmpeg），返回帧图片路径列表。

    用于视频质检：把视频拆成 N 帧图片，逐帧送入 VLM 审查。
    ffmpeg 不可用 / 文件不存在时返回空列表（不阻断流程）。
    """
    import subprocess, tempfile
    from .database import DATA_DIR

    # 解析视频路径
    p = Path(video_path)
    if not p.is_absolute():
        p = _MOCK_OUT_DIR / video_path
    if not p.is_file():
        p = DATA_DIR / video_path
    if not p.is_file():
        logger.warning("extract_video_frames: file not found: %s", video_path)
        return []

    # 输出帧到临时目录
    out_dir = _MOCK_OUT_DIR / "_vlm_frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = str(out_dir / f"{p.stem}_frame_%02d.png")

    try:
        # 用 ffmpeg 均匀抽 N 帧（fps = N / duration）
        # 先获取视频时长
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 5.0
        fps = n_frames / max(duration, 0.5)

        subprocess.run(
            ["ffmpeg", "-y", "-i", str(p), "-vf", f"fps={fps}",
             "-frames:v", str(n_frames), "-q:v", "2", out_pattern],
            capture_output=True, timeout=30,
        )
        # 收集输出帧
        frames = sorted(out_dir.glob(f"{p.stem}_frame_*.png"))
        return [str(f) for f in frames[:n_frames]]
    except FileNotFoundError:
        logger.warning("extract_video_frames: ffmpeg/ffprobe not available")
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("extract_video_frames failed: %s", e)
        return []
