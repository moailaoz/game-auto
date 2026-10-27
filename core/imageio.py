"""图片读写工具。

Windows 下路径可能包含中文，cv2.imread / cv2.imwrite 对此不可靠，
统一改用 numpy + 编解码的方式读写。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np


def imread(path: Path) -> Optional[np.ndarray]:
    """读取 PNG/JPG，失败返回 None。"""
    path = Path(path)
    if not path.exists():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite(path: Path, image: np.ndarray) -> Path:
    """写入 PNG，自动创建父目录。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise OSError("图片编码失败: %s" % path)
    buffer.tofile(str(path))
    return path
