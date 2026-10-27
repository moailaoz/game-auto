"""日志：控制台 + 按天文件，并提供失败截图存档。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from . import imageio

LOGGER_NAME = "game-auto"


def setup_logger(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(
        log_dir / ("%s.log" % datetime.now().strftime("%Y%m%d")), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def save_frame(frame: np.ndarray, log_dir: Path, tag: str) -> Path:
    """保存一帧画面到 logs/failures，方便回看失败现场。"""
    name = "%s_%s.png" % (datetime.now().strftime("%Y%m%d-%H%M%S"), tag)
    return imageio.imwrite(Path(log_dir) / "failures" / name, frame)
