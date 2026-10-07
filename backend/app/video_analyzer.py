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
"""Qwen3-VL 视频诊断分析 —— AI 3 自主工作模式。

设计哲学：
  AI 3 是员工，不是流水线上的被动零件。
  它自己决定怎么分析（策略）、自己写最终诊断结论（综合）。
  ffmpeg 抽帧等机械操作是"工具箱"，如同工人使用工具——工人做决策，工具做苦力。

AI 3 的自主决策环节：
  1. 【策略决策】拿到视频后，AI 3 自己决定 fps、分片长度、分析重点
  2. 【逐段视觉分析】不可避免（必须"看"画面），但由 AI 3 自己执行
  3. 【综合诊断】AI 3 阅读所有片段结果，用自己的智能写出最终报告

与旧版的根本区别：
  - 旧版：Python 硬编码所有参数 → 被动喂图 → Python 字符串拼接报告
  - 新版：AI 3 决策策略 → AI 3 看片段 → AI 3 写诊断（真正的思考输出）
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .capability_cards import CAPABILITY_CARDS

logger = logging.getLogger(__name__)

# ==================== 配置常量（安全边界，不是决策参数） ====================
#
# 唯一真源 = capability_cards.CAPABILITY_CARDS["video_analysis"].limits。
# 本模块不自造边界——边界是"硬件能力"的一部分，由能力卡片统一声明，
# 避免与编排层/能力卡片出现三套互相打架的范围。

def _va_limits() -> dict:
    lim = (CAPABILITY_CARDS.get("video_analysis") or {}).get("limits", {})
    fps = lim.get("fps", {})
    chunk = lim.get("chunk_window_sec", {})
    overlap = lim.get("chunk_overlap_sec", {})
    return {
        "fps": (fps.get("min", 2), fps.get("max", 5), fps.get("default", 3)),
        "chunk": (chunk.get("min", 12), chunk.get("max", 25), chunk.get("default", 18)),
        "overlap": (overlap.get("min", 2), overlap.get("max", 6), overlap.get("default", 4)),
        "max_duration": lim.get("input_video", {}).get("max_duration_sec", 600),
        "max_frames": lim.get("max_frames_per_chunk", {}).get("default", 60),
    }


_LIM = _va_limits()

DEFAULT_FPS = _LIM["fps"][2]              # 兜底默认（AI 3 不做决策时）
DEFAULT_CHUNK_SEC = _LIM["chunk"][2]
DEFAULT_OVERLAP_SEC = _LIM["overlap"][2]
DEFAULT_FRAME_SIZE = 512
MIN_FRAME_SIZE = 128          # 安全下限：压缩目标过小则画质不可辨
MAX_FRAME_SIZE = 2048         # 安全上限：控制送入 VLM 的图像 token
MAX_FRAMES_PER_CHUNK = _LIM["max_frames"]   # 硬上限：VLM context window
MAX_INPUT_DURATION = _LIM["max_duration"]   # 硬上限：超时保护


# ==================== 主入口 ====================

def analyze_video(video_path: str, *,
                  fps: int = DEFAULT_FPS,
                  chunk_sec: int = DEFAULT_CHUNK_SEC,
                  overlap_sec: int = DEFAULT_OVERLAP_SEC,
                  frame_size: int = DEFAULT_FRAME_SIZE,
                  vlm_call=None,
                  focus: str = "advertising",
                  task_description: str = "",
                  prior_strategy: Optional[dict] = None) -> dict:
    """AI 3 自主视频分析流水线。

    Args:
        video_path: 视频文件路径
        fps: 兜底帧率（会被 AI 3 的策略覆盖）
        chunk_sec: 兜底分片时长
        overlap_sec: 兜底重叠时长
        frame_size: 帧压缩目标
        vlm_call: VLM 调用函数 (prompt, images, system, max_tokens) -> str
        focus: 分析焦点提示
        task_description: 任务描述（AI 3 用来理解任务意图）
        prior_strategy: 已由工作前判断（同一 AI）定好的策略；非空则跳过重复的
            策略 VLM 调用（judg_dedup）。字段缺失项回退兜底默认。

    Returns:
        完整诊断报告 dict
    """
    from . import platform_compute

    if vlm_call is None:
        vlm_call = platform_compute.vlm_complete

    # 0. 入参钳位到能力卡片硬边界（安全边界，不是决策——决策在 _decide_strategy）
    fps = max(_LIM["fps"][0], min(_LIM["fps"][1], int(fps)))
    chunk_sec = max(_LIM["chunk"][0], min(_LIM["chunk"][1], int(chunk_sec)))
    overlap_sec = max(_LIM["overlap"][0], min(_LIM["overlap"][1], int(overlap_sec)))
    frame_size = max(MIN_FRAME_SIZE, min(MAX_FRAME_SIZE, int(frame_size)))

    # 1. 解析视频路径
    vp = _resolve_video_path(video_path)
    if not vp:
        return _error_report(f"视频文件不存在: {video_path}")

    # 2. 获取视频基本信息
    duration = _get_duration(vp)
    if duration <= 0:
        return _error_report("无法获取视频时长")
    if duration > MAX_INPUT_DURATION:
        return _error_report(
            f"视频时长 {duration:.0f}s 超出上限 {MAX_INPUT_DURATION}s，请先裁剪")

    # 3. 【AI 3 策略决策】—— 真正的自主判断。
    #    judg_dedup：若上游工作前判断（同一个 AI）已给出策略，直接复用，不再问一次。
    if _has_prior_strategy(prior_strategy):
        strategy = _coerce_prior_strategy(prior_strategy, focus,
                                          fallback_fps=fps, fallback_chunk=chunk_sec,
                                          fallback_overlap=overlap_sec)
    else:
        strategy = _decide_strategy(
            vlm_call, duration=duration, video_name=vp.name,
            task_description=task_description, focus=focus,
            fallback_fps=fps, fallback_chunk=chunk_sec, fallback_overlap=overlap_sec,
        )
    fps = strategy["fps"]
    chunk_sec = strategy["chunk_sec"]
    overlap_sec = strategy["overlap_sec"]
    analysis_focus = strategy.get("analysis_focus", focus)

    logger.info("AI 3 strategy: fps=%d chunk=%ds overlap=%ds focus=%s reasoning=%s",
                fps, chunk_sec, overlap_sec, analysis_focus,
                strategy.get("reasoning", "")[:80])

    # 4. 抽帧 + 压缩（机械工具步骤）
    work_dir = Path(tempfile.mkdtemp(prefix="vidanalysis_"))
    try:
        frames = _extract_frames(vp, work_dir, fps=fps, frame_size=frame_size)
        if not frames:
            return _error_report("ffmpeg抽帧失败（检查ffmpeg是否可用）")

        # 5. 分片
        chunks = _sliding_window_chunks(frames, fps, chunk_sec, overlap_sec)
        logger.info("AI 3: %d frames -> %d chunks", len(frames), len(chunks))

        # 6. 逐分片视觉分析（AI 3 执行"看"的动作）
        chunk_results = []
        for i, chunk in enumerate(chunks):
            result = _analyze_chunk(chunk, i, len(chunks), vlm_call, analysis_focus)
            if result:
                chunk_results.append(result)
            logger.info("AI 3: chunk %d/%d done", i + 1, len(chunks))

        if not chunk_results:
            return _error_report("所有分片分析均失败，无法生成报告")

        # 7. 【AI 3 综合诊断】—— 用自己的智能写报告，不是字符串拼接
        report = _synthesize_report(
            vlm_call, chunk_results=chunk_results,
            duration=duration, video_name=vp.name,
            task_description=task_description,
            strategy=strategy,
        )
        return report

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ==================== AI 3 自主决策：分析策略 ====================

_STRATEGY_SYSTEM_TMPL = """\
You are an AI video analyst (AI 3) about to analyze a video. Before you start \
extracting frames, you must decide your analysis strategy like a professional \
video analyst would. You have full autonomy to choose parameters based on the \
task requirements and video characteristics.

Consider:
- What kind of video is this? (fast cuts need higher fps, slow content can use lower)
- What is the task asking for? (quality check vs. content summary vs. ad diagnosis)
- How long is it? (short videos can use finer granularity, long ones need bigger windows)
- What should I focus on? (lighting, pacing, content accuracy, technical quality?)

Respond ONLY with JSON:
{{"fps": int, "chunk_sec": int, "overlap_sec": int, "analysis_focus": "what to look for", "reasoning": "why this strategy"}}

Constraints (hard limits of the extraction hardware — do not exceed):
- fps: {fps_min}-{fps_max} (higher = more detail but more API calls)
- chunk_sec: {chunk_min}-{chunk_max} seconds per analysis window
- overlap_sec: {overlap_min}-{overlap_max} seconds overlap between windows
"""


def _strategy_system() -> str:
    """构建策略决策系统提示（边界取自能力卡片真源）。"""
    return _STRATEGY_SYSTEM_TMPL.format(
        fps_min=_LIM["fps"][0], fps_max=_LIM["fps"][1],
        chunk_min=_LIM["chunk"][0], chunk_max=_LIM["chunk"][1],
        overlap_min=_LIM["overlap"][0], overlap_max=_LIM["overlap"][1],
    )


def _decide_strategy(vlm_call, *, duration: float, video_name: str,
                     task_description: str, focus: str,
                     fallback_fps: int, fallback_chunk: int,
                     fallback_overlap: int) -> dict:
    """AI 3 自己决定分析策略。判断失败则降级到默认参数。"""
    prompt = (
        f"Video: {video_name}\n"
        f"Duration: {duration:.1f} seconds\n"
        f"Task: {task_description or 'General video analysis'}\n"
        f"General focus hint: {focus}\n\n"
        f"Decide your analysis strategy. What fps, chunk window size, overlap, "
        f"and specific focus will you use for this particular video and task? Explain why."
    )
    try:
        raw = vlm_call(prompt, images=None, system=_strategy_system(), max_tokens=300)
        if not raw or "[[MOCK" in raw:
            return _default_strategy(fallback_fps, fallback_chunk, fallback_overlap)

        text = raw.strip()
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("{"):
                    text = block
                    break
        start_i = text.find("{")
        end_i = text.rfind("}")
        if start_i == -1 or end_i <= start_i:
            return _default_strategy(fallback_fps, fallback_chunk, fallback_overlap)

        obj = json.loads(text[start_i:end_i + 1])
        # 钳位到能力卡片声明的硬边界
        return {
            "fps": max(_LIM["fps"][0], min(_LIM["fps"][1], int(obj.get("fps", fallback_fps)))),
            "chunk_sec": max(_LIM["chunk"][0], min(_LIM["chunk"][1], int(obj.get("chunk_sec", fallback_chunk)))),
            "overlap_sec": max(_LIM["overlap"][0], min(_LIM["overlap"][1], int(obj.get("overlap_sec", fallback_overlap)))),
            "analysis_focus": str(obj.get("analysis_focus", focus))[:200],
            "reasoning": str(obj.get("reasoning", ""))[:300],
        }
    except Exception as e:
        logger.warning("AI 3 strategy decision failed, using defaults: %s", e)
        return _default_strategy(fallback_fps, fallback_chunk, fallback_overlap)


def _has_prior_strategy(prior: Optional[dict]) -> bool:
    """上游判断是否已给出可用的视频策略（judg_dedup 用）。"""
    if not isinstance(prior, dict):
        return False
    return any(prior.get(k) is not None
               for k in ("fps", "chunk_sec", "overlap_sec", "analysis_focus"))


def _coerce_prior_strategy(prior: dict, focus: str, *,
                           fallback_fps: int, fallback_chunk: int,
                           fallback_overlap: int) -> dict:
    """把上游判断给出的策略钳位到能力卡片硬边界（缺失项用兜底默认）。"""
    def _as_int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return {
        "fps": max(_LIM["fps"][0], min(_LIM["fps"][1],
                                       _as_int(prior.get("fps"), fallback_fps))),
        "chunk_sec": max(_LIM["chunk"][0], min(_LIM["chunk"][1],
                                               _as_int(prior.get("chunk_sec"), fallback_chunk))),
        "overlap_sec": max(_LIM["overlap"][0], min(_LIM["overlap"][1],
                                                   _as_int(prior.get("overlap_sec"), fallback_overlap))),
        "analysis_focus": str(prior.get("analysis_focus") or focus)[:200],
        "reasoning": str(prior.get("reasoning", "reused pre-work judgment strategy"))[:300],
    }


def _default_strategy(fps, chunk, overlap) -> dict:
    return {
        "fps": fps, "chunk_sec": chunk, "overlap_sec": overlap,
        "analysis_focus": "general", "reasoning": "AI strategy call failed, using defaults",
    }


# ==================== 工具层：视频路径解析 ====================

def _resolve_video_path(video_path: str) -> Optional[Path]:
    """解析视频路径，支持绝对路径、相对路径、data dir 相对路径。"""
    from .database import DATA_DIR
    from .platform_compute import _MOCK_OUT_DIR

    p = Path(video_path)
    if p.is_file():
        return p
    p2 = _MOCK_OUT_DIR / video_path
    if p2.is_file():
        return p2
    p3 = DATA_DIR / video_path
    if p3.is_file():
        return p3
    return None


def _get_duration(video_path: Path, *, db=None, citizen_id: int = 0) -> float:
    """获取视频时长（秒）。优先走 tool_registry，回退直接 ffprobe。"""
    # 优先走技能库（留痕、统一调度）
    if db is not None:
        try:
            from . import tool_registry
            res = tool_registry.execute(
                db, citizen_id, "media.ffmpeg",
                {"operation": "get_duration", "input_path": str(video_path)},
                note="video_analyzer: get duration",
            )
            if res.get("ok"):
                return float(res.get("duration_sec", 0.0))
        except Exception:
            pass  # 回退
    # 回退：直接 ffprobe
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except Exception:
        return 0.0


# ==================== 工具层：抽帧 + 压缩 ====================

def _extract_frames(video_path: Path, work_dir: Path,
                    fps: int = DEFAULT_FPS,
                    frame_size: int = DEFAULT_FRAME_SIZE,
                    *, db=None, citizen_id: int = 0) -> list[dict]:
    """ffmpeg 抽帧 + 压缩。优先走 tool_registry，回退直接 subprocess。"""
    out_dir = work_dir / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 优先走技能库
    if db is not None:
        try:
            from . import tool_registry
            resolution = f"{frame_size}x{frame_size}"
            res = tool_registry.execute(
                db, citizen_id, "media.ffmpeg",
                {"operation": "extract_frames", "input_path": str(video_path),
                 "output_dir": str(out_dir), "fps": fps, "resolution": resolution},
                note=f"video_analyzer: extract {fps}fps frames",
            )
            if res.get("ok") and res.get("frames"):
                frames = []
                for i, fp in enumerate(res["frames"]):
                    ts = i / fps
                    frames.append({"path": fp, "timestamp_sec": round(ts, 2)})
                logger.info("_extract_frames(registry): got %d frames", len(frames))
                return frames
        except Exception:
            pass  # 回退

    # 回退：直接 subprocess
    pattern = str(out_dir / "f_%05d.jpg")
    vf = (f"fps={fps},"
          f"scale='if(gt(iw,ih),{frame_size},-2)':'if(gt(iw,ih),-2,{frame_size})'")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-vf", vf, "-q:v", "3", pattern],
            capture_output=True, timeout=120,
        )
    except FileNotFoundError:
        logger.error("ffmpeg not found")
        return []
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timeout")
        return []

    frame_files = sorted(out_dir.glob("f_*.jpg"))
    frames = []
    for i, f in enumerate(frame_files):
        ts = i / fps
        frames.append({"path": str(f), "timestamp_sec": round(ts, 2)})

    logger.info("_extract_frames: got %d frames from %s", len(frames), video_path.name)
    return frames


# ==================== 工具层：分片 ====================

def _sliding_window_chunks(frames: list[dict], fps: int,
                           chunk_sec: int, overlap_sec: int) -> list[dict]:
    """将帧列表按滑窗切分（纯机械切分，参数由 AI 3 决定）。"""
    if not frames:
        return []

    total_dur = frames[-1]["timestamp_sec"] + 1.0 / fps
    step_sec = chunk_sec - overlap_sec
    chunks = []
    start = 0.0
    idx = 0

    while start < total_dur:
        end = start + chunk_sec
        chunk_frames = [f for f in frames if start <= f["timestamp_sec"] < end]

        if chunk_frames:
            if len(chunk_frames) > MAX_FRAMES_PER_CHUNK:
                ratio = MAX_FRAMES_PER_CHUNK / len(chunk_frames)
                step = max(1, int(1 / ratio))
                chunk_frames = chunk_frames[::step]

            chunks.append({
                "frames": chunk_frames,
                "start_sec": round(start, 1),
                "end_sec": round(min(end, total_dur), 1),
                "chunk_idx": idx,
            })

        start += step_sec
        idx += 1
        if end >= total_dur:
            break

    return chunks


# ==================== AI 3 执行：逐段视觉分析 ====================

CHUNK_SYSTEM_AD = """\
You are an AI video analyst examining a segment of a video. You receive frames \
in chronological order with timestamps. Analyze what you see and output STRICT JSON:
{
  "segment": {"start": "MM:SS", "end": "MM:SS"},
  "shots": [
    {"time": "MM:SS", "shot_type": "...", "lighting": "...", "expression": "...", "action": "...", "rhythm": "..."}
  ],
  "issues": ["problems found"],
  "highlights": ["what works well"],
  "overall": "one-sentence verdict on this segment"
}
"""

CHUNK_SYSTEM_GENERAL = """\
You are an AI video analyst examining a segment. Output STRICT JSON:
{
  "segment": {"start": "MM:SS", "end": "MM:SS"},
  "shots": [{"time": "MM:SS", "shot_type": "...", "content": "..."}],
  "observations": ["key observations"],
  "overall": "one-sentence summary"
}
"""


def _analyze_chunk(chunk: dict, idx: int, total: int,
                   vlm_call, focus: str) -> Optional[dict]:
    """AI 3 分析单个分片（这是 AI 3 的'眼睛'在工作）。"""
    frames = chunk["frames"]
    start_s = chunk["start_sec"]
    end_s = chunk["end_sec"]

    time_labels = ", ".join(
        f"[{f['timestamp_sec']:.1f}s]" for f in frames[:10]
    )
    if len(frames) > 10:
        time_labels += f" ... ({len(frames)} frames total)"

    prompt = (
        f"Segment: {start_s:.1f}s to {end_s:.1f}s ({len(frames)} frames).\n"
        f"Frame timestamps: {time_labels}\n"
        f"Your analysis focus: {focus}\n\n"
        f"Analyze these frames and output JSON:"
    )

    system = CHUNK_SYSTEM_AD if "ad" in focus or "广告" in focus else CHUNK_SYSTEM_GENERAL
    image_paths = [f["path"] for f in frames]

    try:
        raw = vlm_call(prompt, images=image_paths, system=system, max_tokens=1500)
        if not raw or "[[MOCK" in raw:
            return None

        text = raw.strip()
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("{"):
                    text = block
                    break
        start_i = text.find("{")
        end_i = text.rfind("}")
        if start_i == -1 or end_i <= start_i:
            logger.warning("chunk %d: VLM response not JSON: %.100s", idx, raw)
            return None

        parsed = json.loads(text[start_i:end_i + 1])
        parsed["_actual_start_sec"] = start_s
        parsed["_actual_end_sec"] = end_s
        parsed["_chunk_idx"] = idx
        return parsed

    except json.JSONDecodeError as e:
        logger.warning("chunk %d JSON parse error: %s", idx, e)
        return None
    except Exception as e:
        logger.warning("chunk %d analysis failed: %s", idx, e)
        return None


# ==================== AI 3 自主思考：综合诊断报告 ====================

_SYNTHESIS_SYSTEM = """\
You are the AI video analyst (AI 3) who has just finished examining all segments \
of a video. You are now writing your FINAL DIAGNOSTIC REPORT using your own \
professional judgment.

You have received segment-by-segment analysis notes from your own visual inspection. \
Now you must:
1. Synthesize: What is the overall picture? (not just concatenation — your OWN interpretation)
2. Prioritize: Which issues matter MOST? Why? (rank them by severity and impact)
3. Diagnose: What are ROOT CAUSES? (not just symptoms — what production/process issue caused them?)
4. Recommend: What specific changes would fix the most important problems?
5. Verdict: Overall assessment — is this video fit for its purpose? What's the biggest risk?

Write a comprehensive, professional diagnostic report. Use your own intelligence and judgment. \
Do not just list bullet points — write coherent analytical paragraphs that show YOUR thinking.

Output JSON:
{
  "status": "completed",
  "video": "<name>",
  "duration_sec": <number>,
  "overall_verdict": "one paragraph overall assessment",
  "diagnosis": {
    "critical_issues": [{"issue": "...", "time_range": "...", "root_cause": "...", "severity": "high|medium|low"}],
    "improvements": [{"area": "...", "current": "...", "recommendation": "..."}],
    "strengths": ["what works well"]
  },
  "segment_notes": [{"time_range": "...", "note": "brief per-segment note"}],
  "production_recommendations": ["specific actionable changes"]
}
"""


def _synthesize_report(vlm_call, *, chunk_results: list[dict],
                       duration: float, video_name: str,
                       task_description: str, strategy: dict) -> dict:
    """AI 3 自己写综合诊断报告（不是 Python 拼接，是真正的智能输出）。"""
    # 准备给 AI 3 看的片段摘要
    segment_notes_text = []
    for cr in chunk_results:
        start = cr.get("_actual_start_sec", 0)
        end = cr.get("_actual_end_sec", 0)
        overall = cr.get("overall", "no summary")
        issues = cr.get("issues", [])
        highlights = cr.get("highlights", [])
        line = f"[{start:.0f}s-{end:.0f}s] {overall}"
        if issues:
            line += f" | Issues: {'; '.join(issues[:3])}"
        if highlights:
            line += f" | Good: {'; '.join(highlights[:2])}"
        segment_notes_text.append(line)

    synthesis_prompt = (
        f"You analyzed the video '{video_name}' ({duration:.1f}s total).\n"
        f"Your analysis focus was: {strategy.get('analysis_focus', 'general')}\n"
        f"Your strategy: fps={strategy.get('fps')}, chunks at {strategy.get('chunk_sec')}s intervals\n"
        f"Original task: {task_description or 'General video analysis'}\n\n"
        f"YOUR OWN SEGMENT NOTES (from your visual inspection):\n"
        + "\n".join(segment_notes_text) + "\n\n"
        f"Now write your comprehensive diagnostic report. "
        f"Use your judgment to prioritize, diagnose root causes, and recommend fixes. "
        f"This is YOUR professional assessment, not a data dump."
    )

    try:
        raw = vlm_call(synthesis_prompt, images=None, system=_SYNTHESIS_SYSTEM, max_tokens=2000)
        if not raw or "[[MOCK" in raw:
            # 降级：用基本结构返回（比纯字符串拼接好，至少包含 AI 的片段观察）
            return _fallback_report(chunk_results, duration, video_name, strategy)

        text = raw.strip()
        if "```" in text:
            for block in text.split("```"):
                block = block.strip()
                if block.startswith("{"):
                    text = block
                    break
        start_i = text.find("{")
        end_i = text.rfind("}")
        if start_i == -1 or end_i <= start_i:
            return _fallback_report(chunk_results, duration, video_name, strategy)

        report = json.loads(text[start_i:end_i + 1])
        # 补充元数据
        report.setdefault("video", video_name)
        report.setdefault("duration_sec", round(duration, 1))
        report["analysis_strategy"] = strategy
        report["status"] = "completed"
        return report

    except Exception as e:
        logger.warning("AI 3 synthesis failed: %s", e)
        return _fallback_report(chunk_results, duration, video_name, strategy)


def _fallback_report(chunk_results, duration, video_name, strategy) -> dict:
    """降级报告（AI 3 综合失败时，仍保留其片段观察结果）。"""
    segments = []
    all_issues = []
    for cr in chunk_results:
        segments.append({
            "time_range": f"{cr.get('_actual_start_sec', 0):.0f}s-{cr.get('_actual_end_sec', 0):.0f}s",
            "note": cr.get("overall", ""),
        })
        all_issues.extend(cr.get("issues", []))

    return {
        "status": "completed",
        "video": video_name,
        "duration_sec": round(duration, 1),
        "overall_verdict": "AI synthesis call failed; segment observations preserved below.",
        "diagnosis": {
            "critical_issues": [{"issue": i, "severity": "unknown"} for i in all_issues[:5]],
            "improvements": [],
            "strengths": [],
        },
        "segment_notes": segments,
        "production_recommendations": [],
        "analysis_strategy": strategy,
    }


# ==================== 错误报告 ====================

def _error_report(msg: str) -> dict:
    return {
        "status": "error",
        "error": msg,
        "video": "",
        "duration_sec": 0,
        "overall_verdict": msg,
        "diagnosis": {"critical_issues": [], "improvements": [], "strengths": []},
        "segment_notes": [],
        "production_recommendations": [],
    }
