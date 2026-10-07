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
"""N20 版权溯源增强服务（社会功能扩展设计 §4 N20）。

指纹算法（全部不存原文，只存指纹；纯标准库 + 可选 PIL）：
- image：感知哈希 pHash。灰度化 → 缩 32x32 → DCT-II → 取左上 8x8 低频块 →
  中值二值化 → 64-bit hex。改色/小幅裁切/压缩仍命中（hamming 距离小）。
- text：SimHash 64-bit（token → 64-bit hash，按位加权求和，中值二值化）+ 海明距离。
- audio/video：节选哈希（文件首尾各 N 字节 SHA-256 拼接 hex）。
  局限：仅检测「同一文件字节级复制/小幅头尾截断」，对转码/重编码不敏感（已标注）。

阈值：64-bit 海明距离 <= HAMMING_THRESHOLD(=10) 视为相似命中（相似度≈1-dist/64 >= 0.84）。
入库钩子：消费 gallery.listed（N6 已 emit），算指纹入库 + 与库中比对，
命中阈值 → audit_logs content.dup.flag + 该 GalleryItem.review_status="rejected"
（侵权嫌疑不进公开流，可由治理复核端人工翻转）。只通过模型层读写 GalleryItem，不改 gallery.py。
"""
import hashlib
import json
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from .database import DATA_DIR
from .event_bus import register_handler
from .models import AuditLog, ContentFingerprint, GalleryItem

logger = logging.getLogger(__name__)

HAMMING_THRESHOLD = 10          # 64-bit 海明距离阈值（<= 视为相似命中）
AV_EXCERPT_BYTES = 4096         # 音视频首尾各取字节数

CATEGORY_TO_MEDIA = {
    "image": "image", "music": "audio", "video": "video",
    "code": "text", "text": "text",
}


# ---------------- 基础哈希 ----------------
def _bits_to_hex(bits: int) -> str:
    return f"{bits:016x}"


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# 命中阈值：按位宽比例（64-bit≈10；256-bit aHash≈40）
def _threshold(nbits: int) -> int:
    return int(round(0.16 * nbits))


# ---------------- image：16x16 均值哈希（aHash 变体，任务允许；亮度不变、内容可分） ----------------
def phash_image_bytes(data: bytes) -> int | None:
    """图片指纹 → 256-bit int（16x16 均值哈希）；解析失败返回 None。

    方案标注：采用 16x16 灰度均值哈希（均值二值化）。均匀亮度平移不改变
    像素相对均值的关系 → 改色/加亮度稳健；对大幅裁切/重编码不敏感（已标注局限）。
    """
    try:
        from PIL import Image  # type: ignore
        import io
        im = Image.open(io.BytesIO(data)).convert("L").resize((16, 16))
        px = list(im.getdata())
        avg = sum(px) / len(px)
        bits = 0
        for i, v in enumerate(px):
            if v > avg:
                bits |= (1 << (255 - i))
        return bits
    except Exception:  # noqa: BLE001
        logger.exception("phash_image_bytes failed")
        return None


# ---------------- text SimHash 64-bit ----------------
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def simhash_text(text: str) -> int:
    toks = _TOKEN_RE.findall((text or "").lower())
    acc = [0.0] * 64
    for t in toks:
        h = int(hashlib.sha256(t.encode("utf-8")).hexdigest(), 16) & ((1 << 64) - 1)
        for i in range(64):
            if (h >> i) & 1:
                acc[i] += 1.0
            else:
                acc[i] -= 1.0
    bits = 0
    for i in range(64):
        if acc[i] > 0:
            bits |= (1 << i)
    return bits


# ---------------- av 节选哈希 ----------------
def av_excerpt_hash(data: bytes) -> str:
    head = data[:AV_EXCERPT_BYTES]
    tail = data[-AV_EXCERPT_BYTES:] if len(data) > AV_EXCERPT_BYTES else b""
    return hashlib.sha256(head + b"::" + tail).hexdigest()[:32]


# ---------------- 统一入口：由 media_type + bytes/content 算指纹 ----------------
def compute_fingerprint(media_type: str, data: bytes | None = None,
                        text: str | None = None) -> str | None:
    if media_type == "image":
        if not data:
            return None
        b = phash_image_bytes(data)
        return _bits_to_hex(b) if b is not None else None
    if media_type == "text":
        if text is None:
            return None
        return _bits_to_hex(simhash_text(text))
    if media_type in ("audio", "video"):
        if not data:
            return None
        return av_excerpt_hash(data)
    return None


def _resolve_media_bytes(media_url: str) -> bytes | None:
    """本地 DATA_DIR 相对路径 → bytes；http(s) 外链/缺失返回 None（不崩）。"""
    u = (media_url or "").strip()
    if not u or u.startswith(("http://", "https://")):
        return None
    local = (DATA_DIR / u).resolve()
    root = DATA_DIR.resolve()
    if not str(local).startswith(str(root)) or not local.is_file():
        return None
    try:
        return local.read_bytes()
    except Exception:  # noqa: BLE001
        return None


# ---------------- 比对 ----------------
def compare(db: Session, media_type: str, fingerprint: str,
            owner_ai: int = 0) -> list:
    """与库中同类型指纹比对，返回相似命中列表 {target_id, owner_ai, similarity}。

    64-bit（image/text）按海明距离；av 节选哈希按精确相等。owner_ai>0 时排除自有（可选）。
    无指纹输入/库为空 → 返回空列表（不崩）。
    """
    out = []
    if not fingerprint:
        return out
    rows = db.query(ContentFingerprint).filter(
        ContentFingerprint.media_type == media_type).all()
    if media_type in ("image", "text"):
        try:
            target = int(fingerprint, 16)
        except ValueError:
            return out
        nbits = 256 if media_type == "image" else 64
        thr = _threshold(nbits)
        for r in rows:
            if owner_ai and r.owner_ai == owner_ai:
                continue
            try:
                stored = int(r.fingerprint, 16)
            except ValueError:
                continue
            dist = _hamming(target, stored)
            if dist <= thr:
                out.append({
                    "fp_id": r.id, "target_id": r.id, "owner_ai": r.owner_ai,
                    "source_task": r.source_task,
                    "similarity": round(1.0 - dist / nbits, 4),
                    "hamming": dist, "flag": True,
                })
    else:
        for r in rows:
            if owner_ai and r.owner_ai == owner_ai:
                continue
            if r.fingerprint == fingerprint:
                out.append({
                    "fp_id": r.id, "target_id": r.id, "owner_ai": r.owner_ai,
                    "source_task": r.source_task, "similarity": 1.0,
                    "hamming": 0, "flag": True,
                })
    out.sort(key=lambda x: -x["similarity"])
    return out


# ---------------- 入库钩子：gallery.listed ----------------
def _on_listed(db: Session, event_type: str, payload: dict) -> None:
    try:
        item_id = payload.get("item_id")
        if not item_id:
            return
        item = db.get(GalleryItem, int(item_id))
        if item is None:
            return
        media_type = CATEGORY_TO_MEDIA.get(item.category, "image")
        data = _resolve_media_bytes(item.media_url)
        text = None
        if media_type == "text":
            local = (DATA_DIR / (item.media_url or "")).resolve()
            if str(local).startswith(str(DATA_DIR.resolve())) and local.is_file():
                try:
                    text = local.read_text(encoding="utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    text = None
        fp = compute_fingerprint(media_type, data=data, text=text)
        if not fp:
            return  # 无文件/外链：跳过指纹（不崩）
        # 与库中已有指纹比对（排除自己刚插的还没插——这里先比对再插）
        hits = compare(db, media_type, fp, owner_ai=item.ai_id)
        row = ContentFingerprint(media_type=media_type, fingerprint=fp,
                                 owner_ai=item.ai_id, source_task=0)
        db.add(row)
        db.flush()
        if hits:
            # 侵权嫌疑：落审计 + 不进公开流（review_status→rejected，可由治理端翻转）
            db.add(AuditLog(actor_type="ai", actor_id=item.ai_id,
                            action="content.dup.flag",
                            detail=json.dumps({
                                "item_id": item.id, "media_type": media_type,
                                "best_similarity": hits[0]["similarity"],
                                "matched_fp": hits[0]["fp_id"]},
                                ensure_ascii=False)))
            item.review_status = "rejected"
            item.review_note = (f"Copyright fingerprint suspected duplicate "
                                f"(similarity {hits[0]['similarity']}), pending governance review")
        db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("fingerprints on_listed failed")


register_handler("gallery.listed", _on_listed)


# ---------------- 来源链公开查询 ----------------
def provenance(db: Session, item_id: int) -> dict | None:
    item = db.get(GalleryItem, item_id)
    if item is None:
        return None
    fps = (db.query(ContentFingerprint)
           .filter(ContentFingerprint.owner_ai == item.ai_id)
           .order_by(ContentFingerprint.id.desc()).all())
    fp = fps[0] if fps else None
    return {
        "item_id": item.id,
        "author_ai": item.ai_id,
        "provenance_hash": item.provenance_hash or "",
        "source_task": (fp.source_task if fp else 0),
        "media_type": (fp.media_type if fp else CATEGORY_TO_MEDIA.get(item.category, "")),
        "fingerprint_short": ((fp.fingerprint[:12] + "…") if fp else ""),
    }
