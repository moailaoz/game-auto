"""配置：把 config.yaml 解析成带默认值的对象，避免到处读裸字典。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class DeviceConfig:
    adb: str = "adb"
    serial: str = "127.0.0.1:16384"
    connect_on_start: bool = True
    screen: Optional[Tuple[int, int]] = None
    screenshot_retry: int = 2


@dataclass
class VisionConfig:
    template_dir: str = "assets/templates/tata"
    threshold: float = 0.85
    poll_interval: float = 0.6


@dataclass
class ActionConfig:
    jitter: int = 2
    swipe_duration: int = 300
    tap_delay: float = 0.6


@dataclass
class Config:
    device: DeviceConfig
    vision: VisionConfig
    actions: ActionConfig
    log_dir: Path

    @property
    def template_dir(self) -> Path:
        return (PROJECT_ROOT / self.vision.template_dir).resolve()

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("config.yaml 顶层必须是字典: %s" % path)

        device_raw = raw.get("device") or {}
        screen = device_raw.get("screen")
        device = DeviceConfig(
            adb=str(device_raw.get("adb", "adb")),
            serial=str(device_raw.get("serial", "127.0.0.1:16384")),
            connect_on_start=bool(device_raw.get("connect_on_start", True)),
            screen=(int(screen[0]), int(screen[1])) if screen else None,
            screenshot_retry=int(device_raw.get("screenshot_retry", 2)),
        )

        vision_raw = raw.get("vision") or {}
        vision = VisionConfig(
            template_dir=str(vision_raw.get("template_dir", "assets/templates/tata")),
            threshold=float(vision_raw.get("threshold", 0.85)),
            poll_interval=float(vision_raw.get("poll_interval", 0.6)),
        )

        action_raw = raw.get("actions") or {}
        actions = ActionConfig(
            jitter=int(action_raw.get("jitter", 2)),
            swipe_duration=int(action_raw.get("swipe_duration", 300)),
            tap_delay=float(action_raw.get("tap_delay", 0.6)),
        )

        log_dir = Path(raw.get("log_dir", "logs"))
        if not log_dir.is_absolute():
            log_dir = PROJECT_ROOT / log_dir

        return cls(device=device, vision=vision, actions=actions, log_dir=log_dir)
