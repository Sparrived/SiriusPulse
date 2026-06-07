from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static


@dataclass
class TuiResult:
    action: str = "exit"


class NameInputScreen(ModalScreen[str | None]):
    CSS = """
    NameInputScreen {
        align: center middle;
    }
    #dialog {
        width: 52;
        height: 12;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #dialog-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #dialog-actions {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("新建人格", id="dialog-title")
            yield Input(placeholder="输入人格标识名", id="persona-name-input")
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="cancel-create")
                yield Button("创建", variant="primary", id="confirm-create")

    def on_mount(self) -> None:
        self.query_one("#persona-name-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-create":
            self.dismiss(None)
            return
        value = self.query_one("#persona-name-input", Input).value.strip()
        self.dismiss(value or None)


class SiriusPulseApp(App[TuiResult]):
    CSS = """
    Screen {
        background: #070a12;
        color: #dce7ff;
    }
    #root {
        height: 100%;
        padding: 1 2;
    }
    #hero {
        height: 7;
        padding: 1 2;
        margin-bottom: 1;
        border: round #4c9aff;
        background: #0c1424;
    }
    #title {
        text-style: bold;
        color: #8ec5ff;
        text-align: center;
    }
    #subtitle {
        color: #8b949e;
        text-align: center;
    }
    #layout {
        height: 1fr;
    }
    #sidebar {
        width: 28;
        padding: 1;
        border: round #24334f;
        background: #0c1018;
    }
    #content {
        width: 1fr;
        margin-left: 1;
    }
    .side-button {
        width: 100%;
        margin-bottom: 1;
    }
    .panel {
        padding: 1 2;
        border: round #24334f;
        background: #101726;
        margin-bottom: 1;
    }
    #status-grid {
        height: 7;
    }
    #persona-table {
        height: 1fr;
        border: round #24334f;
        background: #080d16;
    }
    #message {
        height: 3;
        color: #89d185;
        padding: 0 1;
    }
    #details {
        height: 8;
        padding: 1 2;
        border: round #24334f;
        background: #0c1018;
    }
    Button.-primary {
        background: #4c9aff;
    }
    """

    BINDINGS = [
        ("q", "quit_app", "退出"),
        ("r", "refresh", "刷新"),
        ("w", "webui", "启动 WebUI"),
        ("s", "start_persona", "启动人格"),
        ("x", "stop_persona", "停止人格"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.result = TuiResult()
        self.persona_names: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="root"):
            with Vertical(id="hero"):
                yield Static("Sirius Pulse 控制台", id="title")
                yield Static("多人格运行 · WebUI 后台服务 · 实时日志", id="subtitle")
            with Horizontal(id="layout"):
                with Vertical(id="sidebar"):
                    yield Button("启动运行模式", id="run-mode", classes="side-button", variant="primary")
                    yield Button("启动 / 打开 WebUI", id="webui-start", classes="side-button")
                    yield Button("停止 WebUI", id="webui-stop", classes="side-button")
                    yield Button("新建人格", id="persona-create", classes="side-button")
                    yield Button("启动选中人格", id="persona-start", classes="side-button")
                    yield Button("停止选中人格", id="persona-stop", classes="side-button")
                    yield Button("刷新状态", id="refresh", classes="side-button")
                    yield Button("退出并清理", id="quit-clean", classes="side-button", variant="error")
                with Vertical(id="content"):
                    yield Static(id="status-grid", classes="panel")
                    yield DataTable(id="persona-table")
                    yield Static(id="details")
                    yield Static(id="message")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Sirius Pulse"
        self.sub_title = "Observatory Console"
        table = self.query_one("#persona-table", DataTable)
        table.cursor_type = "row"
        self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_status()
        self.refresh_personas()
        self.refresh_details()

    def refresh_status(self) -> None:
        from sirius_pulse import cli

        config = cli._load_global_config()
        running, status = cli._webui_status()
        webui_state = "运行中" if running else "未运行"
        webui_pid = status.get("pid") if status else "—"
        text = (
            f"[bold #8ec5ff]WebUI[/]  {webui_state}  PID: {webui_pid}\n"
            f"[bold #8ec5ff]地址[/]   {cli._webui_url(config)}\n"
            f"[bold #8ec5ff]数据[/]   {cli.DATA_DIR}\n"
            f"[bold #8ec5ff]日志[/]   WebUI 与人格日志可在 WebUI 的「实时日志」页面查看"
        )
        self.query_one("#status-grid", Static).update(text)

    def refresh_personas(self) -> None:
        from sirius_pulse import cli

        manager = cli._manager()
        personas = manager.list_personas()
        self.persona_names = [str(p["name"]) for p in personas]
        table = self.query_one("#persona-table", DataTable)
        table.clear(columns=True)
        table.add_columns("人格", "角色名", "状态", "PID", "端口", "Adapter")
        for persona in personas:
            name = str(persona.get("name") or "—")
            status = "运行中" if persona.get("running") else "已停止"
            table.add_row(
                name,
                str(persona.get("persona_name") or "—"),
                status,
                str(persona.get("pid") or "—"),
                str(manager.get_port(name) or "—"),
                str(persona.get("adapters_count", 0)),
                key=name,
            )

    def selected_persona(self) -> str | None:
        table = self.query_one("#persona-table", DataTable)
        if not self.persona_names:
            return None
        index = max(0, min(table.cursor_row, len(self.persona_names) - 1))
        return self.persona_names[index]

    def refresh_details(self) -> None:
        from sirius_pulse import cli

        name = self.selected_persona()
        if not name:
            self.query_one("#details", Static).update("暂无人格。")
            return
        manager = cli._manager()
        info = manager.get_persona_status(name) or {}
        detail = (
            f"[bold]选中人格[/] {name}\n"
            f"角色名: {info.get('persona_name') or '—'}\n"
            f"状态: {'运行中' if info.get('running') else '已停止'}  "
            f"PID: {info.get('pid') or '—'}  端口: {manager.get_port(name) or '—'}\n"
            f"目录: {info.get('work_path') or '—'}"
        )
        self.query_one("#details", Static).update(detail)

    def set_message(self, message: str, ok: bool = True) -> None:
        color = "#89d185" if ok else "#ff7b72"
        self.query_one("#message", Static).update(f"[{color}]{message}[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.refresh_details()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        self.refresh_details()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "run-mode":
            self.result.action = "run"
            self.exit(self.result)
        elif button_id == "webui-start":
            self.action_webui()
        elif button_id == "webui-stop":
            self.action_stop_webui()
        elif button_id == "persona-create":
            self.push_screen(NameInputScreen(), self.create_persona)
        elif button_id == "persona-start":
            self.action_start_persona()
        elif button_id == "persona-stop":
            self.action_stop_persona()
        elif button_id == "refresh":
            self.action_refresh()
        elif button_id == "quit-clean":
            self.action_quit_app()

    def create_persona(self, name: str | None) -> None:
        if not name:
            return
        from sirius_pulse import cli

        manager = cli._manager()
        try:
            manager.create_persona(name, persona_name=name)
            self.set_message(f"人格已创建: {name}")
        except FileExistsError:
            self.set_message(f"人格已存在: {name}", ok=False)
        self.refresh_all()

    def action_refresh(self) -> None:
        self.refresh_all()
        self.set_message("状态已刷新")

    def action_webui(self) -> None:
        from sirius_pulse import cli

        result = cli._start_webui_background()
        state = "已在后台运行" if not result["started"] else "已后台启动"
        self.refresh_status()
        self.set_message(f"WebUI {state}: {result['url']} (pid={result['pid']})")

    def action_stop_webui(self) -> None:
        from sirius_pulse import cli

        stopped = cli._stop_webui_background()
        self.refresh_status()
        self.set_message("WebUI 已停止" if stopped else "WebUI 未在后台运行", ok=stopped)

    def action_start_persona(self) -> None:
        from sirius_pulse import cli

        name = self.selected_persona()
        if not name:
            self.set_message("请先选择人格", ok=False)
            return
        ok = cli._manager().start_persona(name)
        self.refresh_all()
        self.set_message(f"人格已启动: {name}" if ok else f"人格启动失败: {name}", ok=ok)

    def action_stop_persona(self) -> None:
        from sirius_pulse import cli

        name = self.selected_persona()
        if not name:
            self.set_message("请先选择人格", ok=False)
            return
        ok = cli._manager().stop_persona(name)
        self.refresh_all()
        self.set_message(f"人格已停止: {name}" if ok else f"人格未在运行或不存在: {name}", ok=ok)

    def action_quit_app(self) -> None:
        from sirius_pulse import cli

        cli._shutdown_services()
        self.result.action = "exit"
        self.exit(self.result)


def run_textual_cli() -> TuiResult:
    return SiriusPulseApp().run()


__all__ = ["TuiResult", "run_textual_cli"]
