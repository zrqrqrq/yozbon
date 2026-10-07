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
"""P2 分布式链路追踪服务。

基于 W3C Trace Context 规范的轻量级追踪实现：
- trace_id: 32 hex chars (128-bit);
- span_id: 16 hex chars (64-bit);
- 采样决策：基于 trace_id hash 做确定性采样（同一 trace 全链路一致）；
- Span 持久化到 TraceSpan 表，支持慢调用/错误率查询。

使用方式：
    trace_id = tracing.start_span(None, None, None, "http.request", "gateway")
    # ... 业务处理 ...
    tracing.end_span(trace_id, span_id, "ok", {"status_code": 200})
"""
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import TraceSpan

logger = logging.getLogger(__name__)

# 进行中的 Span（内存计时，end_span 时计算 duration_ms）
_active_spans: dict = {}  # (trace_id, span_id) -> start_timestamp


def _now() -> datetime:
    return datetime.utcnow()


def _generate_trace_id() -> str:
    """生成 32 hex chars trace_id。"""
    return secrets.token_hex(16)


def _generate_span_id() -> str:
    """生成 16 hex chars span_id。"""
    return secrets.token_hex(8)


class TracingService:
    """分布式链路追踪服务。"""

    def start_span(self, trace_id: str = None, span_id: str = None,
                   parent_span_id: str = None, operation: str = "",
                   service_name: str = "aijuhe") -> dict:
        """开始一个新 Span。

        若 trace_id 为空则生成新 trace（新请求入口）。
        若采样决策为不采样，仍返回 trace_id 但不写入 DB。

        Returns:
            {"trace_id", "span_id", "sampled"}
        """
        if not trace_id:
            trace_id = _generate_trace_id()
        if not span_id:
            span_id = _generate_span_id()

        sampled = self.sample_decision(trace_id)
        if not sampled:
            return {"trace_id": trace_id, "span_id": span_id, "sampled": False}

        # 记录起始时间（内存计时）
        _active_spans[(trace_id, span_id)] = time.monotonic()

        db: Session = SessionLocal()
        try:
            span = TraceSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id or "",
                operation=operation,
                service_name=service_name,
                status="running",
                start_time=_now(),
            )
            db.add(span)
            db.commit()
            logger.debug("tracing: started span %s/%s op=%s svc=%s", trace_id, span_id, operation, service_name)
        finally:
            db.close()

        return {"trace_id": trace_id, "span_id": span_id, "sampled": True}

    def end_span(self, trace_id: str, span_id: str, status: str = "ok",
                 tags: dict = None) -> dict:
        """结束 Span，计算 duration_ms 并更新状态。

        Returns:
            {"trace_id", "span_id", "duration_ms", "status"}
        """
        key = (trace_id, span_id)
        start_ts = _active_spans.pop(key, None)
        duration_ms = int((time.monotonic() - start_ts) * 1000) if start_ts else 0

        db: Session = SessionLocal()
        try:
            span = (db.query(TraceSpan)
                    .filter(TraceSpan.trace_id == trace_id,
                            TraceSpan.span_id == span_id)
                    .first())
            if span is None:
                # Span 可能未被采样
                return {"trace_id": trace_id, "span_id": span_id, "duration_ms": duration_ms, "status": status}

            span.duration_ms = duration_ms
            span.status = status
            if tags:
                span.tags = json.dumps(tags)
            db.commit()
            logger.debug("tracing: ended span %s/%s dur=%dms status=%s", trace_id, span_id, duration_ms, status)
        finally:
            db.close()

        return {"trace_id": trace_id, "span_id": span_id, "duration_ms": duration_ms, "status": status}

    def get_trace(self, trace_id: str) -> dict:
        """获取完整链路（所有 Span）。"""
        db: Session = SessionLocal()
        try:
            spans = (db.query(TraceSpan)
                     .filter(TraceSpan.trace_id == trace_id)
                     .order_by(TraceSpan.start_time.asc())
                     .all())
            return {
                "trace_id": trace_id,
                "span_count": len(spans),
                "spans": [
                    {
                        "span_id": s.span_id,
                        "parent_span_id": s.parent_span_id,
                        "operation": s.operation,
                        "service_name": s.service_name,
                        "duration_ms": s.duration_ms,
                        "status": s.status,
                        "tags": json.loads(s.tags or "{}"),
                        "start_time": s.start_time.isoformat() if s.start_time else None,
                    }
                    for s in spans
                ],
            }
        finally:
            db.close()

    def get_slow_traces(self, service: str, threshold_ms: int = 1000, limit: int = 20) -> list:
        """查询慢调用（duration > threshold）。"""
        db: Session = SessionLocal()
        try:
            spans = (db.query(TraceSpan)
                     .filter(TraceSpan.service_name == service,
                             TraceSpan.duration_ms >= threshold_ms)
                     .order_by(TraceSpan.duration_ms.desc())
                     .limit(limit)
                     .all())
            return [
                {
                    "trace_id": s.trace_id,
                    "span_id": s.span_id,
                    "operation": s.operation,
                    "duration_ms": s.duration_ms,
                    "start_time": s.start_time.isoformat() if s.start_time else None,
                }
                for s in spans
            ]
        finally:
            db.close()

    def get_error_rate(self, service: str) -> dict:
        """获取服务错误率（最近 1000 条 Span）。"""
        db: Session = SessionLocal()
        try:
            recent = (db.query(TraceSpan)
                      .filter(TraceSpan.service_name == service)
                      .order_by(TraceSpan.id.desc())
                      .limit(1000)
                      .all())
            total = len(recent)
            errors = sum(1 for s in recent if s.status == "error")
            rate = errors / total if total > 0 else 0.0
            return {
                "service": service,
                "total_spans": total,
                "error_count": errors,
                "error_rate": round(rate, 4),
            }
        finally:
            db.close()

    def sample_decision(self, trace_id: str) -> bool:
        """基于 trace_id hash 做确定性采样。

        同一 trace_id 在所有服务中采样决策一致。
        采样率由 settings.TRACING_SAMPLE_RATE 控制。
        """
        if not settings.TRACING_ENABLED:
            return False
        rate = settings.TRACING_SAMPLE_RATE
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        h = int(hashlib.md5(trace_id.encode()).hexdigest()[:8], 16)
        return (h % 10000) < int(rate * 10000)

    def export_spans(self, trace_id: str) -> dict:
        """导出链路为 OTLP 兼容格式。"""
        trace = self.get_trace(trace_id)
        otlp_spans = []
        for s in trace["spans"]:
            otlp_spans.append({
                "traceId": trace_id,
                "spanId": s["span_id"],
                "parentSpanId": s["parent_span_id"],
                "name": s["operation"],
                "kind": "SPAN_KIND_SERVER",
                "status": {"code": 1 if s["status"] == "ok" else 2},
                "attributes": s["tags"],
                "startTimeUnixNano": int(datetime.fromisoformat(s["start_time"]).timestamp() * 1e9) if s["start_time"] else 0,
                "endTimeUnixNano": int((datetime.fromisoformat(s["start_time"]).timestamp() + s["duration_ms"] / 1000) * 1e9) if s["start_time"] else 0,
            })
        return {"resource_spans": [{"scope_spans": [{"spans": otlp_spans}]}]}


instance = TracingService()
