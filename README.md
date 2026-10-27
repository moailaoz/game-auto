# game-auto

面向 **MuMu 模拟器 + 2D 手游** 的日常任务自动化骨架，当前示例任务为 **塔塔冒险队** 的每日流程。

核心链路只有一条：

```text
adb 截屏 → 模板匹配定位 UI → 状态机判断当前界面 → 模拟点击/滑动 → 再次截屏校验
```

不做内存读写、不做进程注入、不使用控件树（游戏画面本来也不暴露 Android 控件），所有操作都停留在"屏幕级"。

---

## 1. 为什么是这套技术栈

| 方案 | 是否适用 | 原因 |
| --- | --- | --- |
| **Python + ADB + OpenCV 模板匹配**（本项目） | 适用 | 游戏只有像素，没有控件树；模板匹配能"看见"按钮、弹窗、界面状态，改动成本最低 |
| Appium / uiautomator2 | 不适用 | 依赖 Android 无障碍控件树，游戏画面里的按钮不会出现在控件树中 |
| 按键精灵 / 固定坐标脚本 | 勉强 | 分辨率一变、界面一改版就全废，且无法判断"当前是不是我以为的界面" |
| Airtest | 适合探路 | 网易出品、和 MuMu 同厂，IDE 录制上手快；但多任务编排、异常处理不如自己写状态机灵活 |
| YOLO 目标检测 | 后期可选 | 元素种类特别多、外观变化大时才值得训练模型，前期模板匹配足够 |

依赖保持在最小集：`opencv-python` + `numpy` + `PyYAML`。

---

## 2. 架构分层

```text
┌─────────────────────────────────────────────────────────┐
│ 入口层  main.py                CLI：doctor / capture /  │
│                                templates / run / monitor│
├─────────────────────────────────────────────────────────┤
│ 监控层  monitor_gui.py         实时画面 + 当前操作窗口  │
│         core/monitor.py        事件总线（步骤/识别/动作）│
│         core/frames.py         后台截屏、缓存与断线重连  │
├─────────────────────────────────────────────────────────┤
│ 编排层  tasks/*.py             每个游戏的业务步骤        │
│         core/flow.py           截图→识别→动作 的循环封装 │
├─────────────────────────────────────────────────────────┤
│ 感知层  core/vision.py         模板匹配，输出可点击坐标  │
├─────────────────────────────────────────────────────────┤
│ 动作层  core/actions.py        点击/滑动/返回 + 随机抖动 │
├─────────────────────────────────────────────────────────┤
│ 通道层  core/device.py         ADB 连接、shell、截屏     │
├─────────────────────────────────────────────────────────┤
│ 支撑层  core/config.py         配置加载                  │
│         core/logger.py         日志 + 失败截图           │
│         core/imageio.py        中文路径安全的图片读写    │
└─────────────────────────────────────────────────────────┘
              MuMu 模拟器（127.0.0.1:16384）
```

设计原则：

- **一层只做一件事**：`device` 只管 ADB，`vision` 只管找图，`actions` 只管动手，`flow` 只管循环，`tasks` 只写业务。
- **模板缺失即报错**：找不到模板图会抛出 `TemplateMissing`，不会带着错误坐标乱点。
- **失败必留现场**：超时/异常时把当前画面存到 `logs/failures/`，方便回看是哪一步不对。

---

## 3. 目录与文件说明

```text
game-auto/
├─ main.py                       CLI 入口，装配依赖并分发子命令
├─ monitor_gui.py                可视化监控窗口（Tkinter，实时画面 + 当前操作）
├─ config.yaml                   设备/识别/动作/日志配置
├─ requirements.txt              依赖（已兼容 Python 3.8）
├─ README.md                     本文件
│
├─ core/
│  ├─ __init__.py                包声明
│  ├─ device.py                  ADB 通道：connect / shell / screencap / tap / swipe / back
│  ├─ vision.py                  模板匹配：find（单个）/ find_all（多个），带缓存
│  ├─ actions.py                 动作封装：点击抖动、按比例点击/滑动、返回、动作后等待
│  ├─ flow.py                    流程封装：wait / click / click_if_exists / wait_gone / save_failure
│  ├─ monitor.py                 监控事件总线：当前操作、识别框、点击落点
│  ├─ frames.py                  后台截屏：缓存最新帧、供界面与任务共用、断线重连
│  ├─ config.py                  读取 config.yaml → 带默认值的配置对象
│  ├─ logger.py                  控制台+文件日志、失败截图落盘
│  └─ imageio.py                 中文路径安全的 PNG 读写（规避 cv2 的 Windows 路径问题）
│
├─ tasks/
│  ├─ __init__.py                包声明
│  └─ tata_daily.py              塔塔冒险队日常流程示例（邮件/签到/日常 + 模板清单）
│
├─ assets/
│  └─ templates/
│     └─ tata/                   塔塔冒险队的模板图目录（README.txt 是截图规范）
│
└─ logs/                         运行时生成
   ├─ YYYYMMDD.log               当天日志
   ├─ captures/                  `capture` 命令截的图，用来裁剪模板
   └─ failures/                  失败现场截图
```

关键文件各自的职责边界：

| 文件 | 负责 | 不负责 |
| --- | --- | --- |
| `core/device.py` | 建立 ADB 连接、执行 shell、取屏幕尺寸、截图、原始输入事件 | 图像识别、业务判断 |
| `core/vision.py` | 加载模板、`cv2.matchTemplate`、返回中心点坐标 | 点击 |
| `core/actions.py` | 坐标抖动、点击/滑动/返回、动作后延时 | 找目标 |
| `core/flow.py` | 轮询等待、条件点击、超时处理、失败截图 | 具体游戏步骤 |
| `tasks/tata_daily.py` | "先点邮件、再签到、再日常"这类业务顺序 | ADB、图像算法细节 |
| `core/monitor.py` | 发布"当前操作/识别结果/动作坐标"事件 | 画界面、执行任务 |
| `core/frames.py` | 后台截屏、缓存最新帧、断线重连 | 业务判断 |
| `monitor_gui.py` | 实时画面渲染、操作文字与日志展示、任务启停 | 找图、点击 |
| `main.py` | 组装对象、参数解析、退出码 | 业务逻辑 |

---

## 4. 环境准备

### 4.1 Python 依赖

需要 Python 3.8 及以上（本机 3.8.10 可直接用）：

```powershell
python -m pip install -r requirements.txt
# 国内网络下载慢时加镜像：
python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

### 4.2 MuMu 设置

1. 启动 MuMu 模拟器并**进入游戏到主界面**。
2. 打开 MuMu 设置 → 其他 → 确认 **ADB 调试** 已开启（MuMu 默认允许本机连接）。
3. 固定分辨率（推荐 `1280x720`），之后**不要随意改动**——模板图与分辨率强绑定。
4. 关闭可能遮挡画面的悬浮窗、录屏按钮等。

### 4.3 连接端口与 adb 路径

- MuMu 第一个实例默认 `127.0.0.1:16384`，多开实例端口递增（实例 1 是 `16416`）。端口也可以从实例配置文件里的 `nat.port_forward.adb.host_port` 确认。
- MuMu 自带 adb，本机（MuMu 15 / Android 15）的实际路径是：

```yaml
device:
  adb: "D:/Program Files/Netease/MuMu/nx_device/15.0/shell/adb.exe"
  serial: "127.0.0.1:16384"
```

换机器或换 MuMu 版本时，到安装目录下找 `nx_device\<版本>\shell\adb.exe`；`MuMuManager.exe` 在同级的 `nx_main\` 下，可用 `MuMuManager.exe info -v 0` 查看实例状态与端口。

> 如果 `adb devices` 显示 `offline`，先执行 `adb kill-server` 再重试。
> 如果提示 `adb server version doesn't match`，说明系统里还有另一个 adb 在跑，统一改用 MuMu 自带的 adb 即可。

---

## 5. 快速开始（四步跑通）

在 `game-auto/` 目录下执行：

```powershell
# 1. 环境自检：设备、分辨率、模板是否齐全
python main.py doctor

# 2. 截一张当前画面，保存到 logs/captures/
python main.py capture

# 3. 用画图/PS 从截图里裁剪按钮 → 存成 PNG 放到 assets/templates/tata/
#    再用下面的命令确认模板是否齐了
python main.py templates

# 4. 执行日常任务
python main.py run --task daily

# 5. 打开可视化监控窗口（实时画面 + 当前操作）
python main.py monitor
```

`doctor` 的返回码：`0` 通过、`1` 设备问题、`2` 模板缺失。
`run` 的返回码：`0` 成功、`1` 设备异常、`2` 模板缺失、`3` 任务中断、`130` 手动停止。

### 5.1 监控窗口

`python main.py monitor` 打开一个 Tkinter 窗口，用来"看着机器人操作"：

- **实时镜像**：后台线程持续截屏，画面约 5~8 帧/秒，窗口缩放时自动保持宽高比。
- **当前操作**：显示任务此刻在做什么（如"点击 btn_mail""等待 home_marker 出现"）。
- **叠加标记**：绿色框是最近一次模板命中（带模板名与分数），橙色圆圈/箭头是最近 2 秒内的点击与滑动落点。
- **连接状态**：顶部显示在线/离线、分辨率与帧率。MuMu 没启动也可以先开窗口，模拟器启动后会自动重连出画面。
- **界面控任务**：顶部选任务名点"开始任务"即可执行；"停止"是协作式的，会在当前等待/轮询结束后退出，不会强杀 adb 命令。
- 窗口日志区与 `logs/YYYYMMDD.log` 同步输出。

> 监控窗口与 `python main.py run` 不要同时运行：两者会争抢同一路 adb 截图，画面会互相拖慢。

---

## 6. 模板图制作规范

模板匹配的准确率 90% 取决于模板质量，而不是代码。

- **同一分辨率**：必须用 `python main.py capture` 截的图（或同一模拟器同分辨率截图）裁剪，不要用缩放过的图。
- **只截稳定元素**：图标 + 文字；避开动画帧、倒计时、数量角标、半透明蒙层。
- **尺寸适中**：建议不小于 `20x20` 像素；太小会误匹配，太大容易受背景变化影响。
- **元素唯一**：同一张模板在画面里只应对应一个目标；要找列表里的多个相同项时用 `vision.find_all`。
- **命名即引用**：`btn_mail.png` 在代码里写作 `"btn_mail"`。
- **阈值**：默认 `0.85`。日志里会打印命中分数，稳定在 `0.95+` 说明模板很干净；刚好 `0.86` 说明该重截了。

---

## 7. 编写自己的任务

在 `tasks/` 下新建文件，实现 `run(flow)`，再在 `main.py` 的 `TASKS` 里注册即可。

```python
# tasks/my_game_daily.py
from core.flow import Flow

REQUIRED_TEMPLATES = ["home_marker", "btn_task"]   # doctor 会检查

def run(flow: Flow) -> None:
    if not flow.click("btn_task", timeout=20):
        flow.save_failure("no_task_entry")
        return

    flow.tap_ratio(0.5, 0.35)          # 列表第一项，按屏幕比例点
    flow.swipe_ratio((0.5, 0.8), (0.5, 0.3))  # 上滑一屏
    flow.click_if_exists("btn_claim_all")
    flow.back()
```

然后在 `main.py` 里注册：

```python
from tasks import my_game_daily
TASKS = {"daily": tata_daily, "my_game": my_game_daily}
```

### Flow API 速查

| 方法 | 说明 |
| --- | --- |
| `flow.wait(name, timeout)` | 轮询等待目标出现，返回 `Match` 或 `None` |
| `flow.click(name, timeout)` | 等待并点击，返回是否成功 |
| `flow.click_if_exists(name)` | 存在就点，不存在返回 `False` |
| `flow.exists(name)` | 当前画面是否存在 |
| `flow.wait_gone(name, timeout)` | 等待加载弹窗消失 |
| `flow.tap_ratio(rx, ry)` | 按屏幕比例点击（0~1） |
| `flow.swipe_ratio(start, end, duration_ms)` | 按屏幕比例滑动 |
| `flow.back(times=1)` | 返回键 |
| `flow.step(text)` | 声明当前操作，写入日志并推送到监控窗口 |
| `flow.save_failure(tag)` | 保存当前画面到 `logs/failures/` |

`Match` 对象带 `x` / `y`（中心点）、`score`（匹配分数）、`box`（左上角+宽高）。

---

## 8. 常见问题

| 现象 | 排查方向 |
| --- | --- |
| `设备未上线` | MuMu 是否开机；`config.yaml` 的 serial 端口是否正确；设置里 ADB 调试是否开启 |
| `adb devices` 显示 offline | `adb kill-server` 后重连；MuMu 重启后再试 |
| 截图全黑 / 解码失败 | 部分版本需改用 `adb shell screencap -p /sdcard/s.png` + `adb pull`；确认模拟器不是最小化状态 |
| 匹配分数低（0.7~0.8） | 分辨率不一致、模板含动态内容、游戏画质/UI 缩放设置变了 |
| 点到了但没反应 | `tap_delay` 太小（动画未结束）；坐标被 `screen` 缩放；按钮有防误触延迟 |
| 一直等到超时 | 模板截的是错误界面；或者该步骤需要先上滑/切页 |
| 中文路径报错 | 项目已统一用 `core/imageio.py` 读写图片，若自己新增图片读写请复用它 |
| 日志在哪 | `logs/YYYYMMDD.log`；失败现场在 `logs/failures/` |

调参优先级：先查分辨率 → 再重截模板 → 最后才调 `threshold`。把 `threshold` 降到 `0.7` 以下通常意味着模板该重做了。

---

## 9. 当前边界

- 单设备、单进程串行执行，没有多开并行调度。
- 监控窗口是 Tkinter 逐帧刷图（约 5~8 帧/秒），不是视频流；需要更流畅可改用 scrcpy。
- 模板不支持缩放，换分辨率需要重新截图。
- 没有 OCR。需要读体力、次数、剩余时间时，安装 `rapidocr-onnxruntime` 并在 `core/vision.py` 里扩展一个 `read_text` 方法。
- 任务流程没有断线重连和重试队列，异常直接中断并留现场截图（这是刻意保持简单）；监控窗口的截屏线程会自行重连设备。

---

## 10. 风险与合规

- 这类自动化通常与游戏的用户协议/反外挂策略存在冲突，账号风险由使用者自行承担。
- 本项目**只做屏幕级模拟点击**：不读写游戏内存、不注入进程、不修改客户端、不抓包改包。
- 请勿用于账号交易、批量刷取、工作室牟利等场景。
