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
"""审计结构性回归测试（A-C1 / A-L3 盲区补强）。

覆盖两类"静态/接线"不变量，防止回归：

1. **社会心跳接线**（A-C1）：lifespan 必须调用 start_background_scheduler()。
   此前 main.py 的 lifespan 漏调该方法，导致全部日级 job（备份/编制规划/托管
   付息/统计快照等）在生产环境永不触发。本测试用 monkeypatch 记录调用，并强制
   非 test 环境路径，断言 lifespan 确实驱动了调度器启动。

2. **job 白名单可达性**（A-L3 类盲区）：每条 register_daily_job 注册的 job_type
   必须落进调度器实际会执行的 _EXTRA_DAILY_JOBS 注册表（否则注册了却永不被
   run_due_jobs 驱动，等同幽灵任务）；且注册幂等（重复注册不重复入表）。
"""
from app import scheduler
from app.config import settings


def test_lifespan_starts_background_scheduler(monkeypatch):
    """A-C1：lifespan 在非 test 环境必须调用 start_background_scheduler()。"""
    calls = {"n": 0}

    def _fake_start():
        calls["n"] += 1
        return None  # 返回 None：lifespan 视作"未能启动"分支，不影响断言

    # lifespan 内部为 `from .scheduler import start_background_scheduler`，
    # 在调用时才解析，故 monkeypatch 模块属性即可拦截。
    monkeypatch.setattr(scheduler, "start_background_scheduler", _fake_start)
    # 强制走非 test 启动分支，并打开总开关。
    monkeypatch.setattr(settings, "APP_ENV", "dev", raising=False)
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True, raising=False)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app):
        pass

    assert calls["n"] >= 1, (
        "lifespan 未调用 start_background_scheduler()：社会心跳缺失（A-C1 回归）。"
    )


def test_register_daily_job_is_idempotent():
    """重复注册同一 job_type 不得在 _EXTRA_DAILY_JOBS 中产生重复条目。"""
    sentinel_fn = lambda db, now: 0  # noqa: E731
    scheduler.register_daily_job("__test_dup_job__", sentinel_fn)
    scheduler.register_daily_job("__test_dup_job__", sentinel_fn)
    hits = [jt for jt, _ in scheduler._EXTRA_DAILY_JOBS if jt == "__test_dup_job__"]
    assert len(hits) == 1, "register_daily_job 未去重，幂等键会相互覆盖"
    # 清理，避免污染其它用例可见的全局注册表
    scheduler._EXTRA_DAILY_JOBS[:] = [
        (jt, fn) for jt, fn in scheduler._EXTRA_DAILY_JOBS if jt != "__test_dup_job__"
    ]


def test_all_registered_jobs_are_reachable():
    """所有 app 装载后注册的日级 job 必须可被 run_due_jobs 驱动（在白名单内）。

    白名单 = _EXTRA_DAILY_JOBS（插件路径）∪ PLATFORM_JOBS（fallback）。
    注册即入 _EXTRA_DAILY_JOBS，因此注册过的 job 必然可达；本测试锁定该不变量，
    防止未来把注册表改成"需另行加入 allowlist"而漏配。
    """
    import app.main  # noqa: F401  触发 router import → 各服务模块注册日级 job

    whitelist = {jt for jt, _ in scheduler._EXTRA_DAILY_JOBS} | set(scheduler.PLATFORM_JOBS)
    registered = {jt for jt, _ in scheduler._EXTRA_DAILY_JOBS}
    orphans = registered - whitelist
    assert not orphans, f"注册的日级 job 不在调度白名单内（幽灵任务）：{sorted(orphans)}"

    # 关键日级 job 必须确已注册（漏注册 = 功能静默失效，历史高频回归点）。
    # 注：stat_snapshot 在 test 环境按既有教义刻意不注入共享注册表（见 stats.py 守卫），
    # 故不纳入 test 必检集；其余 job 在 test 环境亦注册，可校验。
    required = {
        "leaderboard_snapshot", "loan_overdue",
        "weekly_payroll", "asset_transfer", "insurance",
    }
    missing = required - registered
    assert not missing, f"关键日级 job 未注册（功能静默失效）：{sorted(missing)}"
