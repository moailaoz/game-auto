"""模板匹配：在截图中定位 assets/templates 下的按钮、图标、界面特征。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import imageio


class TemplateMissing(FileNotFoundError):
    """模板图不存在或无法读取。"""


@dataclass
class Match:
    """一次匹配结果，center 是模板中心点（可直接点击）。"""

    name: str
    score: float
    center: Tuple[int, int]
    box: Tuple[int, int, int, int]  # x, y, w, h

    @property
    def x(self) -> int:
        return self.center[0]

    @property
    def y(self) -> int:
        return self.center[1]


class Vision:
    def __init__(self, template_dir: Path, threshold: float = 0.85) -> None:
        self.template_dir = Path(template_dir)
        self.threshold = threshold
        self._cache: Dict[str, np.ndarray] = {}

    # ---------- 模板 ----------
    def path_of(self, name: str) -> Path:
        filename = name if name.lower().endswith(".png") else name + ".png"
        return self.template_dir / filename

    def has(self, name: str) -> bool:
        return self.path_of(name).exists()

    def _template(self, name: str) -> np.ndarray:
        if name not in self._cache:
            path = self.path_of(name)
            if not path.exists():
                raise TemplateMissing(
                    "缺少模板图: %s\n运行 `python main.py capture` 截图后裁剪并放到该目录"
                    % path
                )
            image = imageio.imread(path)
            if image is None:
                raise TemplateMissing("模板图无法读取（需要 PNG）: %s" % path)
            self._cache[name] = image
        return self._cache[name]

    def clear_cache(self) -> None:
        """换过模板图后调用，避免用到旧文件。"""
        self._cache.clear()

    # ---------- 匹配 ----------
    def find(
        self, frame: np.ndarray, name: str, threshold: Optional[float] = None
    ) -> Optional[Match]:
        template = self._template(name)
        top, left = template.shape[:2]
        height, width = frame.shape[:2]
        if top > height or left > width:
            return None

        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        limit = self.threshold if threshold is None else threshold
        if score < limit:
            return None

        x, y = location
        return Match(
            name=name,
            score=float(score),
            center=(x + left // 2, y + top // 2),
            box=(x, y, left, top),
        )

    def find_all(
        self,
        frame: np.ndarray,
        name: str,
        threshold: Optional[float] = None,
        min_gap: float = 40.0,
    ) -> List[Match]:
        """找出画面中所有同名目标（列表项、多个相同按钮）。"""
        template = self._template(name)
        top, left = template.shape[:2]
        height, width = frame.shape[:2]
        if top > height or left > width:
            return []

        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        limit = self.threshold if threshold is None else threshold
        ys, xs = np.where(result >= limit)

        candidates = sorted(
            ((float(result[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
            reverse=True,
        )

        matches: List[Match] = []
        for score, x, y in candidates:
            cx, cy = x + left // 2, y + top // 2
            if all(
                ((cx - match.center[0]) ** 2 + (cy - match.center[1]) ** 2) ** 0.5
                >= min_gap
                for match in matches
            ):
                matches.append(
                    Match(
                        name=name,
                        score=score,
                        center=(cx, cy),
                        box=(x, y, left, top),
                    )
                )
        return matches
