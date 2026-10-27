"""设备通道：通过 ADB 连接 MuMu 模拟器，执行 shell 命令与截屏。"""
from __future__ import annotations

import subprocess
import time
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np


class DeviceError(RuntimeError):
    """ADB 调用失败或设备不可用。"""


class Device:
    """一个 ADB 设备（一个 MuMu 实例）。"""

    def __init__(
        self,
        adb: str = "adb",
        serial: str = "127.0.0.1:16384",
        screenshot_retry: int = 2,
    ) -> None:
        self.adb = adb
        self.serial = serial
        self.screenshot_retry = max(1, screenshot_retry)
        self._screen_size: Optional[Tuple[int, int]] = None

    # ---------- ADB 底层 ----------
    def _run(self, args: Sequence[str], timeout: float = 20.0) -> bytes:
        cmd = [self.adb] + list(args)
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise DeviceError(
                "找不到 adb：请检查 config.yaml 中的 device.adb，"
                "或把 MuMu 自带的 adb.exe 加入 PATH"
            )
        except subprocess.TimeoutExpired:
            raise DeviceError("adb 命令超时: %s" % " ".join(cmd))

        if proc.returncode != 0:
            detail = (
                proc.stderr.decode("utf-8", "ignore").strip()
                or proc.stdout.decode("utf-8", "ignore").strip()
            )
            raise DeviceError(
                "adb 执行失败(%s): %s\n%s" % (proc.returncode, " ".join(cmd), detail)
            )
        return proc.stdout

    def shell(self, command: str, timeout: float = 20.0) -> str:
        """执行一条 shell 命令并返回文本输出。"""
        output = self._run(["-s", self.serial, "shell", command], timeout)
        return output.decode("utf-8", "ignore").strip()

    # ---------- 连接 ----------
    def connect(self, timeout: float = 15.0) -> None:
        """连接并等待设备就绪。带端口的 serial 会先执行 adb connect。"""
        if ":" in self.serial:
            output = self._run(["connect", self.serial], timeout).decode("utf-8", "ignore")
            lowered = output.lower()
            if "failed" in lowered or "cannot" in lowered:
                raise DeviceError(
                    "连接失败: %s\n请确认 MuMu 已启动，并在设置中开启 ADB 调试"
                    % output.strip()
                )

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_online():
                return
            time.sleep(0.5)
        raise DeviceError(
            "设备 %s 未上线：检查 MuMu 是否开机、ADB 调试是否开启" % self.serial
        )

    def is_online(self) -> bool:
        try:
            output = self._run(["devices"], timeout=8.0).decode("utf-8", "ignore")
        except DeviceError:
            return False

        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial:
                return parts[1] == "device"
        return False

    # ---------- 屏幕 ----------
    @property
    def screen_size(self) -> Tuple[int, int]:
        """模拟器当前分辨率 (宽, 高)，优先取 Override size。"""
        if self._screen_size is None:
            text = self.shell("wm size")
            size = None
            for token in text.replace("\r", " ").replace("\n", " ").split():
                if token[:1].isdigit() and "x" in token:
                    width, height = token.split("x", 1)
                    if width.isdigit() and height.isdigit():
                        size = (int(width), int(height))
            if size is None:
                raise DeviceError("无法解析分辨率: %r" % text)
            self._screen_size = size
        return self._screen_size

    def screencap(self) -> np.ndarray:
        """截取当前画面，返回 BGR 数组。失败会按配置重试。"""
        last_error: Optional[DeviceError] = None
        for _ in range(self.screenshot_retry):
            try:
                raw = self._run(
                    ["-s", self.serial, "exec-out", "screencap", "-p"], timeout=20
                )
                frame = cv2.imdecode(
                    np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if frame is not None:
                    return frame
                last_error = DeviceError("截图解码失败（返回数据不是有效 PNG）")
            except DeviceError as exc:
                last_error = exc
            time.sleep(0.3)
        raise last_error or DeviceError("截图失败")

    # ---------- 输入 ----------
    def tap(self, x: int, y: int) -> None:
        self.shell("input tap %d %d" % (int(x), int(y)), timeout=10)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(
            "input swipe %d %d %d %d %d"
            % (int(x1), int(y1), int(x2), int(y2), int(duration_ms)),
            timeout=15,
        )

    def back(self) -> None:
        self.shell("input keyevent 4", timeout=10)
