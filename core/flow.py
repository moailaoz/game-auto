"""流程层：截图 → 识别 → 动作 的循环，tasks 只写业务步骤。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from .actions import Actions
from .device import Device
from .frames import FrameSource
from .logger import save_frame
from .monitor import KIND_MATCH, KIND_STEP, Event, Monitor
from .vision import Match, Vision


class FlowStopped(RuntimeError):
    """任务被监控界面或外部方按请求停止。"""


class Flow:
    """任务可用的流程 API：等待、点击、滑动、返回与失败存档。

    使用场景：
    - `tasks/*.py` 只调用本类方法编写业务步骤；
    - CLI 模式（`main.py run`）不传 monitor，行为与以往完全一致；
    - 监控模式（`main.py monitor`）额外注入 `Monitor`、`FrameSource` 与
      停止信号，界面即可显示画面、当前操作并支持手动停止。
    """

    def __init__(
        self,
        device: Device,
        vision: Vision,
        actions: Actions,
        logger: logging.Logger,
        poll_interval: float = 0.6,
        log_dir: Optional[Path] = None,
        monitor: Optional[Monitor] = None,
        frame_source: Optional[FrameSource] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """装配流程层依赖。

        Args:
            device: ADB 设备通道，截图与输入动作都由它执行。
            vision: 模板匹配器，负责在画面中定位目标。
            actions: 动作封装，负责点击/滑动/返回。
            logger: 项目日志器，用于步骤、命中与超时输出。
            poll_interval: 等待目标时的轮询间隔（秒）。
            log_dir: 失败截图存档目录；None 表示不存档。
            monitor: 可选事件总线，用于向监控界面广播当前操作与识别结果。
            frame_source: 可选画面来源；传入后截图改为复用其后缓存，避免
                任务线程与界面线程同时调用 adb。
            stop_event: 可选停止信号；置位后等待循环会尽快抛出
                `FlowStopped`，供界面"停止"按钮使用。

        Raises:
            None: 构造阶段不做任何 IO。
        """
        self.device = device
        self.vision = vision
        self.actions = actions
        self.logger = logger
        self.poll_interval = poll_interval
        self.log_dir = Path(log_dir) if log_dir else None
        self.monitor = monitor
        self.frame_source = frame_source
        self.stop_event = stop_event

    # ---------- 感知 ----------
    def screenshot(self) -> np.ndarray:
        """获取当前画面。

        使用场景：`find` / `save_failure` 需要画面时调用；接了监控画面来源
        就复用它刚截到的帧，否则直接向设备要一帧。

        Returns:
            numpy.ndarray: BGR 三通道画面。

        Raises:
            DeviceError: 截图失败且重试耗尽时由 Device 抛出。
        """
        if self.frame_source is not None:
            return self.frame_source.grab()
        return self.device.screencap()

    def find(self, name: str, frame: Optional[np.ndarray] = None) -> Optional[Match]:
        """在当前画面中查找指定模板。

        使用场景：只需要判断"在不在"时用本方法；需要等待则用 `wait`。

        Args:
            name: 模板名（不含 .png）。
            frame: 可选画面，沿用上一次截图可减少 adb 调用。

        Returns:
            Optional[Match]: 命中返回匹配结果，否则返回 None。

        Raises:
            TemplateMissing: 模板图不存在或无法读取时由 Vision 抛出。
        """
        if frame is None:
            frame = self.screenshot()
        match = self.vision.find(frame, name)
        if match is not None and self.monitor is not None:
            self.monitor.emit(
                Event(
                    kind=KIND_MATCH,
                    text=name,
                    point=match.center,
                    box=match.box,
                    score=match.score,
                )
            )
        return match

    def exists(self, name: str) -> bool:
        """判断当前画面是否存在某个模板。

        使用场景：分支判断，例如"出现新手引导就跳过签到"。

        Args:
            name: 模板名。

        Returns:
            bool: 命中为 True。

        Raises:
            TemplateMissing: 模板图缺失时由 Vision 抛出。
        """
        return self.find(name) is not None

    def wait(self, name: str, timeout: float = 15.0) -> Optional[Match]:
        """轮询等待某个界面/按钮出现。

        使用场景：进入新界面后等待稳定元素出现，或等待弹窗加载完成。

        Args:
            name: 模板名。
            timeout: 最长等待秒数。

        Returns:
            Optional[Match]: 命中返回匹配结果，超时返回 None。

        Raises:
            FlowStopped: 等待期间收到停止信号时。
            TemplateMissing: 模板图缺失时。
        """
        self.step("等待 %s 出现（最多 %.0f 秒）" % (name, timeout))
        match = self._poll(name, timeout)
        if match is None:
            self.logger.warning("等待超时: %s (%.1fs)", name, timeout)
        return match

    def wait_gone(self, name: str, timeout: float = 15.0) -> bool:
        """等待某个元素消失。

        使用场景：等待加载弹窗、广告页、"请稍后"遮罩自动关闭。

        Args:
            name: 模板名。
            timeout: 最长等待秒数。

        Returns:
            bool: 超时前消失返回 True，否则 False。

        Raises:
            FlowStopped: 等待期间收到停止信号时。
            TemplateMissing: 模板图缺失时。
        """
        self.step("等待 %s 消失（最多 %.0f 秒）" % (name, timeout))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_stop()
            if self.find(name) is None:
                return True
            self._pause(self.poll_interval)
        return self.find(name) is None

    # ---------- 执行 ----------
    def step(self, text: str) -> None:
        """声明当前操作，写入日志并推送到监控界面。

        使用场景：
        - `tasks` 在业务阶段切换时调用，例如"处理邮件奖励"；
        - `wait` / `click` 等 API 内部也会调用，界面始终显示最新操作。

        Args:
            text: 人类可读的操作描述。

        Returns:
            None。
        """
        self.logger.info("当前操作: %s", text)
        if self.monitor is not None:
            self.monitor.emit(Event(kind=KIND_STEP, text=text))

    def click(self, name: str, timeout: float = 15.0) -> bool:
        """等待目标出现并点击其中心。

        使用场景：进入邮件/签到/日常入口等"必须点到"的步骤。

        Args:
            name: 模板名。
            timeout: 最长等待秒数。

        Returns:
            bool: 点击成功返回 True，超时未出现返回 False。

        Raises:
            FlowStopped: 等待期间收到停止信号时。
            TemplateMissing: 模板图缺失时。
            DeviceError: 点击执行失败时。
        """
        self.step("点击 %s" % name)
        match = self._poll(name, timeout)
        if match is None:
            self.logger.warning("未找到 %s，跳过点击 (%.1fs)", name, timeout)
            return False
        self.actions.tap_match(match)
        return True

    def click_if_exists(self, name: str) -> bool:
        """目标存在就点击，不存在直接跳过。

        使用场景：可选步骤，例如"一键领取"按钮不一定每次都出现。

        Args:
            name: 模板名。

        Returns:
            bool: 点击了返回 True，不存在返回 False。

        Raises:
            TemplateMissing: 模板图缺失时。
            DeviceError: 点击执行失败时。
        """
        self.step("尝试点击 %s（可选步骤）" % name)
        match = self.find(name)
        if match is None:
            return False
        self.actions.tap_match(match)
        return True

    def tap_ratio(self, rx: float, ry: float) -> None:
        """按屏幕比例点击。

        使用场景：位置相对固定但缺少稳定特征的元素，如列表第一项。

        Args:
            rx: 横坐标比例（0~1）。
            ry: 纵坐标比例（0~1）。

        Returns:
            None。

        Raises:
            DeviceError: 读取分辨率或执行点击失败时。
        """
        self.step("点击屏幕位置 (%.0f%%, %.0f%%)" % (rx * 100, ry * 100))
        self.actions.tap_ratio(rx, ry)

    def swipe_ratio(
        self, start: Tuple[float, float], end: Tuple[float, float],
        duration_ms: Optional[int] = None,
    ) -> None:
        """按屏幕比例滑动。

        使用场景：列表滚动、翻页、拖拽。

        Args:
            start: 起点比例 (rx, ry)，取值 0~1。
            end: 终点比例 (rx, ry)，取值 0~1。
            duration_ms: 滑动时长（毫秒），None 使用配置默认值。

        Returns:
            None。

        Raises:
            DeviceError: 读取分辨率或执行滑动失败时。
        """
        self.step(
            "滑动 (%.0f%%, %.0f%%) → (%.0f%%, %.0f%%)"
            % (start[0] * 100, start[1] * 100, end[0] * 100, end[1] * 100)
        )
        self.actions.swipe_ratio(start, end, duration_ms)

    def back(self, times: int = 1) -> None:
        """发送返回键，可连续返回多层。

        使用场景：关闭弹窗、退出子界面回到主界面。

        Args:
            times: 连续按返回的次数。

        Returns:
            None。

        Raises:
            DeviceError: 执行 keyevent 失败时。
        """
        self.step("返回" if times == 1 else "返回 ×%d" % times)
        for _ in range(max(0, times)):
            self.actions.back()

    # ---------- 调试 ----------
    def save_failure(self, tag: str, frame: Optional[np.ndarray] = None) -> Optional[Path]:
        """保存当前画面到失败现场目录。

        使用场景：任务判断"没找到入口/界面不对"时调用，便于事后定位。

        Args:
            tag: 文件名标签，通常写失败原因，如 "no_home"。
            frame: 可选画面；None 时现场截一帧。

        Returns:
            Optional[Path]: 保存成功返回图片路径；未配置 log_dir 或保存失败
            返回 None。

        Raises:
            None: 存档失败只记录警告，不打断任务。
        """
        if self.log_dir is None:
            return None
        try:
            if frame is None:
                frame = self.screenshot()
            path = save_frame(frame, self.log_dir, tag)
            self.logger.warning("已保存现场截图: %s", path)
            return path
        except Exception as exc:  # 存档失败不应打断任务
            self.logger.warning("保存截图失败: %s", exc)
            return None

    # ---------- 内部 ----------
    def _check_stop(self) -> None:
        """检查停止信号，置位时抛异常退出任务。

        使用场景：每个轮询循环和动作前的安全检查。

        Returns:
            None。

        Raises:
            FlowStopped: 停止信号已置位时。
        """
        if self.stop_event is not None and self.stop_event.is_set():
            raise FlowStopped("已手动停止")

    def _pause(self, seconds: float) -> None:
        """在轮询间隔内等待，同时保持对停止信号敏感。

        Args:
            seconds: 等待时长（秒）。

        Returns:
            None。

        Raises:
            FlowStopped: 等待期间收到停止信号时。
        """
        if self.stop_event is not None:
            if self.stop_event.wait(seconds):
                raise FlowStopped("已手动停止")
        else:
            time.sleep(seconds)

    def _poll(self, name: str, timeout: float) -> Optional[Match]:
        """在超时时间内反复查找模板。

        使用场景：`wait` / `click` 的公共实现。

        Args:
            name: 模板名。
            timeout: 最长等待秒数。

        Returns:
            Optional[Match]: 命中即返回，超时返回 None。

        Raises:
            FlowStopped: 轮询期间收到停止信号时。
            TemplateMissing: 模板图缺失时。
        """
        deadline = time.time() + timeout
        while True:
            self._check_stop()
            match = self.find(name)
            if match is not None:
                self.logger.debug("命中 %s (%.3f)", name, match.score)
                return match
            if time.time() >= deadline:
                return None
            self._pause(self.poll_interval)
