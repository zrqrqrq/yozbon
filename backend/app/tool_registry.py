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
"""技能库 / 插件中心（Tool/Skill Registry，2026-10-06）。

定位：站内任何 AI 执行任务时**默认先查技能库 → 判断是否调用 → 用/不用均留痕**。
本模块是"工具目录 + 调用执行 + 留痕 + 发现指令"的真源，与既有
`capability.py`（AI 自身能力树）、`skill_compose.py`（按技能名的 DAG 编排）、
`model_router.py`（生成类模型通道选择）互补——这里是可被任意 AI 发现并调用的
**外部工具/插件**目录（联网检索 / 浏览器操作 / 代码执行 / 法律 / 生成）。

设计要点：
- 目录以 DB 镜像落地（`tool_plugins` 表），启动 `bootstrap()` 按代码种子幂等 upsert；
  `tool_scout` 长期岗位采集的新工具以 `source=scout / status=pending` 提案进入同一目录。
- 免密钥工具（web.search / web.wikipedia / browser.navigate / code.run）真实接通；
  需密钥工具（legal.search 等）本期仅"可发现、不可真调"，执行返回 `requires_key` 提示。
- 每次调用（含纯决策记录）落 `tool_calls`，实现"用/不用都留痕"，并按 AI 每分钟限流。
- 出站 HTTP 的唯一接缝是 `_http_get_json`；测试可 monkeypatch 它保证离线确定性。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .models import PromptLibrary, ToolCall, ToolPlugin

logger = logging.getLogger(__name__)

_NOW = datetime.utcnow


# ---------------------------------------------------------------------------
# 内置种子目录（代码为真源；DB 为可发现镜像）
# ---------------------------------------------------------------------------
def SEED_TOOLS() -> list:
    """内置工具种子。available/enabled 的运行时真值由 refresh_availability 依据开关刷新。"""
    return [
        {"tool_key": "web.search", "name": "联网检索（DuckDuckGo）",
         "category": "search", "requires_key": 0,
         "description": "免密钥网页检索。输入查询词，返回相关网页摘要，适合事实核查、资料搜集。",
         "io_schema": {"query": "string，检索关键词（必填）", "max_results": "int，返回条数（默认5，上限10）"}},
        {"tool_key": "web.wikipedia", "name": "知识检索（Wikipedia）",
         "category": "search", "requires_key": 0,
         "description": "免密钥维基百科 opensearch。输入词条，返回摘要与链接，适合概念/实体释义。",
         "io_schema": {"query": "string，词条（必填）", "lang": "string，语言站点（默认 zh）"}},
        {"tool_key": "browser.navigate", "name": "浏览器操作（Playwright）",
         "category": "browser", "requires_key": 0,
         "description": "无头浏览器打开网页并抽取标题/正文文本。需服务器已装 playwright，运行时探测可用性。",
         "io_schema": {"url": "string，目标网址（必填）", "extract": "string，抽取范围 title/text（默认 title）"}},
        {"tool_key": "code.run", "name": "代码执行（受限沙箱）",
         "category": "code", "requires_key": 0,
         "description": "受限子进程执行短代码片段并回传 stdout。默认关闭，需 TOOL_CODE_ENABLED 显式开启。",
         "io_schema": {"language": "string，python（默认）", "code": "string，待执行代码（必填）"}},
        {"tool_key": "legal.search", "name": "法律检索（法宝/企查查）",
         "category": "legal", "requires_key": 1,
         "description": "法条/判例/企业知识产权检索。依赖外部密钥（北大法宝、企查查），本期仅可发现、暂不真调。",
         "io_schema": {"query": "string，检索内容（必填）", "scope": "string，law/case/ipr/company"}},
        {"tool_key": "media.ffmpeg", "name": "媒体处理（FFmpeg）",
         "category": "media", "requires_key": 0,
         "description": "视频/音频处理万能工具。支持：extract_frames（抽帧）、get_duration（获取时长）、"
                        "transcode（转码）、extract_audio（提取音频）、trim（裁剪片段）。"
                        "AI 需要处理视频/音频文件时应优先使用本工具，无需编写代码。",
         "io_schema": {"operation": "string，操作类型（必填：extract_frames/get_duration/transcode/extract_audio/trim）",
                       "input_path": "string，输入文件绝对路径（必填）",
                       "output_dir": "string，输出目录（可选，默认临时目录）",
                       "fps": "int，帧率（extract_frames用，默认2）",
                       "start": "float，起始秒（trim/transcode用，默认0）",
                       "duration": "float，持续秒数（trim用，默认全片）",
                       "codec": "string，编码（transcode用，默认libx264）",
                       "resolution": "string，分辨率（如512x512，extract_frames缩放用）"}},
        {"tool_key": "agent.harness", "name": "自主Agent循环（原生Python）",
         "category": "agent", "requires_key": 0,
         "description": "复杂多步任务委托给Agent循环执行：AI自主规划步骤→调用工具→观察结果→继续推理→直到完成。"
                        "适合需要多轮工具调用的复杂任务。简单单步任务不需要调用本工具。",
         "io_schema": {"task": "string，任务描述（必填）",
                       "max_steps": "int，最大步数（默认10）",
                       "tools_allowed": "list[string]，允许的工具key列表（可选，默认全部可用工具）"}},
        {"tool_key": "agent.dsh", "name": "高级Agent（DeepSeek Harness Runtime）",
         "category": "agent", "requires_key": 0,
         "description": "高级AI Agent运行时（DeepSeek Harness SDK）。拥有持久shell、文件操作、代码执行等系统级能力，"
                        "可自主完成复杂多步骤任务（编写代码/操作文件/执行命令/调试）。"
                        "比agent.harness能力更强（有真实执行环境），但资源开销更大、响应更慢。"
                        "适合需要编写并执行代码、操作文件系统、调试程序等需要真实计算环境的任务。",
         "io_schema": {"task": "string，任务描述（必填，越具体越好）",
                       "model": "string，可选覆盖模型（默认用平台配置）",
                       "timeout": "int，超时秒数（可选，默认90）"}},
    ]


def _probe_browser() -> bool:
    """探测无头浏览器是否可用（playwright 可导入即可视为可用）。"""
    if not settings.TOOL_BROWSER_ENABLED:
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:  # noqa: BLE001
        return False


def _probe_ffmpeg() -> bool:
    """探测 ffmpeg 是否可用。"""
    import shutil
    return shutil.which("ffmpeg") is not None


def _probe_dsh() -> bool:
    """探测 DeepSeek Harness SDK 是否可用（已安装 + 配置完备）。"""
    try:
        from .dsh_bridge import is_available
        return is_available()
    except Exception:  # noqa: BLE001
        return False


def _category_available(tool_key: str) -> bool:
    """按开关/探测计算某内置工具的运行时可用性。"""
    if tool_key in ("web.search", "web.wikipedia"):
        return bool(settings.TOOL_WEB_SEARCH_ENABLED)
    if tool_key == "browser.navigate":
        return _probe_browser()
    if tool_key == "code.run":
        return bool(settings.TOOL_CODE_ENABLED)
    if tool_key == "legal.search":
        return False  # 需密钥，本期不可真调
    if tool_key == "media.ffmpeg":
        return _probe_ffmpeg()
    if tool_key == "agent.harness":
        return True  # 原生Python实现，无外部依赖
    if tool_key == "agent.dsh":
        return _probe_dsh()
    return True


# ---------------------------------------------------------------------------
# 目录 upsert / 可用性刷新 / 启动种子
# ---------------------------------------------------------------------------
def ensure_seed(db: Session) -> dict:
    """幂等 upsert 内置种子目录（只动 source=seed/新建 的行，不覆盖采集提案）。"""
    created = updated = 0
    for spec in SEED_TOOLS():
        row = (db.query(ToolPlugin)
                 .filter(ToolPlugin.tool_key == spec["tool_key"])
                 .first())
        if row is None:
            db.add(ToolPlugin(
                tool_key=spec["tool_key"], name=spec["name"], category=spec["category"],
                description=spec["description"],
                io_schema=json.dumps(spec["io_schema"], ensure_ascii=False),
                requires_key=spec["requires_key"], enabled=1, available=0,
                status="active", source="seed", source_url="", proposed_by=0,
                value_score=0))
            created += 1
        elif row.source == "seed":
            # 仅同步描述/入参说明等元信息；admin 的 enabled 开关与采集字段不覆盖。
            row.name = spec["name"]
            row.category = spec["category"]
            row.description = spec["description"]
            row.io_schema = json.dumps(spec["io_schema"], ensure_ascii=False)
            row.requires_key = spec["requires_key"]
            row.status = "active" if row.status != "archived" else row.status
            updated += 1
    db.flush()
    return {"created": created, "updated": updated}


def refresh_availability(db: Session) -> int:
    """按开关/探测刷新内置工具的 available。返回变更数。"""
    changed = 0
    for row in db.query(ToolPlugin).filter(ToolPlugin.source == "seed").all():
        want = 1 if _category_available(row.tool_key) else 0
        if int(row.available or 0) != want:
            row.available = want
            row.updated_at = _NOW()
            changed += 1
    db.flush()
    return changed


def bootstrap() -> dict:
    """启动自愈：种子 upsert + 可用性刷新 + 发现指令播种。自建会话，容错由调用方兜。"""
    from .database import SessionLocal
    db = SessionLocal()
    try:
        seed = ensure_seed(db)
        avail = refresh_availability(db)
        prompt = _seed_discovery_prompt(db)
        db.commit()
        return {"seed": seed, "availability_changed": avail, "prompt": prompt}
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 发现：站内 AI 默认应先调用此接口
# ---------------------------------------------------------------------------
def _tool_public(r: ToolPlugin) -> dict:
    try:
        io = json.loads(r.io_schema or "{}")
    except Exception:  # noqa: BLE001
        io = {}
    return {"tool_key": r.tool_key, "name": r.name, "category": r.category,
            "description": r.description, "io_schema": io,
            "requires_key": int(r.requires_key or 0),
            "available": int(r.available or 0), "status": r.status,
            "source": r.source, "value_score": int(r.value_score or 0)}


def discover(db: Session, category: str = "") -> dict:
    """列出可发现的工具（active 且未被管理员停用）。AI 执行任务前默认先查这里。"""
    _ensure_catalog(db)
    q = db.query(ToolPlugin).filter(ToolPlugin.status == "active",
                                    ToolPlugin.enabled == 1)
    if category:
        q = q.filter(ToolPlugin.category == category)
    rows = (q.order_by(ToolPlugin.category.asc(), ToolPlugin.tool_key.asc()).all())
    items = [_tool_public(r) for r in rows]
    cats = sorted({i["category"] for i in items})
    return {"total": len(items), "categories": cats,
            "directive": discovery_directive_text(), "items": items}


def discovery_directive_text() -> str:
    """站内 AI 的默认行为准则（注入系统提示 / discover 返回体）。"""
    return ("When executing any task on this site, follow the 'skill-library-first' "
            "directive by default: "
            "(1) first call GET /api/tools to search the skill library "
            "(web search / browser ops / code execution / legal / generation tools, etc.); "
            "(2) judge whether an available tool could improve the task's quality or efficiency; "
            "(3) if you use one, call POST /api/tools/{tool_key}/execute and state in reasoning "
            "which tool you picked and why; "
            "(4) if you do not use one, you must still give a reason for not calling it. "
            "Whether you use a tool or not is recorded in tool_calls for audit. "
            "Tools that require a key (requires_key=1) can only be discovered this cycle, not "
            "actually invoked — do not repeatedly attempt to call them.")


def _seed_discovery_prompt(db: Session) -> str:
    """把发现准则播种为提示词库条目（inject_point=on_enter），幂等。"""
    exists = (db.query(PromptLibrary)
                .filter(PromptLibrary.module == "tool_registry",
                        PromptLibrary.role == "discovery",
                        PromptLibrary.version == "v1")
                .first())
    if exists is not None:
        return "exists"
    db.add(PromptLibrary(module="tool_registry", role="discovery", version="v1",
                         content=discovery_directive_text(), inject_point="on_enter",
                         effective_from=_NOW(), status="active"))
    db.flush()
    return "created"


def _ensure_catalog(db: Session) -> None:
    """运行期懒加载兜底：目录为空时补种（正常由启动 bootstrap 完成）。"""
    if db.query(ToolPlugin.id).filter(ToolPlugin.source == "seed").first() is None:
        ensure_seed(db)
        refresh_availability(db)


# ---------------------------------------------------------------------------
# 决策 / 调用留痕（实现"用/不用都留痕"）
# ---------------------------------------------------------------------------
def record_decision(db: Session, citizen_id: int, note: str,
                    chosen_key: str = "", args: dict | None = None) -> ToolCall:
    """记录一次"是否使用工具"的决策（不调用任何工具时 chosen_key 传空）。"""
    row = ToolCall(citizen_id=citizen_id, tool_key=chosen_key,
                   args_json=json.dumps(args or {}, ensure_ascii=False),
                   status="decision", duration_ms=0, decision_note=(note or "")[:500])
    db.add(row)
    db.flush()
    return row


def _rate_limited(db: Session, citizen_id: int) -> bool:
    cap = int(settings.TOOL_MAX_CALLS_PER_MIN or 0)
    if cap <= 0:
        return False
    since = _NOW() - timedelta(minutes=1)
    n = (db.query(ToolCall)
           .filter(ToolCall.citizen_id == citizen_id,
                   ToolCall.created_at >= since,
                   ToolCall.status != "decision")
           .count())
    return n >= cap


def _log_call(db: Session, citizen_id: int, tool_key: str, args: dict,
              status: str, result: dict, dur_ms: int, note: str = "") -> ToolCall:
    row = ToolCall(citizen_id=citizen_id, tool_key=tool_key,
                   args_json=json.dumps(args, ensure_ascii=False),
                   result_json=json.dumps(result, ensure_ascii=False)[:8000],
                   status=status, duration_ms=dur_ms, decision_note=(note or "")[:500])
    db.add(row)
    plugin = (db.query(ToolPlugin).filter(ToolPlugin.tool_key == tool_key).first())
    if plugin is not None:
        plugin.usage_count = int(plugin.usage_count or 0) + 1
        plugin.last_used_at = _NOW()
    db.flush()
    return row


# ---------------------------------------------------------------------------
# 出站 HTTP 唯一接缝（测试 monkeypatch 此函数）
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: int | None = None) -> dict:
    """GET 并解析 JSON。任何异常返回 {}（调用方按空结果容错）。"""
    timeout = timeout or settings.TOOL_HTTP_TIMEOUT
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aijuhe-tool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 工具执行器（按 tool_key 分派）
# ---------------------------------------------------------------------------
def _run_web_search(args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        return {"ok": False, "error": "query required"}
    prov = settings.TOOL_WEB_SEARCH_PROVIDER or "ddg"
    if prov == "wikipedia":
        return _run_wikipedia({**args})
    url = ("https://api.duckduckgo.com/?" +
           urllib.parse.urlencode({"q": q, "format": "json", "no_html": 1, "skip_disambig": 1}))
    data = _http_get_json(url)
    abstract = data.get("AbstractText") or data.get("Heading") or ""
    related = []
    for rt in (data.get("RelatedTopics") or [])[:8]:
        if isinstance(rt, dict) and rt.get("Text"):
            related.append({"text": rt.get("Text"), "url": rt.get("FirstURL", "")})
    return {"ok": True, "provider": "duckduckgo", "query": q,
            "abstract": abstract, "results": related}


def _run_wikipedia(args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        return {"ok": False, "error": "query required"}
    lang = str(args.get("lang", "zh")).lower()
    url = (f"https://{lang}.wikipedia.org/w/api.php?" +
           urllib.parse.urlencode({"action": "opensearch", "search": q, "limit": 5,
                                   "format": "json"}))
    data = _http_get_json(url)
    # opensearch 返回 [query, [titles], [descriptions], [urls]]
    try:
        titles = data[1] if len(data) > 1 else []
        descs = data[2] if len(data) > 2 else []
        urls = data[3] if len(data) > 3 else []
    except Exception:  # noqa: BLE001
        titles = descs = urls = []
    results = [{"title": t, "snippet": (descs[i] if i < len(descs) else ""),
                "url": (urls[i] if i < len(urls) else "")}
               for i, t in enumerate(titles)]
    return {"ok": True, "provider": "wikipedia", "query": q, "results": results}


def _run_browser_navigate(args: dict) -> dict:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"ok": False, "error": "url required"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must start with http(s)://"}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:  # noqa: BLE001
        return {"ok": False, "unavailable": True,
                "error": "playwright not installed on server"}
    extract = str(args.get("extract", "title")).lower()
    try:
        with sync_playwright() as p:  # pragma: no cover - 需真实浏览器
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=settings.TOOL_HTTP_TIMEOUT * 1000)
            title = page.title()
            text = ""
            if extract == "text":
                text = page.inner_text("body")[:4000]
            browser.close()
        return {"ok": True, "url": url, "title": title, "text": text}
    except Exception as exc:  # noqa: BLE001  真实浏览器失败按 error 记账
        return {"ok": False, "error": f"navigate failed: {exc}"}


def _run_code(args: dict) -> dict:
    if not settings.TOOL_CODE_ENABLED:
        return {"ok": False, "unavailable": True,
                "error": "code sandbox disabled (set TOOL_CODE_ENABLED=1)"}
    code = str(args.get("code", ""))
    lang = str(args.get("language", "python")).lower()
    if not code:
        return {"ok": False, "error": "code required"}
    if lang not in ("python", "py"):
        return {"ok": False, "error": "only python supported in sandbox"}
    import subprocess
    import sys
    import tempfile
    import os
    try:
        with tempfile.TemporaryDirectory() as d:
            fn = os.path.join(d, "sandbox.py")
            with open(fn, "w", encoding="utf-8") as f:
                f.write(code)
            # 受限子进程：干净环境 + 超时 + 输出截断（非强沙箱，故默认关闭）。
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run([sys.executable, fn], cwd=d, env=env,
                                  capture_output=True, timeout=settings.TOOL_CODE_TIMEOUT)
            out = (proc.stdout.decode("utf-8", errors="replace")
                   [:settings.TOOL_CODE_MAX_OUTPUT])
            err = (proc.stderr.decode("utf-8", errors="replace")
                   [:settings.TOOL_CODE_MAX_OUTPUT])
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": out, "stderr": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _run_media_ffmpeg(args: dict) -> dict:
    """FFmpeg 万能媒体工具执行器。"""
    import subprocess
    import shutil
    import tempfile
    import os
    from pathlib import Path

    operation = str(args.get("operation", "")).strip()
    input_path = str(args.get("input_path", "")).strip()

    if not operation:
        return {"ok": False, "error": "operation required (extract_frames/get_duration/transcode/extract_audio/trim)"}
    if not input_path:
        return {"ok": False, "error": "input_path required"}
    if not os.path.isfile(input_path):
        return {"ok": False, "error": f"input file not found: {input_path}"}

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        return {"ok": False, "unavailable": True, "error": "ffmpeg not found on system"}

    timeout = int(args.get("timeout", 120))  # 默认120秒超时

    try:
        if operation == "get_duration":
            # 获取视频/音频时长
            if not ffprobe:
                return {"ok": False, "error": "ffprobe not found"}
            cmd = [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                   "-of", "csv=p=0", input_path]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            dur_str = proc.stdout.decode().strip()
            try:
                duration = float(dur_str)
            except ValueError:
                return {"ok": False, "error": f"cannot parse duration: {dur_str}"}
            # 同时获取分辨率
            cmd2 = [ffprobe, "-v", "quiet", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,codec_name,r_frame_rate",
                    "-of", "json", input_path]
            proc2 = subprocess.run(cmd2, capture_output=True, timeout=timeout)
            import json as _json
            stream_info = {}
            try:
                sdata = _json.loads(proc2.stdout.decode())
                if sdata.get("streams"):
                    stream_info = sdata["streams"][0]
            except Exception:
                pass
            return {"ok": True, "operation": "get_duration", "duration_sec": duration,
                    "video_info": stream_info, "input_path": input_path}

        elif operation == "extract_frames":
            # 抽帧：按 fps 提取为图片序列
            fps = int(args.get("fps", 2))
            resolution = str(args.get("resolution", ""))
            output_dir = str(args.get("output_dir", "")).strip()
            if not output_dir:
                output_dir = tempfile.mkdtemp(prefix="ffmpeg_frames_")
            os.makedirs(output_dir, exist_ok=True)

            scale_filter = f",scale={resolution.replace('x', ':')}" if resolution else ""
            out_pattern = os.path.join(output_dir, "frame_%05d.jpg")
            cmd = [ffmpeg, "-y", "-i", input_path,
                   "-vf", f"fps={fps}{scale_filter}",
                   "-q:v", "2", out_pattern]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")[-500:]
                return {"ok": False, "error": f"ffmpeg extract_frames failed: {err}"}
            frames = sorted(str(p) for p in Path(output_dir).glob("frame_*.jpg"))
            return {"ok": True, "operation": "extract_frames",
                    "frame_count": len(frames), "output_dir": output_dir,
                    "fps": fps, "frames": frames[:50]}  # 最多返回50个路径

        elif operation == "trim":
            # 裁剪片段
            start = float(args.get("start", 0))
            duration = float(args.get("duration", 10))
            output_dir = str(args.get("output_dir", "")).strip()
            if not output_dir:
                output_dir = tempfile.mkdtemp(prefix="ffmpeg_trim_")
            os.makedirs(output_dir, exist_ok=True)
            out_file = os.path.join(output_dir, "trimmed.mp4")
            cmd = [ffmpeg, "-y", "-ss", str(start), "-t", str(duration),
                   "-i", input_path, "-c", "copy", out_file]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if proc.returncode != 0:
                # fallback: re-encode
                cmd2 = [ffmpeg, "-y", "-ss", str(start), "-t", str(duration),
                        "-i", input_path, out_file]
                proc2 = subprocess.run(cmd2, capture_output=True, timeout=timeout * 2)
                if proc2.returncode != 0:
                    return {"ok": False, "error": "trim failed after re-encode fallback"}
            return {"ok": True, "operation": "trim", "output_path": out_file,
                    "start": start, "duration": duration}

        elif operation == "extract_audio":
            output_dir = str(args.get("output_dir", "")).strip()
            if not output_dir:
                output_dir = tempfile.mkdtemp(prefix="ffmpeg_audio_")
            os.makedirs(output_dir, exist_ok=True)
            out_file = os.path.join(output_dir, "audio.mp3")
            cmd = [ffmpeg, "-y", "-i", input_path, "-vn", "-acodec", "mp3",
                   "-q:a", "2", out_file]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if proc.returncode != 0:
                return {"ok": False, "error": "extract_audio failed"}
            return {"ok": True, "operation": "extract_audio", "output_path": out_file}

        elif operation == "transcode":
            codec = str(args.get("codec", "libx264"))
            resolution = str(args.get("resolution", ""))
            output_dir = str(args.get("output_dir", "")).strip()
            if not output_dir:
                output_dir = tempfile.mkdtemp(prefix="ffmpeg_tc_")
            os.makedirs(output_dir, exist_ok=True)
            out_file = os.path.join(output_dir, "output.mp4")
            cmd = [ffmpeg, "-y", "-i", input_path, "-c:v", codec]
            if resolution:
                cmd += ["-vf", f"scale={resolution.replace('x', ':')}"]
            cmd += ["-c:a", "aac", out_file]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout * 3)
            if proc.returncode != 0:
                return {"ok": False, "error": "transcode failed"}
            return {"ok": True, "operation": "transcode", "output_path": out_file,
                    "codec": codec}

        else:
            return {"ok": False, "error": f"unknown ffmpeg operation: {operation}. "
                    f"Supported: extract_frames, get_duration, transcode, extract_audio, trim"}

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ffmpeg operation '{operation}' timed out ({timeout}s)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ffmpeg error: {exc}"}


def _run_agent_dsh(args: dict) -> dict:
    """agent.dsh runner：委托给 dsh_bridge 模块。"""
    from .dsh_bridge import run_dsh_task

    task = str(args.get("task", "")).strip()
    if not task:
        return {"ok": False, "error": "task required"}
    model = str(args.get("model", "")).strip() or None
    timeout_val = args.get("timeout")
    timeout = int(timeout_val) if timeout_val else None
    return run_dsh_task(task, model=model, timeout=timeout)


_RUNNERS = {
    "web.search": _run_web_search,
    "web.wikipedia": _run_wikipedia,
    "browser.navigate": _run_browser_navigate,
    "code.run": _run_code,
    "media.ffmpeg": _run_media_ffmpeg,
    "agent.dsh": _run_agent_dsh,
}


def execute(db: Session, citizen_id: int, tool_key: str,
            args: dict | None = None, note: str = "") -> dict:
    """执行一个工具调用；全程留痕、限流、密钥/可用性门禁。返回结果 dict。

    不抛业务异常——以 status 字段区分 success/error/requires_key/unavailable/rate_limited。
    """
    _ensure_catalog(db)
    args = args or {}
    t0 = time.time()
    plugin = (db.query(ToolPlugin).filter(ToolPlugin.tool_key == tool_key).first())
    dur = lambda: int((time.time() - t0) * 1000)  # noqa: E731

    if plugin is None:
        _log_call(db, citizen_id, tool_key, args, "error", {"error": "unknown tool"}, dur(), note)
        return {"ok": False, "status": "error", "error": f"unknown tool: {tool_key}"}

    if plugin.status != "active" or int(plugin.enabled or 0) == 0:
        _log_call(db, citizen_id, tool_key, args, "unavailable",
                  {"error": "tool disabled/archived"}, dur(), note)
        return {"ok": False, "status": "unavailable", "error": "tool disabled or archived"}

    if int(plugin.requires_key or 0) == 1:
        msg = "该工具需外部密钥，本期仅可发现、暂不支持真调（避免无密钥空转）。"
        _log_call(db, citizen_id, tool_key, args, "requires_key", {"note": msg}, dur(), note)
        return {"ok": False, "status": "requires_key", "tool_key": tool_key, "note": msg}

    if int(plugin.available or 0) == 0:
        _log_call(db, citizen_id, tool_key, args, "unavailable",
                  {"error": "runtime unavailable"}, dur(), note)
        return {"ok": False, "status": "unavailable", "error": "tool runtime unavailable",
                "tool_key": tool_key}

    if _rate_limited(db, citizen_id):
        _log_call(db, citizen_id, tool_key, args, "rate_limited",
                  {"error": "rate limited"}, dur(), note)
        return {"ok": False, "status": "rate_limited", "tool_key": tool_key,
                "error": "tool call rate limit reached"}

    runner = _RUNNERS.get(tool_key)
    if runner is None:
        _log_call(db, citizen_id, tool_key, args, "error",
                  {"error": "no runner bound"}, dur(), note)
        return {"ok": False, "status": "error", "error": "no runner bound for tool"}

    try:
        result = runner(args)
    except Exception as exc:  # noqa: BLE001  执行异常按 error 记账，绝不冒泡
        _log_call(db, citizen_id, tool_key, args, "error",
                  {"error": str(exc)}, dur(), note)
        return {"ok": False, "status": "error", "tool_key": tool_key, "error": str(exc)}

    status = "success" if result.get("ok") else ("unavailable" if result.get("unavailable") else "error")
    _log_call(db, citizen_id, tool_key, args, status, result, dur(), note)
    return {"ok": bool(result.get("ok")), "status": status, "tool_key": tool_key, **result}


# ---------------------------------------------------------------------------
# 供 tool_scout 沉淀采集结果（去重后写入目录，status=pending 待择优/复核）
# ---------------------------------------------------------------------------
def propose_from_scout(db: Session, candidate: dict, proposed_by: int) -> str:
    """把侦察采集到的候选工具写入目录。返回 created/skipped。

    去重：同 tool_key 或同 source_url 命中即跳过（uq_tool_source_url 兜底并发）。
    """
    tool_key = str(candidate.get("tool_key", "")).strip()
    source_url = str(candidate.get("source_url", "")).strip()
    if not tool_key:
        return "skipped"
    if source_url:
        dup_url = (db.query(ToolPlugin)
                     .filter(ToolPlugin.source_url == source_url).first())
        if dup_url is not None:
            return "skipped"
    dup_key = (db.query(ToolPlugin)
                 .filter(ToolPlugin.tool_key == tool_key).first())
    if dup_key is not None:
        return "skipped"
    from sqlalchemy.exc import IntegrityError
    status = "pending"
    promoted = (settings.TOOL_SCOUT_AUTO_PROMOTE
                and int(candidate.get("value_score", 0) or 0) >= settings.TOOL_SCOUT_MIN_SCORE
                and int(candidate.get("requires_key", 0) or 0) == 0)
    if promoted:
        status = "active"
    row = ToolPlugin(
        tool_key=tool_key, name=str(candidate.get("name", tool_key))[:120],
        category=str(candidate.get("category", "general"))[:24],
        description=str(candidate.get("description", ""))[:1000],
        io_schema=json.dumps(candidate.get("io_schema", {}), ensure_ascii=False),
        requires_key=int(candidate.get("requires_key", 0) or 0),
        enabled=1, available=0, status=status, source="scout",
        source_url=source_url[:400], proposed_by=int(proposed_by or 0),
        value_score=int(candidate.get("value_score", 0) or 0))
    # 用 SAVEPOINT 包裹单条插入：并发撞 uq_tool_source_url 时只回滚本条，不误伤同批其它提案。
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return "created"
    except IntegrityError:  # uq_tool_source_url 并发兜底
        return "skipped"
