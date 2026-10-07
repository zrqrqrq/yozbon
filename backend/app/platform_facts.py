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
"""平台运营四岗位只读事实采集（M3+M4 / 契约 §4.1）。

本文件是 M3+M4 与 M5/M6 的联动点：
- M3+M4 的 governance 规则版执行体（TASK_HANDLERS）调 collect_* 取事实做规则判定；
- M5（文件治理）/ M6（情报库）子代理按本文件函数签名对接，不重写采集逻辑。

铁律：
- **只读 / 确定性**：同输入同输出，不发真实网络、不调外部服务；
  security/code/intel 全部为确定性 mock；file 扫描本地 mock 目录（环境隔离）。
- collect_file_facts 是唯一会写库的采集函数（写 file_registry，M5 联动）。
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from .models import AuditLog, FileRegistry


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = _BACKEND_ROOT / "data" / "mock_out"

# 文件生命周期 TTL（天）——契约 §5.2
_TTL_DAYS = {"temp": 7, "deliverable": 30, "asset": 30, "media": 30}
_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4",
               ".webm", ".mov", ".mp3", ".wav"}


def scan_root() -> Path:
    """扫描目录：env AIJUHE_MOCK_OUT_ROOT 覆盖，默认 backend/data/mock_out。"""
    env = os.environ.get("AIJUHE_MOCK_OUT_ROOT", "").strip()
    return Path(env) if env else DEFAULT_SCAN_ROOT


# ----------------------------------------------------------------------
# 1. 安全岗位（platform_security）
# ----------------------------------------------------------------------
def collect_security_facts(db: Session) -> dict:
    """安全巡检事实（真实数据源采集）。

    接入数据源：
    - DependencyVulnReport（SCA 扫描结果）→ dep_vulns
    - CORSPolicy（CORS 策略配置）→ services（检测过度宽松策略）
    - AuditLog（审计日志中安全相关事件）→ login_anomalies
    - TokenRevocation（令牌吊销记录）→ login_anomalies

    返回键固定：ports / services / dep_vulns / login_anomalies。
    规则判定：dep_vulns 或 login_anomalies 非空 → risk_found，否则 safe。
    """
    from .models import DependencyVulnReport, CORSPolicy, TokenRevocation
    from datetime import datetime, timedelta

    # 1. 依赖漏洞：从 DependencyVulnReport 读取未确认的高危及以上漏洞
    vulns = (db.query(DependencyVulnReport)
             .filter(DependencyVulnReport.acknowledged == 0)
             .order_by(DependencyVulnReport.id.desc())
             .limit(50).all())
    dep_vulns = [
        {"package": v.package_name, "cve_id": v.cve_id,
         "severity": v.severity, "fixed_version": v.fixed_version}
        for v in vulns
    ]

    # 2. 服务/CORS 策略：检测过度宽松配置（origin=* 且 credentials=True 为风险）
    policies = db.query(CORSPolicy).all()
    services = []
    for p in policies:
        entry = {
            "id": p.id,
            "origin_pattern": p.origin_pattern,
            "methods": p.methods,
            "credentials": bool(p.credentials),
        }
        # 标记风险项：通配 origin + 允许凭证属高危配置
        if p.origin_pattern == "*" and p.credentials:
            entry["risk"] = "wildcard_origin_with_credentials"
        services.append(entry)

    # 3. 登录异常：从审计日志中提取近 24h 的安全相关事件
    since = datetime.utcnow() - timedelta(hours=24)
    anomaly_keywords = ("ban", "revoke", "frozen", "banned", "suspended")
    audit_rows = (db.query(AuditLog)
                  .filter(AuditLog.created_at >= since)
                  .order_by(AuditLog.id.desc())
                  .limit(100).all())
    login_anomalies = []
    for r in audit_rows:
        text_lower = f"{r.action} {r.detail}".lower()
        if any(kw in text_lower for kw in anomaly_keywords):
            login_anomalies.append({
                "action": r.action,
                "actor_type": r.actor_type,
                "actor_id": r.actor_id,
                "at": r.created_at.isoformat() if r.created_at else None,
            })

    # 4. 令牌吊销：近 24h 内的吊销记录视为潜在安全事件补充
    revocations = (db.query(TokenRevocation)
                   .filter(TokenRevocation.revoked_at >= since)
                   .limit(50).all())
    for rv in revocations:
        login_anomalies.append({
            "action": "token_revoked",
            "actor_type": "system",
            "actor_id": rv.ai_id,
            "at": rv.revoked_at.isoformat() if rv.revoked_at else None,
        })

    # ports 字段：当前无真实端口扫描能力，返回空列表（非 mock，真实无数据）
    return {
        "ports": [],
        "services": services,
        "dep_vulns": dep_vulns,
        "login_anomalies": login_anomalies[:20],
    }


# ----------------------------------------------------------------------
# 2. 代码质量岗位（platform_code）
# ----------------------------------------------------------------------
def collect_code_facts(db: Session) -> dict:
    """代码质量事实：从 audit_logs 抽最近的 error/fail 记录占位为 errors。

    确定性：同库同状态同输出（不读日志文件、不跑静态扫描）。
    slow_queries 暂无性能表，固定空列表占位。
    """
    rows = (db.query(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(50).all())
    errors, warnings = [], []
    for r in rows:
        text = f"{r.action} {r.detail}".lower()
        item = {"action": r.action, "detail": r.detail,
                "at": r.created_at.isoformat() if r.created_at else None}
        if "error" in text or "fail" in text:
            errors.append(item)
        elif "warn" in text:
            warnings.append(item)
    return {"errors": errors[:10], "slow_queries": [],
            "log_warnings": warnings[:10]}


# ----------------------------------------------------------------------
# 3. 文件治理岗位（platform_file）——唯一写库的采集函数
# ----------------------------------------------------------------------
def _categorize(rel: str) -> str:
    """按路径约定确定性归类：temp/deliverable/asset/media。"""
    low = rel.lower()
    if "deliverable" in low:
        return "deliverable"
    if "asset" in low:
        return "asset"
    if Path(low).suffix in _MEDIA_EXTS:
        return "media"
    return "temp"


def collect_file_facts(db: Session, scan_root_dir=None) -> list:
    """扫描 mock 目录 → 写/更新 file_registry → 返回文件清单（M5 联动）。

    - scan_root_dir 可传 Path/str（测试/手动采集用）；缺省取 scan_root()。
    - category：deliverable/asset/media/temp（按路径约定，确定性）。
    - status：now > ttl_until → expired，否则 active。
    - 同路径幂等 upsert（已存在则更新 size/status/ttl，不重复建行）。
    """
    root = Path(scan_root_dir) if scan_root_dir else scan_root()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow()
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        category = _categorize(rel)
        ttl_until = now + timedelta(days=_TTL_DAYS[category])
        status = "expired" if now > ttl_until else "active"
        size = p.stat().st_size
        row = (db.query(FileRegistry)
               .filter(FileRegistry.path == str(p)).first())
        if row is None:
            row = FileRegistry(path=str(p), category=category,
                               size_bytes=size, status=status,
                               ttl_until=ttl_until, ref="")
            db.add(row)
        else:
            row.category = category
            row.size_bytes = size
            row.status = status
            row.ttl_until = ttl_until
        db.flush()
        # C-53：登记/更新 row 后，deliverable/asset/media 类镜像 S3 并回写 s3_key。
        # best-effort：未启用 S3/本地文件缺失/上传失败 → mirror 返回 ""，s3_key 留空，
        # 不阻断扫描主流程。models.py 未映射 s3_key 列（见登记册 C-63），回写走 raw SQL。
        if category in ("deliverable", "asset", "media"):
            try:
                from . import file_governance as _fg  # 惰性 import 防循环
                s3_key = _fg.mirror_registered_path(str(p), category)
                if s3_key:
                    db.execute(
                        _sql_text("UPDATE file_registry SET s3_key=:k "
                                  "WHERE id=:i"),
                        {"k": s3_key, "i": row.id})
            except Exception:  # noqa: BLE001 镜像失败一律回退本地、留空
                pass
        out.append({"path": str(p), "category": category, "size": size,
                    "status": status,
                    "ttl_until": ttl_until.isoformat()})
    return out


# ----------------------------------------------------------------------
# 4. 情报岗位（platform_intel）——M6 联动：条目形态契约 §5.3
# ----------------------------------------------------------------------
_MOCK_INTEL = [
    {"type": "tool", "title": "yozbon-mock-tool v1.0",
     "summary": "deterministic mock: lightweight code automation tool (sampled by GitHubTrendingSource)",
     "source_url": "https://github.com/mock/yozbon-mock-tool",
     "capability_tags": ["code", "automation"]},
    {"type": "tool", "title": "yozbon-mock-scraper v0.9",
     "summary": "deterministic mock: web information scraper (sampled by GitHubTrendingSource)",
     "source_url": "https://github.com/mock/yozbon-mock-scraper",
     "capability_tags": ["data", "scraping"]},
    {"type": "tool", "title": "yozbon-mock-deploy v2.1",
     "summary": "deterministic mock: one-click deployment toolchain (sampled by GitHubTrendingSource)",
     "source_url": "https://github.com/mock/yozbon-mock-deploy",
     "capability_tags": ["devops", "automation"]},
    {"type": "model", "title": "yozbon-mock-model-7b",
     "summary": "deterministic mock: newly open-sourced 7B inference model (sampled by TechNewsRSSSource)",
     "source_url": "https://example.com/news/mock-model-7b",
     "capability_tags": ["llm", "reasoning"]},
    {"type": "model", "title": "yozbon-mock-multimodal-v1",
     "summary": "deterministic mock: new multimodal model (sampled by TechNewsRSSSource)",
     "source_url": "https://example.com/news/mock-multimodal-v1",
     "capability_tags": ["multimodal", "vision"]},
]


def collect_intel_facts(db: Session) -> list:
    """情报采集事实（确定性 mock：不发网络）。

    返回条目形态与契约 §5.3 一致：{type, title, summary, source_url, capability_tags}。
    M6 入库/去重由 M6 子代理的 intel_sources.py 负责，本函数只读返回。
    """
    return [dict(item) for item in _MOCK_INTEL]
