"""动作层：把"点击/滑动/返回"包装成带随机抖动和等待的可读调用。"""
from __future__ import annotations

import random
import time
from typing import Optional, Tuple

from .device import Device
from .monitor import KIND_BACK, KIND_SWIPE, KIND_TAP, Event, Monitor
from .vision import Match


class Actions:
    """模拟器输入动作封装。

    使用场景：`Flow` 的所有点击/滑动/返回都经由此类执行；任务代码不应直接
    调用 `Device.tap`，这样抖动、等待和监控事件才能统一生效。
    """

    def __init__(
        self,
        device: Device,
        jitter: int = 2,
        swipe_duration: int = 300,
        tap_delay: float = 0.6,
        monitor: Optional[Monitor] = None,
    ) -> None:
        """配置动作参数。

        Args:
            device: ADB 设备通道。
            jitter: 点击坐标随机偏移的最大像素数，0 表示不抖动。
            swipe_duration: 滑动默认时长（毫秒）。
            tap_delay: 每个动作后的默认等待（秒），给界面留响应时间。
            monitor: 可选的事件总线；传入后每次落点都会广播给监控界面。
        """
        self.device = device
        self.jitter = max(0, jitter)
        self.swipe_duration = swipe_duration
        self.tap_delay = tap_delay
        self.monitor = monitor

    def _offset(self, value: int) -> int:
        """给坐标叠加随机抖动，避免每次点击落在同一像素。

        Args:
            value: 原始坐标。

        Returns:
            int: 抖动后的坐标；jitter 为 0 时原样返回。
        """
        if self.jitter == 0:
            return int(value)
        return int(value + random.randint(-self.jitter, self.jitter))

    def _emit(self, event: Event) -> None:
        """把动作事件广播给监控界面（没有监控时为空操作）。

        Args:
            event: 待广播的事件。

        Returns:
            None。

        Raises:
            None: `Monitor.emit` 内部会吞掉监听器异常。
        """
        if self.monitor is not None:
            self.monitor.emit(event)

    def tap(self, x: int, y: int, delay: Optional[float] = None) -> None:
        """点击绝对坐标。

        使用场景：
        - `tap_match` / `tap_ratio` 的底层实现；
        - 任务里需要点击已知像素位置的场景（例如固定 UI 元素）。

        Args:
            x: 横坐标（像素，以模拟器分辨率为准）。
            y: 纵坐标（像素）。
            delay: 点击后的等待秒数，None 表示使用构造时的 tap_delay。

        Returns:
            None。

        Raises:
            DeviceError: adb 执行 `input tap` 失败时由 Device 抛出。
        """
        x, y = self._offset(x), self._offset(y)
        self._emit(Event(kind=KIND_TAP, point=(x, y)))
        self.device.tap(x, y)
        time.sleep(self.tap_delay if delay is None else delay)

    def tap_match(self, match: Match, delay: Optional[float] = None) -> None:
        """点击模板匹配结果的中心点。

        使用场景：`Flow.click` / `Flow.click_if_exists` 命中模板后调用。

        Args:
            match: `Vision.find` 返回的匹配结果。
            delay: 点击后的等待秒数，None 表示使用默认值。

        Returns:
            None。

        Raises:
            DeviceError: adb 执行点击失败时由 Device 抛出。
        """
        self.tap(match.x, match.y, delay)

    def tap_ratio(
        self, rx: float, ry: float, delay: Optional[float] = None
    ) -> None:
        """按屏幕比例点击，适合没有模板价值的固定位置。

        使用场景：列表第一项、屏幕中央的"继续"按钮等位置相对固定的元素。

        Args:
            rx: 横坐标比例（0~1）。
            ry: 纵坐标比例（0~1）。
            delay: 点击后的等待秒数，None 表示使用默认值。

        Returns:
            None。

        Raises:
            DeviceError: 读取分辨率或执行点击失败时由 Device 抛出。
        """
        width, height = self.device.screen_size
        self.tap(int(width * rx), int(height * ry), delay)

    def swipe(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
        duration_ms: Optional[int] = None,
        delay: Optional[float] = None,
    ) -> None:
        """从一点滑动到另一点。

        使用场景：列表滚动、拖拽道具、翻页等需要按住拖动的操作。

        Args:
            start: 起点坐标 (x, y)。
            end: 终点坐标 (x, y)。
            duration_ms: 滑动时长（毫秒），None 表示使用默认值。
            delay: 滑动后的等待秒数，None 表示使用默认值。

        Returns:
            None。

        Raises:
            DeviceError: adb 执行 `input swipe` 失败时由 Device 抛出。
        """
        start_point = (self._offset(start[0]), self._offset(start[1]))
        end_point = (self._offset(end[0]), self._offset(end[1]))
        self._emit(
            Event(kind=KIND_SWIPE, point=start_point, end_point=end_point)
        )
        self.device.swipe(
            start_point[0],
            start_point[1],
            end_point[0],
            end_point[1],
            self.swipe_duration if duration_ms is None else duration_ms,
        )
        time.sleep(self.tap_delay if delay is None else delay)

    def swipe_ratio(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        duration_ms: Optional[int] = None,
        delay: Optional[float] = None,
    ) -> None:
        """按屏幕比例滑动。

        使用场景：`Flow.swipe_ratio` 的底层实现，任务里写相对坐标更稳。

        Args:
            start: 起点比例 (rx, ry)，取值 0~1。
            end: 终点比例 (rx, ry)，取值 0~1。
            duration_ms: 滑动时长（毫秒），None 表示使用默认值。
            delay: 滑动后的等待秒数，None 表示使用默认值。

        Returns:
            None。

        Raises:
            DeviceError: 读取分辨率或执行滑动失败时由 Device 抛出。
        """
        width, height = self.device.screen_size
        self.swipe(
            (int(width * start[0]), int(height * start[1])),
            (int(width * end[0]), int(height * end[1])),
            duration_ms,
            delay,
        )

    def back(self, delay: Optional[float] = None) -> None:
        """发送 Android 返回键（keyevent 4）。

        使用场景：关闭弹窗、退回上一级界面。

        Args:
            delay: 返回后的等待秒数，None 表示使用默认值。

        Returns:
            None。

        Raises:
            DeviceError: adb 执行 keyevent 失败时由 Device 抛出。
        """
        self._emit(Event(kind=KIND_BACK))
        self.device.back()
        time.sleep(self.tap_delay if delay is None else delay)
