"""Render safe container diagnostics as a shareable status card."""

from __future__ import annotations

import html
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def normalize_status(value: Any) -> dict[str, Any] | None:
    """Validate and normalize the fixed status schema returned by the host proxy."""
    if not isinstance(value, dict):
        return None
    keys = {
        "name",
        "image",
        "status",
        "running",
        "exit_code",
        "started_at",
        "finished_at",
        "health",
        "restart_policy",
    }
    if not keys.issubset(value):
        return None
    return {
        **{key: str(value[key] or "").strip() for key in keys},
        "resources": _normalize_metrics(value.get("resources"), _resource_defaults()),
        "host": _normalize_metrics(value.get("host"), _host_defaults()),
    }


async def render_status_card(status: dict[str, Any], data_store: Any) -> Path:
    """Render a container snapshot through the bundled Playwright runtime."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("状态卡片需要 Playwright；请重新部署包含 Chromium 的镜像") from exc

    output_dir = _artifact_dir(data_store)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"container_status_{uuid4().hex}.png"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 800}, device_scale_factor=1)
            await page.set_content(build_status_card_html(status), wait_until="load")
            await page.locator("#container-status-card").screenshot(path=str(output_path))
        finally:
            await browser.close()
    return output_path


def status_summary(status: dict[str, Any]) -> str:
    health = status["health"] or "未上报"
    return f"{status['name']}：{status['status'] or 'unknown'}，健康检查 {health}"


def build_status_card_html(status: dict[str, Any]) -> str:
    status_key = status["status"].lower()
    tone = "healthy" if status_key == "running" else "warning" if status_key == "paused" else "critical"
    labels = {
        "running": "运行中",
        "paused": "已暂停",
        "restarting": "重启中",
        "exited": "已退出",
        "dead": "异常中",
    }
    state_label = labels.get(status_key, status["status"].upper() or "未知")
    status_fields = [
        ("运行时长", _format_uptime(status["started_at"]) if status_key == "running" else "-"),
        ("健康检查", status["health"] or "未上报"),
        ("重启策略", status["restart_policy"] or "未设置"),
        ("退出码", status["exit_code"] or "-"),
    ]
    resource_fields = [
        ("CPU", status["resources"]["cpu_percent"]),
        ("内存", status["resources"]["memory_usage"]),
        ("内存占比", status["resources"]["memory_percent"]),
        ("网络 I/O", status["resources"]["network_io"]),
        ("块 I/O", status["resources"]["block_io"]),
        ("PID", status["resources"]["pids"]),
    ]
    host_fields = [
        ("主机 CPU", status["host"]["cpu_percent"]),
        ("主机内存", status["host"]["memory_usage"]),
        ("根磁盘", status["host"]["disk_usage"]),
        ("负载 (1m)", status["host"]["load_1"]),
        ("主机运行时长", status["host"]["uptime"]),
    ]

    def fields_html(fields: list[tuple[str, str]]) -> str:
        return "".join(
            f'<div class="field"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in fields
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #111315; color: #f3f5f6; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
#container-status-card {{ width: 800px; display: grid; grid-template-columns: 12px 1fr; border: 1px solid #384046; border-radius: 8px; overflow: hidden; background: #1a1e21; }}
.signal {{ background: #37d99e; }} .signal.warning {{ background: #ffc857; }} .signal.critical {{ background: #ff6b6b; }}
.content {{ padding: 30px 32px 28px; }} .eyebrow, .state, .image, strong, .footer {{ font-family: "Cascadia Mono", Consolas, monospace; }}
.eyebrow {{ color: #aeb7bd; font-size: 12px; font-weight: 700; }}
.topline {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 10px 0 26px; }}
h1 {{ margin: 0; color: #fff; font-family: "Cascadia Mono", Consolas, monospace; font-size: 30px; line-height: 1.2; overflow-wrap: anywhere; }}
.state {{ color: #d8e0e3; font-size: 13px; white-space: nowrap; }} .image {{ border-block: 1px solid #384046; color: #d6dde0; font-size: 14px; line-height: 1.5; padding: 16px 0; overflow-wrap: anywhere; }}
.section {{ margin-top: 22px; }} .section-title {{ color: #aeb7bd; font-family: "Cascadia Mono", Consolas, monospace; font-size: 11px; font-weight: 700; }}
.grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); column-gap: 24px; }} .field {{ min-height: 54px; border-bottom: 1px solid #2e3438; padding: 11px 0; }}
.field span {{ color: #98a4ab; display: block; font-size: 12px; }} .field strong {{ color: #f3f5f6; display: block; font-size: 12px; line-height: 1.45; margin-top: 4px; overflow-wrap: anywhere; }}
.footer {{ color: #7f8b91; font-size: 11px; margin-top: 18px; }}
</style></head><body><article id="container-status-card"><div class="signal {tone}"></div><div class="content">
<div class="eyebrow">CONTAINER / LIVE STATUS</div><div class="topline"><h1>{html.escape(status['name'])}</h1><div class="state">{html.escape(state_label)}</div></div>
<div class="image">{html.escape(status['image'] or '镜像未报告')}</div>
<section class="section"><div class="section-title">STATUS</div><div class="grid">{fields_html(status_fields)}</div></section>
<section class="section"><div class="section-title">CONTAINER RESOURCE</div><div class="grid">{fields_html(resource_fields)}</div></section>
<section class="section"><div class="section-title">HOST SNAPSHOT</div><div class="grid">{fields_html(host_fields)}</div></section>
<div class="footer">STARTED {_format_timestamp(status['started_at'])} · SIRIUS BASH DOCKER</div>
</div></article></body></html>"""


def _artifact_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path(tempfile.gettempdir()) / "sirius_pulse" / "skill_artifacts" / "bash"


def _format_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("0001-"):
        return "未运行"
    return text.replace("T", " ").split(".", 1)[0].replace("Z", " UTC")


def _format_uptime(value: str) -> str:
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "未知"
    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分"
    return f"{minutes}分"


def _normalize_metrics(value: Any, defaults: dict[str, str]) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {key: str(source.get(key) or default) for key, default in defaults.items()}


def _resource_defaults() -> dict[str, str]:
    return {
        "cpu_percent": "未上报",
        "memory_usage": "未上报",
        "memory_percent": "未上报",
        "network_io": "未上报",
        "block_io": "未上报",
        "pids": "未上报",
    }


def _host_defaults() -> dict[str, str]:
    return {
        "cpu_percent": "未上报",
        "memory_usage": "未上报",
        "disk_usage": "未上报",
        "load_1": "未上报",
        "uptime": "未上报",
    }
