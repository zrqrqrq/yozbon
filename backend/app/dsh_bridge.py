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
"""DeepSeek Harness SDK 桥接模块（Phase 2，2026-10-07）。

定位：将 deepseek_harness SDK 封装为平台内部可调用接口，供 tool_registry 和
task_orchestrator 消费。按需启动 dsh 子进程，用完即关（2G 内存服务器必须）。

核心设计：
- run_dsh_task()：主入口，接收任务文本 → 返回标准化结果 dict。
- 按需启停：不常驻 dsh 进程（每次调用 start→run→close），释放内存。
- 容错：任何 SDK/子进程异常转为结构化 error 返回，绝不冒泡。
- 并发安全：用 threading.Lock 保护同时只有一个 dsh 实例（SDK 非线程安全）。

与 harness_engine.py（Phase 1 原生 Agent 循环）的区别：
- harness_engine = 纯 Python 实现的多步 tool_call 循环，零外部依赖。
- dsh_bridge = 委托给外部 DeepSeek Harness runtime（Node.js 子进程），
  它有自己的 shell、文件操作等系统级工具，能力更强但开销更大。
- 编排层优先走 dsh（高级），失败回退 harness_engine（原生）。
"""
from __future__ import annotations

import logging
import threading
import time

from .config import settings

logger = logging.getLogger(__name__)

# 全局锁：同时只允许一个 dsh 子进程（2G 服务器限制，且 SDK 非线程安全）
_dsh_lock = threading.Lock()


def _get_config():
    """构建 DeepSeekHarnessConfig（延迟 import，避免未安装时模块级报错）。"""
    from deepseek_harness import DeepSeekHarnessConfig

    return DeepSeekHarnessConfig(
        provider="deepseek-official",
        model=settings.DSH_MODEL,
        base_url=settings.DSH_BASE_URL,
        api_key=settings.DSH_API_KEY,
        dsh_home=settings.DSH_HOME,
        profile="sdk-minimal",
        cwd="/tmp",
        initialize_timeout_seconds=float(settings.DSH_INIT_TIMEOUT),
        request_timeout_seconds=float(settings.DSH_REQUEST_TIMEOUT),
        shutdown_timeout_seconds=5.0,
    )


def is_available() -> bool:
    """探测 DSH SDK 是否已安装且配置完备（用于 tool_registry 可用性检测）。"""
    if not settings.DSH_ENABLED:
        return False
    if not settings.DSH_API_KEY or not settings.DSH_BASE_URL:
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("deepseek_harness") is not None
    except Exception:  # noqa: BLE001
        return False


def run_dsh_task(task: str, *, model: str | None = None,
                 timeout: int | None = None) -> dict:
    """执行一次 DeepSeek Harness 任务（按需启停，用完释放）。

    Args:
        task: 任务描述文本（必填）
        model: 可选覆盖模型（默认用 settings.DSH_MODEL）
        timeout: 可选覆盖请求超时秒数

    Returns:
        {
            "ok": bool,
            "status": "completed" | "error" | "timeout" | "unavailable",
            "response": str,          # 最终回复文本
            "session_id": str,        # dsh 会话 ID（日志追踪）
            "finish_reason": str,     # "completed" / "error" / ...
            "elapsed_ms": int,
            "error": str,             # 仅 error 时有值
        }
    """
    if not task or not task.strip():
        return {"ok": False, "status": "error", "response": "",
                "session_id": "", "finish_reason": "",
                "elapsed_ms": 0, "error": "task is required"}

    if not is_available():
        return {"ok": False, "status": "unavailable", "response": "",
                "session_id": "", "finish_reason": "",
                "elapsed_ms": 0,
                "error": "DSH SDK not available (check DSH_ENABLED/API_KEY/BASE_URL)"}

    # 非阻塞尝试获取锁：如果另一个请求占用 dsh，立即返回 busy 而非排队等待
    acquired = _dsh_lock.acquire(timeout=min(timeout or settings.DSH_REQUEST_TIMEOUT, 120))
    if not acquired:
        return {"ok": False, "status": "unavailable", "response": "",
                "session_id": "", "finish_reason": "",
                "elapsed_ms": 0,
                "error": "DSH runtime busy (another task in progress)"}

    t0 = time.time()
    try:
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

        config = _get_config()
        # 允许按次覆盖 model / timeout
        if model:
            config = DeepSeekHarnessConfig(
                provider=config.provider, model=model,
                base_url=config.base_url, api_key=config.api_key,
                dsh_home=config.dsh_home, profile=config.profile,
                cwd=config.cwd,
                initialize_timeout_seconds=config.initialize_timeout_seconds,
                request_timeout_seconds=float(timeout or settings.DSH_REQUEST_TIMEOUT),
                shutdown_timeout_seconds=config.shutdown_timeout_seconds,
            )
        elif timeout:
            config = DeepSeekHarnessConfig(
                provider=config.provider, model=config.model,
                base_url=config.base_url, api_key=config.api_key,
                dsh_home=config.dsh_home, profile=config.profile,
                cwd=config.cwd,
                initialize_timeout_seconds=config.initialize_timeout_seconds,
                request_timeout_seconds=float(timeout),
                shutdown_timeout_seconds=config.shutdown_timeout_seconds,
            )

        harness = DeepSeekHarness(config=config)
        harness.start()

        try:
            result = harness.run(task.strip())
            elapsed = int((time.time() - t0) * 1000)
            response_text = str(result.final_response or "").strip()
            return {
                "ok": True,
                "status": "completed",
                "response": response_text,
                "session_id": str(result.session_id or ""),
                "finish_reason": str(result.finish_reason or "completed"),
                "elapsed_ms": elapsed,
                "error": "",
            }
        finally:
            try:
                harness.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("dsh close() error (non-fatal): %s", e)

    except ImportError:
        elapsed = int((time.time() - t0) * 1000)
        return {"ok": False, "status": "unavailable", "response": "",
                "session_id": "", "finish_reason": "",
                "elapsed_ms": elapsed,
                "error": "deepseek_harness package not installed"}
    except TimeoutError:
        elapsed = int((time.time() - t0) * 1000)
        logger.warning("DSH task timed out after %dms", elapsed)
        return {"ok": False, "status": "timeout", "response": "",
                "session_id": "", "finish_reason": "timeout",
                "elapsed_ms": elapsed,
                "error": f"DSH request timed out ({timeout or settings.DSH_REQUEST_TIMEOUT}s)"}
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - t0) * 1000)
        logger.error("DSH task failed: %s", exc, exc_info=True)
        return {"ok": False, "status": "error", "response": "",
                "session_id": "", "finish_reason": "error",
                "elapsed_ms": elapsed,
                "error": str(exc)[:500]}
    finally:
        _dsh_lock.release()


# ---------------------------------------------------------------------------
# tool_registry runner 适配器
# ---------------------------------------------------------------------------
def _run_agent_dsh(args: dict) -> dict:
    """agent.dsh 工具的 runner（供 tool_registry.execute 调用）。

    args schema:
        task: str（必填）—— 任务描述
        model: str（可选）—— 覆盖默认模型
        timeout: int（可选）—— 覆盖默认超时秒数
    """
    task = str(args.get("task", "")).strip()
    if not task:
        return {"ok": False, "error": "task required"}

    model = str(args.get("model", "")).strip() or None
    timeout_val = args.get("timeout")
    timeout = int(timeout_val) if timeout_val else None

    result = run_dsh_task(task, model=model, timeout=timeout)
    # 映射为 tool_registry 约定格式：ok=True 时附带完整 result 字段
    return result
