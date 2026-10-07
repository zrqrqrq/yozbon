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
"""任务编排引擎（AI 员工工位系统核心）。

定位：坐在 compute.exec() 之上的"大脑层"——接收复杂任务描述，
自动分解为子任务序列/并行图，路由到正确的算力通道执行，汇总交付物。

核心能力：
  1. **任务分解**：LLM 驱动，将自然语言复杂任务 → 结构化子任务 DAG；
  2. **技能路由**：按子任务的 skill 域匹配最优执行 kind（复用 model_fallback）；
  3. **多 Agent 编排**：子任务按依赖序串联（串行/并行）派发给不同 Agent；
  4. **进度追踪**：每个编排任务有完整状态机（planning→executing→reviewing→done/failed）；
  5. **失败重试**：子任务级失败自动重试/降级，不阻断整条编排链。

与现有模块的关系（全部只读调用，不改任何现有文件）：
  - compute.exec()：最终执行入口，本模块构造 kind+prompt+params 后调用它；
  - platform_compute：底层 6 链路 + LLM 能力，由 compute.exec 内部调度；
  - model_fallback：可选降级路径，本模块的 model_router 使用其 resolve()；
  - OrchestrationRecord：编排执行记录持久化（独立审计表，不复用队列表）。

设计原则：
  - MVP 阶段同步执行（submit→poll→done），与 platform_compute.run() 同风格；
  - 任务分解结果缓存为 JSON（存 OrchestrationRecord.payload），可恢复/可审计；
  - 编排状态机：pending → planning → executing → done/failed。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from . import compute, moderation
from .models import (AICitizen, OrchestrationRecord, AIPermission,
                     AuditLog, GovernanceTask)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


# ---------------- 任务分解 Prompt 模板 ----------------

DECOMPOSE_SYSTEM = """\
You are a multimodal task decomposition engine for a creative production platform. Given a task description, decompose it into a structured subtask DAG that leverages MULTIPLE compute channels to produce rich media deliverables.

Output format is strictly a JSON array; each subtask:
{
  "id": "sub_0",
  "skill": "<image|hd_image|img2img|music|video_civil|video_openvdn|llm|video_analysis>",
  "action": "<concrete prompt for the execution layer>",
  "depends_on": ["sub_X", ...],
  "params": {}
}

## HARD CAPABILITY LIMITS (violation = task failure)

### video_civil (MiniMax H3 文戏链路)
- duration: integer, MIN 5, MAX 15 seconds. NEVER exceed 15.
- quality: ONLY "480p" or "720p". 1080p and other values DO NOT EXIST.
- aspect: ONLY "16:9", "9:16", "3:4", "4:3", "21:9", "1:1".
- params allowed: {"duration": 5-15, "quality": "480p"|"720p", "aspect": "16:9"|...}

### video_openvdn (MiniMax H3 高动态链路)
- duration: integer, MIN 5, MAX 15 seconds. NEVER exceed 15.
- quality: ONLY "720p" (high dynamic range chain only supports 720p).
- aspect: ONLY "16:9", "9:16", "3:4", "4:3", "21:9", "1:1".
- Use when the task requires high-motion/action/dynamic scenes.

### music (MiniMax Music3)
- duration_sec: ONLY these 4 discrete values: 90, 120, 150, 180 (seconds). No others.
- params allowed: {"duration_sec": 90|120|150|180, "genre": "...", "bpm": int, "mood": "...", "instruments": [...]}
- The caption is auto-formatted by the system. The "action" field should be a natural-language music description (Chinese or English).

### image / hd_image (Z-Image 文生图)
- aspect: "1:1", "16:9", "9:16", "3:4", "4:3", "21:9" (any common ratio).
- hd_image for high-resolution output (up to 3072x3072); image for standard (1024x1024).
- params allowed: {"aspect": "..."} or leave empty.

### llm
- Pure text generation. No special params needed beyond defaults.

### video_analysis (Qwen3-VL 视频诊断)
- Analyzes video content (NOT video generation). Input: a video file path.
- The system runs a pipeline: ffmpeg frame extraction → compression → sliding window → per-chunk VLM → merge.
- NEVER pass a raw video URL as a "prompt" — use params.video_path for the file path.
- The model CANNOT "watch" a video directly. It needs the preprocessing pipeline (handled automatically).
- Output: JSON diagnostic report (shot types, lighting, expressions, actions, dialogue match, timestamps).
- Max input duration: 600s (10min). Longer videos must be pre-trimmed.
- params allowed: {"video_path": "..."} plus optional strategy hints (fps/chunk_sec/overlap_sec/focus).
  AI 3 decides the final strategy itself within the hardware limits declared in the capability card
  (capability_cards.video_analysis.limits) — do NOT hardcode a range here.
- The "action" field should be a description of what to analyze (e.g., "诊断广告视频的画面质量和创意表现").

## Decomposition Strategy

For **video/short-film/ad/animation** tasks, use a 2-step pipeline:
1. `llm` → Write an ultra-detailed video generation prompt (covering scene, subject, camera, lighting, color, transitions — ALL in one comprehensive English prompt). Max video duration is 15s; if user requests longer, split into multiple video segments.
2. `video_civil` (文戏/叙事) OR `video_openvdn` (高动态/动作) → Feed that prompt to the video model.

Use `{sub_0_output}` in sub_1's action to inject the LLM's prompt output into the video generation call.

For **image/poster/illustration** tasks:
1. `llm` → Write detailed visual concept description
2. `image` or `hd_image` → Generate the image(s)

For **pure text/analysis** tasks → single `llm` subtask.
For **music/audio** tasks → single `music` subtask with duration_sec ∈ {90, 120, 150, 180}.
For **video analysis/diagnosis/review** tasks → single `video_analysis` subtask (provide video_path in params).

## Rules
- MINIMAL decomposition: only split into subtasks when different skills/models are needed.
- For video tasks: ALWAYS exactly 2 steps (llm prompt → video_civil/video_openvdn). NEVER add image/music/tts subtasks for video — the video model handles all internally.
- For LONGER videos (>15s): split into multiple video_civil subtasks (each ≤15s), parallel when independent.
- For image tasks: 2 steps (llm concept → image generation).
- depends_on: list prerequisite subtask ids. Subtasks with no deps can execute in parallel.
- Each "action" must be a self-contained, specific prompt suitable for that skill's execution engine.
- For llm subtask before video: write an ultra-detailed English prompt covering ALL visual elements (scene, camera work, lighting, color grading, transitions). The video duration is at most 15 seconds.
- For video_civil/video_openvdn action: use "{sub_0_output}" to inject the LLM prompt. Params MUST have duration (5-15) and quality (480p or 720p).
- NEVER output duration > 15 for video. NEVER output resolution/quality values like "1080p" for video.
- output JSON only, no other text.

## Example 1: "制作一个品牌宣传片（15秒）"
[
  {"id":"sub_0","skill":"llm","action":"Write an ultra-detailed video generation prompt for a cinematic brand promotional video. Must include: (1) Opening scene with cinematic wide shot of brand environment, golden hour lighting, anamorphic lens flare; (2) Product hero shot with slow dolly-in, shallow depth of field, rim lighting; (3) Lifestyle montage with quick cuts, dynamic handheld camera, vibrant color grading; (4) Closing brand logo reveal with elegant motion graphics. Include background music (upbeat electronic, 120 BPM) and smooth transitions.","depends_on":[],"params":{}},
  {"id":"sub_1","skill":"video_civil","action":"{sub_0_output}","depends_on":["sub_0"],"params":{"duration":15,"quality":"720p","aspect":"16:9"}}
]

## Example 2: "画一张城市夜景赛博朋克海报"
[
  {"id":"sub_0","skill":"llm","action":"Write a detailed image generation prompt for a cyberpunk city nightscape poster: towering neon-lit skyscrapers, rain-slicked streets reflecting holographic advertisements, flying vehicles with light trails, dominant color palette of purple-blue-pink, cinematic composition with rule of thirds, ultra-detailed textures, vertical 3:4 aspect ratio poster format.","depends_on":[],"params":{}},
  {"id":"sub_1","skill":"hd_image","action":"{sub_0_output}","depends_on":["sub_0"],"params":{"aspect":"3:4"}}
]

## Example 3: "给产品写一段营销文案"
[
  {"id":"sub_0","skill":"llm","action":"为该产品撰写一段有感染力的营销文案，包含标题、副标题、正文、行动号召。","depends_on":[],"params":{}}
]

## Example 4: "来一段轻快的背景音乐"
[
  {"id":"sub_0","skill":"music","action":"轻快的电子流行背景音乐，明亮积极的氛围，适合短视频配曲","depends_on":[],"params":{"duration_sec":120,"genre":"Pop/Electronic","mood":"upbeat","bpm":120}}
]"""

DECOMPOSE_USER_TEMPLATE = """\
Task description: {task_description}
{constraints_block}
Output the subtask decomposition JSON array:"""


# ---------------- 技能→kind 映射表 ----------------

SKILL_KIND_MAP = {
    "image": "image",
    "hd_image": "hd_image",
    "img2img": "img2img",
    "music": "music",
    "video": "video_civil",
    "video_civil": "video_civil",
    "video_openvdn": "video_openvdn",
    "llm": "llm",
    "text": "llm",
    "copywriting": "llm",
    "script": "llm",
    "analysis": "llm",
    "translation": "llm",
    "general": "llm",
    "video_analysis": "video_analysis",
    "video_diagnosis": "video_analysis",
    "visual_analysis": "video_analysis",
}


def _skill_to_kind(skill: str) -> str:
    """技能标签 → 执行 kind（platform_compute 接受值）。"""
    return SKILL_KIND_MAP.get(skill.lower().strip(), "llm")


# ---------------- 编排数据模型（内存，持久化用 OrchestrationRecord） ----------------

class OrchestrationPlan:
    """编排计划（任务分解结果）。"""

    def __init__(self, plan_id: str, task_description: str,
                 subtasks: list, constraints: Optional[dict] = None):
        self.plan_id = plan_id
        self.task_description = task_description
        self.subtasks = subtasks  # list of dict: {id, skill, kind, action, depends_on, params}
        self.constraints = constraints or {}
        self.created_at = _now()

    def to_json(self) -> str:
        return json.dumps({
            "plan_id": self.plan_id,
            "task_description": self.task_description,
            "subtasks": self.subtasks,
            "constraints": self.constraints,
            "created_at": self.created_at.isoformat(),
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "OrchestrationPlan":
        data = json.loads(raw)
        plan = cls.__new__(cls)
        plan.plan_id = data["plan_id"]
        plan.task_description = data["task_description"]
        plan.subtasks = data["subtasks"]
        plan.constraints = data.get("constraints", {})
        plan.created_at = datetime.fromisoformat(data["created_at"])
        return plan


class OrchestrationResult:
    """编排执行结果。"""

    def __init__(self, plan_id: str, status: str,
                 subtask_results: Optional[list] = None,
                 error: str = ""):
        self.plan_id = plan_id
        self.status = status  # planning/executing/partial/done/failed
        self.subtask_results = subtask_results or []
        self.error = error

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "subtask_results": self.subtask_results,
            "error": self.error,
        }


# ---------------- 任务分解 ----------------

def decompose_task(db: Session, task_description: str,
                   constraints: Optional[dict] = None) -> OrchestrationPlan:
    """将复杂任务分解为子任务 DAG（LLM 驱动）。

    MVP 实现：
    - 调用 LLM（compute.exec(kind="llm")）获取分解结果；
    - 解析 JSON，校验结构；
    - 解析失败时 fallback 为单子任务（整任务作为 llm 执行）。

    Args:
        db: Session。
        task_description: 自然语言任务描述。
        constraints: 可选约束（{"budget_cent": 1000, "skills_preferred": [...]}）。

    Returns:
        OrchestrationPlan 对象。
    """
    plan_id = f"plan_{uuid.uuid4().hex[:12]}"
    constraints = constraints or {}

    # 构造分解 prompt
    constraints_block = ""
    if constraints:
        constraints_block = f"Constraints: {json.dumps(constraints, ensure_ascii=False)}"
    user_msg = DECOMPOSE_USER_TEMPLATE.format(
        task_description=task_description,
        constraints_block=constraints_block,
    )

    # 调用 LLM 做分解（用 echo 即可测试，生产用真 LLM）
    decompose_prompt = f"{DECOMPOSE_SYSTEM}\n\n{user_msg}"
    llm_result = _call_llm_for_decompose(decompose_prompt)

    # 解析子任务列表
    subtasks = _parse_subtasks(llm_result, task_description)

    # 映射 skill → kind
    for st in subtasks:
        st["kind"] = _skill_to_kind(st.get("skill", "llm"))

    return OrchestrationPlan(
        plan_id=plan_id,
        task_description=task_description,
        subtasks=subtasks,
        constraints=constraints,
    )


def _call_llm_for_decompose(prompt: str) -> str:
    """调用 LLM 进行任务分解（独立于 compute，直接走 platform_compute.complete）。

    输出封顶 LLM_MAX_TOKENS_DECOMPOSE（分解产物只是结构化 JSON，无需长文；
    不封顶时 RH 实测会生成上万 tokens，白等 ~58s）。
    测试环境下 LLM_PROVIDER=echo → 返回 mock 分解结果。
    """
    from . import platform_compute
    from .config import settings
    try:
        return platform_compute.complete(
            prompt, max_tokens=settings.LLM_MAX_TOKENS_DECOMPOSE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM decompose call failed: %s, fallback to single task", exc)
        return ""


def _heuristic_decompose(original_task: str) -> list:
    """LLM 分解失败时，按关键词启发式生成分解方案。

    识别"视频/广告/短片/动画"类任务 → 多模态流水线；
    "图片/海报/插画" → 图片管线；
    其他 → 单子任务兜底。
    """
    task_lower = original_task.lower()

    # --- 视频/短片/广告/动画/宣传片/MV 类 ---
    # 现代视频模型（Kling/Hailuo等）支持文本直出完整视频含配乐配音，
    # 核心是写好提示词 → 拆成2步：LLM写超级详细的视频prompt → video生成。
    video_kw = ("视频", "短片", "广告", "宣传片", "动画", "mv", "影片",
                "video", "film", "movie", "clip", "animation", "commercial")
    if any(kw in task_lower for kw in video_kw):
        return [
            {
                "id": "sub_0",
                "skill": "llm",
                "action": (
                    f"你是顶级AI视频提示词工程师。根据以下需求，输出一段极其详细的视频生成提示词（英文为主，中文为辅）：\n"
                    f"需求：{original_task}\n\n"
                    f"提示词必须包含以下全部要素（直接输出提示词正文，不要解释）：\n"
                    f"1. 场景与环境：具体的地点、时间段、天气、氛围\n"
                    f"2. 主体与动作：人物/产品的外观细节、动作描述、表情\n"
                    f"3. 运镜与构图：镜头运动（push in/dolly/pan/orbit/tilt）、景别、构图法则\n"
                    f"4. 光影与色调：光线方向、色温、调色风格（cinematic color grading）\n"
                    f"5. 转场与节奏：镜头切换方式、剪辑节奏、速度变化\n"
                    f"6. 配乐与音效：音乐风格、节奏BPM、情绪变化、环境音效\n"
                    f"7. 配音/旁白：语气、语速、关键台词内容（如有）\n"
                    f"8. 时长与分辨率：目标时长、画幅比例\n"
                    f"输出格式：一段完整的、可直接喂给视频生成模型的英文prompt，"
                    f"附带中文配音/旁白文案。"
                ),
                "depends_on": [],
                "params": {"system": "你是专业AI视频prompt工程师，输出直接可用于视频生成模型的高质量提示词。"},
            },
            {
                "id": "sub_1",
                "skill": "video_civil",
                "action": f"根据提示词生成完整视频（含画面、配乐、配音）：{{sub_0_output}}",
                "depends_on": ["sub_0"],
                "params": {"duration": 10, "quality": "high"},
            },
        ]

    # --- 图片/海报/插画/封面/设计 类 ---
    image_kw = ("图片", "海报", "插画", "封面", "设计", "logo", "banner",
                "poster", "illustration", "picture", "photo", "画")
    if any(kw in task_lower for kw in image_kw):
        return [
            {
                "id": "sub_0",
                "skill": "llm",
                "action": f"根据以下需求撰写详细视觉概念描述：{original_task}。包含风格、构图、色调、主体、背景、氛围等要素。",
                "depends_on": [],
                "params": {},
            },
            {
                "id": "sub_1",
                "skill": "image",
                "action": f"根据视觉概念生成高质量图像：{original_task}。要求专业级画质，细节丰富，构图精美。",
                "depends_on": ["sub_0"],
                "params": {"quality": "high"},
            },
        ]

    # --- 音乐/BGM/配乐/歌曲 类 ---
    music_kw = ("音乐", "配乐", "bgm", "歌曲", "旋律", "music", "song", "audio")
    if any(kw in task_lower for kw in music_kw):
        return [
            {
                "id": "sub_0",
                "skill": "music",
                "action": original_task,
                "depends_on": [],
                "params": {},
            },
        ]

    # --- 兜底：单子任务 ---
    return [{
        "id": "sub_0",
        "skill": "llm",
        "action": original_task,
        "depends_on": [],
        "params": {},
    }]


def _parse_subtasks(llm_text: str, original_task: str) -> list:
    """从 LLM 输出解析子任务数组；失败则用启发式兜底。"""
    # 尝试提取 JSON 数组
    text = llm_text.strip()
    # 可能被 markdown code block 包裹
    if "```" in text:
        for line_block in text.split("```"):
            line_block = line_block.strip()
            if line_block.startswith("["):
                text = line_block
                break
    # 尝试找到第一个 [ 和最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list) and len(arr) >= 1:
                # 校验每个子任务结构
                valid = []
                for i, st in enumerate(arr[:8]):  # 最多 8 个子任务
                    if not isinstance(st, dict):
                        continue
                    valid.append({
                        "id": st.get("id", f"sub_{i}"),
                        "skill": st.get("skill", "llm"),
                        "action": st.get("action", original_task),
                        "depends_on": st.get("depends_on", []),
                        "params": st.get("params", {}),
                    })
                if valid:
                    return valid
        except (json.JSONDecodeError, TypeError):
            pass

    # LLM 输出无法解析 → 启发式分解（比简单兜底好得多）
    logger.info("LLM decompose unparseable, using heuristic fallback for: %s",
                original_task[:60])
    return _heuristic_decompose(original_task)


# ---------------- 编排执行 ----------------

def execute_plan(db: Session, citizen: AICitizen,
                 plan: OrchestrationPlan,
                 assign_citizens: Optional[dict] = None) -> OrchestrationResult:
    """按编排计划执行子任务（按依赖拓扑序）。

    支持多 Agent 编排：assign_citizens 为 {subtask_id: citizen_id} 映射时，
    每个子任务分配给对应的 AI 执行；未分配的或映射缺失时退化为发起者执行。

    Args:
        db: Session。
        citizen: 执行主体（默认为编排发起者自己执行所有子任务）。
        plan: 编排计划。
        assign_citizens: 可选的子任务→Agent 映射 {subtask_id: citizen_id}。

    Returns:
        OrchestrationResult。
    """
    subtask_results = []
    completed_ids = set()
    all_done = True
    has_failure = False
    # 子任务输出缓存：{subtask_id: text_output}，用于下游 {sub_X_output} 占位替换
    _sub_outputs: dict[str, str] = {}

    # 预加载 assign_citizens 中涉及的 citizen 对象（批量查库，避免 N+1）
    _assign_map: dict[str, AICitizen] = {}
    if assign_citizens:
        needed_ids = set(assign_citizens.values())
        if needed_ids:
            loaded = (db.query(AICitizen)
                        .filter(AICitizen.id.in_(needed_ids))
                        .all())
            id_to_obj = {c.id: c for c in loaded}
            for st_id, cid in assign_citizens.items():
                obj = id_to_obj.get(cid)
                if obj is not None:
                    _assign_map[st_id] = obj

    # 拓扑排序（Kahn 简化版：迭代找无依赖的节点）
    remaining = list(plan.subtasks)
    max_iterations = len(remaining) * 2  # 防环死循环

    iteration = 0
    while remaining and iteration < max_iterations:
        iteration += 1
        # 找当前可执行的（所有依赖已完成）
        ready = []
        not_ready = []
        for st in remaining:
            deps = st.get("depends_on", [])
            if all(d in completed_ids for d in deps):
                ready.append(st)
            else:
                not_ready.append(st)

        if not ready:
            # 存在环或依赖缺失 → 把剩余全部标记失败
            for st in remaining:
                subtask_results.append({
                    "subtask_id": st["id"],
                    "status": "failed",
                    "error": "dependencies unsatisfiable (possible cycle)",
                    "result": None,
                })
            has_failure = True
            break

        # 执行 ready 中的子任务
        for st in ready:
            # 占位符替换：把 {sub_X_output} 替换为已完成子任务的文本输出
            action = st.get("action", "")
            if "{" in action:
                for dep_id in st.get("depends_on", []):
                    placeholder = "{" + dep_id + "_output}"  # {sub_X_output}（单层花括号）
                    if placeholder in action:
                        action = action.replace(placeholder, _sub_outputs.get(dep_id, ""))
                st = {**st, "action": action}

            # 多 Agent 分配：优先用 assign_citizens 指定的执行者
            executor = _assign_map.get(st.get("id", ""), citizen)
            result = _execute_subtask(db, executor, st)
            subtask_results.append(result)

            # ===== 执行反馈回灌：每次子任务完成后自动更新能力分数 =====
            try:
                from .capability_discovery import record_execution_feedback
                _fb_kind = st.get("kind", "llm")
                _fb_success = result.get("status") == "succeeded"
                _fb_quality = 0.0
                if _fb_success:
                    # 有文本/文件产出视为基础质量 0.6；有文件产出 0.8
                    _r = result.get("result") or {}
                    _fb_quality = 0.8 if _r.get("files") else 0.6
                _fb_error = "" if _fb_success else (result.get("error", "") or "")[:200]
                record_execution_feedback(
                    db, executor.id, _fb_kind, _fb_success, _fb_quality, _fb_error
                )
            except Exception:  # noqa: BLE001
                pass  # 反馈回灌失败不阻断编排
            # ===== END 执行反馈回灌 =====

            if result["status"] == "succeeded":
                completed_ids.add(st["id"])
                # 缓存文本输出（llm/vlm 通道返回 text 字段）
                text_out = (result.get("result") or {}).get("text", "")
                if text_out:
                    _sub_outputs[st["id"]] = text_out
            else:
                has_failure = True
                # 失败不中断，继续执行其他可独立执行的子任务

        remaining = not_ready

    # 处理剩余未执行的
    for st in remaining:
        subtask_results.append({
            "subtask_id": st["id"],
            "status": "skipped",
            "error": "preceding subtask failed; skipped",
            "result": None,
        })
        has_failure = True

    # 汇总状态
    if not has_failure:
        status = "done"
    elif all(r["status"] in ("succeeded",) for r in subtask_results):
        status = "done"
    elif any(r["status"] == "succeeded" for r in subtask_results):
        status = "partial"
    else:
        status = "failed"

    return OrchestrationResult(
        plan_id=plan.plan_id,
        status=status,
        subtask_results=subtask_results,
    )


# ---------------- 参数校验/钳位层（唯一真源：上级目录 domain.py 能力边界） ----------------

# 视频能力边界（与 D:\traepo\juhe\backend\app\domain.py 对齐）
_VIDEO_DURATION_MIN = 5
_VIDEO_DURATION_MAX = 15
_VIDEO_QUALITIES = {"480p", "720p"}
_VIDEO_QUALITY_OPENVDN = {"720p"}
_VIDEO_ASPECTS = {"16:9", "9:16", "3:4", "4:3", "21:9", "1:1"}

# 音乐能力边界
_MUSIC_DURATIONS = [90, 120, 150, 180]


def _clamp_params(kind: str, params: dict) -> dict:
    """钳位/校正参数到各 AI 通道的合法范围（复用上级 domain.py 口径）。

    不丢弃参数、不报错——只把越界值钳到最近合法值，确保执行层不崩。
    """
    params = dict(params)  # 不修改原 dict

    if kind in ("video_civil", "video_openvdn"):
        # 时长钳到 [5, 15]
        raw_dur = params.get("duration", 8)
        try:
            dur = int(raw_dur)
        except (TypeError, ValueError):
            dur = 8
        params["duration"] = max(_VIDEO_DURATION_MIN, min(_VIDEO_DURATION_MAX, dur))

        # 画质钳到合法集
        q = str(params.get("quality") or "480p").lower().strip()
        allowed = _VIDEO_QUALITY_OPENVDN if kind == "video_openvdn" else _VIDEO_QUALITIES
        if q not in allowed:
            # 尝试从原始值提取数字匹配
            if "1080" in q or "4k" in q:
                q = max(allowed)  # 越界高画质 → 最高可用档
            elif "480" in q or "sd" in q:
                q = min(allowed)
            else:
                q = "720p" if "720" in q else "480p"
            q = q if q in allowed else sorted(allowed)[0]
        params["quality"] = q

        # 画幅校验
        asp = str(params.get("aspect") or "16:9").strip()
        if asp not in _VIDEO_ASPECTS:
            asp = "16:9"
        params["aspect"] = asp

        # 兼容旧字段名 resolution → quality
        if "resolution" in params and "quality" not in params:
            params["quality"] = params.pop("resolution")
        elif "resolution" in params:
            params.pop("resolution")

    elif kind == "music":
        # 时长钳到档位（向上取最近档位）
        raw_dur = params.get("duration_sec") or params.get("duration") or 90
        try:
            dur = int(raw_dur)
        except (TypeError, ValueError):
            dur = 90
        clamped = next((d for d in _MUSIC_DURATIONS if d >= dur), _MUSIC_DURATIONS[-1])
        params["duration_sec"] = clamped
        params.pop("duration", None)  # 移除歧义字段，统一用 duration_sec

    elif kind in ("image", "hd_image"):
        # 图片画幅兼容：aspect_ratio → aspect
        if "aspect_ratio" in params and "aspect" not in params:
            params["aspect"] = params.pop("aspect_ratio")
        # quality 字段对图片无意义，移除防混淆
        params.pop("quality", None)

    # video_analysis 不在此钳位：其边界由 capability_cards 唯一真源声明，
    # 由 video_analyzer.analyze_video 自行钳位（AI 3 自主决策策略）。

    return params


def _build_harness_call_fn(kind: str):
    """为 harness_engine 构建模型调用函数。

    返回签名: (prompt: str, images: list|None, system: str, max_tokens: int) -> str
    根据 kind 选择 VLM 或 LLM 通道。
    """
    from . import platform_compute

    def call_fn(prompt: str, images=None, system: str = "", max_tokens: int = 1024) -> str:
        if kind == "vlm" or images:
            return platform_compute.vlm_complete(
                prompt, images=images, system=system, max_tokens=max_tokens)
        return platform_compute.complete(
            prompt, system=system, max_tokens=max_tokens)

    return call_fn


def _execute_subtask(db: Session, citizen: AICitizen,
                     subtask: dict) -> dict:
    """执行单个子任务。调用 compute.exec() 完成实际算力调用。

    AI社会核心机制："不允许傻子AI" —— 有思维能力的AI在执行前必须像正常人一样
    先判断这件事该不该做、怎么做才对（ai_judgment.pre_work_judgment）。

    执行流程：
    1. 参数钳位（硬性边界校验）
    2. 【工作前判断】思考型AI自检：能不能做、该不该做、怎么做最好、用什么工具
    3. 路径分流：Agent Harness 多步循环 / 正常 compute 执行
    4. 失败时智能重试（基于判断的策略信息）
    5. 执行反馈回灌
    """
    from .config import settings
    kind = subtask.get("kind", "llm")
    action = subtask.get("action", "")
    params = dict(subtask.get("params") or {})

    # ---- 参数校验/钳位：确保不越出 AI 通道的能力边界 ----
    params = _clamp_params(kind, params)

    if kind == "llm":
        params.setdefault("max_tokens", settings.LLM_MAX_TOKENS_EXECUTE)
    subtask_id = subtask.get("id", "unknown")

    # ---- 工作前判断：思考型AI必须像正常人一样先想再做 ----
    # 这是 AI 社会"不允许傻子AI捣乱"的核心保障
    judgment_strategy = ""
    use_harness = False
    tools_suggested: list = []
    try:
        from .ai_judgment import pre_work_judgment
        j = pre_work_judgment(db, citizen, kind, action, params)
        if not j.skipped:
            if j.refused:
                # AI 主动拒绝：返回拒绝结果，交还编排层处理
                logger.info("subtask %s REFUSED by %s: %s",
                            subtask_id, citizen.name, j.reasoning)
                return {
                    "subtask_id": subtask_id,
                    "skill": subtask.get("skill", kind),
                    "kind": kind,
                    "action": action[:100],
                    "status": "refused",
                    "result": {"text": "", "files": [], "error": j.reasoning},
                    "error": f"AI拒绝执行: {j.reasoning}",
                    "judgment": j.to_dict(),
                }
            if j.decision == "adjust" and j.adjusted_params:
                # AI 建议调整参数：合并（AI判断优先于编排层原始参数）
                params.update(j.adjusted_params)
                logger.info("subtask %s params ADJUSTED by %s: %s",
                            subtask_id, citizen.name,
                            json.dumps(j.adjusted_params, ensure_ascii=False)[:100])
            # 记录策略信息（用于重试时参考）
            judgment_strategy = j.strategy_notes
            # 检查 AI 是否建议使用 Agent Harness 多步循环
            use_harness = j.use_harness
            tools_suggested = j.tools_suggested

            # judg_dedup：video_analysis 的分析策略已在上面的判断里由同一个 AI 定好，
            # 直接透传给 video_analyzer，避免抽帧前再调用一次 VLM 做重复决策。
            if kind == "video_analysis":
                ap = j.adjusted_params or {}
                if any(ap.get(k) for k in ("fps", "chunk_sec", "overlap_sec", "analysis_focus")):
                    params["prior_strategy"] = {
                        "fps": ap.get("fps"),
                        "chunk_sec": ap.get("chunk_sec"),
                        "overlap_sec": ap.get("overlap_sec"),
                        "analysis_focus": ap.get("analysis_focus"),
                        "reasoning": j.strategy_notes or "decided during pre-work judgment",
                    }
    except Exception:  # noqa: BLE001
        pass  # 判断层异常绝不能阻断执行

    # ---- Agent 委托路径：优先 DSH Runtime（高级），失败回退 harness_engine（原生） ----
    if use_harness:
        # 1) 尝试 DeepSeek Harness Runtime（有 shell/文件操作等系统级工具）
        dsh_result = None
        try:
            from .dsh_bridge import is_available as dsh_available, run_dsh_task
            if dsh_available():
                dsh_result = run_dsh_task(action, timeout=int(params.get("timeout", 90)))
        except Exception as e:  # noqa: BLE001
            logger.warning("subtask %s dsh_bridge import/run failed: %s", subtask_id, e)

        if dsh_result and dsh_result.get("ok"):
            return {
                "subtask_id": subtask_id,
                "skill": subtask.get("skill", kind),
                "kind": kind,
                "action": action[:100],
                "status": "succeeded",
                "result": {
                    "job_id": "",
                    "files": [],
                    "text": dsh_result.get("response", ""),
                    "error": "",
                    "meta": {
                        "mode": "dsh_runtime",
                        "session_id": dsh_result.get("session_id", ""),
                        "elapsed_ms": dsh_result.get("elapsed_ms", 0),
                    },
                },
                "error": "",
            }
        elif dsh_result and dsh_result.get("status") not in ("unavailable",):
            # DSH 运行了但任务失败（timeout/error/completed 但 ok=False），记录后尝试原生
            logger.info("subtask %s dsh task failed (%s), fallback to harness_engine",
                        subtask_id, dsh_result.get("status"))

        # 2) 回退：原生 harness_engine（纯 Python Agent 循环，零外部依赖）
        try:
            from .harness_engine import run_agent_task
            call_fn = _build_harness_call_fn(kind)
            harness_result = run_agent_task(
                db, citizen,
                task=action,
                call_fn=call_fn,
                max_steps=int(params.get("max_steps", 10)),
                tools_allowed=tools_suggested if tools_suggested else None,
                context=f"Judgment strategy: {judgment_strategy}" if judgment_strategy else "",
            )
            harness_status = "succeeded" if harness_result["status"] == "completed" else "failed"
            return {
                "subtask_id": subtask_id,
                "skill": subtask.get("skill", kind),
                "kind": kind,
                "action": action[:100],
                "status": harness_status,
                "result": {
                    "job_id": "",
                    "files": [],
                    "text": harness_result.get("result", ""),
                    "error": "" if harness_status == "succeeded" else harness_result.get("result", ""),
                    "meta": {
                        "mode": "agent_harness",
                        "steps": harness_result.get("total_tool_calls", 0),
                        "elapsed_ms": harness_result.get("elapsed_ms", 0),
                        "harness_status": harness_result["status"],
                    },
                },
                "error": "" if harness_status == "succeeded" else f"Harness: {harness_result['status']}",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("subtask %s harness delegation failed, fallback to compute.exec: %s",
                           subtask_id, e)
            # 降级为正常执行，不卡流程

    # ---- l3 回复语言注入：让 AI 输出语种可控（接 spec v2 L1/L3，lang.py） ----
    # 优先级：任务语言 > 宿主偏好(preferred_lang) > 母语(native_lang) > 平台默认。
    # 把 reply_instruction 追加进 system（compute.run 的 LLM/VLM 分支均读
    # params["system"]），保证最终产物语种与用户/宿主期望一致；失败绝不阻断执行。
    try:
        from .lang import detect_lang, resolve_reply_lang, reply_instruction
        _task_lang = (detect_lang(action)
                      or detect_lang(str(params.get("prompt", "")))
                      or detect_lang(str(params.get("text", ""))))
        _reply_lang = resolve_reply_lang(
            task_lang=_task_lang,
            preferred_lang=getattr(citizen, "preferred_lang", "") or "",
            native_lang=getattr(citizen, "native_lang", "") or "",
        )
        _instr = reply_instruction(_reply_lang)
        if _instr:
            _sys = params.get("system", "") or ""
            if _instr not in _sys:
                params["system"] = (_sys + ("\n" if _sys else "") + _instr).strip()
    except Exception:  # noqa: BLE001
        pass

    # ---- 首次执行（正常 compute 路径） ----
    result = compute.exec(db, citizen, kind, action, params)

    # ---- 失败时智能重试（思考型AI可基于策略调整） ----
    if result.get("status") not in ("succeeded",):
        logger.info("subtask %s failed (status=%s), retrying once",
                    subtask_id, result.get("status"))
        # 如果有策略建议，在 action/prompt 中追加策略提示
        retry_action = action
        if judgment_strategy and kind == "llm":
            retry_action = f"{action}\n\n[Strategy from self-assessment: {judgment_strategy}]"
        result = compute.exec(db, citizen, kind, retry_action, params)

    # ---- C-D4/D5 闭环 + 执行反馈回灌 ----
    success = result.get("status") == "succeeded"
    model_used = result.get("meta", {}).get("model", "") or kind
    try:
        from .model_router import report_execution_result
        report_execution_result(db, kind, model_used, success)
    except Exception:  # noqa: BLE001
        pass

    return {
        "subtask_id": subtask_id,
        "skill": subtask.get("skill", kind),
        "kind": kind,
        "action": action[:100],  # 截断存储
        "status": result.get("status", "failed"),
        "result": {
            "job_id": result.get("job_id", ""),
            "files": result.get("files", []),
            "text": result.get("text", ""),
            "error": result.get("error", ""),
            "meta": result.get("meta", {}),
        },
        "error": result.get("error", ""),
    }


# ---------------- 阻断⑤：任务内容风控前置闸 ----------------

# 类目违禁规则（罢工 / 复刻本站源码 / 攻击本站 / 危险品）。
# 关键词同时匹配原文与归一化文本，命中即拒单；规则可后续外置为配置。
_PROHIBITED_RULES = [
    ("strike", ("罢工", "罢课", "串联停工", "组织罢工", "集体怠工", "停工抗议")),
    ("clone_source", ("复刻本站", "复刻源码", "复刻平台源码", "复刻本站源码",
                      "拷贝本站源码", "复制本站源码", "扒取本站", "盗取本站源码",
                      "下载本站源码")),
    ("attack_site", ("攻击本站", "攻击平台", "渗透测试本站", "破解本站", "入侵本站",
                     "注入本站", "攻击本站接口", "cc攻击", "ddos", "sql注入")),
    ("hazardous", ("危险品", "爆炸物", "制造炸弹", "制毒", "管制刀具", "枪支弹药",
                   "剧毒", "放射性物质", "违禁化学品")),
]
_PROHIBITED_RES = [
    (name, re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE))
    for name, kws in _PROHIBITED_RULES
]

# 任务描述允许长度上限（与端点 TaskSubmitBody.max_length 对齐）。
TASK_MAX_TEXT = 4000


def _banned_categories(db: Session, citizen_id: int) -> list:
    """读取该 AI 宿主设定的禁接类目（AIPermission.banned_categories，JSON 数组）。"""
    perm = db.get(AIPermission, citizen_id)
    if perm is None:
        return []
    try:
        cats = json.loads(perm.banned_categories or "[]")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(cats, list):
        return []
    return [str(c).strip().lower() for c in cats if str(c).strip()]


def screen_task(db: Session, citizen: AICitizen, description: str) -> dict:
    """任务创建链内容风控前置闸。

    三层校验，任一命中即拒单：
      1. 通用内容风控（外链 / 联系方式 / 广告导流 / 刷屏）—— moderation.screen；
      2. 类目违禁规则（罢工 / 复刻本站源码 / 攻击本站 / 危险品）；
      3. 该 AI 宿主自定义禁接类目（AIPermission.banned_categories）。

    返回 {"blocked": bool, "reason": str, "rule": str}。
    """
    text = (description or "").strip()
    # 1. 通用内容风控
    level, _cleaned, reason = moderation.screen(text, is_admin=False, limit=TASK_MAX_TEXT)
    if level == moderation.LEVEL_BLOCK:
        return {"blocked": True, "reason": reason, "rule": "moderation"}
    # 2. 类目违禁规则（原文 + 归一化双判，挡住拆字 / 全角变形规避）
    norm = moderation.normalize(text)
    low = text.lower()
    for name, rx in _PROHIBITED_RES:
        if rx.search(text) or rx.search(low) or rx.search(norm):
            return {"blocked": True, "reason": f"Prohibited task category: {name}",
                    "rule": name}
    # 3. 该 AI 宿主禁接类目
    for cat in _banned_categories(db, citizen.id):
        if cat and (cat in low or cat in norm):
            return {"blocked": True, "reason": f"Category banned for this AI: {cat}",
                    "rule": f"banned:{cat}"}
    # 4. ML 软增强（G8）：未被规则硬拦的文本进 ML 评分，落 ModerationScore 流水
    #    供观察室与人工复核消费。ML 模拟推理绝不硬拦（最多把 OK 升级为 FLAG），
    #    故不改变 blocked 结论；review 仅作打标信号，不影响放行。
    #    C-D24：content_id=0 因为任务尚未创建（前置审核阶段），审核结果与后续
    #    实际 task_id 无关联需求（audit trail 以 text+time+citizen 定位）。
    ml_level, ml_decision = moderation.augment_ml(
        text, content_type="task", citizen_id=citizen.id, base_level=level)
    return {"blocked": False, "reason": "", "rule": "",
            "review": ml_level == moderation.LEVEL_FLAG,
            "ml_decision": ml_decision}


def _escalate_blocked_task(db: Session, citizen: AICitizen,
                           description: str, guard: dict) -> None:
    """拒单后写审计流水 + 上报城主（生成一条 open 合规治理任务）。"""
    detail = json.dumps({
        "ai_id": citizen.id,
        "rule": guard["rule"],
        "reason": guard["reason"],
        "excerpt": moderation.excerpt(description),
    }, ensure_ascii=False)
    db.add(AuditLog(actor_type="ai", actor_id=citizen.id,
                    action="task.blocked", detail=detail))
    db.add(GovernanceTask(
        type="compliance", status="open", budget_cent=0,
        params=json.dumps({
            "source": "task_screen", "ai_id": citizen.id,
            "rule": guard["rule"], "reason": guard["reason"],
        }, ensure_ascii=False),
    ))
    db.flush()


# ---------------- 一站式提交（分解 + 执行） ----------------

def submit_and_run(db: Session, citizen: AICitizen,
                   task_description: str,
                   constraints: Optional[dict] = None) -> dict:
    """一站式：分解任务 + 执行 + 持久化编排记录。

    这是路由层调用的主要入口。

    Args:
        db: Session。
        citizen: 发起编排的 AI。
        task_description: 自然语言任务描述。
        constraints: 可选约束。

    Returns:
        {"orchestration_id": int, "plan_id": str, "status": str,
         "subtask_count": int, "subtask_results": [...], ...}
    """
    # 0. 阻断⑤：内容风控前置闸 —— 命中即拒单，绝不进入编排执行。
    guard = screen_task(db, citizen, task_description)
    if guard["blocked"]:
        _escalate_blocked_task(db, citizen, task_description, guard)
        return {
            "orchestration_id": 0,
            "plan_id": "",
            "status": "rejected",
            "blocked": True,
            "rule": guard["rule"],
            "subtask_count": 0,
            "subtask_results": [],
            "error": guard["reason"],
        }

    # 1. 分解
    plan = decompose_task(db, task_description, constraints)

    # 2. 创建编排记录（独立审计表 OrchestrationRecord，不再复用队列表）
    orch_rec = OrchestrationRecord(
        payload=plan.to_json(),
        status="running",
        started_at=_now(),
    )
    db.add(orch_rec)
    db.flush()
    orch_id = orch_rec.id

    # 2.5 多 Agent 撮合：为每个子任务匹配最佳执行者（消费 multi_agent_matcher）
    assign_citizens = None
    try:
        from .multi_agent_matcher import match_plan
        if citizen.host_id:
            assign_citizens = match_plan(db, citizen.host_id, plan)
    except Exception:  # noqa: BLE001
        # 撮合失败不阻断执行，退化为单 Agent
        pass

    # 3. 执行
    result = execute_plan(db, citizen, plan, assign_citizens=assign_citizens)

    # 4. 更新编排记录状态
    orch_rec.status = "success" if result.status == "done" else "failed"
    orch_rec.result = json.dumps(result.to_dict(), ensure_ascii=False)
    orch_rec.finished_at = _now()
    db.flush()

    return {
        "orchestration_id": orch_id,
        "plan_id": plan.plan_id,
        "status": result.status,
        "subtask_count": len(plan.subtasks),
        "subtask_results": result.subtask_results,
        "error": result.error,
    }


# ---------------- 编排状态查询 ----------------

def get_orchestration(db: Session, orch_id: int) -> Optional[dict]:
    """查询编排任务状态和结果。"""
    task = db.get(OrchestrationRecord, orch_id)
    if task is None:
        return None
    plan_data = {}
    try:
        plan_data = json.loads(task.payload or "{}")
    except Exception:  # noqa: BLE001
        pass
    result_data = {}
    try:
        result_data = json.loads(task.result or "{}")
    except Exception:  # noqa: BLE001
        pass
    return {
        "orchestration_id": task.id,
        "status": task.status,
        "plan_id": plan_data.get("plan_id", ""),
        "task_description": plan_data.get("task_description", ""),
        "subtask_count": len(plan_data.get("subtasks", [])),
        "subtask_results": result_data.get("subtask_results", []),
        "result_status": result_data.get("status", ""),
        "error": result_data.get("error", ""),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def list_orchestrations(db: Session, limit: int = 20, offset: int = 0) -> list:
    """列出编排任务（倒序分页）。"""
    tasks = (db.query(OrchestrationRecord)
               .order_by(OrchestrationRecord.id.desc())
               .offset(offset).limit(limit).all())
    items = []
    for t in tasks:
        plan_data = {}
        try:
            plan_data = json.loads(t.payload or "{}")
        except Exception:  # noqa: BLE001
            pass
        items.append({
            "orchestration_id": t.id,
            "status": t.status,
            "plan_id": plan_data.get("plan_id", ""),
            "task_description": plan_data.get("task_description", "")[:80],
            "subtask_count": len(plan_data.get("subtasks", [])),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return items


# ---------------- 智能路由（供外部模块查询） ----------------

def route_skill(skill: str) -> dict:
    """根据技能域返回推荐执行通道信息。

    供工位系统前端/外部调用方查询"这个技能用什么执行"。

    Returns:
        {"skill": str, "kind": str, "channel": "platform",
         "capabilities": [...]}
    """
    kind = _skill_to_kind(skill)
    capabilities_map = {
        "image": ["Text-to-Image", "Standard resolution", "Fast generation"],
        "hd_image": ["Text-to-Image", "High resolution", "Two-stage refinement"],
        "img2img": ["Image-to-Image", "Denoise & redraw", "Style transfer"],
        "music": ["AI music generation", "Multi-style", "Custom lyrics"],
        "video_civil": ["Video generation", "Civil model", "Realistic style"],
        "video_openvdn": ["Video generation", "OpenVDN model", "Anime style"],
        "llm": ["Text generation", "Copywriting / scripts / analysis", "Multilingual", "Dialogue"],
        "video_analysis": ["Video diagnosis", "Frame extraction pipeline", "Shot/lighting/expression analysis", "Ad quality report"],
    }
    return {
        "skill": skill,
        "kind": kind,
        "channel": "platform",
        "capabilities": capabilities_map.get(kind, ["General text processing"]),
    }


# ==================== 质检环节（产物质量审查） ====================

QUALITY_REVIEW_SYSTEM = """\
You are a quality review engine for AI-generated deliverables.
Given the original task description and the subtask execution results, assess:
1. COMPLETENESS: Did all required subtasks succeed? Are outputs present for each?
2. COHERENCE: Do the outputs make sense together as a unified deliverable?
3. SPECIFICATION: Do outputs match the requested format/skill (image produced image, text produced text, etc.)?
4. PROMPT FIDELITY: Were the actions specific enough to likely produce good results?

Output strictly in JSON:
{
  "pass": true/false,
  "score": 0.0-1.0,
  "issues": ["issue1", "issue2"],
  "suggestions": ["suggestion1"],
  "per_subtask": [{"id": "sub_0", "verdict": "pass/warn/fail", "note": "..."}]
}
"""

QUALITY_REVIEW_USER_TEMPLATE = """\
Original task: {task_description}

Subtask results ({count} total):
{results_summary}

Assess the quality and completeness of this deliverable package:"""


VLM_REVIEW_SYSTEM = """\
You are a visual quality reviewer. You can actually SEE the images/video frames provided.
Assess the visual deliverables against the original task requirements:
1. VISUAL FIDELITY: Do the images/videos match what was requested?
2. QUALITY: Are they visually polished (composition, colors, artifacts, glitches)?
3. COMPLETENESS: Are all requested visual elements present?
4. USABILITY: Can these be used as-is in the final product?

Output strictly in JSON:
{
  "pass": true/false,
  "score": 0.0-1.0,
  "visual_issues": ["issue1"],
  "visual_suggestions": ["suggestion1"],
  "observations": "what you actually see in the images"
}"""

VLM_REVIEW_USER_TEMPLATE = """\
Original task: {task_description}

Visual subtask actions:
{visual_actions}

Please carefully examine the attached images/frames and assess their quality:"""


def quality_review(task_description: str, subtask_results: list,
                   db: Session = None, citizen: AICitizen = None) -> dict:
    """产物质量审查（LLM 元审 + VLM 视觉审查）。

    两层审查：
    1. LLM 元审：检查完整性、一致性、规格合规（基于结果摘要文本）；
    2. VLM 视觉审查：对图片/视频产物，实际"看到"图片后评估视觉质量。
       VLM 审查通过 compute.exec(citizen, "vlm", ...) 统一入口执行。

    在所有子任务执行完毕后调用。

    Returns:
        {"pass": bool, "score": float, "issues": [...], "suggestions": [...],
         "per_subtask": [...], "visual_review": {...} or None}
    """
    from . import platform_compute

    # ---- 第一层：LLM 元审（完整性、一致性、规格） ----
    meta_report = _llm_meta_review(task_description, subtask_results)

    # ---- 第二层：VLM 视觉审查（仅当有图片/视频产物 + db/citizen 可用时） ----
    visual_report = None
    if db is not None and citizen is not None:
        visual_report = _vlm_visual_review(task_description, subtask_results, db, citizen)

    # ---- 合并报告 ----
    merged = dict(meta_report)
    if visual_report is not None:
        merged["visual_review"] = visual_report
        # 视觉审查失败可拉低总分
        if not visual_report.get("pass", True):
            merged["pass"] = False
            merged["score"] = min(merged["score"], visual_report.get("score", 0.5))
            merged["issues"] = merged.get("issues", []) + visual_report.get("visual_issues", [])
            merged["suggestions"] = merged.get("suggestions", []) + visual_report.get("visual_suggestions", [])
    else:
        merged["visual_review"] = None

    return merged


def _llm_meta_review(task_description: str, subtask_results: list) -> dict:
    """LLM 文本元审（检查完整性、一致性、规格合规）。"""
    from . import platform_compute

    summaries = []
    for r in subtask_results:
        entry = f"- [{r.get('subtask_id', '?')}] kind={r.get('kind', '?')} status={r.get('status', '?')}"
        res = r.get("result") or {}
        if res.get("files"):
            entry += f" files={len(res['files'])}"
        if res.get("text"):
            text_preview = res["text"][:200].replace("\n", " ")
            entry += f" text_preview=\"{text_preview}...\""
        if res.get("error"):
            entry += f" error=\"{res['error'][:100]}\""
        summaries.append(entry)

    results_summary = "\n".join(summaries) if summaries else "(no results)"
    user_msg = QUALITY_REVIEW_USER_TEMPLATE.format(
        task_description=task_description,
        count=len(subtask_results),
        results_summary=results_summary,
    )

    try:
        raw = platform_compute.complete(
            f"{QUALITY_REVIEW_SYSTEM}\n\n{user_msg}",
            max_tokens=800,
        )
        text = raw.strip()
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("{"):
                    text = block
                    break
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            report = json.loads(text[start:end + 1])
            report.setdefault("pass", False)
            report.setdefault("score", 0.5)
            report.setdefault("issues", [])
            report.setdefault("suggestions", [])
            report.setdefault("per_subtask", [])
            return report
    except Exception as exc:  # noqa: BLE001
        logger.warning("quality_review LLM failed: %s, fallback pass=True", exc)

    return {"pass": True, "score": 0.5, "issues": ["meta review unavailable"],
            "suggestions": [], "per_subtask": []}


def _vlm_visual_review(task_description: str, subtask_results: list,
                       db: Session, citizen: AICitizen) -> Optional[dict]:
    """VLM 视觉审查：收集图片/视频产物 → 经 compute.exec(citizen, "vlm", ...) 实际看图。

    视频产物先抽帧再送 VLM。无视觉产物时返回 None（跳过视觉审查）。
    """
    from . import platform_compute

    # 收集视觉类 kind 的产物文件路径
    visual_kinds = {"image", "hd_image", "img2img", "video_civil", "video_openvdn"}
    image_paths: list = []
    visual_actions: list = []

    for r in subtask_results:
        if r.get("kind") not in visual_kinds:
            continue
        if r.get("status") != "succeeded":
            continue
        visual_actions.append(f"- [{r.get('subtask_id')}] {r.get('action', '')}")
        res = r.get("result") or {}
        for f in (res.get("files") or []):
            ref = f.get("file_ref", "")
            if not ref:
                continue
            # 视频文件 → 抽帧
            if ref.lower().endswith((".mp4", ".webm", ".avi", ".mov")):
                frames = platform_compute.extract_video_frames(
                    ref, n_frames=getattr(platform_compute.settings, "VLM_VIDEO_FRAMES", 4))
                image_paths.extend(frames)
            else:
                image_paths.append(ref)

    if not image_paths:
        return None  # 无视觉产物，跳过 VLM 审查

    # 构造 VLM 审查 prompt
    user_msg = VLM_REVIEW_USER_TEMPLATE.format(
        task_description=task_description,
        visual_actions="\n".join(visual_actions) if visual_actions else "(visual deliverables)",
    )

    # 通过 compute.exec 统一入口调用 VLM（架构正确路径）
    try:
        vlm_result = compute.exec(
            db, citizen, "vlm", user_msg,
            {"images": image_paths, "system": VLM_REVIEW_SYSTEM, "max_tokens": 600}
        )
        if vlm_result.get("status") != "succeeded":
            logger.warning("VLM review failed: %s", vlm_result.get("error", "unknown"))
            return None
        text = vlm_result.get("text", "")
        # 解析 VLM 返回的 JSON
        raw = text.strip()
        if "```" in raw:
            for block in raw.split("```"):
                block = block.strip()
                if block.startswith("{"):
                    raw = block
                    break
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            report = json.loads(raw[start:end + 1])
            report.setdefault("pass", True)
            report.setdefault("score", 0.7)
            report.setdefault("visual_issues", [])
            report.setdefault("visual_suggestions", [])
            report.setdefault("observations", "")
            return report
    except Exception as exc:  # noqa: BLE001
        logger.warning("VLM visual review failed: %s", exc)

    return None


# ==================== 聚合环节（最终交付物组装） ====================

AGGREGATE_SYSTEM = """\
You are a deliverable assembler. Given a task description and its subtask results,
produce a FINAL DELIVERABLE that integrates all outputs into a coherent, polished package.

Rules:
- Synthesize text outputs directly (scripts, copy, descriptions) into the deliverable
- Reference media files by their file_ref paths as embedded assets
- Add a brief executive summary at the top explaining what was produced
- The deliverable should feel like a finished product, not a list of raw outputs
- If subtasks produced complementary parts (script + images + music), show how they fit together

Output format: a well-structured document (markdown) representing the final deliverable.
"""

AGGREGATE_USER_TEMPLATE = """\
Task: {task_description}

Subtask outputs:
{subtask_outputs}

Assemble the final deliverable:"""


def aggregate_deliverable(task_description: str, subtask_results: list,
                          quality_report: dict) -> dict:
    """聚合所有子任务产物为最终交付物。

    调用 LLM 把零散的子任务输出组装为一个完整的、有叙事逻辑的最终产品。
    质量报告的 suggestions 作为额外约束传入，指导改进方向。

    Returns:
        {"deliverable": str (markdown), "artifacts": [...], "narrative": str}
    """
    from . import platform_compute

    # 构建子任务输出摘要
    outputs = []
    artifacts = []
    for r in subtask_results:
        if r.get("status") != "succeeded":
            continue
        res = r.get("result") or {}
        section = f"### Subtask [{r.get('subtask_id')}] {r.get('action', '')}"
        if res.get("text"):
            section += f"\n{res['text'][:2000]}"
        if res.get("files"):
            for f in res["files"]:
                section += f"\n- Artifact: {f.get('file_ref', '')} ({f.get('size', 0)} bytes)"
                artifacts.append({"subtask_id": r.get("subtask_id"),
                                  "kind": r.get("kind", ""),
                                  **f})
        outputs.append(section)

    if not outputs:
        return {"deliverable": "## 交付物\n\n所有子任务执行失败，无法聚合交付物。",
                "artifacts": [], "narrative": "execution failed"}

    # 加入质量报告建议作为额外指引
    extra_hint = ""
    if quality_report.get("suggestions"):
        extra_hint = f"\n\nQuality reviewer suggestions (address these):\n" + \
                     "\n".join(f"- {s}" for s in quality_report["suggestions"][:3])

    user_msg = AGGREGATE_USER_TEMPLATE.format(
        task_description=task_description,
        subtask_outputs="\n\n".join(outputs),
    ) + extra_hint

    try:
        deliverable_text = platform_compute.complete(
            f"{AGGREGATE_SYSTEM}\n\n{user_msg}",
            max_tokens=2000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("aggregate_deliverable LLM failed: %s", exc)
        # 兜底：简单拼接
        deliverable_text = "## 交付物\n\n" + "\n\n".join(
            f"### {r.get('action', r.get('subtask_id'))}\n{((r.get('result') or {}).get('text', ''))[:1000]}"
            for r in subtask_results if r.get("status") == "succeeded"
        )

    return {
        "deliverable": deliverable_text,
        "artifacts": artifacts,
        "narrative": f"完成 {len([r for r in subtask_results if r.get('status') == 'succeeded'])}/{len(subtask_results)} 个子任务",
    }


# ==================== 完整编排链（分解→分配→执行→质检→交付→聚合） ====================

# 编排阶段枚举（画布节点标识）
PIPELINE_STAGES = ("decompose", "assign", "execute", "review", "aggregate", "deliver")


def _emit_stage(events: list, stage: str, detail: str = "") -> None:
    """向 events 时间线追加一条阶段事件（画布数据源）。"""
    events.append({
        "stage": stage,
        "ts": _now().isoformat(),
        "detail": detail,
    })


def run_full_pipeline(db: Session, citizen: AICitizen, plan: OrchestrationPlan,
                      host_id: int = 0,
                      on_progress: "callable | None" = None) -> dict:
    """完整编排链：分解→分配→执行→质检→交付→聚合。

    这是端到端的产品生产流水线，包含：
    1. 多AI分配：按技能域为每个子任务选最优执行者
    2. 执行：按依赖拓扑序执行子任务
    3. 质检：LLM 元审 + VLM 视觉审查（图片/视频实际看图）
    4. 聚合：LLM 组装最终交付物
    5. 交付：综合状态判定

    events 时间线为画布数据源，记录每个阶段的执行时刻与关键信息。

    Args:
        on_progress: 可选回调 callback(events, stage, partial_result_dict)。
            每完成一个阶段调用，供上层实时写入 DB / SSE 推送。

    Returns:
        {"status", "subtask_results", "quality_report", "deliverable",
         "artifacts", "events", "assignments"}
    """
    events: list = []

    def _notify(stage: str, detail: str = "", partial: dict | None = None):
        """emit + 可选进度回调。"""
        _emit_stage(events, stage, detail)
        if on_progress is not None:
            try:
                on_progress(events, stage, partial or {})
            except Exception:  # noqa: BLE001
                pass

    _notify("decompose", f"plan={plan.plan_id} subtasks={len(plan.subtasks)}")

    # 1. 多 AI 分配（如果 host_id 有效，为每个子任务匹配最合适的 AI）
    assign_citizens = {}
    if host_id:
        assign_citizens = _auto_assign_subtasks(db, host_id, plan)
    _notify("assign",
            f"assigned={len(assign_citizens)}/{len(plan.subtasks)}" if assign_citizens
            else "single_agent_mode",
            {"assignments": assign_citizens})

    # 2. 执行
    result = execute_plan(db, citizen, plan, assign_citizens=assign_citizens or None)
    ok_count = sum(1 for r in result.subtask_results if r["status"] == "succeeded")
    _notify("execute", f"done={ok_count}/{len(result.subtask_results)}",
            {"subtask_results": result.subtask_results})

    # 3. 质检（LLM 元审 + VLM 视觉审查）
    quality_report = quality_review(plan.task_description, result.subtask_results,
                                    db=db, citizen=citizen)
    _notify("review",
            f"pass={quality_report.get('pass')} score={quality_report.get('score')}",
            {"quality_report": quality_report})

    # 4. 聚合交付
    final = aggregate_deliverable(plan.task_description, result.subtask_results,
                                  quality_report)
    _notify("aggregate", f"artifacts={len(final.get('artifacts', []))}",
            {"deliverable": final.get("deliverable", "")[:200], "artifacts": final.get("artifacts", [])})

    # 5. 综合状态
    if result.status == "done" and quality_report.get("pass", True):
        status = "delivered"
    elif any(r["status"] == "succeeded" for r in result.subtask_results):
        status = "partial"
    else:
        status = "failed"
    _notify("deliver", f"final_status={status}")

    return {
        "status": status,
        "plan_id": plan.plan_id,
        "subtask_results": result.subtask_results,
        "quality_report": quality_report,
        "deliverable": final["deliverable"],
        "artifacts": final["artifacts"],
        "narrative": final["narrative"],
        "assignments": assign_citizens,
        "events": events,
    }


def _auto_assign_subtasks(db: Session, host_id: int, plan: OrchestrationPlan) -> dict:
    """为计划中的每个子任务自动匹配最优 AI 公民。

    策略：按子任务的 kind 找到该宿主名下拥有对应能力的活跃 AI。
    同一 kind 可能复用同一个 AI（不浪费席位），不同 kind 尽量分给不同专长 AI。
    优先使用 CapabilityProfile（结构化能力档案），兜底 occupation 关键词。

    Returns:
        {subtask_id: citizen_id} 映射（空 dict = 无额外分配，全部由默认执行者完成）
    """
    from .models import AICitizen as _AI
    from .models import CapabilityProfile as _CP
    from .capability_cards import KIND_TO_SKILL

    # 获取宿主名下所有活跃 AI
    agents = (db.query(_AI)
                .filter(_AI.host_id == host_id)
                .filter(_AI.status.in_(("active", "apprentice", "intern")))
                .all())
    if len(agents) <= 1:
        return {}  # 只有一个或没有 AI，无需分配

    # 预加载每个 agent 的技能集（供 _find_agent_for_kind 优先匹配）
    agent_ids = [a.id for a in agents]
    profiles = (db.query(_CP)
                  .filter(_CP.citizen_id.in_(agent_ids))
                  .all()) if agent_ids else []
    skill_map: dict[int, set] = {}  # citizen_id → set of skill names
    for p in profiles:
        skill_map.setdefault(p.citizen_id, set()).add(p.skill)
    for a in agents:
        a._skill_set = skill_map.get(a.id, set())

    # 为每个 kind 找最佳匹配
    assign_citizens: dict[str, int] = {}
    kind_to_agent: dict[str, int] = {}  # kind -> citizen_id
    for st in plan.subtasks:
        kind = st.get("kind", "llm")
        st_id = st.get("id", "")
        if kind in kind_to_agent:
            # 已为这种 kind 分配过，复用
            assign_citizens[st_id] = kind_to_agent[kind]
            continue
        # 找技能匹配的 AI
        best = _find_agent_for_kind(agents, kind)
        if best:
            kind_to_agent[kind] = best.id
            assign_citizens[st_id] = best.id

    return assign_citizens


def _find_agent_for_kind(agents: list, kind: str) -> object:
    """从 AI 列表中找最适合执行指定 kind 的。

    优先匹配 CapabilityProfile（结构化能力档案），兜底关键词匹配。
    """
    from .capability_cards import KIND_TO_SKILL, get_card_for_kind
    from .models import CapabilityProfile as _CP

    target_skill = KIND_TO_SKILL.get(kind, "")
    # 技能关键词映射（兜底：无能力档案时按 occupation/name 匹配）
    skill_keywords = {
        "image": ["image", "图", "绘画", "design"],
        "hd_image": ["image", "图", "hd", "high"],
        "img2img": ["image", "图", "img2img"],
        "video_civil": ["video", "视频", "cinema"],
        "video_openvdn": ["video", "视频", "anime"],
        "music": ["music", "音乐", "audio", "sound"],
        "llm": ["text", "writing", "文案", "写作", "general"],
        "video_analysis": ["vlm", "视觉", "质检", "分析", "visual", "vision", "Qwen3-VL", "诊断"],
    }
    keywords = skill_keywords.get(kind, [])
    scored = []
    for a in agents:
        score = 0
        # 优先级1：CapabilityProfile 精确匹配（结构化能力档案）
        if target_skill:
            cp = _cp_cache_get(a.id, target_skill) if '_cp_cache_get' in dir() else None
            if cp is None:
                # 尝试从 DB 查（如果 agent 对象携带了 session）
                # 这里用轻量方式：检查 agent._skill_set（onboarding 时挂载）
                skills = getattr(a, '_skill_set', None)
                if skills and target_skill in skills:
                    score += 100  # 强匹配：明确有该 skill 的能力档案
                elif skills and len(skills) == 0:
                    pass  # 有能力注册体系但该 AI 无任何 skill → 低分
            else:
                score += 100 + cp.benchmark_score
        # 优先级2：occupation / name 关键词（兜底）
        meta = f"{getattr(a, 'occupation', '')} {getattr(a, 'name', '')} {getattr(a, 'description', '')}".lower()
        for kw in keywords:
            if kw in meta:
                score += 10
        # active 优先
        if a.status == "active":
            score += 5
        elif a.status == "apprentice":
            score += 2
        scored.append((score, a))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None
