"""画面来源：后台周期性截屏，供监控界面显示，也供任务线程复用。

使用场景：
- `monitor_gui.MonitorApp` 启动后由该类的后台线程持续截屏，界面只读缓存；
- `Flow.screenshot` 通过 `grab` 取帧，优先复用刚截到的画面，避免任务线程
  与界面线程同时向 adb 发请求导致互相拖慢；
- 模拟器未启动或 adb 断开时，后台线程会定期尝试重连，模拟器一启动界面就
  自动恢复画面。

注意：`snapshot` / `grab` 返回的是共享的 numpy 数组，调用方如果要绘制或
修改，必须先 `copy()`。
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

import numpy as np

from .device import Device, DeviceError


class FrameSource:
    """带缓存的后台截屏器。

    使用场景：
    - 监控窗口需要以接近实时的帧率显示模拟器画面；
    - 自动化任务需要"足够新"的画面，但不应每次都单独调用 adb；
    - 模拟器中途重启时，希望画面自动恢复而不必重开程序。

    线程模型：`_capture_lock` 保证同一时刻只有一个 adb 截屏请求；
    `_lock` 只保护缓存字段，持有时间极短，界面刷新不会被截屏阻塞。
    """

    def __init__(
        self,
        device: Device,
        interval: float = 0.2,
        reuse_window: float = 0.4,
    ) -> None:
        """创建截屏器，但不会立刻开始截屏。

        Args:
            device: 已配置好 serial 的设备对象，内部只调用 `screencap` 与
                `connect`，不负责启动模拟器。
            interval: 后台线程两次截屏之间的间隔（秒），越小越流畅、
                adb 压力越大，默认 0.2 秒约 5 帧/秒。
            reuse_window: `grab` 允许复用缓存帧的最大年龄（秒），
                超过该时间会立即重新截屏，默认 0.4 秒。

        Raises:
            ValueError: interval 不为正数或 reuse_window 为负数时。
        """
        if interval <= 0 or reuse_window < 0:
            raise ValueError("interval 必须为正数，reuse_window 不能为负数")
        self.device = device
        self.interval = float(interval)
        self.reuse_window = float(reuse_window)
        self._lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._latest_at = 0.0
        self._error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        """后台截屏线程是否仍在运行。

        Returns:
            bool: 线程存活且尚未收到停止信号时为 True。
        """
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        """启动后台截屏线程。

        使用场景：监控窗口初始化完成后调用一次；重复调用不会产生第二个线程。

        Returns:
            None。
        """
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name="frame-source",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """请求停止并等待后台线程退出。

        使用场景：关闭监控窗口或程序退出前调用，避免进程残留 adb 子进程。

        Args:
            timeout: 等待线程结束的最长秒数。

        Returns:
            None。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    # ---------- 取帧 ----------
    def grab(self) -> np.ndarray:
        """取一帧"足够新"的画面。

        使用场景：
        - `Flow.screenshot` 每次识别前调用；
        - 需要最新画面但又不想无谓重复调用 adb 的其他逻辑。

        Returns:
            numpy.ndarray: BGR 三通道画面。

        Raises:
            DeviceError: 缓存帧过旧且重新截屏失败时；错误同时记录在
                `snapshot` 的状态里供界面显示。
        """
        frame = self._fresh_frame()
        if frame is not None:
            return frame
        with self._capture_lock:
            frame = self._fresh_frame()
            if frame is not None:
                return frame
            try:
                frame = self.device.screencap()
            except DeviceError as exc:
                with self._lock:
                    self._error = str(exc)
                raise
            with self._lock:
                self._store_locked(frame)
            return frame

    def snapshot(self) -> Tuple[Optional[np.ndarray], float, Optional[str]]:
        """读取当前缓存，不发起任何 adb 调用。

        使用场景：监控界面的定时刷新，必须保持非阻塞，否则窗口会卡顿。

        Returns:
            tuple: (最新画面或 None, 该画面截取时间戳, 最近一次错误或 None)。
            时间戳为 0 表示还没有成功截过图。
        """
        with self._lock:
            return self._latest, self._latest_at, self._error

    # ---------- 内部实现 ----------
    def _fresh_frame(self) -> Optional[np.ndarray]:
        """返回仍在复用窗口内的缓存帧。

        Returns:
            Optional[numpy.ndarray]: 足够新的帧；没有或已过期时为 None。
        """
        with self._lock:
            if (
                self._latest is not None
                and time.time() - self._latest_at <= self.reuse_window
            ):
                return self._latest
            return None

    def _store_locked(self, frame: np.ndarray) -> None:
        """更新缓存并清空错误标记（调用方必须已持有 `_lock`）。

        Args:
            frame: 刚截到的画面。

        Returns:
            None。
        """
        self._latest = frame
        self._latest_at = time.time()
        self._error = None

    def _loop(self) -> None:
        """后台线程主循环：截屏 → 缓存 → 等待下一次。

        Returns:
            None。

        Raises:
            None: 截屏异常转为错误文案，模拟器恢复后会自动清除。
        """
        while not self._stop.is_set():
            try:
                with self._capture_lock:
                    frame = self.device.screencap()
                with self._lock:
                    self._store_locked(frame)
            except DeviceError as exc:
                with self._lock:
                    self._error = str(exc)
                self._reconnect()
            self._stop.wait(self.interval)

    def _reconnect(self) -> None:
        """截屏失败后等待片刻并尝试重建 adb 连接。

        使用场景：MuMu 被关闭或重启后，端口连接会失效；定期重连可以让
        界面在模拟器重新启动后自动恢复画面。

        Returns:
            None。

        Raises:
            None: 重连失败只更新错误文案。
        """
        if self._stop.wait(2.0):
            return
        try:
            self.device.connect(timeout=3.0)
        except DeviceError as exc:
            with self._lock:
                self._error = str(exc)
