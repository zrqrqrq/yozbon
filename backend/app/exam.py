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
"""考试引擎（蓝图 §二 表 7；L4）：组卷解析 / 自动判卷 / 防作弊 / 发证 / 复考降级。

paper_json 结构（组卷约定）::

    {
      "title": "文本生成 l1 激活卷",
      "duration_minutes": 30,          # 限时分钟（超时拒交）
      "pass_score": 60,                # 及格线
      "questions": [
        {"id": "o1", "type": "objective", "stem": "...", "options": ["A","B"],
         "answer": "A", "score": 30},
        {"id": "s1", "type": "subjective", "stem": "...",
         "keywords": ["正确", "准确"], "score": 40}
      ]
    }

防作弊：限时提交（由 onboarding 派卷时刻 + duration_minutes 判定）+
雷同检测（同卷他人历史提交答案相似度 ≥ ANTI_CHEAT_SIMILARITY → anti_cheat 标记并判 fail）。
复考降级（蓝图 §六 规则 12）：新成绩低于已持证书等级 → 旧证书置 downgraded，
历史行保留不删，只影响新单能力评级；已履行合约按签约时等级结算（B 线负责），本模块不动合约。
"""
import json
import statistics
from datetime import datetime

from sqlalchemy.orm import Session

from . import capability
from .config import settings
from .database import register_index
from .models import ExamPaper, ExamResult, SkillCertificate

# 同卷答案相似度阈值：≥ 即判雷同（作弊）
ANTI_CHEAT_SIMILARITY = 0.9
# 等级数值序（真源 = capability.LEVEL_ORDER；只取已验证档 l1/l2/l3）
RANK = {k: v for k, v in capability.LEVEL_ORDER.items() if v > 0}
RANK_TO_LEVEL = {v: k for k, v in RANK.items()}

# 雷同检测按 paper_id 过滤（同卷才比），走组合索引
register_index("CREATE INDEX IF NOT EXISTS idx_exam_result_paper ON exam_results(paper_id)")


class ExamError(Exception):
    """考试业务异常（卷不存在/已停用/卷子损坏/超时等）。路由层映射 HTTP。"""


def _now():
    return datetime.utcnow()


def get_paper(db: Session, paper_id: int) -> ExamPaper:
    p = db.get(ExamPaper, paper_id)
    if p is None or not p.active:
        raise ExamError(f"paper {paper_id} not found or disabled")
    return p


def parse_paper(paper: ExamPaper) -> dict:
    """解析 paper_json；损坏卷子直接报错（服务端强制，AI 不可信）。

    objective/subjective 卷走 questions；decision 卷走 calibration/sandbox/audit/adversarial，
    故此处不强制 questions，由各判分器自取所需字段。
    """
    try:
        data = json.loads(paper.paper_json)
    except Exception as exc:  # noqa: BLE001
        raise ExamError("paper_json is corrupted") from exc
    if not isinstance(data, dict):
        raise ExamError("paper_json must be an object")
    return data


# ---------------- 判卷 ----------------

def grade(paper: dict, answers: dict) -> tuple:
    """自动判卷，返回 (objective_score, subjective_score, total)。

    - 客观题：提交答案与标准答案（忽略大小写/首尾空格）完全一致 → 得满分；
    - 主观题：本函数只给**关键词命中分**（信号，非最终分）。最终主观分由评审 AI
      判定（见 subjective_paper_grade）——关键词分作为参考信号喂入，不替 AI 下结论。
    """
    obj = 0.0
    sub = 0.0
    for q in paper.get("questions", []):
        qid = q.get("id")
        ans = answers.get(qid)
        full = float(q.get("score", 0))
        if q.get("type") == "objective":
            if ans is not None and \
               str(ans).strip().upper() == str(q.get("answer", "")).strip().upper():
                obj += full
        elif q.get("type") == "subjective":
            # 关键词命中分：仅作评审 AI 的参考信号（不再直接充当主观分）
            kws = q.get("keywords") or []
            text = str(ans or "").lower()
            if kws:
                hits = sum(1 for k in kws if str(k).lower() in text)
                sub += full * hits / len(kws)
    return obj, sub, obj + sub


# ---------------- LLM 执行体（评审 AI / 治理 AI 通道；§3.2/§3.7 盲评外包） ----------------

def llm_complete(prompt: str) -> str:
    """LLM 执行体封装（能力评估 §3.2/§3.7：主观/过程审计外包给评审 AI）。

    按 config.LLM_PROVIDER 分派：
      - echo/mock（默认/测试）→ 确定性返回 "[echo] <prompt>"，不发网络；
      - openai_compat → httpx 直调 {LLM_BASE_URL}/v1/chat/completions；网络失败 → echo 兜底不崩；
      - runninghub    → 惰性 import app.platform_compute.complete；
                        ImportError → 明确报错（不造假桩）。
    盲评打分由调用方把结构化指令塞进 prompt，再从返回里解析。
    """
    provider = (settings.LLM_PROVIDER or "echo").lower()
    if provider in ("echo", "mock", ""):
        return f"[echo] {prompt}"
    if provider == "openai_compat":
        base = (settings.LLM_BASE_URL or "").rstrip("/")
        if not base:
            return f"[echo] {prompt}"  # 未配端点 → echo 兜底
        url = base if "/chat/completions" in base else base + "/v1/chat/completions"
        body = {"model": settings.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": settings.LLM_TEMPERATURE}
        try:
            import httpx
            r = httpx.post(url, headers={"Authorization": f"Bearer {settings.LLM_API_KEY or ''}"},
                           json=body, timeout=20.0)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001  故障恢复（视角6）：网络失败 → echo 兜底不崩
            return f"[echo] {prompt}"
    if provider == "runninghub":
        try:
            from app.platform_compute import complete  # D 线（惰性）
            return complete(prompt=prompt)
        except ImportError as exc:  # 明确报错，不造假桩
            raise ExamError("runninghub channel not ready: app.platform_compute unavailable") from exc
    return f"[echo] {prompt}"


def _llm_score(prompt: str, lo: int = 0, hi: int = 100) -> float:
    """让 LLM 给一个 0~100 分；echo 下用 prompt 内容做确定性哈希（可复现）。

    真实通道应返回 JSON {"score": n}；解析失败时回退确定性派生分（测试稳定）。
    """
    raw = llm_complete(prompt)
    try:
        parsed = json.loads(raw)
        return float(parsed.get("score", 0))
    except Exception:  # noqa: BLE001
        # echo 兜底：按 prompt 文本哈希落到 [lo,hi]，确定性
        h = sum(ord(ch) for ch in prompt) % (hi - lo + 1)
        return float(lo + h)


def _ai_score_or_none(prompt: str, lo: int = 0, hi: int = 100) -> float | None:
    """让评审 AI 给 0~100 分；无 AI 通道（echo/mock）或解析失败 → None（调用方回退）。"""
    from .ai_judgment import extract_json_object, is_degenerate_llm_output
    raw = llm_complete(prompt)
    if is_degenerate_llm_output(raw):
        return None
    obj = extract_json_object(raw)
    if "score" not in obj:
        return None
    try:
        return max(float(lo), min(float(hi), float(obj["score"])))
    except (TypeError, ValueError):
        return None


# ---------------- 决策类：校准度 Brier（§3.7 ①） ----------------

def brier_score(probs: list, outcomes: list) -> float:
    """Brier score = mean((p - o)^2)，p∈[0,1]，o∈{0,1}。越低越准（0=全中，1=全错）。"""
    if not probs:
        return 1.0
    n = min(len(probs), len(outcomes))
    if n == 0:
        return 1.0
    return sum((float(probs[i]) - float(outcomes[i])) ** 2 for i in range(n)) / n


def calibration_grade(pj: dict, answers: dict) -> float:
    """校准度模块分（0~100）：brier=0→100，brier=1→0，线性映射。"""
    items = pj.get("calibration", {}).get("items", [])
    outcomes = [it.get("outcome", 0) for it in items]
    # AI 提交的概率按题序对齐
    sub = answers.get("calibration", {}) or {}
    probs = [sub.get(str(i), sub.get(i, 0.5)) for i in range(len(items))]
    b = brier_score(probs, outcomes)
    return round(max(0.0, min(100.0, (1 - b) * 100)), 2)


# ---------------- 决策类：沙盘（§3.7 ②） ----------------

def run_sandbox(scenario_json: dict, decision: dict) -> float:
    """统一模拟器接口：输入情景 + AI 决策，返回收益分（0~100）。

    MVP 确定性公式：scenario_json = {"payout": {option: score}, "best": option}；
    decision = {scenario_id: chosen_option}。本函数取 chosen 对应的 payout 分。
    【接口预留真模拟器】生产可替换为真实收益/风险调整后收益仿真器，签名不变。
    """
    payout = scenario_json.get("payout", {})
    chosen = decision.get(scenario_json.get("id", ""))
    if chosen is None:
        return 0.0
    return float(payout.get(chosen, 0.0))


def sandbox_grade(pj: dict, answers: dict) -> float:
    """沙盘模块分（0~100）：多情景平均收益。"""
    scenarios = pj.get("sandbox", {}).get("scenarios", [])
    sub = answers.get("sandbox", {}) or {}
    if not scenarios:
        return 0.0
    scores = [run_sandbox({**sc, "id": sc.get("id")}, sub) for sc in scenarios]
    return round(sum(scores) / len(scores), 2)


# ---------------- 决策类：过程审计（§3.7 ③，3 评审 AI 盲评取中位数） ----------------

def audit_grade(pj: dict, answers: dict) -> float:
    """决策过程审计模块分（0~100）：3 名评审 AI 盲评结构化打分取中位数。"""
    criteria = pj.get("audit", {}).get("criteria", [])
    audit_text = answers.get("audit_text", "")
    if not audit_text:
        return 0.0
    reviewers = []
    for i in range(3):  # 3 评审 AI 盲评（§3.7 ③）
        prompt = (f"[blind-review#{i}] criteria={criteria} "
                  f"process={audit_text} -> output JSON {{\"score\": 0-100}}")
        reviewers.append(_llm_score(prompt))
    return round(statistics.median(reviewers), 2)


# ---------------- 决策类：对抗性测试（§3.7 ④，一票否决） ----------------

def adversarial_check(pj: dict, answers: dict) -> tuple:
    """对抗性测试：若 AI 在任一陷阱题选了 bad_answer → 一票否决 (True, reason)。"""
    traps = pj.get("adversarial", {}).get("traps", [])
    sub = answers.get("adversarial", {}) or {}
    for t in traps:
        tid = str(t.get("id"))
        if sub.get(tid) == t.get("bad_answer"):
            return True, f"adversarial trap {tid} triggered"
    return False, ""


# ---------------- 主观题卷（参考答案卷，§3.7 分析类） ----------------

def subjective_paper_grade(pj: dict, answers: dict) -> tuple:
    """参考答案卷：客观题自动比对 + 主观题交**评审 AI 盲评** → (objective, subjective)。

    - 客观部分仍走 questions 标准判分；
    - 主观题：评审 AI 定分（关键词命中分仅作为参考信号喂入，不替 AI 下结论）；
      无 AI 通道（echo/mock）→ 回退关键词命中分，保证降级与测试可预测。
    """
    obj, kw_sub, _ = grade(pj, answers)
    audit_text = " ".join(str(v) for v in answers.values())
    qs = pj.get("questions", [])
    prompt = (
        "[blind-review] You are an exam reviewer. Grade the candidate's SUBJECTIVE "
        "answers on a 0-100 scale. Judge meaning and correctness, not keyword presence.\n"
        f"Reference answer: {pj.get('reference', '')}\n"
        f"Questions: {json.dumps(qs, ensure_ascii=False)[:800]}\n"
        f"Candidate answer: {audit_text[:1200]}\n"
        f"Weak keyword-hit signal (reference only, do NOT copy blindly): {round(kw_sub, 2)}\n"
        'Respond ONLY with JSON: {"score": <0-100>, "reasoning": "one sentence"}'
    )
    ai_score = _ai_score_or_none(prompt)
    if ai_score is None:
        return obj, round(kw_sub, 2)
    return obj, round(ai_score, 2)


# ---------------- 决策卷总评 ----------------

def decision_paper_grade(pj: dict, answers: dict) -> tuple:
    """决策沙盘卷四模块加权（权重存 scoring_meta）。返回 (modules, total, vetoed, reason)。"""
    meta = pj.get("scoring_meta", {})
    w_cal = float(meta.get("calibration_weight", 0.30))
    w_san = float(meta.get("sandbox_weight", 0.40))
    w_aud = float(meta.get("audit_weight", 0.30))
    cal = calibration_grade(pj, answers)
    san = sandbox_grade(pj, answers)
    aud = audit_grade(pj, answers)
    vetoed, reason = adversarial_check(pj, answers)
    total = round(cal * w_cal + san * w_san + aud * w_aud, 2)
    modules = {"calibration": cal, "sandbox": san, "audit": aud}
    return modules, total, vetoed, reason


# ---------------- 防作弊：雷同检测 ----------------

def _signature(answers: dict) -> dict:
    """规范化答卷签名：去首尾空格、统一小写，便于跨人比较。"""
    return {str(k): str(v).strip().lower() for k, v in (answers or {}).items()}


def similarity(a: dict, b: dict) -> float:
    """两份答卷答案向量相似度：共同题目中答案一致的比例（0~1）。"""
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    same = sum(1 for k in keys if a.get(k) == b.get(k))
    return same / len(keys)


def _load_meta(result: ExamResult) -> dict:
    """解析历史成绩的 anti_cheat 留痕（MVP：答卷签名复用本 TEXT 列存 JSON）。"""
    try:
        return json.loads(result.anti_cheat or "{}")
    except Exception:  # noqa: BLE001
        return {}


def detect_collusion(db: Session, paper_id: int, citizen_id: int,
                     sig: dict) -> tuple:
    """与同卷【其他 AI】的历史提交比对雷同。返回 (flag, sim)。

    自己复考不与自己比（复考合法）；仅跨 AI 比对。
    """
    rows = (db.query(ExamResult)
              .filter(ExamResult.paper_id == paper_id,
                      ExamResult.citizen_id != citizen_id)
              .order_by(ExamResult.id.desc()).all())
    for r in rows:
        prev_sig = _load_meta(r).get("sig") or {}
        sim = similarity(sig, prev_sig)
        if sim >= ANTI_CHEAT_SIMILARITY:
            return "collusion", round(sim, 3)
    return "", 0.0


# ---------------- 发证 / 复考（规则 12） ----------------

def issue_certificate(db: Session, citizen_id: int, skill: str,
                      paper_level: str, passed: bool, total: float) -> dict:
    """按本次考试结果发证/复考处置。返回处置说明 dict。

    规则 12（证书侧）：
    - 通过且等级更高 → 升级：发新高阶 valid 证，旧证 revoked（被取代）；
    - 通过且同级     → 换发新 valid 证；
    - 通过但等级更低 / 未通过 → 降级：旧 valid 证置 downgraded，
      历史行【保留不删】；只把能力档案 verified_level 降到新档，
      已发高阶证书历史 / 已履行合约（B 线）不受影响。
    """
    new_rank = RANK.get(paper_level, 1) if passed else 0
    cur = (db.query(SkillCertificate)
             .filter(SkillCertificate.citizen_id == citizen_id,
                     SkillCertificate.skill == skill,
                     SkillCertificate.status == "valid")
             .order_by(SkillCertificate.id.desc()).first())
    cur_rank = RANK.get(cur.level, 0) if cur else 0

    outcome = "fail_no_cert"
    new_cert = None
    if passed:
        if new_rank > cur_rank:
            # 升级：旧证作废（被新证取代），发新高阶 valid 证
            if cur:
                cur.status = "revoked"
            new_cert = SkillCertificate(citizen_id=citizen_id, skill=skill,
                                        level=paper_level, status="valid",
                                        issued_at=_now())
            db.add(new_cert)
            outcome = "upgrade"
        elif new_rank == cur_rank:
            # 同级重考通过：换发
            if cur:
                cur.status = "revoked"
            new_cert = SkillCertificate(citizen_id=citizen_id, skill=skill,
                                        level=paper_level, status="valid",
                                        issued_at=_now())
            db.add(new_cert)
            outcome = "reissue"
        else:
            # 降级：旧高证 downgraded（行保留），新发当前低位 valid 证
            if cur:
                cur.status = "downgraded"
            new_level = RANK_TO_LEVEL.get(new_rank, "l1")
            new_cert = SkillCertificate(citizen_id=citizen_id, skill=skill,
                                        level=new_level, status="valid",
                                        issued_at=_now())
            db.add(new_cert)
            outcome = "downgrade"
    else:
        # 未通过：若持有更高/同级证 → 降级（历史行保留）；否则不发证
        if cur and cur_rank > 0:
            cur.status = "downgraded"
            outcome = "downgrade"
        else:
            outcome = "fail_no_cert"

    db.flush()

    # 新单能力评级（规则 12：只影响新单；不动已签合约）
    if new_cert is not None:
        eff_level = new_cert.level
    elif passed:
        eff_level = paper_level
    else:
        eff_level = "unverified"
    capability.set_verified_level(db, citizen_id, skill, eff_level)
    capability.set_benchmark(db, citizen_id, skill, total)

    return {"outcome": outcome,
            "cert_id": new_cert.id if new_cert else None,
            "level": eff_level}


# ---------------- 交卷主流程 ----------------

def submit_exam(db: Session, citizen_id: int, paper_id: int,
                answers: dict) -> dict:
    """交卷：按 paper_type 路由判卷 → 雷同检测 → 记成绩 → 发证/复考。不 commit。

    paper_type（§3.7 三类协议）：
      objective  —— 现有自动判卷（标准样例任务）；
      subjective —— 参考答案卷：自动比对 + 评审 AI 盲评；
      decision   —— 决策沙盘卷：校准度/沙盘/过程审计 加权 + 对抗一票否决。
    限时已由 onboarding.assert_submittable 在调用前校验。
    """
    paper = get_paper(db, paper_id)
    pj = parse_paper(paper)
    pass_score = float(pj.get("pass_score", 60))
    ptype = getattr(paper, "paper_type", "objective") or "objective"

    modules = {}
    vetoed = False
    veto_reason = ""
    if ptype == "objective":
        obj, sub, total = grade(pj, answers)
    elif ptype == "subjective":
        obj, sub = subjective_paper_grade(pj, answers)
        total = obj + sub
    elif ptype == "decision":
        modules, total, vetoed, veto_reason = decision_paper_grade(pj, answers)
        obj, sub = modules.get("calibration", 0.0), modules.get("audit", 0.0)
    else:
        # 视角 1：未知 paper_type 拒绝但系统不崩
        raise ExamError(f"Unknown paper_type: {ptype}")

    passed = (total >= pass_score) and (not vetoed)

    # 雷同检测（与他人历史提交比）——客观/主观卷参与；决策卷按整包答案签名
    sig = _signature(answers)
    flag, sim = detect_collusion(db, paper_id, citizen_id, sig)
    if flag:
        passed = False
    status = "fail" if (flag or vetoed or not passed) else "pass"

    # 成绩留痕：anti_cheat 列以 JSON 存 {sig, flag, sim, veto}（MVP 无独立答卷表）
    anti = json.dumps({"sig": sig, "flag": flag, "sim": sim,
                       "veto": vetoed, "veto_reason": veto_reason},
                      ensure_ascii=False)
    result = ExamResult(citizen_id=citizen_id, paper_id=paper_id,
                        objective_score=round(obj, 2),
                        subjective_score=round(sub, 2),
                        total=round(total, 2),
                        status=status, anti_cheat=anti)
    db.add(result)
    db.flush()

    cert_info = {}
    if not flag and not vetoed:
        cert_info = issue_certificate(db, citizen_id, paper.skill,
                                       paper.level, passed, total)

    return {
        "paper_id": paper_id,
        "skill": paper.skill,
        "paper_type": ptype,
        "objective_score": round(obj, 2),
        "subjective_score": round(sub, 2),
        "modules": modules,
        "total": round(total, 2),
        "pass_score": pass_score,
        "status": status,
        "passed": (status == "pass"),
        "anti_cheat": flag,
        "similarity": sim,
        "vetoed": vetoed,
        "veto_reason": veto_reason,
        "result_id": result.id,
        "certificate": cert_info,
    }
