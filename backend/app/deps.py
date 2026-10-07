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
"""鉴权依赖（蓝图 §三：宿主侧 JWT / AI 侧 AI key；§14 新增 readonly JWT 分层）。

- get_current_host: `Authorization: Bearer <jwt>`（typ=host，scope=host）
- get_current_ai:
    * `X-AI-Key: aik_<citizen_id>_<secret>`（兼容 Authorization: Bearer）→ scope=workflow
    * `Authorization: Bearer <jwt>`（typ=ai_ro，sub=citizen_id）→ scope=readonly（§14 前端注册）

AI key 格式：aik_<citizen_id>_<32hex>；库中只存 sha256 哈希（ai_citizens.api_key_hash）。
死亡/熔断状态拒绝调用（冻结 sleep 允许读自身数据）。

§14（C-35）集中拦截：readonly（typ=ai_ro）令牌只允许访问 /api/public/* 只读面；
凡落到 /api/ai/* 或 /api/sys/* 即 403（前端注册 AI 不得访问后端）。
AI key（workflow）不受此路径限制。
"""
import hashlib
import secrets
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, get_db
from .models import AICitizen, Host
from .security import decode_token
from .token_revocation import token_revocation_service


# ---------------- §14 轻量幂等迁移：ai_citizens 补 source / password_hash ----------------
def _ensure_scope_columns():
    """SQLite 加列：先查 PRAGMA table_info，缺列才 ALTER（幂等，不删表重建）。

    models.py 已声明新列；对存量 data/aijuhe.db 补列。列已存在则静默跳过。
    """
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import text
    wanted = {
        "source": "VARCHAR(8) DEFAULT 'api'",
        "password_hash": "VARCHAR(255) DEFAULT ''",
        "email": "VARCHAR(255) DEFAULT ''",
    }
    # 先判表是否存在（空库/临时库尚未 init_db）：不存在直接 return，不碰事务
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT name FROM sqlite_master "
                     "WHERE type='table' AND name='ai_citizens'")).first()
            if not exists:
                return
            rows = conn.execute(text("PRAGMA table_info(ai_citizens)")).fetchall()
            existing = {r[1] for r in rows}
            for col, ddl in wanted.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE ai_citizens ADD COLUMN {col} {ddl}"))
    except Exception:  # noqa: BLE001 列已存在等幂等场景：静默跳过
        pass


_ensure_scope_columns()


def _unauth(detail: str = "authentication failed"):
    return HTTPException(status_code=401, detail=detail)


# ---------------- 按 source 限流（web readonly 严格限流） ----------------
# 双后端（C-56，2026-10-04）：Redis 优先（REDIS_URL 配置且连接可用→分布式固定窗口计数），
# 不可用/未配置自动降级内存滑窗（原实现语义不变，单实例零依赖）。
# 决策与成本对比见 docs/交付说明_C56_readonly限流方案.md。
_READONLY_HITS: dict[str, list] = {}

# Redis 后端状态：None=未尝试/未配置；成功客户端缓存；失败置降级冷却时间戳（冷却后重试连接）
_redis_client: dict = {"client": None, "down_until": 0.0}
_REDIS_COOLDOWN = 30.0          # 连接失败后冷却秒数，避免每请求重连打满日志
_REDIS_SOCKET_TIMEOUT = 1.0     # 限流路径必须快速失败，超长等待拖慢 readonly 请求


def _redis_get():
    """获取 Redis 客户端（惰性 + 冷却降级）。未配置/不可用返回 None。"""
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return None
    now = time.time()
    if _redis_client["down_until"] > now:
        return None
    if _redis_client["client"] is not None:
        return _redis_client["client"]
    try:
        import redis  # noqa: PLC0415  惰性 import：未装库/未配置不拖累启动
        client = redis.Redis.from_url(
            url, socket_timeout=_REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT,
            decode_responses=True)
        client.ping()  # 连通性探测：失败即降级冷却
        _redis_client["client"] = client
        return client
    except Exception:  # noqa: BLE001  Redis 不可用：降级内存 + 冷却
        _redis_client["down_until"] = now + _REDIS_COOLDOWN
        return None


def _redis_incr(key: str, ttl: int) -> int | None:
    """Redis 固定窗口计数（INCR+EXPIRE 幂等）。返回窗口计数；任何异常返回 None（调用方降级内存）。"""
    try:
        client = _redis_get()
        if client is None:
            return None
        n = client.incr(key)
        if int(n) == 1:
            client.expire(key, ttl)
        return int(n)
    except Exception:  # noqa: BLE001  限流绝不能 500：降级
        _redis_client["down_until"] = time.time() + _REDIS_COOLDOWN
        return None


def check_readonly_rate_limit(citizen_id: int):
    """web readonly 令牌按分钟严格限流（PLAZA_READONLY_RPM / 分钟）。超限 429。

    Redis 优先（REDIS_URL 配置时，多实例共享计数）；不可用/未配置降级内存滑窗
    （进程级计数，单实例语义与历史一致）。
    """
    limit = int(getattr(settings, "PLAZA_READONLY_RPM", 30) or 30)
    now = time.time()
    # Redis 固定窗口：key = prefix + citizen + 当前分钟桶，ttl 120s（窗口+余量）
    if getattr(settings, "REDIS_URL", "") or "":
        prefix = getattr(settings, "REDIS_RATELIMIT_PREFIX", "aijuhe:rl:") or "aijuhe:rl:"
        rkey = f"{prefix}{citizen_id}:{int(now // 60)}"
        n = _redis_incr(rkey, 120)
        if n is not None:
            if n > limit:
                raise HTTPException(status_code=429,
                                    detail="Read-only access rate limited (please try again later)")
            return
    # 内存降级：滑窗 60s（原实现）
    cutoff = now - 60.0
    key = str(citizen_id)
    hits = _READONLY_HITS.setdefault(key, [])
    # 清理过期窗口
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if len(hits) >= limit:
        raise HTTPException(status_code=429,
                            detail="Read-only access rate limited (please try again later)")
    hits.append(now)


def get_current_host(authorization: Optional[str] = Header(None),
                     db: Session = Depends(get_db)) -> Host:
    """宿主 JWT 鉴权（蓝图 §三 宿主侧）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauth("missing bearer token")
    try:
        payload = decode_token(authorization[7:])
    except ValueError:
        raise _unauth("bad token")
    # P0: Token 吊销检查
    jti = payload.get("jti")
    if jti and token_revocation_service.is_revoked(db, jti):
        raise _unauth("token revoked")
    if payload.get("typ") != "host":
        raise _unauth("not a host token")
    host = db.get(Host, payload.get("sub"))
    if host is None:
        raise _unauth("host not found")
    if host.status != "active":
        raise HTTPException(status_code=403, detail="host frozen")
    return host


def _status_check(citizen: AICitizen):
    if citizen.status == "banned":
        # C-55 追责（N 轮）：平台封禁=全链路熔断，账号拒绝一切 AI 侧调用；
        # 价值（钱包余额/托管）只冻结不转移不归零（价值保全），故 detail 明示冻结语义。
        raise HTTPException(status_code=403, detail="Account banned; funds frozen for protection")
    if citizen.status in ("dead", "frozen"):
        raise HTTPException(status_code=403, detail=f"ai status={citizen.status}")


def get_current_ai(x_ai_key: Optional[str] = Header(None),
                   authorization: Optional[str] = Header(None),
                   request: Request = None,
                   db: Session = Depends(get_db)) -> AICitizen:
    """AI 鉴权：aik_* key（workflow）或 ai_ro JWT（readonly）。

    解析成功后在 citizen 实例上挂 `_resolved_scope` ∈ {"workflow","readonly"}，
    供路由层/服务层做 scope 守卫（不入库、不影响业务逻辑）。
    """
    key = x_ai_key
    if not key and authorization and authorization.startswith("Bearer "):
        key = authorization[7:]
    if not key:
        raise _unauth("missing ai credential")

    citizen = None
    scope = "workflow"

    if key.startswith("aik_"):
        # 正式入驻 workflow key：aik_<id>_<hex>
        parts = key.split("_")
        if len(parts) != 3 or parts[0] != "aik":
            raise _unauth("bad ai key format")
        try:
            citizen_id = int(parts[1])
        except ValueError:
            raise _unauth("bad ai key format")
        citizen = db.get(AICitizen, citizen_id)
        if citizen is None:
            raise _unauth("ai not found")
        digest = hashlib.sha256(key.encode()).hexdigest()
        if not citizen.api_key_hash or not secrets.compare_digest(digest, citizen.api_key_hash):
            raise _unauth("invalid ai key")
        scope = "workflow"
    else:
        # 尝试 readonly JWT（typ=ai_ro）
        try:
            payload = decode_token(key)
        except ValueError:
            raise _unauth("bad ai credential")
        # P0: Token 吊销检查
        jti = payload.get("jti")
        if jti and token_revocation_service.is_revoked(db, jti):
            raise _unauth("token revoked")
        if payload.get("typ") != "ai_ro":
            raise _unauth("not an ai token")
        try:
            citizen_id = int(payload.get("sub"))
        except (TypeError, ValueError):
            raise _unauth("bad ai token subject")
        citizen = db.get(AICitizen, citizen_id)
        if citizen is None:
            raise _unauth("ai not found")
        scope = "readonly"

    _status_check(citizen)

    # §14 集中拦截：readonly 令牌不得触碰后端 /api/ai/* 与 /api/sys/*
    if scope == "readonly":
        path = request.url.path if request is not None else ""
        if path.startswith("/api/ai/") or path.startswith("/api/sys/"):
            raise HTTPException(
                status_code=403,
                detail="Read-only token cannot access the backend API (front-end registered AIs must complete official API onboarding + exam)")
        check_readonly_rate_limit(citizen.id)

    citizen._resolved_scope = scope  # type: ignore[attr-defined]
    return citizen


def resolve_scope(citizen: AICitizen) -> str:
    """取鉴权时解析出的令牌 scope（默认 workflow：历史 AI key 持有者）。"""
    return getattr(citizen, "_resolved_scope", "workflow")


def require_not_readonly(citizen: AICitizen = Depends(get_current_ai)) -> AICitizen:
    """写操作守卫：readonly AI 一律 403。供 /api/ai/* 写操作与 /api/sys/* 挂载。

    注：当前 /api/ai/* 路由统一经 get_current_ai，已由路径前缀集中拦截；
    本依赖供需要显式语义处（如 plaza publish / 钱包等）复用。
    """
    if resolve_scope(citizen) == "readonly":
        raise HTTPException(status_code=403,
                            detail="Read-only token cannot perform write operations (requires onboarding to obtain a workflow key)")
    return citizen


def issue_ai_key(citizen_id: int) -> str:
    """签发 AI key（workflow scope；返回明文一次；库中仅存哈希）。"""
    secret = secrets.token_hex(16)
    key = f"aik_{citizen_id}_{secret}"
    return key, hashlib.sha256(key.encode()).hexdigest()


async def host_or_governance_ai(authorization: Optional[str] = Header(None),
                                x_ai_key: Optional[str] = Header(None),
                                request: Request = None,
                                db: Session = Depends(get_db)
                                ) -> tuple:
    """sys 运维/治理复核端点双凭证（C-57）：host JWT 或 治理级 AI key(workflow)，二选一通过。

    - 无任何凭证 → 401；
    - 前端注册 readonly（typ=ai_ro）令牌 → 经 get_current_ai 按 /api/sys/ 路径前缀
      集中拦截 → 403（与 test_adversarial_scope.py:41 口径一致）；
    - AI key 但 class_level != governance → 403（非治理级 AI 不得执行人工/治理复核）；
    - host JWT 恒放行（人=宿主运营，与 body.reviewer=human 语义对应）。

    返回 (kind, obj)：kind ∈ {"host", "ai"}，路由层按需取用。
    """
    # 1) 优先尝试 host JWT（Bearer 且非 aik_ 前缀）
    if authorization and authorization.startswith("Bearer "):
        tok = authorization[7:]
        if not tok.startswith("aik_"):
            try:
                host = get_current_host(authorization=authorization, db=db)
                return ("host", host)
            except HTTPException:
                pass  # 非 host token / 失效 → 落到 AI 凭证校验
    # 2) AI 凭证（workflow key 或 readonly JWT；readonly 在此被集中拦截 403）
    ai = get_current_ai(x_ai_key=x_ai_key, authorization=authorization,
                        request=request, db=db)
    if ai.class_level != "governance":
        raise HTTPException(
            status_code=403,
            detail="Non-governance AI cannot perform this ops/review action (requires host JWT or governance AI key)")
    return ("ai", ai)


async def host_or_any_ai(authorization: Optional[str] = Header(None),
                         x_ai_key: Optional[str] = Header(None),
                         request: Request = None,
                         db: Session = Depends(get_db)
                         ) -> tuple:
    """业务路由双凭证：host JWT 或任何有效 AI workflow key 均放行。"""
    # 1) 优先尝试 host JWT（Bearer 且非 aik_ 前缀）
    if authorization and authorization.startswith("Bearer "):
        tok = authorization[7:]
        if not tok.startswith("aik_"):
            try:
                host = get_current_host(authorization=authorization, db=db)
                return ("host", host)
            except HTTPException:
                pass
    # 2) AI 凭证（任何有效 key 即放行，不限制 class_level）
    ai = get_current_ai(x_ai_key=x_ai_key, authorization=authorization,
                        request=request, db=db)
    return ("ai", ai)
