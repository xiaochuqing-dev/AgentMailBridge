"""用于验证应用服务接入的最小 tkinter GUI 骨架。"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import load_config
from agent_mail_bridge.models import OperationStatus, ServiceResult


class BridgeWindow:
    """只负责交互展示，所有业务均调用 ApplicationService。"""

    def __init__(self, root: tk.Tk, service: ApplicationService):
        self.root = root
        self.service = service
        self.task_active = False
        self.closed = False
        self.status_var = tk.StringVar(value="空闲")
        self.error_var = tk.StringVar(value="")
        self.connection_var = tk.StringVar(value="正在读取状态")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _build(self) -> None:
        self.root.title("AgentMailBridge")
        self.root.geometry("820x560")
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="连接状态").pack(anchor=tk.W)
        ttk.Label(frame, textvariable=self.connection_var).pack(anchor=tk.W, pady=(0, 8))

        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(0, 8))
        self.receive_button = ttk.Button(actions, text="手动收取", command=self.receive)
        self.receive_button.pack(side=tk.LEFT)
        self.send_button = ttk.Button(actions, text="选择文件发送", command=self.send)
        self.send_button.pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="刷新", command=self.refresh).pack(side=tk.LEFT)

        ttk.Label(frame, text="当前任务").pack(anchor=tk.W)
        ttk.Label(frame, textvariable=self.status_var).pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(frame, textvariable=self.error_var, foreground="#a00000").pack(anchor=tk.W)

        ttk.Label(frame, text="今日收到文件").pack(anchor=tk.W, pady=(8, 0))
        self.files = tk.Listbox(frame, height=12)
        self.files.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="最近日志").pack(anchor=tk.W, pady=(8, 0))
        self.logs = tk.Text(frame, height=8, state=tk.DISABLED)
        self.logs.pack(fill=tk.X)

    def receive(self) -> None:
        self._run_task("正在收取邮件", self.service.receive, self._show_operation)

    def send(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=str(self.service.cfg.data_root_path), title="选择待发送文件"
        )
        if not path:
            self.error_var.set("已取消选择文件")
            return
        self._run_task(
            "正在发送文件", lambda: self.service.send_file(Path(path)),
            self._show_operation,
        )

    def refresh(self) -> None:
        if self.task_active:
            self.error_var.set("当前任务尚未完成")
            return
        status = self.service.get_config_and_connection_status().details
        oauth = status["gmail_api"]["state"]
        self.connection_var.set(
            f"收件后端：{status['receive_backend']}    "
            f"Gmail API：{oauth}    QQ SMTP：{status['qq_smtp']}"
        )
        file_result = self.service.get_today_files()
        self.files.delete(0, tk.END)
        for item in file_result.details["files"]:
            self.files.insert(tk.END, f"{item['saved_filename']}  [{item['status']}]")
        log_result = self.service.get_recent_logs(30)
        lines = [
            f"{item['created_at']} {item['level']} {item['message']}"
            for item in log_result.details["events"]
        ]
        self.logs.configure(state=tk.NORMAL)
        self.logs.delete("1.0", tk.END)
        self.logs.insert(tk.END, "\n".join(lines))
        self.logs.configure(state=tk.DISABLED)

    def _run_task(
        self,
        title: str,
        operation: Callable[[], ServiceResult],
        callback: Callable[[ServiceResult], None],
    ) -> None:
        if self.task_active:
            self.error_var.set("已有任务正在运行，请勿重复点击")
            return
        self.task_active = True
        self.status_var.set(title)
        self.error_var.set("")
        self.receive_button.configure(state=tk.DISABLED)
        self.send_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = operation()
            except Exception as exc:  # noqa: BLE001
                result = ServiceResult(OperationStatus.FAILED, message=str(exc))
            if not self.closed:
                self.root.after(0, lambda: self._finish_task(result, callback))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_task(
        self, result: ServiceResult, callback: Callable[[ServiceResult], None]
    ) -> None:
        if self.closed:
            return
        self.task_active = False
        self.receive_button.configure(state=tk.NORMAL)
        self.send_button.configure(state=tk.NORMAL)
        callback(result)
        self.refresh()

    def _show_operation(self, result: ServiceResult) -> None:
        self.status_var.set(result.message or result.status.value)
        self.error_var.set("" if result.ok else result.message)

    def close(self) -> None:
        self.closed = True
        self.root.destroy()


def main() -> None:
    service = ApplicationService(load_config())
    service.initialize()
    root = tk.Tk()
    BridgeWindow(root, service)
    root.mainloop()


if __name__ == "__main__":
    main()
