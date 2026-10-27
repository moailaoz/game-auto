"""塔塔冒险队 · 每日流程示例。

说明：
- 所有模板名都是占位，需要你用自己的游戏截图裁剪后放到 assets/templates/tata/。
- 缺少模板时 Vision 会抛出 TemplateMissing，不会静默乱点，避免误操作。
- 结构上只做三件事：找到入口 → 执行领取 → 回到主界面。改成别的游戏只需改这个文件。
"""
from __future__ import annotations

from typing import List

from core.flow import Flow

# doctor 命令会检查这些模板是否齐全
REQUIRED_TEMPLATES: List[str] = [
    "home_marker",      # 主界面特征（例如底部"冒险"按钮），用来确认当前在哪
    "btn_mail",         # 邮件入口
    "btn_claim_all",    # 一键领取
    "btn_close",        # 通用关闭按钮
    "btn_sign_in",      # 签到入口
    "btn_daily",        # 日常/任务入口
]

# 单步等待上限（秒）；加载慢的机型可以调大
STEP_TIMEOUT = 20.0


def run(flow: Flow) -> None:
    """执行塔塔冒险队的日常任务。

    使用场景：由 `main.py run --task daily` 或监控界面的"开始任务"调用。

    Args:
        flow: 已装配好的流程对象。

    Returns:
        None。

    Raises:
        RuntimeError: 长时间识别不到主界面时。
        FlowStopped: 监控界面点了"停止"时。
        TemplateMissing: 模板图缺失时。
    """
    flow.logger.info("== 日常任务开始 ==")
    flow.step("确认当前是否在主界面")

    if flow.wait("home_marker", timeout=60) is None:
        flow.save_failure("no_home")
        raise RuntimeError(
            "没有识别到主界面：确认游戏已启动、分辨率与模板一致，"
            "并且 home_marker.png 截取的是稳定不变的界面元素"
        )

    _claim_mail(flow)
    _sign_in(flow)
    _daily_rewards(flow)

    flow.logger.info("== 日常任务结束 ==")


def _claim_mail(flow: Flow) -> None:
    """邮件：进入 → 一键领取（可能有多封）→ 关闭。

    使用场景：`run` 在确认主界面后调用。

    Args:
        flow: 已装配好的流程对象。

    Returns:
        None。

    Raises:
        FlowStopped: 收到停止信号时。
        TemplateMissing: 模板图缺失时。
    """
    flow.step("处理邮件奖励")
    if not flow.click("btn_mail", timeout=STEP_TIMEOUT):
        flow.logger.warning("未找到邮件入口，跳过")
        return

    for _ in range(3):
        if not flow.click_if_exists("btn_claim_all"):
            break

    flow.click_if_exists("btn_close")
    flow.wait("home_marker", timeout=STEP_TIMEOUT)


def _sign_in(flow: Flow) -> None:
    """签到：进入 → 关闭签到弹窗。

    使用场景：`run` 在邮件处理完成后调用。

    Args:
        flow: 已装配好的流程对象。

    Returns:
        None。

    Raises:
        FlowStopped: 收到停止信号时。
        TemplateMissing: 模板图缺失时。
    """
    flow.step("处理每日签到")
    if not flow.click("btn_sign_in", timeout=STEP_TIMEOUT):
        flow.logger.warning("未找到签到入口，跳过")
        return

    flow.click_if_exists("btn_claim_all")
    flow.click_if_exists("btn_close")
    flow.wait("home_marker", timeout=STEP_TIMEOUT)


def _daily_rewards(flow: Flow) -> None:
    """日常：进入列表 → 点第一项 → 领取 → 返回主界面。

    使用场景：`run` 在签到处理完成后调用。

    Args:
        flow: 已装配好的流程对象。

    Returns:
        None。

    Raises:
        FlowStopped: 收到停止信号时。
        TemplateMissing: 模板图缺失时。
    """
    flow.step("处理日常任务奖励")
    if not flow.click("btn_daily", timeout=STEP_TIMEOUT):
        flow.logger.warning("未找到日常入口，跳过")
        return

    # 列表第一项没有稳定特征时，用相对坐标点击（1280x720 下约为 x=640, y=252）
    flow.tap_ratio(0.5, 0.35)
    flow.click_if_exists("btn_claim_all")
    flow.click_if_exists("btn_close")

    flow.back()
    flow.wait("home_marker", timeout=STEP_TIMEOUT)
