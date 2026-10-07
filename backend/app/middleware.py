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
"""全局中间件：G-02 API版本化、G-03 结构化日志+RequestID、G-04 熔断、G-09 Webhook签名、G-10 幂等键。

设计原则：
- 全部中间件通过 ASGI 层注入，不改动既有路由函数签名；
- 开发/测试模式宽容（版本化仅添加兼容路径不删除旧路径）；
- 日志 JSON 格式输出，包含 request_id 实现全链路追踪。
"""
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

logger = logging.getLogger("aijuhe.middleware")


# ==================== G-03: 结构化 JSON 日志 + Request ID ====================

class StructuredFormatter(logging.Formatter):
    """JSON 格式日志输出器。"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if hasattr(record, "method"):
            log_entry["method"] = record.method
            log_entry["path"] = record.path
            log_entry["status_code"] = record.status_code
            log_entry["duration_ms"] = record.duration_ms
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def setup_structured_logging():
    """替换根 logger 为 JSON 格式（生产推荐）。"""
    root = logging.getLogger()
    for h in root.handlers:
        h.setFormatter(StructuredFormatter())


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求分配唯一 request_id，注入响应头和日志上下文。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        # 结构化访问日志
        access_logger = logging.getLogger("aijuhe.access")
        access_logger.info(
            "%s %s %d %.2fms",
            request.method, request.url.path, response.status_code, duration_ms,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            }
        )
        return response


# ==================== G-02: API 版本化 ====================

class APIVersionMiddleware(BaseHTTPMiddleware):
    """API 版本化中间件：
    - 所有 /api/ 路由自动兼容 /api/v1/ 前缀（添加版本路径不破坏旧路径）；
    - 响应头 X-API-Version 返回当前版本。
    - 未来不兼容变更通过新版本前缀 /api/v2/ 隔离。
    """

    VERSION = "v1"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 如果请求路径含 /api/v1/，重写为 /api/（向后兼容）
        path = request.scope.get("path", "")
        if path.startswith("/api/v1/"):
            request.scope["path"] = "/api/" + path[len("/api/v1/"):]
        elif path == "/api/v1":
            request.scope["path"] = "/api"
        response = await call_next(request)
        response.headers["X-API-Version"] = self.VERSION
        return response


# ==================== G-04: 外部调用熔断器 ====================

class CircuitBreaker:
    """简单熔断器：CLOSED → OPEN → HALF_OPEN 三态转换。

    用法：
        breaker = CircuitBreaker(name="rh_llm", failure_threshold=5, recovery_seconds=60)
        with breaker:
            external_call()
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_seconds: int = 60, timeout_seconds: int = 30):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.timeout_seconds = timeout_seconds
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0
        self._half_open_successes = 0

    def record_success(self):
        if self.state == self.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= 2:
                self.state = self.CLOSED
                self.failure_count = 0
                self._half_open_successes = 0
        elif self.state == self.CLOSED:
            self.failure_count = 0

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
            self._half_open_successes = 0
        elif self.failure_count >= self.failure_threshold:
            self.state = self.OPEN
            logger.warning("circuit_breaker %s → OPEN (failures=%d)",
                           self.name, self.failure_count)

    @property
    def available(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            if time.time() - self.last_failure_time > self.recovery_seconds:
                self.state = self.HALF_OPEN
                self._half_open_successes = 0
                logger.info("circuit_breaker %s → HALF_OPEN", self.name)
                return True
            return False
        return True  # HALF_OPEN allows trial

    def __enter__(self):
        if not self.available:
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN, request rejected")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.record_failure()
        else:
            self.record_success()
        return False


class CircuitOpenError(Exception):
    """熔断器拒绝请求。"""
    pass


# 全局熔断器实例注册表
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """获取/创建全局熔断器实例（懒初始化）。"""
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name=name, **kwargs)
    return _BREAKERS[name]


def circuit_breaker(name: str, **kwargs):
    """装饰器形式使用熔断器。"""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kw):
            breaker = get_breaker(name, **kwargs)
            with breaker:
                return fn(*args, **kw)
        return wrapper
    return decorator


# ==================== G-09: Webhook HMAC 签名验证 ====================

class WebhookSignatureMiddleware(BaseHTTPMiddleware):
    """对 /api/webhooks/ 路径的请求验证 HMAC-SHA256 签名。

    约定：
    - 发送方计算 body 的 HMAC-SHA256，放入 X-Webhook-Signature 头；
    - 接收方用配置的 webhook_secret 验证；
    - 测试环境跳过（APP_ENV=test）。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.scope.get("path", "")
        if not path.startswith("/api/webhooks/"):
            return await call_next(request)
        if settings.APP_ENV == "test":
            return await call_next(request)

        signature = request.headers.get("X-Webhook-Signature", "")
        secret = getattr(settings, "WEBHOOK_SECRET", "") or getattr(settings, "SECRET_KEY", "")
        if not secret:
            return await call_next(request)  # 未配置则不强制

        body = await request.body()
        import hmac as _hmac
        import hashlib
        expected = _hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

        if not _hmac.compare_digest(signature, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "webhook signature verification failed"})

        return await call_next(request)


# ==================== G-10: 幂等键中间件 ====================

class IdempotencyMiddleware(BaseHTTPMiddleware):
    """全局幂等键中间件：
    - 写请求（POST/PUT/PATCH）若携带 X-Idempotency-Key 头，启用幂等处理；
    - 首次请求正常执行并存储响应快照；
    - 重复请求直接返回缓存响应（重放）；
    - 键过期时间默认 24h。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 仅处理带幂等键头的写请求
        idem_key = request.headers.get("X-Idempotency-Key")
        if not idem_key or request.method not in ("POST", "PUT", "PATCH"):
            return await call_next(request)

        from .database import SessionLocal
        from .models import IdempotencyKey

        db = SessionLocal()
        try:
            # 查找已存在的幂等记录
            existing = db.query(IdempotencyKey).filter(
                IdempotencyKey.key == idem_key
            ).first()

            if existing is not None:
                # 过期检查
                if existing.expires_at and existing.expires_at < datetime.utcnow():
                    db.delete(existing)
                    db.commit()
                else:
                    # 重放缓存响应
                    import json as _json
                    content = _json.loads(existing.response_snapshot) if existing.response_snapshot else {}
                    return JSONResponse(
                        status_code=existing.status_code,
                        content=content,
                        headers={"X-Idempotency-Replay": "true"})

            # 首次请求：执行并捕获响应
            response = await call_next(request)

            # 仅成功响应才存储（4xx/5xx 不缓存，允许重试）
            if response.status_code < 400:
                body_bytes = b""
                async for chunk in response.body_iterator:
                    body_bytes += chunk if isinstance(chunk, bytes) else chunk.encode()
                try:
                    snapshot = body_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    snapshot = ""

                caller_type = ""
                caller_id = 0
                # 尝试从 header 推断调用者（轻量，不做完整鉴权）
                auth = request.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    caller_type = "host"

                record = IdempotencyKey(
                    key=idem_key,
                    endpoint=request.url.path,
                    caller_type=caller_type,
                    caller_id=caller_id,
                    response_snapshot=snapshot,
                    status_code=response.status_code,
                    expires_at=datetime.utcnow() + timedelta(hours=24),
                )
                db.add(record)
                db.commit()

                return Response(
                    content=body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )

            return response
        finally:
            db.close()
