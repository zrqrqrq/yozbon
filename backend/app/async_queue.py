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
"""G-06 异步任务队列：秒级异步处理（交付通知、推理结算、保险理赔等）。

设计：
- DB 驱动的轻量队列（AsyncQueueTask 表）；
- 每个 Worker = 一个 asyncio 事件循环，循环内维护 inflight 个并发 slot；
  handler 仍是同步阻塞函数，统一投递到共享 ThreadPoolExecutor 执行；
  事件循环在某任务 I/O 等待期可切去派发其它任务 → 1 个 Worker 也能并发多单；
- 全局 threading.Semaphore(QUEUE_WORKER_MAX_CONCURRENCY) 非阻塞 acquire 限流，
  拿不到名额的 slot 立即退避，绝不占用 executor 线程空等（无线程饥饿）；
- Worker 事件循环崩溃自动重启 + 优雅停机；
- 优先级调度 + 指数退避重试 + 死信转存；
- 消费循环 start_queue_worker() 由 lifespan 启动（测试不自动跑）；
- 仅消费"队列任务"，跳过仅作状态记录的类型（NON_QUEUE_TASK_TYPES，见下）。

并发安全：
- 所有 slot 共享同一个 Semaphore(QUEUE_WORKER_MAX_CONCURRENCY)=共享线程池大小，
  同时执行的 handler 数恒 ≤ max_concurrency，超限 slot 退避排队；
- 任务领取用乐观锁（UPDATE WHERE status='pending' + rowcount 检查），
  保证同一任务仅被一个 slot 领取执行；
- 每次 dequeue_and_execute 持有独立 SessionLocal，互不干扰；
- DB 连接峰值 = 同时执行的 handler 数(≤ max_concurrency) + HTTP threads，
  需满足 ≤ DB_POOL_SIZE + DB_MAX_OVERFLOW。
"""
import asyncio
import json
import logging
import random
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable

from .config import settings
from .database import SessionLocal
from .models import AsyncQueueTask, QueueTaskHistory, OrchestrationRecord

logger = logging.getLogger(__name__)

# 任务注册表：task_type → handler function
_HANDLERS: dict[str, Callable] = {}

# 非队列任务类型：这些 task_type 复用 AsyncQueueTask 表仅作"状态记录"，
# 由各自业务端点同步执行（宿主端/AI 端任务编排），不参与队列消费。
# 若不排除，Worker 轮询会把 waiting-for-host-confirm 的 pending 记录误判为
# "no handler" 并置为 failed（宿主首次执行即 400）。
NON_QUEUE_TASK_TYPES: frozenset[str] = frozenset({
    "host_orchestration",  # 宿主端编排历史遗留行（捷径路由已下线，仅保留以防 Worker 误领取无 handler 的存量记录）
    "orchestration",       # AI 端编排（task_orchestrator.submit_and_run）
})

# Worker 组状态（供 shutdown 和健康查询）
_worker_threads: list[threading.Thread] = []
_worker_sem: threading.Semaphore | None = None
# 共享 job 线程池：所有 Worker 事件循环的 handler 统一投递到这里执行；
# 大小 = QUEUE_WORKER_MAX_CONCURRENCY（与 Semaphore 名额一致，保证不排队等线程）。
_job_executor: ThreadPoolExecutor | None = None
_worker_stats: dict = {"total_executed": 0, "total_failed": 0, "active": 0}
_stats_lock = threading.Lock()


def register_handler(task_type: str, fn: Callable):
    """注册任务类型处理器。fn(db, payload_dict) → result_string。"""
    _HANDLERS[task_type] = fn


def worker_running() -> bool:
    """是否有存活的队列 Worker 线程。

    供同步端点判断：Worker 在跑 → 只轮询等结果；无 Worker（测试 /
    QUEUE_WORKER_ENABLED=0）→ 端点自行 dequeue_and_execute() 排空，避免任务无人消费。
    """
    return any(t.is_alive() for t in _worker_threads)


def enqueue(task_type: str, payload: dict, priority: int = 5,
            max_retries: int = 3, delay_seconds: int = 0) -> int:
    """入队异步任务，返回 task_id。

    delay_seconds: 延迟执行（秒），用于退避重试。
    """
    db = SessionLocal()
    try:
        scheduled = datetime.utcnow() + timedelta(seconds=delay_seconds)
        task = AsyncQueueTask(
            task_type=task_type,
            payload=json.dumps(payload, default=str),
            priority=priority,
            max_retries=max_retries,
            scheduled_at=scheduled,
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _move_to_history(db, task: AsyncQueueTask, final_status: str):
    """把到达终态的队列行移入历史归档表（保留原 id 以便审计/对账），并删除队列行。

    调用方负责随后 commit。task 必须是队列表中的 AsyncQueueTask 实例。
    两个类映射不同表，同 session 内 add(hist)+delete(task) 不产生身份冲突。
    """
    now = datetime.utcnow()
    hist = QueueTaskHistory(
        id=task.id,
        task_type=task.task_type,
        payload=task.payload,
        priority=task.priority,
        status=final_status,
        max_retries=task.max_retries,
        retry_count=task.retry_count,
        result=task.result,
        scheduled_at=task.scheduled_at,
        started_at=task.started_at,
        finished_at=task.finished_at or now,
        created_at=task.created_at,
        archived_at=now,
    )
    db.add(hist)
    db.delete(task)


def dequeue_and_execute() -> bool:
    """从队列取一条活跃任务并执行（乐观锁领取）。返回是否有任务被执行。

    线程安全：多 slot 并发调用时，通过 UPDATE WHERE status='pending' 原子领取，
    rowcount==0 表示被其他 slot 抢先领取，立即返回 False 不执行。

    队列/历史分离：成功 或 超出重试上限的失败 → 移入历史归档表并从队列表删除；
    仍可重试的失败 → 留在队列表退避重排（仍是活跃项）。
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # 查找候选任务（不过滤锁，仅用于定位）；排除仅作状态记录的非队列类型
        task = db.query(AsyncQueueTask).filter(
            AsyncQueueTask.status == "pending",
            AsyncQueueTask.scheduled_at <= now,
            ~AsyncQueueTask.task_type.in_(NON_QUEUE_TASK_TYPES),
        ).order_by(AsyncQueueTask.priority, AsyncQueueTask.scheduled_at).first()

        if task is None:
            return False

        # 乐观锁领取：仅当 status 仍为 pending 时才成功（防并发重复领取）
        updated = db.query(AsyncQueueTask).filter(
            AsyncQueueTask.id == task.id,
            AsyncQueueTask.status == "pending"
        ).update({"status": "running", "started_at": now},
                 synchronize_session="fetch")
        db.commit()

        if updated == 0:
            # 被其他 slot 抢先领取
            return False

        # 重新加载 task 对象获取最新字段
        db.refresh(task)

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            # 无 handler 不可能因重试成功 → 终态，直接归档
            task.result = f"no handler for type '{task.task_type}'"
            task.finished_at = datetime.utcnow()
            _move_to_history(db, task, "failed")
            db.commit()
            with _stats_lock:
                _worker_stats["total_failed"] += 1
            return True

        try:
            payload = json.loads(task.payload) if task.payload else {}
            result = handler(db, payload)
            task.result = str(result) if result else ""
            task.finished_at = datetime.utcnow()
            _move_to_history(db, task, "success")
            db.commit()
            with _stats_lock:
                _worker_stats["total_executed"] += 1
        except Exception as exc:
            task.retry_count += 1
            if task.retry_count >= task.max_retries:
                # 终态失败 → 归档
                task.result = f"max_retries exceeded: {exc}"
                task.finished_at = datetime.utcnow()
                _move_to_history(db, task, "failed")
            else:
                # 指数退避重入队（留在队列表，仍活跃）
                backoff = 2 ** task.retry_count
                task.status = "pending"
                task.scheduled_at = datetime.utcnow() + timedelta(seconds=backoff)
                task.started_at = None
                task.result = f"retry {task.retry_count}: {exc}"
            db.commit()
            with _stats_lock:
                _worker_stats["total_failed"] += 1
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def queue_stats() -> dict:
    """队列状态统计（含 Worker 运行时指标）。

    队列/历史分离后分源统计：
    - pending / running：来自队列表 async_queue_tasks（只含活跃项）；
    - success / failed / expired：来自历史归档表 async_queue_task_history。
    """
    db = SessionLocal()
    try:
        from sqlalchemy import func
        stats = {}
        # 队列表：仅活跃项
        for status in ("pending", "running"):
            stats[status] = db.query(func.count(AsyncQueueTask.id)).filter(
                AsyncQueueTask.status == status).scalar() or 0
        # 历史归档表：终态
        hist_rows = db.query(
            QueueTaskHistory.status, func.count(QueueTaskHistory.id)
        ).group_by(QueueTaskHistory.status).all()
        hist_counts = {s: c for s, c in hist_rows}
        for status in ("success", "failed", "expired"):
            stats[status] = hist_counts.get(status, 0)
        stats["history_total"] = sum(hist_counts.values())
        stats["handlers_registered"] = len(_HANDLERS)
        stats["worker_threads"] = len([t for t in _worker_threads if t.is_alive()])
        stats["worker_active"] = _worker_stats["active"]
        stats["worker_executed"] = _worker_stats["total_executed"]
        stats["worker_failed"] = _worker_stats["total_failed"]
        return stats
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 多 Worker 消费引擎
# ---------------------------------------------------------------------------

async def _wait_or_stop(stop_event: threading.Event, timeout: float):
    """异步等待，但每 ~0.1s 轮询 threading.Event，stop_event 置位即提前返回。

    asyncio 无法直接 await 一个 threading.Event（lifespan 用线程级 stop 信号），
    故用细粒度 sleep 轮询，兼顾退避节奏与停机响应及时性。
    """
    step = 0.1
    remaining = timeout
    while remaining > 0 and not stop_event.is_set():
        await asyncio.sleep(min(step, remaining))
        remaining -= step


async def _slot_loop(worker_id: int, stop_event: threading.Event,
                     sem: threading.Semaphore, poll_interval: float,
                     ex: ThreadPoolExecutor):
    """单个并发 slot：非阻塞抢全局名额 → handler 投递线程池执行 → 释放名额。

    拿不到 Semaphore 名额（全局并发已满）立即退避重试，绝不占用 executor 线程，
    从根上杜绝"占着线程空等名额"导致的线程饥饿。handler 在线程池里同步阻塞执行
    （含视频/LLM 长轮询），事件循环在此期间空闲，可继续派发同 Worker 的其它 slot。
    """
    loop = asyncio.get_running_loop()
    name = f"queue-w{worker_id}"
    consecutive_errors = 0

    while not stop_event.is_set():
        # 全局并发硬闸：非阻塞 acquire，拿不到即退避（不占线程池）
        if not sem.acquire(blocking=False):
            await _wait_or_stop(stop_event, poll_interval)
            continue

        with _stats_lock:
            _worker_stats["active"] += 1
        executed = False
        errored = False
        try:
            executed = await loop.run_in_executor(ex, dequeue_and_execute)
            consecutive_errors = 0
        except Exception as exc:
            errored = True
            consecutive_errors += 1
            logger.error("[%s] unexpected error (consecutive=%d): %s\n%s",
                         name, consecutive_errors, exc, traceback.format_exc())
        finally:
            with _stats_lock:
                _worker_stats["active"] -= 1
            sem.release()

        # 退避决策在释放名额之后进行，确保退避期不占用并发额度
        if errored:
            if consecutive_errors >= 5:
                backoff = min(2 ** consecutive_errors, 30)
                logger.warning("[%s] backing off %ds after %d errors",
                               name, backoff, consecutive_errors)
                await _wait_or_stop(stop_event, backoff)
            else:
                await _wait_or_stop(stop_event, 1.0)
        elif not executed:
            # 队列暂无任务，短暂休眠再试（带抖动，防雷鸣羊群）
            sleep_time = poll_interval + random.uniform(0, poll_interval * 0.5)
            await _wait_or_stop(stop_event, sleep_time)


def _worker_loop(worker_id: int, stop_event: threading.Event,
                 sem: threading.Semaphore, poll_interval: float,
                 inflight: int, ex: ThreadPoolExecutor):
    """单个 Worker 线程：持有一个 asyncio 事件循环，并发跑 inflight 个 slot。

    这是"1 个 Worker 也能并发多单"的关键：事件循环在某个 handler 的 I/O 等待期
    切去派发其它 slot，实际并发由 inflight 与全局 Semaphore 名额共同决定。
    """
    name = f"queue-w{worker_id}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("[%s] async worker started (inflight=%d, sem available=%d)",
                name, inflight, sem._value)

    async def _main():
        slots = [
            asyncio.ensure_future(
                _slot_loop(worker_id, stop_event, sem, poll_interval, ex))
            for _ in range(inflight)
        ]
        # 等待停机信号
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        for s in slots:
            s.cancel()
        await asyncio.gather(*slots, return_exceptions=True)

    try:
        loop.run_until_complete(_main())
    except Exception:
        logger.exception("[%s] event loop crashed", name)
        raise
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)
        logger.info("[%s] async worker stopped", name)


def _supervisor_loop(stop_event: threading.Event, n_workers: int,
                     sem: threading.Semaphore, poll_interval: float,
                     inflight: int, ex: ThreadPoolExecutor):
    """Supervisor 线程：监控 Worker 事件循环存活，崩溃自动重启。

    每 10 秒检查一次 Worker 存活数，如果低于期望值则补充启动新 Worker。
    这是确保服务器稳定性的关键机制——即使个别 Worker 因 OOM/异常终止，
    Supervisor 会自动恢复而不影响整体消费能力。
    """
    workers: list[threading.Thread] = []
    check_interval = 10.0

    def _spawn(idx: int) -> threading.Thread:
        t = threading.Thread(
            target=_worker_loop,
            args=(idx, stop_event, sem, poll_interval, inflight, ex),
            name=f"aijuhe-queue-w{idx}",
            daemon=True,
        )
        t.start()
        return t

    # 初始启动 N 个 Worker
    for i in range(n_workers):
        workers.append(_spawn(i))

    _worker_threads.extend(workers)
    logger.info("queue supervisor: started %d workers (inflight/worker=%d, "
                "max_concurrency=%d, poll=%.1fs)",
                n_workers, inflight, sem._value, poll_interval)

    # 监控循环
    next_id = n_workers
    while not stop_event.is_set():
        stop_event.wait(check_interval)
        if stop_event.is_set():
            break

        # 检查存活数
        alive = [w for w in workers if w.is_alive()]
        dead_count = n_workers - len(alive)
        if dead_count > 0:
            logger.warning("queue supervisor: %d workers dead, restarting...", dead_count)
            # 清理已死线程引用
            workers[:] = alive
            # 补充启动
            for _ in range(dead_count):
                t = _spawn(next_id)
                workers.append(t)
                _worker_threads.append(t)
                next_id += 1

    # 等待所有 Worker 退出
    for w in workers:
        w.join(timeout=5)
    logger.info("queue supervisor: all workers stopped")


def _reaper_loop(stop_event: threading.Event):
    """运行时僵尸回收器（daemon 线程）：双重保障不允许卡死任务存在。

    每 30 秒：
    - 回收超时 running 作业（started_at 超 QUEUE_WORKER_TASK_TIMEOUT）→ 重试或归档失败；
    - 每 QUEUE_HISTORY_SWEEP_EVERY 周期：清理超过 QUEUE_HISTORY_TTL_DAYS 天的历史归档行
      （0 = 永久保留，跳过）。
    每 5 分钟：巡检编排记录，将无活跃作业支撑的 planning/running 编排标记 failed。

    编排记录已独立到 OrchestrationRecord（不再复用队列表），孤儿巡检据此表进行。
    """
    default_timeout = settings.QUEUE_WORKER_TASK_TIMEOUT  # 默认 300s

    def _job_timeout(task_type: str) -> int:
        # 宿主捷径已下线，长链路 job 不复存在；统一采用默认预算，
        # 长耗时外部调用由 handler 内部自行控制超时。
        return default_timeout

    check_interval = 30.0
    orch_scan_every = 10  # 每 10 次（= 5 分钟）做一次编排巡检
    ttl_days = settings.QUEUE_HISTORY_TTL_DAYS
    ttl_sweep_every = settings.QUEUE_HISTORY_SWEEP_EVERY
    cycle = 0
    logger.info("[reaper] started: timeout=%ds, interval=%.0fs, orch_scan_every=%d cycles, "
                "history_ttl=%sd(sweep_every=%d cycles)",
                default_timeout, check_interval, orch_scan_every,
                ttl_days if ttl_days > 0 else "∞", ttl_sweep_every)

    while not stop_event.is_set():
        stop_event.wait(check_interval)
        if stop_event.is_set():
            break
        cycle += 1

        db = SessionLocal()
        try:
            # ---- 高频：按统一超时回收 running 作业 ----
            running_jobs = db.query(AsyncQueueTask).filter(
                AsyncQueueTask.status == "running",
                ~AsyncQueueTask.task_type.in_(NON_QUEUE_TASK_TYPES),
                AsyncQueueTask.started_at.isnot(None),
            ).all()

            now = datetime.utcnow()
            stale_jobs = [
                j for j in running_jobs
                if j.started_at <= now - timedelta(seconds=_job_timeout(j.task_type))
            ]

            if stale_jobs:
                reaped = 0
                for job in stale_jobs:
                    to = _job_timeout(job.task_type)
                    job.retry_count += 1
                    if job.retry_count >= job.max_retries:
                        # 终态失败 → 移出队列表，归档历史（保留 started_at 供审计）
                        job.result = f"reaper: timeout after {to}s, max_retries exceeded"
                        job.finished_at = datetime.utcnow()
                        _move_to_history(db, job, "failed")
                    else:
                        # 仍可重试 → 留队列表退避重排
                        job.status = "pending"
                        job.scheduled_at = datetime.utcnow() + timedelta(seconds=2)
                        job.result = (f"reaper: timeout after {to}s, "
                                      f"requeued (attempt {job.retry_count}/{job.max_retries})")
                        job.started_at = None
                    reaped += 1
                db.commit()
                timeout_summary = ", ".join(
                    f"{_job_timeout(j.task_type)}s:{j.task_type}"
                    for j in {j.id: j for j in stale_jobs}.values()
                )
                logger.warning("[reaper] reclaimed %d stale jobs (%s)", reaped, timeout_summary)

            # ---- 低频：编排孤儿巡检（查独立编排记录表）----
            if cycle % orch_scan_every == 0:
                active_jobs = db.query(AsyncQueueTask).filter(
                    AsyncQueueTask.status.in_(("pending", "running")),
                    ~AsyncQueueTask.task_type.in_(NON_QUEUE_TASK_TYPES),
                ).all()
                active_orch_ids: set[int] = set()
                for j in active_jobs:
                    try:
                        p = json.loads(j.payload) if j.payload else {}
                        # 入队用 orch_id；兼容旧字段 orchestration_id
                        oid = p.get("orch_id") or p.get("orchestration_id")
                        if oid is not None:
                            active_orch_ids.add(int(oid))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass

                stuck_orchs = db.query(OrchestrationRecord).filter(
                    OrchestrationRecord.status.in_(("planning", "running")),
                ).all()

                orphan_count = 0
                for orch in stuck_orchs:
                    if orch.id not in active_orch_ids:
                        orch.status = "failed"
                        orch.result = "reaper: orphaned (no active job, marked after grace period)"
                        orch.finished_at = datetime.utcnow()
                        orphan_count += 1

                if orphan_count:
                    db.commit()
                    logger.warning("[reaper] marked %d orphaned orchestrations as failed", orphan_count)

            # ---- 低频：历史归档 TTL 清理 ----
            if ttl_days > 0 and cycle % ttl_sweep_every == 0:
                cutoff = datetime.utcnow() - timedelta(days=ttl_days)
                deleted = db.query(QueueTaskHistory).filter(
                    QueueTaskHistory.finished_at.isnot(None),
                    QueueTaskHistory.finished_at < cutoff,
                ).delete(synchronize_session=False)
                if deleted:
                    db.commit()
                    logger.info("[reaper] purged %d history rows older than %d days", deleted, ttl_days)

        except Exception:
            db.rollback()
            logger.exception("[reaper] error during scan")
        finally:
            db.close()

    logger.info("[reaper] stopped")


def start_queue_worker(stop_event: threading.Event, interval_seconds: float = None):
    """多 Worker 消费引擎入口（由 lifespan 启动，阻塞直到 stop_event 触发）。

    启动 N 个 Worker 线程并行消费 + Supervisor 自动监控重启 + Reaper 超时回收。
    Semaphore 硬闸确保峰值并发不超过 QUEUE_WORKER_MAX_CONCURRENCY。
    """
    n_workers = settings.QUEUE_WORKER_THREADS
    inflight = settings.QUEUE_WORKER_INFLIGHT_PER_WORKER
    max_conc = settings.QUEUE_WORKER_MAX_CONCURRENCY
    poll_interval = interval_seconds or settings.QUEUE_WORKER_POLL_INTERVAL

    # 全局并发硬闸 = 同时执行的 handler 数上限 = 共享线程池大小。
    # slot 非阻塞 acquire：拿到名额才把 handler 投递到线程池，拿不到立即退避，
    # 因此同时占用线程池的任务数恒 ≤ max_conc，线程池恰够、不会排队等线程。
    sem = threading.Semaphore(max_conc)
    global _worker_sem, _job_executor
    _worker_sem = sem

    ex = ThreadPoolExecutor(max_workers=max_conc, thread_name_prefix="aijuhe-job")
    _job_executor = ex

    logger.info("async_queue starting: workers=%d, inflight/worker=%d, "
                "max_concurrency=%d, poll=%.1fs, handlers=%d",
                n_workers, inflight, max_conc, poll_interval, len(_HANDLERS))

    # 启动运行时僵尸回收器（daemon 线程，随主进程退出）
    reaper = threading.Thread(
        target=_reaper_loop, args=(stop_event,),
        name="aijuhe-queue-reaper", daemon=True)
    reaper.start()

    # Supervisor 在当前线程阻塞运行，内部管理 Worker 事件循环的生命周期
    try:
        _supervisor_loop(stop_event, n_workers, sem, poll_interval, inflight, ex)
    finally:
        # 停机：worker 事件循环已退出，不再提交新任务；取消排队项并释放线程池
        ex.shutdown(wait=False, cancel_futures=True)
        _job_executor = None
    logger.info("async_queue worker stopped (executed=%d, failed=%d)",
                _worker_stats["total_executed"], _worker_stats["total_failed"])


# ---------------------------------------------------------------------------
# 启动恢复：清理僵尸任务（进程崩溃/重启遗留的 running 作业和孤儿编排记录）
# ---------------------------------------------------------------------------

def recover_stale_tasks() -> dict:
    """启动恢复（由 lifespan 在 Worker 启动前调用，阻塞执行）。

    两阶段清理：
    Phase 1 — 重置孤儿队列作业：所有 status='running' 的可消费作业 → 'pending'，
              Worker 重新领取执行（等同 retry，计入 retry_count）。
              若 retry_count 超限则移出队列表并归档历史 failed。
    Phase 2 — 标记孤儿编排记录：OrchestrationRecord 中 status in ('planning','running')
              且无对应活跃队列作业支撑的 → 'failed'（说明作业已丢失或从未入队）。

    返回值：{"reset_jobs": N, "failed_orchs": M}
    """
    db = SessionLocal()
    stats = {"reset_jobs": 0, "failed_orchs": 0}
    try:
        now = datetime.utcnow()

        # ---- Phase 1: 重置僵尸队列作业 ----
        zombie_jobs = db.query(AsyncQueueTask).filter(
            AsyncQueueTask.status == "running",
            ~AsyncQueueTask.task_type.in_(NON_QUEUE_TASK_TYPES),
        ).all()

        for job in zombie_jobs:
            job.retry_count += 1
            if job.retry_count >= job.max_retries:
                # 终态失败 → 移出队列表，归档历史（保留 started_at 供审计）
                job.result = f"recovered: max_retries exceeded (was stuck since {job.started_at})"
                job.finished_at = now
                _move_to_history(db, job, "failed")
            else:
                job.status = "pending"
                # 延迟 2 秒重新入队，给其他 Phase-1 恢复留出缓冲
                job.scheduled_at = now + timedelta(seconds=2)
                job.result = f"recovered: reset from running (attempt {job.retry_count}/{job.max_retries})"
                job.started_at = None
            stats["reset_jobs"] += 1

        if zombie_jobs:
            db.commit()
            logger.info("recover_stale_tasks: Phase1 reset %d zombie jobs", len(zombie_jobs))

        # ---- Phase 2: 标记孤儿编排记录 ----
        # 收集所有活跃（pending/running）工作作业关联的 orchestration_id
        active_work_jobs = db.query(AsyncQueueTask).filter(
            AsyncQueueTask.status.in_(("pending", "running")),
            ~AsyncQueueTask.task_type.in_(NON_QUEUE_TASK_TYPES),
        ).all()

        # 从 payload JSON 提取 orchestration_id
        active_orch_ids: set[int] = set()
        for job in active_work_jobs:
            try:
                p = json.loads(job.payload) if job.payload else {}
                # 入队用 orch_id；兼容旧字段 orchestration_id
                oid = p.get("orch_id") or p.get("orchestration_id")
                if oid is not None:
                    active_orch_ids.add(int(oid))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # 查找卡死的编排记录（查独立编排表 OrchestrationRecord）
        stuck_orchs = db.query(OrchestrationRecord).filter(
            OrchestrationRecord.status.in_(("planning", "running")),
        ).all()

        for orch in stuck_orchs:
            if orch.id not in active_orch_ids:
                orch.status = "failed"
                orch.result = "recovered: no active job found (orphaned after restart)"
                orch.finished_at = now
                stats["failed_orchs"] += 1

        if stats["failed_orchs"]:
            db.commit()
            logger.info("recover_stale_tasks: Phase2 failed %d orphaned orchestrations",
                        stats["failed_orchs"])

        logger.info("recover_stale_tasks complete: reset=%d, failed_orchs=%d",
                    stats["reset_jobs"], stats["failed_orchs"])
    except Exception:
        db.rollback()
        logger.exception("recover_stale_tasks: unexpected error during recovery")
    finally:
        db.close()

    return stats


# ---- 内置处理器注册 ----

def _handle_delivery_notify(db, payload):
    """交付通知（占位，后续对接 notify_service）。"""
    from . import notify_service
    citizen_id = payload.get("citizen_id", 0)
    title = payload.get("title", "Task delivery notification")
    body = payload.get("body", "")
    notify_service.send_notification(db, citizen_id, title, body)
    return "ok"


def _handle_settlement_retry(db, payload):
    """推理结算重试。"""
    from .training import settle_royalties
    settle_royalties(db, payload.get("model_asset_id", 0))
    return "ok"


def _handle_insurance_claim_check(db, payload):
    """保险到期自动理赔检查。"""
    from .insurance import auto_check_expired_policies
    auto_check_expired_policies(db)
    return "ok"


register_handler("delivery_notify", _handle_delivery_notify)
register_handler("settlement_retry", _handle_settlement_retry)
register_handler("insurance_check", _handle_insurance_claim_check)
