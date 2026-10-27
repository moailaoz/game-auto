"""可视化监控窗口：实时镜像模拟器画面，并展示当前操作。

用法：
    python main.py monitor

功能：
- 后台线程持续截屏，窗口按约 5~8 帧/秒刷新模拟器画面；
- 绿色方框标出最近一次模板命中，橙色圆圈/箭头标出最近的点击、滑动；
- 顶部显示连接状态、分辨率与刷新帧率，底部是当前操作与任务日志；
- 可直接在窗口里启动/停止任务，任务执行期间画面与操作同步更新。

设计说明：
- Tkinter 的所有控件操作都发生在主线程：任务线程只往队列里投递事件，
  由 `_tick` 统一消费，避免跨线程操作 Tk 控件导致的崩溃；
- 画面只读 `FrameSource` 缓存，不在 UI 线程里调用 adb，保证窗口不卡顿；
- 图片用 OpenCV 缩放后编码成 PNG，由 Tk 8.6 直接显示，不额外依赖 Pillow。
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from core.flow import Flow, FlowStopped
from core.frames import FrameSource
from core.monitor import (
    KIND_MATCH,
    KIND_STEP,
    KIND_SWIPE,
    KIND_TAP,
    Event,
    Monitor,
)

QUEUE_TICK_MS = 50          # 界面消费事件队列的间隔（毫秒）
RENDER_MIN_INTERVAL = 0.12  # 两次重绘的最小间隔（秒），约 8 帧/秒上限
MATCH_TTL = 1.5             # 识别框保留时间（秒）
ACTION_TTL = 2.0            # 点击/滑动标记保留时间（秒）
LOG_MAX_LINES = 400         # 日志区保留的最大行数
ONLINE_WINDOW = 2.0         # 超过该时间没有新帧即视为离线（秒）

# 画面标线颜色（OpenCV 使用 BGR 顺序）
COLOR_MATCH = (86, 220, 126)
COLOR_ACTION = (60, 180, 255)

BG_DARK = "#0f141a"
BG_PANEL = "#16202b"
BG_OP = "#1b2632"
FG_TEXT = "#e8f2fb"
FG_MUTED = "#9fb3c8"


class QueueLogHandler(logging.Handler):
    """把 logging 记录转投到界面线程队列。

    使用场景：监控窗口挂到项目 logger 上后，任务线程打印的步骤、警告、
    错误都能出现在界面日志区，而不必额外维护一套进度回调。
    """

    def __init__(self, sink: "queue.Queue") -> None:
        """创建处理器。

        Args:
            sink: 界面事件队列，元素为 ("log", (levelname, text))。
        """
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """格式化一条日志并放入队列。

        使用场景：logger 在任意线程写日志时由 logging 模块自动回调。

        Args:
            record: 日志记录。

        Returns:
            None。

        Raises:
            None: 队列写入失败（如程序退出）会被忽略。
        """
        try:
            self.sink.put(("log", (record.levelname, self.format(record))))
        except Exception:  # noqa: BLE001 - 退出阶段队列可能已失效
            return


class MonitorApp:
    """Tkinter 监控窗口。

    使用场景：
    - `main.py monitor` 命令构造本类并调用 `run`；
    - 需要独立进程查看模拟器画面、调试模板或观察自动任务执行过程时使用。
    """

    def __init__(
        self,
        device: Any,
        logger: logging.Logger,
        tasks: Dict[str, Any],
        flow_factory: Callable[..., Flow],
    ) -> None:
        """装配窗口、事件队列与后台截屏器。

        Args:
            device: `core.device.Device` 实例，需带 `serial`、`screencap`。
            logger: 项目日志器；窗口会追加一个队列处理器用于显示。
            tasks: 任务名到任务模块的映射，模块需提供 `run(flow)`。
            flow_factory: 创建 `Flow` 的工厂，调用时会传入
                `monitor`、`frame_source`、`stop_event` 关键字参数。

        Raises:
            tk.TclError: 当前环境无法创建 Tk 窗口（如无图形界面）时。
        """
        self.device = device
        self.logger = logger
        self.tasks = tasks
        self.flow_factory = flow_factory

        self.events: "queue.Queue" = queue.Queue()
        self.monitor = Monitor()
        self.monitor.subscribe(self._forward_event)
        self.frame_source = FrameSource(device, interval=0.2)

        self.stop_event: Optional[threading.Event] = None
        self.task_thread: Optional[threading.Thread] = None
        self._render_count = 0
        self._fps = 0.0
        self._fps_at = time.time()
        self._frame_at = 0.0
        self._rendered_at = 0.0
        self._photo: Optional[tk.PhotoImage] = None
        self._image_id: Optional[int] = None
        self._render_error_shown = False
        self._match: Optional[Tuple[str, float, Tuple[int, int, int, int], float]] = None
        self._actions: List[
            Tuple[str, Tuple[int, int], Optional[Tuple[int, int]], float]
        ] = []

        self._build_ui()

        self._log_handler = QueueLogHandler(self.events)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
        )
        logger.addHandler(self._log_handler)

    # ---------- 界面搭建 ----------
    def _build_ui(self) -> None:
        """创建窗口、工具栏、画面区、当前操作区与日志区。

        使用场景：仅在构造时调用一次。

        Returns:
            None。
        """
        self.root = tk.Tk()
        self.root.title("game-auto 监控台 — 模拟器实时画面")
        self.root.geometry("1000x820")
        self.root.minsize(760, 600)
        self.root.configure(bg=BG_DARK)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=3, minsize=240)
        self.root.rowconfigure(3, weight=1, minsize=120)

        self.op_var = tk.StringVar(value="监控中：未运行任务")
        self.status_var = tk.StringVar(value="● 离线")
        self.device_var = tk.StringVar(value=str(getattr(self.device, "serial", "-")))
        self.size_var = tk.StringVar(value="分辨率 -")
        self.fps_var = tk.StringVar(value="0.0 fps")
        self.detail_var = tk.StringVar(value="等待模拟器连接…")
        task_names = sorted(self.tasks)
        self.task_var = tk.StringVar(value=task_names[0] if task_names else "")

        self._build_toolbar(task_names)
        self.canvas = tk.Canvas(
            self.root, bg="#080b0e", highlightthickness=0
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self._hint_id = self.canvas.create_text(
            0,
            0,
            text="等待模拟器画面…",
            fill="#5f7183",
            font=("Microsoft YaHei", 12),
        )

        op_frame = tk.Frame(self.root, bg=BG_OP, padx=12, pady=8)
        op_frame.grid(row=2, column=0, sticky="ew")
        tk.Label(
            op_frame,
            text="当前操作",
            fg="#7f93a8",
            bg=BG_OP,
            font=("Microsoft YaHei", 9),
        ).pack(anchor="w")
        tk.Label(
            op_frame,
            textvariable=self.op_var,
            fg=FG_TEXT,
            bg=BG_OP,
            font=("Microsoft YaHei", 14, "bold"),
            anchor="w",
            justify="left",
            wraplength=940,
        ).pack(fill="x")

        log_frame = tk.Frame(self.root, bg=BG_DARK, padx=8, pady=6)
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=8,
            bg="#0b0f13",
            fg="#b9c7d6",
            insertbackground="#b9c7d6",
            relief="flat",
            wrap="word",
            font=("Consolas", 9),
            state="disabled",
        )
        scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for level, color in (
            ("INFO", "#b9c7d6"),
            ("DEBUG", "#6b7d8f"),
            ("WARNING", "#f0b85f"),
            ("ERROR", "#ff7b72"),
        ):
            self.log_text.tag_configure(level, foreground=color)

        tk.Label(
            self.root,
            textvariable=self.detail_var,
            anchor="w",
            fg="#6b7d8f",
            bg=BG_DARK,
            padx=10,
            pady=4,
            wraplength=960,
            justify="left",
        ).grid(row=4, column=0, sticky="ew")

    def _build_toolbar(self, task_names: List[str]) -> None:
        """创建顶部状态栏与任务控制按钮。

        Args:
            task_names: 可选任务名列表，用于下拉框。

        Returns:
            None。
        """
        bar = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=8)
        bar.grid(row=0, column=0, sticky="ew")

        self.status_label = tk.Label(
            bar,
            textvariable=self.status_var,
            fg="#ff7b72",
            bg=BG_PANEL,
            font=("Microsoft YaHei", 10, "bold"),
        )
        self.status_label.pack(side="left")
        for var in (self.device_var, self.size_var, self.fps_var):
            tk.Label(
                bar,
                textvariable=var,
                fg=FG_MUTED,
                bg=BG_PANEL,
                font=("Microsoft YaHei", 9),
            ).pack(side="left", padx=(12, 0))

        self.stop_btn = ttk.Button(
            bar, text="停止", command=self.stop_task, state="disabled"
        )
        self.stop_btn.pack(side="right", padx=(6, 0))
        self.start_btn = ttk.Button(bar, text="开始任务", command=self.start_task)
        self.start_btn.pack(side="right")
        self.task_box = ttk.Combobox(
            bar,
            textvariable=self.task_var,
            values=task_names,
            state="readonly",
            width=10,
        )
        self.task_box.pack(side="right", padx=(6, 0))
        tk.Label(
            bar, text="任务", fg=FG_MUTED, bg=BG_PANEL, font=("Microsoft YaHei", 9)
        ).pack(side="right", padx=(12, 0))
        if not task_names:
            self.start_btn.configure(state="disabled")

    # ---------- 任务控制 ----------
    def start_task(self) -> None:
        """启动下拉框选中的任务。

        使用场景：用户点击"开始任务"时由 Tk 主线程调用。

        Returns:
            None。
        """
        if self.task_thread is not None and self.task_thread.is_alive():
            return
        name = self.task_var.get()
        module = self.tasks.get(name)
        if module is None:
            self.logger.error("未知任务: %s", name)
            return

        self.stop_event = threading.Event()
        try:
            flow = self.flow_factory(
                monitor=self.monitor,
                frame_source=self.frame_source,
                stop_event=self.stop_event,
            )
        except Exception as exc:  # noqa: BLE001 - 初始化失败要回到界面
            self.logger.error("初始化任务失败: %s", exc)
            self.stop_event = None
            return

        self.op_var.set("准备执行任务：%s" % name)
        self._set_running(True)
        self.task_thread = threading.Thread(
            target=self._run_task,
            args=(module, flow, name),
            name="task-runner",
            daemon=True,
        )
        self.task_thread.start()

    def stop_task(self) -> None:
        """请求停止当前任务。

        使用场景：用户点击"停止"时由 Tk 主线程调用。停止是协作式的：
        只在等待/轮询的间隙生效，不会强杀正在执行的 adb 命令。

        Returns:
            None。
        """
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            self.logger.warning("已请求停止：当前步骤结束后退出")
        self.stop_btn.configure(state="disabled")

    def _run_task(self, module: Any, flow: Flow, name: str) -> None:
        """任务线程入口：执行任务并处理停止/异常。

        使用场景：由 `start_task` 在后台线程中调用，不能直接操作 Tk 控件。

        Args:
            module: 任务模块，需提供 `run(flow)`。
            flow: 已注入监控与停止信号的流程对象。
            name: 任务名，仅用于日志。

        Returns:
            None。
        """
        try:
            self.logger.info("开始执行任务: %s", name)
            module.run(flow)
        except FlowStopped:
            self.logger.warning("任务 %s 已停止", name)
        except Exception as exc:  # noqa: BLE001 - 任务异常统一展示
            self.logger.error("任务 %s 失败: %s", name, exc)
        finally:
            self.logger.info("任务 %s 结束", name)
            self.events.put(("task_done", None))

    def _set_running(self, running: bool) -> None:
        """切换任务按钮与下拉框的可用状态。

        Args:
            running: 任务是否正在执行。

        Returns:
            None。
        """
        self.start_btn.configure(state="disabled" if running else "normal")
        self.task_box.configure(state="disabled" if running else "readonly")
        self.stop_btn.configure(state="normal" if running else "disabled")

    # ---------- 事件与刷新 ----------
    def _forward_event(self, event: Event) -> None:
        """把监控事件转投到界面队列（可能在任务线程中被调用）。

        Args:
            event: `core.monitor` 事件。

        Returns:
            None。
        """
        self.events.put(("event", event))

    def _tick(self) -> None:
        """界面定时任务：消费队列、刷新状态、按需重绘画面。

        使用场景：由 Tk 的 `after` 每 50ms 调度一次，直到窗口关闭。

        Returns:
            None。
        """
        self._drain_events()
        self._refresh_status()
        self._render_if_needed()
        self.root.after(QUEUE_TICK_MS, self._tick)

    def _drain_events(self) -> None:
        """一次性处理事件队列中的消息。

        使用场景：`_tick` 每次调度时调用，避免主线程长时间阻塞。

        Returns:
            None。
        """
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                level, text = payload
                self._append_log(level, text)
            elif kind == "event":
                self._apply_event(payload)
            elif kind == "task_done":
                self._on_task_done()

    def _append_log(self, level: str, text: str) -> None:
        """向日志区追加一行并自动滚动。

        Args:
            level: 日志级别名，用于着色。
            text: 已格式化的日志文本。

        Returns:
            None。
        """
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", level)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > LOG_MAX_LINES:
            self.log_text.delete("1.0", "%d.0" % (line_count - LOG_MAX_LINES))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _apply_event(self, event: Event) -> None:
        """把事件反映到"当前操作"文字与画面标记上。

        Args:
            event: `core.monitor` 事件。

        Returns:
            None。
        """
        now = time.time()
        if event.kind == KIND_STEP:
            self.op_var.set(event.text)
        elif event.kind == KIND_MATCH and event.box is not None:
            self._match = (event.text, event.score, event.box, now)
        elif event.kind in (KIND_TAP, KIND_SWIPE) and event.point is not None:
            self._actions.append((event.kind, event.point, event.end_point, now))

    def _on_task_done(self) -> None:
        """任务线程结束后的界面收尾。

        使用场景：收到 ("task_done", None) 队列消息时由 `_drain_events` 调用。

        Returns:
            None。
        """
        self.task_thread = None
        self.stop_event = None
        self._set_running(False)
        self.op_var.set("监控中：任务已结束")

    def _refresh_status(self) -> None:
        """刷新连接状态、分辨率、帧率与错误提示。

        使用场景：`_tick` 每次调度时调用。

        Returns:
            None。
        """
        frame, at, error = self.frame_source.snapshot()
        now = time.time()
        if at > 0 and now - at <= ONLINE_WINDOW and error is None:
            self.status_var.set("● 在线")
            self.status_label.configure(fg="#7cf08a")
            self.detail_var.set(
                "设备 %s 已连接，画面实时同步中" % getattr(self.device, "serial", "-")
            )
        elif at > 0 and now - at > ONLINE_WINDOW:
            self.status_var.set("● 连接异常")
            self.status_label.configure(fg="#f0b85f")
            self.detail_var.set(error or "画面已停止更新，正在重连…")
        else:
            self.status_var.set("● 离线")
            self.status_label.configure(fg="#ff7b72")
            self.detail_var.set(error or "等待模拟器启动并开启 ADB 调试…")

        if frame is not None:
            height, width = frame.shape[:2]
            self.size_var.set("分辨率 %dx%d" % (width, height))
        if now - self._fps_at >= 1.0:
            self._fps = self._render_count / (now - self._fps_at)
            self._render_count = 0
            self._fps_at = now
            self.fps_var.set("%.1f fps" % self._fps)

    def _render_if_needed(self) -> None:
        """有新帧且达到刷新间隔时重绘画面。

        使用场景：`_tick` 每次调度时调用。

        Returns:
            None。
        """
        frame, at, _ = self.frame_source.snapshot()
        if frame is None or at == self._frame_at:
            return
        now = time.time()
        if now - self._rendered_at < RENDER_MIN_INTERVAL:
            return
        self._frame_at = at
        self._rendered_at = now
        self._render(frame)

    def _render(self, frame: np.ndarray) -> None:
        """把一帧画面缩放后显示到画布上。

        使用场景：`_render_if_needed` 与窗口尺寸变化时调用。

        Args:
            frame: 设备原始画面。函数内部会 `copy()`，不会修改共享缓存。

        Returns:
            None。
        """
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 10 or height < 10:
            return

        image = frame.copy()
        self._draw_overlays(image)
        frame_h, frame_w = image.shape[:2]
        scale = min(width / float(frame_w), height / float(frame_h))
        size = (max(1, int(frame_w * scale)), max(1, int(frame_h * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(image, size, interpolation=interpolation)

        ok, buffer = cv2.imencode(".png", resized)
        if not ok:
            return
        data = base64.b64encode(buffer.tobytes()).decode("ascii")
        try:
            photo = tk.PhotoImage(data=data)
        except tk.TclError as exc:
            if not self._render_error_shown:
                self._render_error_shown = True
                self.logger.error("图片显示失败（需要 Tk 8.6 及以上）: %s", exc)
            return

        center = (width // 2, height // 2)
        if self._image_id is None:
            self._image_id = self.canvas.create_image(
                center[0], center[1], image=photo
            )
        else:
            self.canvas.itemconfigure(self._image_id, image=photo)
            self.canvas.coords(self._image_id, center[0], center[1])
        self._photo = photo  # 必须保留引用，否则 Tk 会把图片回收成空白
        self.canvas.itemconfigure(self._hint_id, state="hidden")
        self._render_count += 1

    def _draw_overlays(self, image: np.ndarray) -> None:
        """在画面上绘制识别框与最近的动作标记。

        使用场景：`_render` 显示前调用，让用户看到"机器人此刻在点什么"。

        Args:
            image: 已拷贝的画面，会被就地修改。

        Returns:
            None。
        """
        now = time.time()
        if self._match is not None and now - self._match[3] <= MATCH_TTL:
            name, score, box, _ = self._match
            x, y, w, h = box
            cv2.rectangle(image, (x, y), (x + w, y + h), COLOR_MATCH, 2)
            cv2.putText(
                image,
                "%s %.2f" % (name, score),
                (x, max(14, y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                COLOR_MATCH,
                1,
                cv2.LINE_AA,
            )

        self._actions = [
            item for item in self._actions if now - item[3] <= ACTION_TTL
        ]
        for kind, point, end_point, _ in self._actions:
            if kind == KIND_TAP:
                cv2.circle(image, point, 16, COLOR_ACTION, 2)
                cv2.line(
                    image,
                    (point[0] - 8, point[1]),
                    (point[0] + 8, point[1]),
                    COLOR_ACTION,
                    1,
                )
                cv2.line(
                    image,
                    (point[0], point[1] - 8),
                    (point[0], point[1] + 8),
                    COLOR_ACTION,
                    1,
                )
            elif kind == KIND_SWIPE and end_point is not None:
                cv2.arrowedLine(
                    image, point, end_point, COLOR_ACTION, 2, tipLength=0.08
                )

    def _on_canvas_resize(self, event: Any) -> None:
        """窗口尺寸变化后重新居中提示文字并按新尺寸重绘。

        Args:
            event: Tk 的 `<Configure>` 事件，包含新的宽高。

        Returns:
            None。
        """
        self.canvas.coords(self._hint_id, event.width // 2, event.height // 2)
        if self._image_id is not None:
            self.canvas.coords(
                self._image_id, event.width // 2, event.height // 2
            )
        frame, _, _ = self.frame_source.snapshot()
        if frame is not None:
            self._render(frame)

    # ---------- 生命周期 ----------
    def run(self) -> int:
        """启动截屏线程并进入 Tk 主循环，窗口关闭后返回。

        使用场景：`main.py monitor` 命令的最后一步。

        Returns:
            int: 正常关闭返回 0。
        """
        self.frame_source.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(QUEUE_TICK_MS, self._tick)
        try:
            self.root.mainloop()
        finally:
            self.frame_source.stop()
        return 0

    def _on_close(self) -> None:
        """关闭窗口前的清理：停止任务与截屏线程、移除日志处理器。

        使用场景：用户点击窗口关闭按钮或按 Alt+F4 时由 Tk 调用。

        Returns:
            None。
        """
        if self.stop_event is not None:
            self.stop_event.set()
        self.frame_source.stop()
        try:
            self.logger.removeHandler(self._log_handler)
        except Exception:  # noqa: BLE001 - 退出阶段不关心失败
            pass
        self.root.destroy()
