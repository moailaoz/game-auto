"""监控事件总线：把任务运行中的"当前操作"广播给可视化界面。

使用场景：
- `Flow` 每次等待/点击/滑动前发布"当前操作"文本；
- `Actions` 每次真正落点时发布点击/滑动/返回的坐标；
- `vision` 命中模板时由 `Flow.find` 发布识别框；
- `monitor_gui.MonitorApp` 订阅这些事件，画到实时画面上并显示文字。

设计约束：
- `emit` 在任务线程中被同步调用，实现必须轻量，不做任何阻塞操作；
- 任一监听器抛异常都会被吞掉，界面故障不能中断任务；
- 没有订阅者时所有调用都退化为空操作，CLI 模式下没有额外开销。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

# 事件类型：与界面展示逻辑一一对应，新增类型时同步更新界面即可
KIND_STEP = "step"      # 当前操作（人类可读文本）
KIND_MATCH = "match"    # 模板命中（给出识别框与分数）
KIND_TAP = "tap"        # 点击（给出实际落点）
KIND_SWIPE = "swipe"    # 滑动（给出起点与终点）
KIND_BACK = "back"      # 返回键


@dataclass(frozen=True)
class Event:
    """一条监控事件。

    Attributes:
        kind: 事件类型，取模块级常量 KIND_*。
        text: 事件说明。STEP 为操作描述，MATCH 为模板名，其余可为空。
        point: 主坐标点。MATCH 为模板中心，TAP 为落点，SWIPE 为起点。
        end_point: 滑动终点，仅 SWIPE 使用。
        box: 识别框 (x, y, w, h)，仅 MATCH 使用。
        score: 模板匹配分数，仅 MATCH 使用。
    """

    kind: str
    text: str = ""
    point: Optional[Tuple[int, int]] = None
    end_point: Optional[Tuple[int, int]] = None
    box: Optional[Tuple[int, int, int, int]] = None
    score: float = 0.0


class Monitor:
    """线程安全的事件发布/订阅总线。

    使用场景：
    - 监控窗口创建实例后 `subscribe` 一个回调，任务侧持有同一实例；
    - `Flow` / `Actions` 在识别和动作时 `emit`，界面端自行决定如何渲染；
    - 单元测试里也可以订阅事件，断言任务的步骤顺序。

    该类不做缓冲和节流：事件即发即弃，避免任务线程被界面拖慢。
    """

    def __init__(self) -> None:
        """初始化一个没有任何订阅者的空总线。"""
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Event], None]] = []

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """注册一个事件监听器。

        Args:
            listener: 接收 `Event` 的回调。会在发布事件的线程中执行，
                因此界面代码必须自行把它转投到 UI 线程（如队列）。

        Returns:
            None。

        Raises:
            TypeError: 当 listener 不是可调用对象时。
        """
        if not callable(listener):
            raise TypeError("listener 必须是可调用对象")
        with self._lock:
            self._listeners.append(listener)

    def emit(self, event: Event) -> None:
        """广播一条事件给所有监听器。

        使用场景：
        - Flow 的等待/点击步骤发布 STEP 事件；
        - Actions 落点后发布 TAP/SWIPE/BACK 事件；
        - Flow.find 命中模板后发布 MATCH 事件。

        Args:
            event: 待广播的事件对象。

        Returns:
            None。

        Raises:
            None: 监听器内部异常会被捕获并忽略，保证任务不被打断。
        """
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - 界面异常绝不能影响任务
                continue
