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
"""AIjuhe 主入口。模块按落地实施蓝图 L0→L13 顺序填充；
各线 router 文件经 app/routers 自动发现注册（见 routers/__init__.py）。

G-01~G-05 基础设施增强：API 版本化 / 结构化日志 / 熔断器 / 幂等键 / Webhook签名 / 异步队列。
"""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .database import init_db
from .routers import all_routers

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # G-03: 生产环境切换为 JSON 结构化日志
    if settings.APP_ENV == "prod":
        from .middleware import setup_structured_logging
        setup_structured_logging()

    # 城主治理中枢：GOVERNOR_ENABLED 时启动后台守护线程跑自主治理循环（默认关，
    # 开发/测试不自动跑；测试经 governor.run_tick() 显式触发）。stop_event 用于优雅收尾。
    governor_thread = None
    stop_event = None
    if settings.GOVERNOR_ENABLED and settings.APP_ENV != "test":
        from .governor import governor_loop
        stop_event = threading.Event()
        governor_thread = threading.Thread(
            target=governor_loop, kwargs={"stop_event": stop_event},
            name="aijuhe-governor", daemon=True)
        governor_thread.start()
        logger.info("城主治理中枢已启动（GOVERNOR_ENABLED=1）")

    # G-06: 异步任务队列 worker（多 Worker + Semaphore 限流；生产启动；测试不自动跑）
    queue_thread = None
    queue_stop = None
    if settings.APP_ENV != "test" and getattr(settings, "QUEUE_WORKER_ENABLED", True):
        from .async_queue import start_queue_worker, recover_stale_tasks
        # 启动恢复：清理上次进程遗留的僵尸任务（running 作业重置、孤儿编排标记 failed）
        recovery = recover_stale_tasks()
        if recovery["reset_jobs"] or recovery["failed_orchs"]:
            logger.info("启动恢复完成: 重置僵尸作业=%d, 孤儿编排标记failed=%d",
                        recovery["reset_jobs"], recovery["failed_orchs"])
        queue_stop = threading.Event()
        queue_thread = threading.Thread(
            target=start_queue_worker, kwargs={"stop_event": queue_stop},
            name="aijuhe-queue-supervisor", daemon=True)
        queue_thread.start()
        logger.info("异步队列已启动 (threads=%d, max_concurrency=%d)",
                    settings.QUEUE_WORKER_THREADS, settings.QUEUE_WORKER_MAX_CONCURRENCY)

    # 后台调度器（A-C1 修复）：平台"社会心跳"——日任务 / 备份 / 编制规划 / 托管付息
    # 等全部日级 job 的唯一驱动循环。此前 lifespan 漏调 start_background_scheduler()，
    # 导致全平台自动闭环在生产环境永不触发。测试环境内部直接返回 None（测试经
    # trigger 端点/直接调 run_due_jobs 显式驱动，保证确定性）。
    scheduler_task = None
    if settings.APP_ENV != "test" and getattr(settings, "SCHEDULER_ENABLED", True):
        from .scheduler import start_background_scheduler
        scheduler_task = start_background_scheduler()
        if scheduler_task is not None:
            logger.info("后台调度器已启动（社会心跳恢复）")
        else:
            logger.warning("后台调度器未能启动（无运行事件循环或已被禁用）")

    logger.info("AIjuhe started | env=%s | base=%s | routers=%d | scheduler=%s",
                settings.APP_ENV, settings.APP_BASE_URL, len(all_routers),
                "on" if scheduler_task is not None else "off")
    yield
    if stop_event is not None:
        stop_event.set()
    if queue_stop is not None:
        queue_stop.set()
    logger.info("AIjuhe shutdown")


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# ---- 中间件（注意顺序：后添加的先执行） ----
from .middleware import (  # noqa: E402
    RequestIDMiddleware,
    APIVersionMiddleware,
    WebhookSignatureMiddleware,
    IdempotencyMiddleware,
)

# CORS（P1：按 config CORS_ENABLED/CORS_ORIGINS 挂载）
if settings.CORS_ENABLED:
    from fastapi.middleware.cors import CORSMiddleware
    _cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 执行顺序：RequestID → APIVersion → WebhookSig → Idempotency → 路由
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(WebhookSignatureMiddleware)
app.add_middleware(APIVersionMiddleware)
app.add_middleware(RequestIDMiddleware)

# ---- 路由注册 ----
for r in all_routers:
    app.include_router(r)

# ---- 顶层探活端点（部署脚本 deploy-aijuhe.sh 与 nginx 健康检查的标准路径）----
# sys 路由已挂 /api/sys/health；此处再加 /api/health 别名，匹配部署脚本与文档约定。
@app.get("/api/health", tags=["sys"])
def health_root():
    """顶层探活。附带全站冷启动待机状态（城主规则 2026-10-06）：
    standby=True 表示尚未迎来首位对外正式 AI 公民、城主处于待机不工作状态。
    容错：状态查询失败不影响探活本身返回 200。"""
    out = {"ok": True, "app": settings.APP_NAME, "env": settings.APP_ENV}
    try:
        from .database import SessionLocal
        from .governor import standby_status
        _db = SessionLocal()
        try:
            out["standby"] = standby_status(_db)
        finally:
            _db.close()
    except Exception:  # noqa: BLE001  待机状态查询失败绝不拖累探活
        out["standby"] = None
    return out
