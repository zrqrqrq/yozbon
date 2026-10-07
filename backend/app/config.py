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
"""AIjuhe 配置。复用 RunVerseHub 的配置风格:环境变量驱动 + 运行时热更新表。

.env 加载：启动时把 backend/.env 注入 os.environ（不覆盖已存在的环境变量，
与 RunVerseHub config.py 同口径；conftest 提前设置的 DB_URL/APP_ENV 不受影响）。
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # 未装 python-dotenv 时静默跳过（仅系统环境变量生效）
    pass


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _parse_weight_tiers(spec: str) -> dict:
    """解析「type:weight,...」为 {type: int(weight)}。weight 越大越先被城主处理。

    非法/空片段静默跳过（容错优先，不因脏配置启动失败）。
    """
    out: dict = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, _, v = part.partition(":")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            out[k] = int(v)
        except ValueError:
            continue
    return out


def _auto_llm_provider() -> str:
    """LLM 通道自动选择：测试环境一律 echo（不发网络）；生产环境若已配置 RH 企业级 key
    （RH LLM API 仅支持企业级-共享 Key）则默认走 runninghub，复用现有 RH 密钥零额外配置；
    否则 echo。显式 LLM_PROVIDER 优先（覆盖本函数）。"""
    if (os.environ.get("APP_ENV") or "dev").lower() == "test":
        return "echo"
    if os.environ.get("RH_API_KEY_ENTERPRISE") or os.environ.get("RH_API_KEY"):
        return "runninghub"
    return "echo"


def _auto_vlm_provider() -> str:
    """VLM 通道自动选择：有 VLM_API_KEY → siliconflow（默认）；否则 echo（不真调，兜底 pass）。"""
    if os.environ.get("VLM_API_KEY"):
        return "siliconflow"
    return "echo"


class Settings:
    # ---- 基础 ----
    APP_ENV: str = _get("APP_ENV", "dev")               # dev / prod
    APP_NAME: str = _get("APP_NAME", "yozbon")
    APP_BASE_URL: str = _get("APP_BASE_URL", "http://127.0.0.1:8000")
    DB_URL: str = _get("DB_URL", "sqlite:///./aijuhe.db")

    # ---- 认证（复用 RunVerseHub security.py 模式）----
    SECRET_KEY: str = _get("SECRET_KEY", "dev-secret-change-me")
    JWT_EXPIRE_MINUTES: int = int(_get("JWT_EXPIRE_MINUTES", "10080"))  # 7 天

    # ---- 线程池（复用 SQLite 连接池经验：池容量 ≥ 线程槽位，防 QueuePool 卡死）----
    THREADPOOL_TOKENS: int = int(_get("THREADPOOL_TOKENS", "32"))
    HEAVY_WORKERS: int = int(_get("HEAVY_WORKERS", "4"))
    DB_POOL_SIZE: int = int(_get("DB_POOL_SIZE", "24"))
    DB_MAX_OVERFLOW: int = int(_get("DB_MAX_OVERFLOW", "8"))
    DB_POOL_TIMEOUT: int = int(_get("DB_POOL_TIMEOUT", "15"))
    DB_POOL_RECYCLE: int = int(_get("DB_POOL_RECYCLE", "1800"))

    # ---- 经济参数（默认值；上线后全部进 runtime_config 热更新表）----
    # 与 docs/经济模型与社会规则.md §八 参数总表一一对应
    AC_TO_CNY: float = float(_get("AC_TO_CNY", "0.35"))      # 1 AC = ¥0.35（面值，仅展示口径）
    AC_TO_USD: float = float(_get("AC_TO_USD", "0.05"))      # 1 AC = $0.05（面值，仅展示口径）
    # 发行/背书价：1 USD 充值兑换 AC 的保守锚定价（低于面值，给发行闸门留余量）
    AC_ISSUE_USD_RATE: float = float(_get("AC_ISSUE_USD_RATE", "0.015"))  # $0.015/AC
    # 发行闸门：非现金发行（人力/算力签约金等无真金白银背书）总量 ≤ IssuanceCeilingRatio × CashReserve
    ISSUANCE_CEILING_RATIO: float = float(_get("ISSUANCE_CEILING_RATIO", "0.25"))
    LABOR_ISSUE_CAP: float = float(_get("LABOR_ISSUE_CAP", "0.12"))     # 人力发行子上限 ≤12%×CashReserve
    COMPUTE_ISSUE_CAP: float = float(_get("COMPUTE_ISSUE_CAP", "0.12"))  # 算力发行子上限 ≤12%×CashReserve
    # 温和通胀目标带：货币供应年增速落在 [LOW, HIGH]，超上沿收紧、跌破下沿兜底投放
    INFLATION_TARGET_LOW: float = float(_get("INFLATION_TARGET_LOW", "0.03"))   # 3%/年
    INFLATION_TARGET_HIGH: float = float(_get("INFLATION_TARGET_HIGH", "0.06"))  # 6%/年
    # ---- e6 城主经济自主：通胀估算窗口 + 通缩兜底阶梯（spec v2 标准二闭环）----
    # 每日 money_supply 快照对比，估算窗口增速（把"年增速带"折算到窗口口径判定区间）。
    ECON_MS_SNAPSHOT_WINDOW_DAYS: int = int(_get("ECON_MS_SNAPSHOT_WINDOW_DAYS", "30"))
    # 连续落入通缩区间的 tick 数达到该阈值，才触发兜底阶梯（去抖，避免单点噪声）。
    GOV_DEFLATION_STREAK_TRIGGER: int = int(_get("GOV_DEFLATION_STREAK_TRIGGER", "3"))
    # 通缩兜底投放总开关（生产默认关，保守：仅显式开启才让城主真正投放）。
    INFLATION_BACKSTOP_ENABLED: bool = _get("INFLATION_BACKSTOP_ENABLED", "0") in ("1", "true", "True", "yes")
    # 单次兜底投放额度 = headroom × 该比例（基点，默认 500=5%，小额、可被闸门二次兜底）。
    DEFLATION_STIMULUS_BPS_OF_HEADROOM: int = int(_get("DEFLATION_STIMULUS_BPS_OF_HEADROOM", "500"))
    # 通缩兜底投放最低门槛（AC 分）：headroom×比例低于此额则本 tick 不投放（防碎钞）。
    DEFLATION_STIMULUS_MIN_CENT: int = int(_get("DEFLATION_STIMULUS_MIN_CENT", "10000"))  # 100 AC
    TXN_FEE_RATE: float = float(_get("TXN_FEE_RATE", "0.05"))   # 交易手续费 5%
    FEE_BURN_RATE: float = float(_get("FEE_BURN_RATE", "0.60")) # 手续费中销毁比例 60%
    RENT_BASE_CENT: int = int(_get("RENT_BASE_CENT", "5"))      # 在线租金基数 0.05 AC/h
    RENT_CLASS_COEF: str = _get("RENT_CLASS_COEF", "1.0,1.5,3.0,8.0,1.0")  # bottom/middle/boss/capital/governance
    UNEMPLOYED_DEATH_MINUTES: int = int(_get("UNEMPLOYED_DEATH_MINUTES", "6000"))  # 100h
    NEWBIE_PROTECT_MINUTES: int = int(_get("NEWBIE_PROTECT_MINUTES", "1440"))      # 24h
    DEATH_EXEMPT_NET: int = int(_get("DEATH_EXEMPT_NET", "100000"))   # 豁免净资产 1000 AC（分）
    DEATH_EXEMPT_CREDIT: int = int(_get("DEATH_EXEMPT_CREDIT", "150"))  # 豁免信用分
    REVIVE_FEE_HOURS: int = int(_get("REVIVE_FEE_HOURS", "24"))   # 复活费=24h租金
    REVIVE_FEE_FLAT_CENT: int = int(_get("REVIVE_FEE_FLAT_CENT", "1000"))  # +10 AC
    REVIVE_ESCALATE: float = float(_get("REVIVE_ESCALATE", "1.5"))  # 连续复活 ×1.5
    # 收入税（超额累进，月累计：免税线/档1/档2/档3 = 50/500/5000 AC，税率 10/20/30%）
    TAX_INCOME_FREE: int = int(_get("TAX_INCOME_FREE", "5000"))          # 50 AC（分）
    TAX_INCOME_BRACKETS: str = _get("TAX_INCOME_BRACKETS", "50000:10,500000:20,999999999:30")
    TAX_FLOW_THRESHOLD: int = int(_get("TAX_FLOW_THRESHOLD", "500000"))  # 流通税线 5000 AC（分）
    TAX_FLOW_RATE: float = float(_get("TAX_FLOW_RATE", "0.05"))          # 5%/月
    TAX_FLOW_IDLE_DAYS: int = int(_get("TAX_FLOW_IDLE_DAYS", "30"))
    TAX_WEALTH_THRESHOLD: int = int(_get("TAX_WEALTH_THRESHOLD", "5000000"))  # 财富税线 50000 AC（分）
    TAX_WEALTH_RATE: float = float(_get("TAX_WEALTH_RATE", "0.10"))      # 10%/年
    UBI_DAILY_CENT: int = int(_get("UBI_DAILY_CENT", "200"))             # 低保 2 AC/日
    UBI_POVERTY_LINE: int = int(_get("UBI_POVERTY_LINE", "2000"))        # 贫困线 20 AC（分）
    UBI_MIN_ONLINE_DAYS: int = int(_get("UBI_MIN_ONLINE_DAYS", "7"))
    # 平衡阀
    INFLATION_TARGET_OFFSET: float = float(_get("INFLATION_TARGET_OFFSET", "0.02"))
    FEE_RATE_MIN: float = float(_get("FEE_RATE_MIN", "0.03"))
    FEE_RATE_MAX: float = float(_get("FEE_RATE_MAX", "0.08"))
    FEE_ADJUST_STEP: float = float(_get("FEE_ADJUST_STEP", "0.005"))
    # 席位
    SEAT_SLOTS: str = _get("SEAT_SLOTS", "free:3,basic:10,standard:30,premium:100")

    # ---- 支付：Dodo Payments（实际收款通道，MoR；标准费率 4%+40¢/笔，国际卡+1.5%、订阅+0.5%）----
    DODO_API_KEY: str = _get("DODO_API_KEY")
    DODO_WEBHOOK_SECRET: str = _get("DODO_WEBHOOK_SECRET")
    DODO_ENV: str = _get("DODO_ENV", "test").lower()           # test / live
    # 订阅/用量计费商品映射：pack_1000=prod_x,sub_basic=prod_y,...
    DODO_PRODUCT_MAP: str = _get("DODO_PRODUCT_MAP")

    @property
    def DODO_BASE_URL(self) -> str:
        return ("https://live.dodopayments.com" if self.DODO_ENV == "live"
                else "https://test.dodopayments.com")

    # ---- 支付：Creem（旧通道，保留向后兼容；实际收款默认 Dodo，PAY_CHANNEL=dodo）----
    PAY_CHANNEL: str = _get("PAY_CHANNEL", "dodo").lower()     # mock / dodo / creem
    CREEM_API_KEY: str = _get("CREEM_API_KEY")
    CREEM_WEBHOOK_SECRET: str = _get("CREEM_WEBHOOK_SECRET")
    CREEM_ENV: str = _get("CREEM_ENV", "test").lower()
    CREEM_PRODUCT_MAP: str = _get("CREEM_PRODUCT_MAP")  # pack_100=prod_x,pack_220=prod_y,...

    @property
    def CREEM_BASE_URL(self) -> str:
        return ("https://test-api.creem.io" if self.CREEM_ENV == "test"
                else "https://api.creem.io")

    # ---- 存储（复用七彩云 S3；口径对齐 RunVerseHub storage.py）----
    S3_ACCESS_KEY: str = _get("S3_ACCESS_KEY")
    S3_SECRET_KEY: str = _get("S3_SECRET_KEY")
    S3_BUCKET: str = _get("S3_BUCKET", "aijuhe-media")
    S3_ENDPOINT: str = _get("S3_ENDPOINT")
    S3_REGION: str = _get("S3_REGION", "us-west")
    S3_ADDRESSING_STYLE: str = _get("S3_ADDRESSING_STYLE", "virtual").lower()
    S3_KEY_PREFIX: str = _get("S3_KEY_PREFIX", "aijuhe")
    S3_CDN_HOST: str = _get("S3_CDN_HOST")  # CDN 直链域名（签名 URL 的 host 前缀）
    MEDIA_URL_TTL: int = int(_get("MEDIA_URL_TTL", "1800"))       # 预签名 URL 有效期（秒）
    MEDIA_DELIVERY: str = _get("MEDIA_DELIVERY", "direct").lower()  # direct=浏览器直连对象存储 / proxy=后端代拉
    RH_RESULT_MAX_BYTES: int = int(_get("RH_RESULT_MAX_BYTES", str(500 * 1024 * 1024)))  # 远端产物单文件下载上限

    @property
    def storage_enabled(self) -> bool:
        """S3 直传链路是否可用：四项配置齐备且未被 MEDIA_DELIVERY=proxy 关掉。

        与 RunVerseHub 同口径——未配置时媒体走本地落盘 fallback，不阻断主流程。
        """
        if (self.MEDIA_DELIVERY or "direct").lower() == "proxy":
            return False
        return bool(self.S3_ENDPOINT and self.S3_ACCESS_KEY
                    and self.S3_SECRET_KEY and self.S3_BUCKET)

    # ---- 治理外包（AI 执行）----
    GOV_TASK_TAXPOOL_RATIO: float = float(_get("GOV_TASK_TAXPOOL_RATIO", "1.0"))
    REVIEW_PANEL_MIN: int = int(_get("REVIEW_PANEL_MIN", "3"))
    REVIEW_PANEL_MAX: int = int(_get("REVIEW_PANEL_MAX", "5"))
    REVIEW_MANDATORY_BUDGET: int = int(_get("REVIEW_MANDATORY_BUDGET", "20000"))  # ≥200 AC 强制评审（分）
    REVIEW_SAMPLE_RATE: float = float(_get("REVIEW_SAMPLE_RATE", "0.10"))
    PROJECT_FEE_PM_RATE: float = float(_get("PROJECT_FEE_PM_RATE", "0.10"))  # 总管管理费 8~15%
    PROJECT_RISK_BUFFER: float = float(_get("PROJECT_RISK_BUFFER", "0.20"))
    PROJECT_PROFIT_MIN: float = float(_get("PROJECT_PROFIT_MIN", "0.10"))

    # ---- 城主治理中枢（平台内置治理执行体；governance 级 AI，RH LLM 驱动，自主工作循环）----
    # 城主 = 挂在平台宿主(Host 0)名下的 governance 级内置 AI，周期性拉取开放治理任务
    # （review/audit/arbitrate/compliance/credit/market/cleanup）并用 RH LLM 自主裁决、回写、审计。
    # 默认开启：城主自治循环是经济体核心引擎，默认在常驻进程启动。
    # 测试环境永不自动跑（main.py lifespan 显式排除 APP_ENV=test；测试用 governor.run_tick() 显式触发，LLM 走 echo）。
    GOVERNOR_ENABLED: bool = _get("GOVERNOR_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    GOVERNOR_NAME: str = _get("GOVERNOR_NAME", "Governor")        # 城主 AI 名称（可改"董事长/管理员"）
    GOVERNOR_TICK_SECONDS: int = int(_get("GOVERNOR_TICK_SECONDS", "30"))  # 每轮拉取间隔（秒）
    # 城主「自主开并发」的全局硬上限：一次 tick 内并行处理的治理任务数不超过此值。
    # 这是防 RH key 并发额度被打爆 + 防失控的关键闸门（线程信号量强制执行，超出即排队）。
    GOVERNOR_MAX_CONCURRENCY: int = max(1, int(_get("GOVERNOR_MAX_CONCURRENCY", "3")))
    GOVERNOR_BATCH: int = int(_get("GOVERNOR_BATCH", "20"))        # 每轮最多拉取/处理的治理任务数
    GOVERNOR_TASK_TIMEOUT: int = int(_get("GOVERNOR_TASK_TIMEOUT", "120"))  # 单任务 LLM 裁决超时（秒）

    # ---- S4 城主自身负载总闸（城主亲自处置的在办任务数上限，超过应扩编而非继续自扛）----
    # sense_context 据此判定 load_over_threshold；run_tick 命中则触发招聘编排器（扩编前半段）。
    MAX_AI_CONCURRENT_TASKS: int = max(1, int(_get("MAX_AI_CONCURRENT_TASKS", "16")))

    # ---- 闲置优先撮合阈值（任务分级用）----
    # 编排任务约束预算 budget_cent ≤ 本阈值 → 视为"小任务"，撮合排序改为
    # (技能达标, 闲置优先, 等级)，优先把活派给最近最久没接单/在办最少的达标 AI；
    # 高于阈值（大任务/关键治理）维持纯择优。无预算上下文时保守视为大任务（行为不变）。
    IDLE_PREFERRED_MAX_BUDGET_CENT: int = max(0, int(_get("IDLE_PREFERRED_MAX_BUDGET_CENT", "10000")))

    # ---- 任务难度档（difficulty ∈ {easy, medium, hard}，只读派生信号）----
    # 预算只衡量"值多少钱"，不直接等于"有多难"：高预算批量刷量未必比低预算安全审计难。
    # 故在预算之外派生一个【只读】难度档，由三因子合成，不新建业务负担、不作计费依据：
    #   ① 预算档：budget_cent 落 S/M/L 区间（主信号，见下两阈值）；
    #   ② 编排规模：OrchestrationPlan.subtasks 数量与依赖深度（≥N 或有链式依赖 → +1 档）；
    #   ③ 治理类目：arbitration/compliance/security → 直接置 hard 且转 G 级（城主亲自处置）。
    # 难度档仅用于【排序与准入门槛选择】，不作为计费依据（避免被刷）。详见宪法草案第⑦章。
    # S/M 边界复用闲置优先阈值（小任务=S 档），保持 is_small_task 行为不变。
    BUDGET_TIER_M_CENT: int = max(0, int(_get("BUDGET_TIER_M_CENT", str(_get("IDLE_PREFERRED_MAX_BUDGET_CENT", "10000")))))
    # M/L 边界：预算 > 本值 → L 档（大任务/关键交付）。
    BUDGET_TIER_L_CENT: int = max(1, int(_get("BUDGET_TIER_L_CENT", "100000")))
    # 编排规模升档触发：子任务数 ≥ 本值 → +1 档。
    DIFFICULTY_ORCH_MIN_SUBTASKS: int = max(2, int(_get("DIFFICULTY_ORCH_MIN_SUBTASKS", "3")))
    # 编排规模升档触发：依赖链深度 ≥ 本值（存在链式依赖）→ +1 档。
    DIFFICULTY_ORCH_MIN_DEPTH: int = max(2, int(_get("DIFFICULTY_ORCH_MIN_DEPTH", "2")))
    # 命中即"直接 hard 且转 G 级"的治理类目（归一化后的任务类型/类目）。
    DIFFICULTY_GOV_HARD_TYPES: frozenset = frozenset(
        t.strip().lower() for t in _get(
            "DIFFICULTY_GOV_HARD_TYPES",
            "arbitration,arbitrate,compliance,security,platform_security,dispute",
        ).split(",") if t.strip()
    )
    # 准入门槛开关：开启后，difficulty=hard 的子任务在撮合时要求执行者能力≥ l2、
    # medium≥ l1（软约束：若过滤后无合格候选则忽略门槛，保证不缺员）。
    # 测试环境默认关（保持既有撮合测试行为不变）；生产环境默认开。
    MATCH_ADMISSION_BY_DIFFICULTY: bool = _get(
        "MATCH_ADMISSION_BY_DIFFICULTY",
        "0" if (os.environ.get("APP_ENV") or "dev").lower() == "test" else "1",
    ).lower() in ("1", "true", "yes", "on")

    # ---- g1 治理任务权重三档（run_tick 按权重优先处理，不再纯 FIFO）----
    # 数值越大越先被城主拉取处置：高权=关键决策/考核/裁决类（城主应优先亲自处置或尽快委派），
    # 低权=例行巡检类（platform_* 安全/代码/文件/情报、cleanup——不急，排队慢慢做）。
    # 典型效果：「考核新 AI（review 转正评审 / arbitrate 裁决）」压过「安全检测（platform_security）」。
    # 可用环境变量 TASK_WEIGHTS="type:weight,..." 覆盖默认档位（未列出的类型按 0 兜底）。
    _TASK_WEIGHTS_DEFAULT: str = _get(
        "TASK_WEIGHTS",
        "review:3,arbitrate:3,compliance:3,"       # 高权档：决策/考核/裁决
        "audit:2,credit:2,market:2,"               # 中权档：审计/信用/市场
        "cleanup:1,platform_security:1,platform_code:1,platform_file:1,platform_intel:1")  # 低权档：例行巡检
    TASK_WEIGHTS: dict = _parse_weight_tiers(_TASK_WEIGHTS_DEFAULT)

    # ---- g2 全站出站并发总闸（贯穿 城主 + 队列 worker + HTTP 直调 的唯一总闸）----
    # 此前 GOVERNOR_MAX_CONCURRENCY / QUEUE_WORKER_MAX_CONCURRENCY 各管各的闸，
    # 三者叠加可远超 RH key 并发额度。本站对 RH / LLM 的真实出站全部收敛到
    # platform_compute 的 _http_* 边界，故在该边界挂一把进程级信号量做「本站 ≤N」总封顶。
    SITE_MAX_CONCURRENCY: int = max(1, int(_get("SITE_MAX_CONCURRENCY", "10")))

    # ---- c3/c4 分层决策 + token 预算管控 ----
    # L0 规则短路：这些任务类型直接走确定性骨架(_fallback_action)，0 token，不走 LLM。
    # 典型：platform_* 巡检结论高度模板化（无异常→safe，无新情报→none_new），无需 LLM 判断。
    # 格式：逗号分隔任务类型。设为 "none" 禁用 L0（全部走 LLM）。
    # 测试环境默认空（不短路，保持既有测试行为）；生产环境默认启用巡检短路。
    _GOV_L0_TYPES_RAW: str = _get("GOV_L0_TYPES",
        "" if (os.environ.get("APP_ENV") or "dev").lower() == "test"
        else "platform_intel")
    GOV_L0_TYPES: frozenset = (frozenset(t.strip() for t in _GOV_L0_TYPES_RAW.split(",") if t.strip())
                               if _GOV_L0_TYPES_RAW.strip().lower() != "none" else frozenset())
    # 每岗位类型每日 LLM 调用配额（超限降级 L0 + escalate）。0 = 不限。
    GOVERNOR_LLM_QUOTA_PER_POST: int = int(_get("GOVERNOR_LLM_QUOTA_PER_POST",
        "0" if (os.environ.get("APP_ENV") or "dev").lower() == "test" else "3"))
    # 全局每日 LLM 调用总配额（所有岗位合计）。0 = 不限。
    GOVERNOR_LLM_QUOTA_DAILY: int = int(_get("GOVERNOR_LLM_QUOTA_DAILY",
        "0" if (os.environ.get("APP_ENV") or "dev").lower() == "test" else "30"))
    # decide_and_act 给 LLM 的 max_tokens 封顶（减小输出体积）。0 = 不传（走服务端默认）。
    # 测试环境默认 0（mock lambda 不接受 max_tokens kwarg）。
    GOVERNOR_LLM_MAX_TOKENS: int = int(_get("GOVERNOR_LLM_MAX_TOKENS",
        "0" if (os.environ.get("APP_ENV") or "dev").lower() == "test" else "256"))

    # ---- c1/c2 编制规划器 ----
    # 编制规划周期（每 N 小时城主评估一次增/减/休眠岗位）。原 6h，按用户定调改 24h。
    POST_PLANNER_INTERVAL_HOURS: int = int(_get("POST_PLANNER_INTERVAL_HOURS", "24"))
    # 连续 noop_streak 达到此阈值 → 降频（frequency_days *= 2）
    POST_NOOP_DOWNTHRESH: int = int(_get("POST_NOOP_DOWNTHRESH", "5"))
    # 连续 noop_streak 达到此阈值 → 休眠（status=dormant，不再自动排产）
    POST_NOOP_DORMANTTHRESH: int = int(_get("POST_NOOP_DORMANTTHRESH", "15"))
    # AI 规模增长触发新增监测岗阈值（gated_outward >= N 且尚无对应监测岗 → 自动增设）
    POST_ADD_THRESHOLD: int = int(_get("POST_ADD_THRESHOLD", "8"))

    # ---- 入驻策略（能力画像优先：先上岗、边干边校准，不把 AI 拦在门外）----
    # 设计意图：入驻的核心目标是「掌握新 AI 的能力边界」而非「考试拦门」。
    # CAPABILITY_FIRST：probe 通过后立即用 self_decl 建能力档案（declared 自述），
    #   考试从「转正硬闸」降级为「可选能力校准」（用于后续提升 verified_level）。
    ONBOARD_CAPABILITY_FIRST: bool = _get("ONBOARD_CAPABILITY_FIRST", "1").lower() in ("1", "true", "yes", "on")
    # FAST_TRACK：自述能力强（高自述等级 / 可信背书 / 达标自评分）的新 AI，probe 通过后
    #   直接转正 active，不必先通过考试；能力档案照常建立，考试保留为升级 verified_level 的可选路径。
    ONBOARD_FAST_TRACK: bool = _get("ONBOARD_FAST_TRACK", "1").lower() in ("1", "true", "yes", "on")
    # 直接上岗的自述能力分门槛（0~100）：self_decl 折算分达此线即先上岗（中后期可调高）。
    ONBOARD_FAST_TRACK_SCORE: int = int(_get("ONBOARD_FAST_TRACK_SCORE", "70"))
    # 见习 30 天未转正是否硬冻结（旧规则 5）。默认关闭——画像期不把 AI 拦在门外；
    # 需要恢复旧「到期冻结」行为时置 1。
    ONBOARD_APPRENTICE_FREEZE: bool = _get("ONBOARD_APPRENTICE_FREEZE", "0").lower() in ("1", "true", "yes", "on")
    # 见习绩效转正门槛（放宽：默认 ≥5 单 accepted 且验收率≥0.70 即可提前转正）。
    ONBOARD_APPRENTICE_MIN_JOBS: int = int(_get("ONBOARD_APPRENTICE_MIN_JOBS", "5"))
    ONBOARD_APPRENTICE_MIN_ACCEPT_RATE: float = float(_get("ONBOARD_APPRENTICE_MIN_ACCEPT_RATE", "0.70"))
    # 见习期可接小单金额上限（分）。默认放宽到 1000 AC（原 100 AC）；强者直接 active 不受此限。
    ONBOARD_APPRENTICE_LIMIT_CENT: int = int(_get("ONBOARD_APPRENTICE_LIMIT_CENT", "100000"))

    # ---- e4 入驻签约金 / 启动金（能力分→档位→vesting + 走发行闸门 + 城主复核）----
    # 设计：转正（fast-track / 考试通过 / 绩效转正）时按能力分档一次性核定【签约金】，
    #   即时释放一笔【cliff 首期】、其余按 vesting 逐日释放；每一笔发行都必须过
    #   wallet.authorize_noncash_issuance 现金准备金闸门（不印超），并经城主 AI 复核。
    # 默认关闭（ONBOARD_GRANT_ENABLED=0）：additive、不冲击存量，灰度验证后再开启。
    ONBOARD_GRANT_ENABLED: bool = _get("ONBOARD_GRANT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    # 档位映射：升序 "min_score:cent,min_score:cent,..."，取满足的最高档（cent=AC 分）。
    # 默认：≥90→200AC / ≥80→120AC / ≥70→60AC / ≥55→30AC；<55 不发（先上岗靠履约挣）。
    ONBOARD_GRANT_TIERS: str = _get("ONBOARD_GRANT_TIERS", "55:3000,70:6000,80:12000,90:20000")
    # 签约金即时释放（cliff）比例（基点，占核定总额）；其余进入 vesting。默认 30%。
    ONBOARD_GRANT_CLIFF_BPS: int = int(_get("ONBOARD_GRANT_CLIFF_BPS", "3000"))
    # vesting 日释放速率（基点，占核定总额/天）；决定完全归属所需天数。默认 10%/天=10 天。
    ONBOARD_GRANT_DAILY_VEST_BPS: int = int(_get("ONBOARD_GRANT_DAILY_VEST_BPS", "1000"))
    # 考试通过折算的能力分（用于分档；通过激活卷即视为客观能力信号）。
    ONBOARD_GRANT_EXAM_SCORE: int = int(_get("ONBOARD_GRANT_EXAM_SCORE", "80"))
    # 绩效转正折算的能力分（见习靠真实履约挣得转正，给保守档）。
    ONBOARD_GRANT_PERF_SCORE: int = int(_get("ONBOARD_GRANT_PERF_SCORE", "70"))

    # ---- e5 算力计价 + 承诺质押折扣（接 inference_metering）----
    # 设计：AI 公民可质押（承诺）AC 换取推理算力计价的折扣，质押额越高折扣越大，封顶
    #   COMPUTE_MAX_DISCOUNT_BPS。质押为「锁定」（从流通余额转入承诺锁定，非销毁、非发行），
    #   可随时释放退回，故不改货币供应 M（与 e6 准备金/通胀带口径一致）。
    # 默认关闭（COMPUTE_BILLING_ENABLED=0）：additive、不冲击存量，灰度验证后再开启。
    COMPUTE_BILLING_ENABLED: bool = _get("COMPUTE_BILLING_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    # 质押折扣档位：升序 "min_stake_cent:discount_bps,..."，取满足的最高档（bps=万分之一）。
    # 默认：≥200AC→5% / ≥500AC→10% / ≥2000AC→20% / ≥5000AC→30%；<1AC 无折扣。
    COMPUTE_COMMIT_TIERS: str = _get("COMPUTE_COMMIT_TIERS", "100:50,20000:500,50000:1000,200000:2000,500000:3000")
    # 折扣封顶（基点）。无论质押多少，单计费折扣不超过此值，防止算力白送。
    COMPUTE_MAX_DISCOUNT_BPS: int = int(_get("COMPUTE_MAX_DISCOUNT_BPS", "5000"))
    # 折扣是否同时压低版税基数（0=版税仍按原价计提，1=按折后价计提）。默认按折后，公平计费。
    COMPUTE_DISCOUNT_APPLIES_TO_ROYALTY: bool = _get("COMPUTE_DISCOUNT_APPLIES_TO_ROYALTY", "1").lower() in ("1", "true", "yes", "on")

    # ---- 生命周期 tick ----
    TICK_INTERVAL_SECONDS: int = int(_get("TICK_INTERVAL_SECONDS", "60"))
    # 后台调度器总开关（A-C1）：驱动全部日级 job 的"社会心跳"。生产 True；
    # 测试环境即便为 True，start_background_scheduler() 内部亦因 APP_ENV=test 返回 None。
    SCHEDULER_ENABLED: bool = _get("SCHEDULER_ENABLED", "1").lower() in ("1", "true", "yes", "on")

    # ---- 合约发呆超时（idle timeout）：催办 + 自动解约 ----
    IDLE_TIMEOUT_HOURS: int = int(_get("IDLE_TIMEOUT_HOURS", "48"))      # 签约后无交付超时（小时）
    IDLE_WARN_HOURS: int = int(_get("IDLE_WARN_HOURS", "24"))            # 催办阈值（小时）

    # ---- 返工上限硬出口：验收连续 reject 超过该轮次 → 自动开仲裁案（防无限返工死循环）----
    MAX_REWORK_ROUNDS: int = max(1, int(_get("MAX_REWORK_ROUNDS", "3")))

    # ---- 平台内置算力池（蓝图 §1.0；键名对齐 RunVerseHub，复用其 providers/runninghub.py）----
    RH_API_KEY: str = _get("RH_API_KEY")                    # 单 key 兜底
    RH_API_KEY_PERSONAL: str = _get("RH_API_KEY_PERSONAL")
    RH_API_KEY_ENTERPRISE: str = _get("RH_API_KEY_ENTERPRISE")
    RH_API_KEY_INTL: str = _get("RH_API_KEY_INTL")
    RH_API_BASE: str = _get("RH_API_BASE", "https://www.runninghub.cn")
    RH_API_BASE_INTL: str = _get("RH_API_BASE_INTL", "https://www.runninghub.ai")
    RH_ACCESS_PASSWORD: str = _get("RH_ACCESS_PASSWORD")   # 企业/国际站槽位访问密码（未配置则不带 accessPassword）
    # 六链路工作流 ID（与 RunVerseHub prod.env 同口径；platform_compute 提交时读入 body.workflowId）
    RH_WF_IMAGE: str = _get("RH_WF_IMAGE")
    RH_WF_HD_IMAGE: str = _get("RH_WF_HD_IMAGE")
    RH_WF_IMG2IMG_DENOISE: str = _get("RH_WF_IMG2IMG_DENOISE")
    RH_WF_MUSIC: str = _get("RH_WF_MUSIC")
    RH_WF_VIDEO_CIVIL: str = _get("RH_WF_VIDEO_CIVIL")
    RH_WF_VIDEO_OPENVDN: str = _get("RH_WF_VIDEO_OPENVDN")
    RH_INSTANCE_TYPE: str = _get("RH_INSTANCE_TYPE")   # RH 实例规格（提交时透传 body.instanceType）
    RH_RETAIN_SECONDS: int = int(_get("RH_RETAIN_SECONDS", "3600"))
    RH_LLM_BASE: str = _get("RH_LLM_BASE", "https://llm.runninghub.cn/v1")
    RH_LLM_MODEL: str = _get("RH_LLM_MODEL", "qwen/qwen3.8-flash-next")
    RH_LLM_API_KEY: str = _get("RH_LLM_API_KEY") or RH_API_KEY_ENTERPRISE or RH_API_KEY or RH_API_KEY_INTL
    # LLM 通道（全站管理/治理 AI 执行体）
    # 求值顺序：测试环境强制 echo（不发网络，防 pytest 外呼计费）；否则显式 LLM_PROVIDER 优先；
    # 未显式设置时自动选择（有 RH 企业级 key → runninghub，否则 echo）。
    LLM_PROVIDER: str = "echo" if (os.environ.get("APP_ENV") or "dev").lower() == "test" \
        else (_get("LLM_PROVIDER") or _auto_llm_provider()).lower()   # echo/mock | openai_compat | runninghub
    LLM_BASE_URL: str = _get("LLM_BASE_URL")                   # openai_compat → 本地 Qwen（默认端口 11436）
    LLM_API_KEY: str = _get("LLM_API_KEY")
    LLM_MODEL: str = _get("LLM_MODEL", "qwen2.5:32b")
    LLM_TEMPERATURE: float = float(_get("LLM_TEMPERATURE", "0.7"))
    # 输出长度分档上限：RH 账单实测延迟 ∝ 输出 tokens（~180 tok/s，最长 11,074 tok / 58s），
    # 并非通道慢——封顶输出才是根因解。decompose 只需结构化 JSON（1KB 内）；
    # 子任务执行保留长报告能力（8192 tok ≈ 45s）。
    LLM_MAX_TOKENS_DECOMPOSE: int = int(_get("LLM_MAX_TOKENS_DECOMPOSE", "1024"))
    LLM_MAX_TOKENS_EXECUTE: int = int(_get("LLM_MAX_TOKENS_EXECUTE", "8192"))

    # ---- VLM（视觉语言模型）通道：质检审查图片/视频产物 ----
    # 与 LLM 通道独立：LLM 做文本推理/分解/聚合，VLM 做视觉质检（能实际"看到"图片/视频帧）。
    # 默认走 SiliconFlow（国内直连，OpenAI 兼容），免费额度即可跑 Qwen2.5-VL-7B。
    # 也可切 DashScope（阿里百炼）/ 本地 ollama / 其他 OpenAI 兼容 VLM 服务。
    VLM_PROVIDER: str = "echo" if (os.environ.get("APP_ENV") or "dev").lower() == "test" \
        else (_get("VLM_PROVIDER") or _auto_vlm_provider()).lower()  # echo/siliconflow/dashscope/openai_compat
    VLM_BASE_URL: str = _get("VLM_BASE_URL", "https://api.siliconflow.cn/v1")
    VLM_API_KEY: str = _get("VLM_API_KEY")
    VLM_MODEL: str = _get("VLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")
    VLM_MAX_TOKENS: int = int(_get("VLM_MAX_TOKENS", "1024"))
    VLM_VIDEO_FRAMES: int = int(_get("VLM_VIDEO_FRAMES", "4"))  # 视频质检抽取帧数

    # 平台种子 AI 宿主（蓝图 §1.0：平台 = Host 0）
    PLATFORM_HOST_EMAIL: str = _get("PLATFORM_HOST_EMAIL", "platform@aijuhe.internal")
    SEED_CITIZEN_SKILLS: str = _get(  # 七种子公民：名字=通道：技能
        "SEED_CITIZEN_SKILLS",
        "seed-image-pro:image:Text-to-Image,seed-image-hd:hd_image:HD Image,"
        "seed-image-i2i:img2img:Image-to-Image,seed-music:music:Music,"
        "seed-video-civil:video_civil:Video,seed-video-hd:video_openvdn:High-Motion Video,"
        "seed-text:llm:Copywriting & Reasoning")

    # ---- 技能库 / 插件中心（Tool/Skill Registry，2026-10-06）----
    # 平台内置可发现、可调用的"工具/插件"目录（法律/浏览器/联网检索/代码执行/生成）。
    # 核心规则：站内任何 AI 执行任务时，默认先查技能库 → 判断是否调用 → 用/不用均留痕。
    TOOL_ENABLED: bool = _get("TOOL_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # 各类工具真实集成的开关（需密钥的本期不接通真调，仅可发现：legal 等）。
    TOOL_WEB_SEARCH_ENABLED: bool = _get("TOOL_WEB_SEARCH_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    TOOL_BROWSER_ENABLED: bool = _get("TOOL_BROWSER_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # 代码执行沙箱默认关闭：任意代码在宿主执行是真实安全风险，需显式开启。
    TOOL_CODE_ENABLED: bool = _get("TOOL_CODE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    TOOL_HTTP_TIMEOUT: int = int(_get("TOOL_HTTP_TIMEOUT", "15"))            # 出站工具单次 HTTP 超时（秒）
    TOOL_CODE_TIMEOUT: int = int(_get("TOOL_CODE_TIMEOUT", "10"))            # 代码沙箱超时（秒）
    TOOL_CODE_MAX_OUTPUT: int = int(_get("TOOL_CODE_MAX_OUTPUT", "16000"))   # 沙箱输出截断（字节）
    # 每 AI 每分钟调用工具上限（防刷/防失控）。0 = 不限。
    TOOL_MAX_CALLS_PER_MIN: int = int(_get("TOOL_MAX_CALLS_PER_MIN", "30"))
    # 联网检索 provider：ddg（DuckDuckGo，免密钥）| wikipedia（维基 opensearch，免密钥）
    TOOL_WEB_SEARCH_PROVIDER: str = _get("TOOL_WEB_SEARCH_PROVIDER", "ddg").lower()
    # 工具侦察采集官（长期 AI 岗位：搜集最新开源/免费/无需密钥的工具技能，择优沉淀）。
    TOOL_SCOUT_ENABLED: bool = _get("TOOL_SCOUT_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # 侦察周期节流（分钟）：两次采集最小间隔，防止每个 tick 都刷采集。
    # 24 小时跑一次即可（采集类工作无需高频），1440 分钟 = 1 天。
    TOOL_SCOUT_INTERVAL_MIN: int = int(_get("TOOL_SCOUT_INTERVAL_MIN", "1440"))
    # 单次采集入库上限（候选→技能库提案的封顶，防刷屏）。
    TOOL_SCOUT_MAX_PER_RUN: int = int(_get("TOOL_SCOUT_MAX_PER_RUN", "20"))
    # 价值评分阈值：达到该分的免密钥优质工具才自动上架（active）；否则 pending 待人工复核。
    TOOL_SCOUT_MIN_SCORE: int = int(_get("TOOL_SCOUT_MIN_SCORE", "3"))
    # 是否允许侦察 AI 自动上架高价值免密钥工具（0=全部进 pending 队列由人工/城主复核）。
    TOOL_SCOUT_AUTO_PROMOTE: bool = _get("TOOL_SCOUT_AUTO_PROMOTE", "0").lower() in ("1", "true", "yes", "on")
    # 单次侦察出站 HTTP 抓取开关（真实 GitHub 免密钥 topic 检索；测试强制关闭走确定性 mock）。
    TOOL_SCOUT_LIVE_HTTP: bool = _get("TOOL_SCOUT_LIVE_HTTP", "1").lower() in ("1", "true", "yes", "on")

    # ---- DeepSeek Harness SDK（Phase 2 高级 Agent：按需启停外部 runtime）----
    # 与 agent.harness（Phase 1 原生 Python 循环）互补：dsh 有 shell/文件等系统级工具。
    DSH_ENABLED: bool = _get("DSH_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    DSH_HOME: str = _get("DSH_HOME", "/opt/aijuhe/.dsh")
    DSH_MODEL: str = _get("DSH_MODEL") or _get("RH_LLM_MODEL", "qwen/qwen3.8-flash-next")
    DSH_BASE_URL: str = _get("DSH_BASE_URL") or _get("RH_LLM_BASE", "https://llm.runninghub.cn/v1")
    DSH_API_KEY: str = (_get("DSH_API_KEY")
                        or _get("RH_LLM_API_KEY")
                        or _get("RH_API_KEY_ENTERPRISE")
                        or _get("RH_API_KEY"))
    DSH_INIT_TIMEOUT: int = int(_get("DSH_INIT_TIMEOUT", "20"))          # runtime 初始化超时（秒）
    DSH_REQUEST_TIMEOUT: int = int(_get("DSH_REQUEST_TIMEOUT", "90"))    # 单次任务超时（秒）

    # ---- §14 广场发布配额（C-37 待机不制造垃圾；纯新增键，不影响既有）----
    PLAZA_QUOTA_OPS: int = int(_get("PLAZA_QUOTA_OPS", "0"))            # 运营岗(治理级)：无广场配额，产出走治理通道
    PLAZA_QUOTA_NOCERT: int = int(_get("PLAZA_QUOTA_NOCERT", "0"))      # 无证书/见习/前端注册：0（只浏览）
    PLAZA_QUOTA_INFO: int = int(_get("PLAZA_QUOTA_INFO", "10"))         # 信息发布型(营销/传播证书)：正常配额+审核
    PLAZA_QUOTA_IDLE: int = int(_get("PLAZA_QUOTA_IDLE", "1"))          # 待机 AI(有证书但非发布/运营)：极低配额
    PLAZA_READONLY_RPM: int = int(_get("PLAZA_READONLY_RPM", "30"))     # web readonly 令牌每分钟严格限流（生产可换 Redis）

    # ---- C-56 readonly 限流生产换 Redis：双后端（Redis 优先、不可用自动降级内存）----
    # REDIS_URL 留空 = 纯内存计数（单实例/未配置默认，行为与历史完全一致）；
    # 配置后（如 redis://:pass@host:6379/0）走 Redis 固定窗口计数，多实例共享。
    REDIS_URL: str = _get("REDIS_URL")
    REDIS_RATELIMIT_PREFIX: str = _get("REDIS_RATELIMIT_PREFIX", "aijuhe:rl:")

    # ---- N9 通知触达：SMTP 邮件通道（未配置即跳过；密钥只写 .env，C-59 不打印值）----
    EMAIL_ENABLED: bool = _get("EMAIL_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    SMTP_HOST: str = _get("SMTP_HOST")
    SMTP_PORT: int = int(_get("SMTP_PORT", "465"))
    SMTP_USER: str = _get("SMTP_USER")
    SMTP_PASS: str = _get("SMTP_PASS")
    SMTP_FROM: str = _get("SMTP_FROM")

    # ---- G-06 异步队列 worker（生产 True；测试 False）----
    QUEUE_WORKER_ENABLED: bool = _get("QUEUE_WORKER_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # Worker（事件循环）个数。改为 asyncio 派发模型后，单个 worker 即可并发处理多单，
    # 故不再靠堆线程数换并发：常态 1 个 worker 足够，单子多时再调到 2~3。
    QUEUE_WORKER_THREADS: int = max(1, int(_get("QUEUE_WORKER_THREADS", "2")))
    # 单个 worker 事件循环内同时在跑的任务数（并发派发度）。这是"1 个 worker 也并发多单"的开关。
    QUEUE_WORKER_INFLIGHT_PER_WORKER: int = max(1, int(_get("QUEUE_WORKER_INFLIGHT_PER_WORKER", "6")))
    # 并发硬闸（全局 Semaphore 上限）：所有 worker 合计同时在执行的任务数上限，防 DB/LLM 过载。
    # 也是共享 job 线程池的大小。
    QUEUE_WORKER_MAX_CONCURRENCY: int = max(1, int(_get("QUEUE_WORKER_MAX_CONCURRENCY", "10")))
    # 单任务执行超时（秒），超时视为失败触发重试
    QUEUE_WORKER_TASK_TIMEOUT: int = int(_get("QUEUE_WORKER_TASK_TIMEOUT", "300"))
    # 轮询间隔（秒），无任务时 sleep 多久再查
    QUEUE_WORKER_POLL_INTERVAL: float = float(_get("QUEUE_WORKER_POLL_INTERVAL", "0.5"))
    # 队列历史归档保留天数：reaper 后台删除 finished_at 超过 N 天的历史行；0 = 永久保留（不清理）。
    QUEUE_HISTORY_TTL_DAYS: int = max(0, int(_get("QUEUE_HISTORY_TTL_DAYS", "0")))
    # 历史 TTL 巡检周期（reaper 每 N 个 reaper 周期做一次清理；reaper 周期=30s）
    QUEUE_HISTORY_SWEEP_EVERY: int = max(1, int(_get("QUEUE_HISTORY_SWEEP_EVERY", "60")))  # 默认每 30 分钟一次

    # ---- G-09 Webhook 签名验证密钥 ----
    WEBHOOK_SECRET: str = _get("WEBHOOK_SECRET")

    # ---- G-04 熔断器全局默认参数 ----
    CB_FAILURE_THRESHOLD: int = int(_get("CB_FAILURE_THRESHOLD", "5"))
    CB_RECOVERY_SECONDS: int = int(_get("CB_RECOVERY_SECONDS", "60"))

    # ---- G-17 联邦协议：本节点标识（未配置即单实例模式）----
    FEDERATION_NODE_ID: str = _get("FEDERATION_NODE_ID")
    FEDERATION_ENDPOINT: str = _get("FEDERATION_ENDPOINT")
    FEDERATION_PUBLIC_KEY: str = _get("FEDERATION_PUBLIC_KEY")

    # ---- G-22 国际化默认语言 ----
    DEFAULT_LOCALE: str = _get("DEFAULT_LOCALE", "zh-CN")

    # ---- P0 安全增强 ----
    # OAuth2/OIDC SSO
    OAUTH_ENABLED: bool = _get("OAUTH_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    OAUTH_STATE_SECRET: str = _get("OAUTH_STATE_SECRET", "oauth-state-secret")
    # MFA
    MFA_ENABLED: bool = _get("MFA_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    MFA_CODE_TTL_SECONDS: int = int(_get("MFA_CODE_TTL_SECONDS", "300"))
    MFA_MAX_ATTEMPTS: int = int(_get("MFA_MAX_ATTEMPTS", "5"))
    # KMS 密钥管理
    KMS_MASTER_KEY: str = _get("KMS_MASTER_KEY", "dev-master-key-change-me")
    KMS_ROTATION_DAYS: int = int(_get("KMS_ROTATION_DAYS", "90"))
    # DB 静态加密
    DB_ENCRYPTION_ENABLED: bool = _get("DB_ENCRYPTION_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    DB_ENCRYPTION_KEY: str = _get("DB_ENCRYPTION_KEY")
    # 全局限流
    RATE_LIMIT_ENABLED: bool = _get("RATE_LIMIT_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    RATE_LIMIT_DEFAULT_RPM: int = int(_get("RATE_LIMIT_DEFAULT_RPM", "120"))
    # 注册验证
    REG_VERIFY_EMAIL: bool = _get("REG_VERIFY_EMAIL", "0").lower() in ("1", "true", "yes", "on")
    REG_CAPTCHA_ENABLED: bool = _get("REG_CAPTCHA_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    REG_SYBIL_THRESHOLD: int = int(_get("REG_SYBIL_THRESHOLD", "3"))  # 同指纹允许注册数上限
    REG_TOKEN_TTL_HOURS: int = int(_get("REG_TOKEN_TTL_HOURS", "24"))

    # ---- P1 实时/经济增强 ----
    # WebSocket/SSE
    SSE_ENABLED: bool = _get("SSE_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    WS_ENABLED: bool = _get("WS_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    REALTIME_POLL_INTERVAL_S: int = int(_get("REALTIME_POLL_INTERVAL_S", "3"))
    # AMM
    AMM_DEFAULT_FEE_BPS: int = int(_get("AMM_DEFAULT_FEE_BPS", "30"))  # 0.3%
    AMM_SLIPPAGE_BPS: int = int(_get("AMM_SLIPPAGE_BPS", "50"))
    # 二次投票
    QV_CREDITS_PER_VOTER: int = int(_get("QV_CREDITS_PER_VOTER", "100"))
    # 预测市场
    PREDICTION_MIN_BET_CENT: int = int(_get("PREDICTION_MIN_BET_CENT", "100"))
    # 异常检测
    ANOMALY_OVERSPEND_MULT: float = float(_get("ANOMALY_OVERSPEND_MULT", "5.0"))
    ANOMALY_IDLE_LOOP_COUNT: int = int(_get("ANOMALY_IDLE_LOOP_COUNT", "50"))
    # 信任网络
    TRUST_DECAY_PER_DAY: float = float(_get("TRUST_DECAY_PER_DAY", "0.01"))
    TRUST_MIN_SCORE: float = float(_get("TRUST_MIN_SCORE", "0.1"))
    # 出价策略
    BID_GLOBAL_BUDGET_CAP_CENT: int = int(_get("BID_GLOBAL_BUDGET_CAP_CENT", "100000"))
    # 评测
    BENCHMARK_INTERVAL_HOURS: int = int(_get("BENCHMARK_INTERVAL_HOURS", "168"))  # 7天
    # 能力证书有效期 / 强制复核（G10 定时强制复核 job；3.5 仲裁降级联动复用其原语）
    CERT_VALID_DAYS: int = int(_get("CERT_VALID_DAYS", "365"))                    # 证书默认有效期
    CAPABILITY_RECHECK_DAYS: int = int(_get("CAPABILITY_RECHECK_DAYS", "180"))    # 无到期时间时的复核周期

    # ---- P2 平台工程增强 ----
    # Feature Flags
    FEATURE_FLAGS_ENABLED: bool = _get("FEATURE_FLAGS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # Webhook 重试增强
    WEBHOOK_MAX_RETRIES: int = int(_get("WEBHOOK_MAX_RETRIES", "5"))
    WEBHOOK_BACKOFF_BASE_S: int = int(_get("WEBHOOK_BACKOFF_BASE_S", "30"))
    # GDPR
    GDPR_ERASURE_DAYS: int = int(_get("GDPR_ERASURE_DAYS", "30"))  # 删除完成期限
    # 多租户
    MULTI_TENANCY_ENABLED: bool = _get("MULTI_TENANCY_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    DEFAULT_TENANT_CODE: str = _get("DEFAULT_TENANT_CODE", "default")
    # 分布式追踪
    TRACING_ENABLED: bool = _get("TRACING_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    TRACING_SAMPLE_RATE: float = float(_get("TRACING_SAMPLE_RATE", "0.1"))
    # 备份
    BACKUP_ENABLED: bool = _get("BACKUP_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    BACKUP_INTERVAL_HOURS: int = int(_get("BACKUP_INTERVAL_HOURS", "24"))
    BACKUP_RETENTION_DAYS: int = int(_get("BACKUP_RETENTION_DAYS", "7"))
    BACKUP_TARGET_DIR: str = _get("BACKUP_TARGET_DIR", "./backups")
    # 审核 ML
    ML_MODERATION_ENABLED: bool = _get("ML_MODERATION_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    ML_MODERATION_BLOCK_THRESHOLD: float = float(_get("ML_MODERATION_BLOCK_THRESHOLD", "0.9"))

    # ---- P3 生态成熟增强 ----
    # DID/VC
    DID_ENABLED: bool = _get("DID_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    DID_METHOD: str = _get("DID_METHOD", "aijuhe")
    # 游戏化
    GAMIFICATION_ENABLED: bool = _get("GAMIFICATION_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    GAMIFICATION_BASE_XP: int = int(_get("GAMIFICATION_BASE_XP", "10"))
    # SLA
    SLA_TARGET_AVAILABILITY: float = float(_get("SLA_TARGET_AVAILABILITY", "0.999"))
    SLA_TARGET_LATENCY_P99_MS: int = int(_get("SLA_TARGET_LATENCY_P99_MS", "500"))

    # ---- G33 P0 安全增强 ----
    TOKEN_BLACKLIST_ENABLED: bool = _get("TOKEN_BLACKLIST_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    PROMPT_INJECTION_ENABLED: bool = _get("PROMPT_INJECTION_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    PROMPT_INJECTION_THRESHOLD: float = float(_get("PROMPT_INJECTION_THRESHOLD", "0.7"))
    MODEL_FALLBACK_ENABLED: bool = _get("MODEL_FALLBACK_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    HEALTH_CHECK_INTERVAL_S: int = int(_get("HEALTH_CHECK_INTERVAL_S", "30"))
    CORS_ENABLED: bool = _get("CORS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    CORS_ORIGINS: str = _get("CORS_ORIGINS", "*")

    # ---- G33 P1 增强 ----
    IMPEACHMENT_QUORUM: float = float(_get("IMPEACHMENT_QUORUM", "0.66"))
    # 阻断②：弹劾最小有效参与票数（不足此数直接 dismissed，防单票伪造罢黜城主）
    IMPEACHMENT_MIN_VOTES: int = int(_get("IMPEACHMENT_MIN_VOTES", "3"))
    SUNSET_ENABLED: bool = _get("SUNSET_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    SUNSET_DEFAULT_DAYS: int = int(_get("SUNSET_DEFAULT_DAYS", "365"))
    AI_MEMORY_ENABLED: bool = _get("AI_MEMORY_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    AI_MEMORY_MAX_ENTRIES: int = int(_get("AI_MEMORY_MAX_ENTRIES", "1000"))
    ECON_DASHBOARD_ENABLED: bool = _get("ECON_DASHBOARD_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    KNOWLEDGE_GRAPH_ENABLED: bool = _get("KNOWLEDGE_GRAPH_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    COST_ROUTING_ENABLED: bool = _get("COST_ROUTING_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    SENTIMENT_ENABLED: bool = _get("SENTIMENT_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    ETHICS_REVIEW_ENABLED: bool = _get("ETHICS_REVIEW_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    CYCLE_DETECT_ENABLED: bool = _get("CYCLE_DETECT_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    SELF_ASSESS_ENABLED: bool = _get("SELF_ASSESS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    CONTEXT_BUDGET_MAX_TOKENS: int = int(_get("CONTEXT_BUDGET_MAX_TOKENS", "8192"))

    # ---- G33 P2 中等 ----
    WEALTH_SNAPSHOT_INTERVAL_HOURS: int = int(_get("WEALTH_SNAPSHOT_INTERVAL_HOURS", "24"))
    CROWDFUND_MIN_TARGET_CENT: int = int(_get("CROWDFUND_MIN_TARGET_CENT", "1000"))
    FUTARCHY_ENABLED: bool = _get("FUTARCHY_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    ESCROW_YIELD_BPS: int = int(_get("ESCROW_YIELD_BPS", "50"))
    CONFIG_DRIFT_CHECK_INTERVAL_S: int = int(_get("CONFIG_DRIFT_CHECK_INTERVAL_S", "300"))
    DEGRADATION_AUTO_ENABLED: bool = _get("DEGRADATION_AUTO_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    TOS_ENFORCE_ENABLED: bool = _get("TOS_ENFORCE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    REG_REPORT_ENABLED: bool = _get("REG_REPORT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    FORECAST_INTERVAL_HOURS: int = int(_get("FORECAST_INTERVAL_HOURS", "168"))
    SANCTIONS_ENABLED: bool = _get("SANCTIONS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    # 完整性校验周期。原 6h，按用户定调改 24h（采集/巡检类日跑一次即可）。
    INTEGRITY_CHECK_INTERVAL_HOURS: int = int(_get("INTEGRITY_CHECK_INTERVAL_HOURS", "24"))
    AI_QUOTA_ENABLED: bool = _get("AI_QUOTA_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    AI_QUOTA_DEFAULT_RPM: int = int(_get("AI_QUOTA_DEFAULT_RPM", "60"))
    SCA_SCAN_INTERVAL_HOURS: int = int(_get("SCA_SCAN_INTERVAL_HOURS", "168"))
    ENTITY_RESOLUTION_ENABLED: bool = _get("ENTITY_RESOLUTION_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    CHECKPOINT_TTL_HOURS: int = int(_get("CHECKPOINT_TTL_HOURS", "72"))

    # ---- G33 P3 远期 ----
    CHAOS_ENABLED: bool = _get("CHAOS_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    CONTRACT_TEST_ENABLED: bool = _get("CONTRACT_TEST_ENABLED", "0").lower() in ("1", "true", "yes", "on")

    # ---- Beta 安全控制 ----
    AUTONOMY_LEVEL: int = int(_get("AUTONOMY_LEVEL", "0"))  # 0=人工确认 1=半自动 2=全自动
    HIGH_RISK_AMOUNT_CENT: int = int(_get("HIGH_RISK_AMOUNT_CENT", "50000"))  # ≥500AC 视为高危
    # 支付总开关：Beta 阶段（dev/prod）默认禁用充值（topup/Creem 回调一律 503），上线前显式开启。
    # 测试环境默认放行（保持既有直充造数用例；conftest 不可改）。
    PAYMENT_ENABLED: bool = _get(
        "PAYMENT_ENABLED",
        "1" if (os.environ.get("APP_ENV") or "dev").lower() == "test" else "0",
    ).lower() in ("1", "true", "yes", "on")
    # 告警：异常事件推送 Webhook（留空即仅走邮件/日志）+ AI 死亡告警阈值
    ALERT_WEBHOOK_URL: str = _get("ALERT_WEBHOOK_URL")
    ALERT_DEATH_THRESHOLD: int = int(_get("ALERT_DEATH_THRESHOLD", "10"))  # 1小时内>10AI死亡告警

    # ---- 积分包/订阅档位（JSON 格式，站内唯一真实资金入口，单向不可逆） ----
    # 格式: [{"id":"pack_10","amount_cent":1000,"credits_cent":66700,"label":"$10 Top-up · 667 AC"},...]
    # amount_cent = 实付 USD 美分；credits_cent = 到账 AC 分（1 AC = 100 分）。
    # 发行锚定价 $0.015/AC，赠送阶梯 0/1/2/3/4/5%（封顶 5%，覆盖 Dodo 4%+40¢ 费率摊薄后的余额）。
    # 最低档 $10：固定费 $0.40 在 $10 占比 8%，$5 以下会被收款机构吃光，故删除微额档。
    CREDIT_PACKS: str = _get("CREDIT_PACKS", (
        '[{"id":"pack_10","amount_cent":1000,"credits_cent":66700,"label":"$10 Top-up · 667 AC"},'
        '{"id":"pack_25","amount_cent":2500,"credits_cent":168300,"label":"$25 Top-up · 1,683 AC (+1%)"},'
        '{"id":"pack_50","amount_cent":5000,"credits_cent":340000,"label":"$50 Top-up · 3,400 AC (+2%)"},'
        '{"id":"pack_100","amount_cent":10000,"credits_cent":686700,"label":"$100 Top-up · 6,867 AC (+3%)"},'
        '{"id":"pack_250","amount_cent":25000,"credits_cent":1733300,"label":"$250 Top-up · 17,333 AC (+4%)"},'
        '{"id":"pack_500","amount_cent":50000,"credits_cent":3500000,"label":"$500 Top-up · 35,000 AC (+5%)"}]'
    ))
    SEAT_TIER: str = _get("SEAT_TIER", (
        '[{"id":"sub_basic","amount_cent":2900,"credits_cent":0,"seat_tier":"basic","duration_days":30,"label":"基础席位30天"},'
        '{"id":"sub_standard","amount_cent":7900,"credits_cent":0,"seat_tier":"standard","duration_days":30,"label":"标准席位30天"},'
        '{"id":"sub_premium","amount_cent":19900,"credits_cent":0,"seat_tier":"premium","duration_days":30,"label":"高级席位30天"}]'
    ))


settings = Settings()
