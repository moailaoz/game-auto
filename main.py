"""game-auto 命令行入口：doctor / capture / templates / run。

用法：
    python main.py doctor                 # 检查设备、分辨率、模板是否就绪
    python main.py capture                # 截一张图，用于裁剪模板
    python main.py templates              # 查看模板目录与缺失项
    python main.py run --task daily       # 执行日常任务
    python main.py monitor                # 打开实时画面 + 当前操作监控窗口
"""
from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from core import imageio
from core.actions import Actions
from core.config import PROJECT_ROOT, Config
from core.device import Device, DeviceError
from core.flow import Flow
from core.frames import FrameSource
from core.logger import setup_logger
from core.monitor import Monitor
from core.vision import TemplateMissing, Vision
from tasks import tata_daily

TASKS = {"daily": tata_daily}
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"


def load_config(path: Optional[Path]) -> Config:
    """加载 YAML 配置。

    使用场景：所有子命令启动时都需要配置，先调用本函数再装配其他对象。

    Args:
        path: 配置文件路径；None 时使用项目根目录的 config.yaml。

    Returns:
        Config: 带默认值的配置对象。

    Raises:
        SystemExit: 配置文件不存在时直接终止进程并提示路径。
        ValueError: YAML 顶层不是字典时由 Config.load 抛出。
    """
    config_path = Path(path) if path else DEFAULT_CONFIG
    if not config_path.exists():
        raise SystemExit("找不到配置文件: %s" % config_path)
    return Config.load(config_path)


def build_device(config: Config) -> Device:
    """按配置创建设备通道，并按需连接模拟器。

    使用场景：`doctor` / `capture` / `run` 需要真实设备时调用；监控模式为了
    支持"先开窗口再启动模拟器"，不使用本函数而直接构造 Device。

    Args:
        config: 已加载的配置对象。

    Returns:
        Device: 可直接截屏和执行输入事件的设备对象。

    Raises:
        DeviceError: 配置要求启动即连接，而模拟器未上线或 ADB 不可用时。
    """
    device = Device(
        adb=config.device.adb,
        serial=config.device.serial,
        screenshot_retry=config.device.screenshot_retry,
    )
    if config.device.connect_on_start:
        device.connect()
    return device


def build_flow(
    config: Config,
    device: Device,
    logger,
    monitor: Optional[Monitor] = None,
    frame_source: Optional[FrameSource] = None,
    stop_event: Optional[threading.Event] = None,
) -> Flow:
    """装配视觉识别、动作与流程对象。

    使用场景：
    - `cmd_run` 以纯 CLI 方式执行任务（不传监控相关参数）；
    - 监控窗口通过 `flow_factory` 调用，注入事件总线、共用画面来源和停止信号。

    Args:
        config: 已加载的配置对象。
        device: 已连接的 ADB 设备。
        logger: 项目日志器。
        monitor: 可选事件总线，用于把当前操作推送到监控界面。
        frame_source: 可选画面来源，注入后任务与界面共用同一路截图。
        stop_event: 可选停止信号，置位后等待循环会抛出 `FlowStopped`。

    Returns:
        Flow: 可直接交给 `tasks/*.py` 使用的流程对象。

    Raises:
        None: 构造阶段不做 IO，异常会在任务执行时才出现。
    """
    vision = Vision(config.template_dir, threshold=config.vision.threshold)
    actions = Actions(
        device,
        jitter=config.actions.jitter,
        swipe_duration=config.actions.swipe_duration,
        tap_delay=config.actions.tap_delay,
        monitor=monitor,
    )
    return Flow(
        device=device,
        vision=vision,
        actions=actions,
        logger=logger,
        poll_interval=config.vision.poll_interval,
        log_dir=config.log_dir,
        monitor=monitor,
        frame_source=frame_source,
        stop_event=stop_event,
    )


def check_screen_size(config: Config, device: Device, logger) -> Optional[Tuple[int, int]]:
    """读取模拟器分辨率并与配置比对。

    使用场景：执行任务前确认模板图与模拟器分辨率一致，不一致只告警不阻断。

    Args:
        config: 配置对象，使用其中的 `device.screen`。
        device: 已连接的设备。
        logger: 项目日志器。

    Returns:
        Optional[Tuple[int, int]]: 模拟器实际分辨率 (宽, 高)。

    Raises:
        DeviceError: 无法从 adb 读取分辨率时由 Device 抛出。
    """
    actual = device.screen_size
    expected = config.device.screen
    if expected and tuple(expected) != actual:
        logger.warning(
            "分辨率不一致：模拟器 %dx%d，配置 %dx%d。模板图必须与模拟器分辨率一致",
            actual[0],
            actual[1],
            expected[0],
            expected[1],
        )
    return actual


def cmd_doctor(args: argparse.Namespace) -> int:
    """检查设备连接、分辨率与模板是否就绪。

    使用场景：首次运行或换机器、换分辨率后执行 `python main.py doctor`。

    Args:
        args: 命令行参数，使用 `args.config` 指定配置文件。

    Returns:
        int: 0 全部通过；1 设备或模板目录问题；2 模板缺失。

    Raises:
        None: 预期内的失败都转成日志和返回码。
    """
    config = load_config(args.config)
    logger = setup_logger(config.log_dir)

    logger.info("配置文件: %s", Path(args.config) if args.config else DEFAULT_CONFIG)
    logger.info("adb: %s | serial: %s", config.device.adb, config.device.serial)

    try:
        device = build_device(config)
        size = check_screen_size(config, device, logger)
        logger.info("设备在线，分辨率 %dx%d", size[0], size[1])
        frame = device.screencap()
        logger.info("截图正常，画面 %dx%d", frame.shape[1], frame.shape[0])
    except DeviceError as exc:
        logger.error("设备检查失败: %s", exc)
        return 1

    logger.info("模板目录: %s", config.template_dir)
    if not config.template_dir.exists():
        logger.warning("模板目录不存在，请先创建并放入模板图")
        return 1

    templates = sorted(p.name for p in config.template_dir.glob("*.png"))
    logger.info("已有模板: %d 张", len(templates))

    missing = [name for name in tata_daily.REQUIRED_TEMPLATES if not (config.template_dir / (name + ".png")).exists()]
    if missing:
        logger.warning("日常任务缺少模板: %s", ", ".join(missing))
        logger.warning("先运行 `python main.py capture` 截图，裁剪后按同名放到模板目录")
        return 2

    logger.info("环境检查通过，可以运行: python main.py run --task daily")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """截取当前画面并保存到 logs/captures/。

    使用场景：制作模板图前执行 `python main.py capture`，再用图像工具从截图里
    裁剪按钮。

    Args:
        args: 命令行参数，使用 `args.config` 指定配置文件。

    Returns:
        int: 0 成功；1 设备不可用或截图失败。

    Raises:
        None: 失败转成日志和返回码。
    """
    config = load_config(args.config)
    logger = setup_logger(config.log_dir)

    try:
        device = build_device(config)
        frame = device.screencap()
    except DeviceError as exc:
        logger.error("截图失败: %s", exc)
        return 1

    out_dir = config.log_dir / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s.png" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    imageio.imwrite(path, frame)
    logger.info("截图已保存: %s (%dx%d)", path, frame.shape[1], frame.shape[0])
    logger.info("用画图/截图工具从这张图里裁剪按钮，保存为 PNG 放进 %s", config.template_dir)
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    """列出模板目录内容并标记任务所需的缺失项。

    使用场景：裁剪完模板后确认命名与是否齐全，执行
    `python main.py templates`。

    Args:
        args: 命令行参数，使用 `args.config` 指定配置文件。

    Returns:
        int: 0 列目录成功；1 模板目录不存在。

    Raises:
        None。
    """
    config = load_config(args.config)
    logger = setup_logger(config.log_dir)

    if not config.template_dir.exists():
        logger.warning("模板目录不存在: %s", config.template_dir)
        return 1

    existing = sorted(p.stem for p in config.template_dir.glob("*.png"))
    logger.info("模板目录: %s", config.template_dir)
    logger.info("现有模板 (%d): %s", len(existing), ", ".join(existing) or "无")

    for name in tata_daily.REQUIRED_TEMPLATES:
        mark = "OK  " if name in existing else "MISS"
        logger.info("[%s] %s", mark, name)

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """连接设备并执行指定任务（纯 CLI 模式，无监控界面）。

    使用场景：定时任务、无人值守运行等不需要实时看画面的场景。

    Args:
        args: 命令行参数，`args.config` 指定配置，`args.task` 指定任务名。

    Returns:
        int: 0 成功；1 设备异常；2 模板缺失；3 任务中断；130 手动停止。

    Raises:
        None: 所有预期异常都会转成日志和返回码。
    """
    config = load_config(args.config)
    logger = setup_logger(config.log_dir)
    task = TASKS[args.task]

    try:
        device = build_device(config)
        check_screen_size(config, device, logger)
        flow = build_flow(config, device, logger)
        task.run(flow)
    except TemplateMissing as exc:
        logger.error("模板缺失: %s", exc)
        return 2
    except DeviceError as exc:
        logger.error("设备异常: %s", exc)
        return 1
    except RuntimeError as exc:
        logger.error("任务中断: %s", exc)
        return 3
    except KeyboardInterrupt:
        logger.warning("已手动停止")
        return 130

    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """打开可视化监控窗口。

    使用场景：需要实时查看模拟器画面、观察自动任务执行过程，或直接在界面里
    启动/停止任务时执行 `python main.py monitor`。

    Args:
        args: 命令行参数，其中 `args.config` 指定配置文件路径。

    Returns:
        int: 窗口正常关闭返回 0，Tk 不可用返回 1，手动中断返回 130。

    Raises:
        None: 模拟器未启动不会阻断窗口，界面会持续尝试重连并显示离线状态。
    """
    config = load_config(args.config)
    logger = setup_logger(config.log_dir)

    # 监控模式下不预连接设备：MuMu 尚未启动时也应该能打开窗口，
    # 由 FrameSource 的后台线程负责持续重连。
    device = Device(
        adb=config.device.adb,
        serial=config.device.serial,
        screenshot_retry=config.device.screenshot_retry,
    )

    try:
        from monitor_gui import MonitorApp
    except ImportError as exc:
        logger.error("无法加载监控窗口（需要 Tkinter）: %s", exc)
        return 1

    app = MonitorApp(
        device=device,
        logger=logger,
        tasks=TASKS,
        flow_factory=lambda **kwargs: build_flow(config, device, logger, **kwargs),
    )
    try:
        return app.run()
    except KeyboardInterrupt:
        logger.warning("已手动关闭监控窗口")
        return 130


def main(argv: Optional[List[str]] = None) -> int:
    """解析命令行参数并分派到对应子命令。

    使用场景：`python main.py <command>` 的入口，也便于测试时传入 argv。

    Args:
        argv: 命令行参数列表；None 表示读取 `sys.argv`。

    Returns:
        int: 子命令的退出码；未指定子命令时打印帮助并返回 0。

    Raises:
        SystemExit: argparse 在参数非法或请求帮助时抛出。
    """
    parser = argparse.ArgumentParser(
        prog="game-auto",
        description="MuMu 模拟器 + 2D 手游日常任务自动化骨架",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认 config.yaml",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="检查设备、分辨率与模板")
    subparsers.add_parser("capture", help="截取当前画面用于制作模板")
    subparsers.add_parser("templates", help="列出模板与缺失项")
    subparsers.add_parser("monitor", help="打开实时画面 + 当前操作监控窗口")

    run_parser = subparsers.add_parser("run", help="执行指定任务")
    run_parser.add_argument(
        "--task",
        default="daily",
        choices=sorted(TASKS.keys()),
        help="任务名，目前支持: %s" % ", ".join(sorted(TASKS.keys())),
    )

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "doctor": cmd_doctor,
        "capture": cmd_capture,
        "templates": cmd_templates,
        "run": cmd_run,
        "monitor": cmd_monitor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
