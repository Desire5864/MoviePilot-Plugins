import base64
import hashlib
import re
import secrets
import threading
import urllib.parse
import urllib3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 关闭 urllib3 的 InsecureRequestWarning，避免每轮检查刷屏
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# SCP 控制面板（Server Control Panel）端点
SCP_BASE = "https://www.servercontrolpanel.de"
SCP_CLIENT_ID = "scp"
SCP_REDIRECT_URI = SCP_BASE + "/scp-ui/reauth-callback.html"
SCP_CORE_API = SCP_BASE + "/scp-core/api"
SCP_TOKEN_URL = SCP_BASE + "/realms/scp/protocol/openid-connect/token"
SCP_AUTH_URL = SCP_BASE + "/realms/scp/protocol/openid-connect/auth"

# 流量历史持久化键名
TRAFFIC_HISTORY_KEY = "traffic_history"
# 流量历史最多保留条数（详情页展示同样取此条数）
TRAFFIC_HISTORY_LIMIT = 10
# 告警状态持久化键名（避免重复通知）
ALERT_STATE_KEY = "alert_state"
# 最近一次查询结果缓存键名（用于详情页秒开）
RESULT_CACHE_KEY = "last_result_cache"
# 打开详情页时缓存的最大容忍年龄（秒）；超过则同步刷新，否则后台刷新
PAGE_CACHE_TTL = 180

# ---------------------------------------------------------------------------
# 高速流量额度与速率档（详情页展示口径，均可在插件配置里覆盖）
# ---------------------------------------------------------------------------
# 高速流量额度（TB/月），按「每台服务器独立」计算
DEFAULT_QUOTA_TB = 120.0
# 额度内的速率（Mbps）
DEFAULT_FAST_MBPS = 2500
# 超出额度后的限速（Mbps）
DEFAULT_SLOW_MBPS = 200
# 进度条满刻度 = 额度 + 溢出可视上限。所有服务器共用同一刻度，
# 这样两台在视觉上可以直接横向比较（额度线是公共参照物）。
OVERFLOW_VISUAL_CAP_TB = 15.0
# 溢出段最小宽度（px）：刚超一点点时按真实比例画几乎看不见，
# 给个下限保证「已经越线」这件事至少看得见（代价：极小幅超额时长度略失真）
OVERFLOW_MIN_WIDTH_PX = 8
# 进度条配色分档：<80% 用主题主色 / 80–100% 橙 / >=100% 红
BAR_COLOR_WARN = "#E08A17"
BAR_COLOR_OVER = "#E52D15"
# 超出额度部分的斜纹纹理
OVERFLOW_HATCH = "repeating-linear-gradient(45deg, #C0392B 0 3px, #7E1B0F 3px 6px)"
# 语义色（卡片右下角的「剩余 / 已超」）
COLOR_TAIL_OK = "#2E7D32"
COLOR_TAIL_BAD = "#C0392B"
# 卡片里「标签 / 取值」两级文字色。一律用带透明度的**颜色**而不是 opacity：
# opacity 按子树整体合成，挂在容器上会把同级加粗的数值一起压暗（加粗就白加了）。
COLOR_FIELD_LABEL = "rgba(var(--v-theme-on-surface), 0.62)"
# 机器名（v2202… 这种长串）
COLOR_FIELD_NAME = "rgba(var(--v-theme-on-surface), 0.80)"
# 真正要看清的取值（账号 / 速率档数字）
COLOR_FIELD_VALUE = "rgba(var(--v-theme-on-surface), 0.94)"

# ---------------------------------------------------------------------------
# 计费周期
# ---------------------------------------------------------------------------
# 流量按「计费月」计，每月 25 日 00:00（站点时区）服务端把
# rxMonthlyInMiB / txMonthlyInMiB 清零重新累计。
#
# 🔴 SCP 的 `/scp-core/api` 不返回任何「周期 / 重置日」字段（2026-09-28 实测
#    serverLiveInfo.interfaces 里只有 rxMonthlyInMiB / txMonthlyInMiB /
#    speedInMBits / trafficThrottled 等），所以重置日是本地按这个常量推算的，
#    不是从接口读的。也正因为计数在服务端清零，插件**不需要自己重置任何数据**，
#    只要按同一条规则切分历史即可。
DEFAULT_BILLING_RESET_DAY = 25


def billing_period(reset_day: int = DEFAULT_BILLING_RESET_DAY, now: Optional[datetime] = None) -> Dict[str, Any]:
    """推算 `now` 所处的计费周期（每月 `reset_day` 日 00:00 重置）。

    :param reset_day: 每月重置日；<=0 或非数字回落默认值，>28 夹到 28
    :param now: 参照时刻，留空取当前时间
    :return: dict ——
        ``key`` 周期标识（用起始日 ``"2026-09-25"``，比「按结束月命名」少一层误读）；
        ``label`` 起止（``"09-25 ~ 10-24"``）；``start`` / ``next_reset`` 起止日期；
        ``day_index`` 今天第几天；``days_total`` 本周期共几天；``days_left`` 距重置几天。
    """
    now = now or datetime.now()
    try:
        day = DEFAULT_BILLING_RESET_DAY if reset_day is None else int(reset_day)
    except (TypeError, ValueError):
        day = DEFAULT_BILLING_RESET_DAY
    # 与其他数值配置项同口径：非正数回落默认，再夹到 28（29–31 会让部分月份对不上）
    if day <= 0:
        day = DEFAULT_BILLING_RESET_DAY
    day = min(day, 28)

    # 本周期起点：本月的重置日；若今天还没到重置日，则起点在上个月
    if now.day >= day:
        start = datetime(now.year, now.month, day)
    else:
        prev_month_end = datetime(now.year, now.month, 1) - timedelta(days=1)
        start = datetime(prev_month_end.year, prev_month_end.month, day)

    # 下一个重置日：起点 + 1 个月（用「当月 1 日 ± 月数」避免 31 日溢出）
    if start.month == 12:
        next_reset = datetime(start.year + 1, 1, day)
    else:
        next_reset = datetime(start.year, start.month + 1, day)

    day_index = (now.date() - start.date()).days + 1
    days_total = (next_reset.date() - start.date()).days
    days_left = (next_reset.date() - now.date()).days
    return {
        "key": start.strftime("%Y-%m-%d"),
        "label": f"{start.strftime('%m-%d')} ~ "
                 f"{(next_reset - timedelta(days=1)).strftime('%m-%d')}",
        "start": start,
        "next_reset": next_reset,
        "day_index": day_index,
        "days_total": days_total,
        "days_left": days_left,
    }


def _traffic_tone(pct: float) -> str:
    """按占比返回进度条 / 状态点的颜色。"""
    if pct >= 100.0:
        return BAR_COLOR_OVER
    if pct >= 80.0:
        return BAR_COLOR_WARN
    return "rgb(var(--v-theme-primary))"


class ScpTrafficMonitor(_PluginBase):
    """Server Control Panel（servercontrolpanel.de）流量监控插件。

    定时登录 SCP 控制面板，抓取每台服务器本计费月已用流量（Traffic current month），
    统一换算为 TB 展示（GiB ÷ 1024），超过设定阈值时发送通知提醒。

    计费月定义：每月 25 日 00:00 服务端清零重新累计（见 ``DEFAULT_BILLING_RESET_DAY``）。
    """

    # 插件名称
    plugin_name = "SCP流量监控"
    # 插件描述
    plugin_desc = "监控Server Control Panel服务器计费月流量，按TB展示，超阈值时通知。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.4.1"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "scptrafficmonitor_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _username: str = ""
    _password: str = ""
    _username2: str = ""
    _password2: str = ""
    _proxy: bool = False
    _interval_value: int = 6
    _interval_unit: str = "hours"
    # 流量告警阈值（TB），0 表示不告警
    _threshold: float = 0.0
    # 高速流量额度（TB/月，每台独立）
    _quota_tb: float = DEFAULT_QUOTA_TB
    # 额度内速率（Mbps）
    _fast_mbps: float = DEFAULT_FAST_MBPS
    # 超出额度后的限速（Mbps）
    _slow_mbps: float = DEFAULT_SLOW_MBPS
    # 计费月重置日（每月第几天 00:00 清零）
    _reset_day: int = DEFAULT_BILLING_RESET_DAY
    # 最近一次流量查询结果
    _last_result: Optional[Dict[str, Any]] = None
    # 最近一次查询时间
    _last_check_time: Optional[str] = None
    # 最近一次查询错误
    _last_error: str = ""
    # 告警状态（避免重复通知）
    _alerting: bool = False
    # 后台刷新中标记（防止线程堆积）
    _refreshing: bool = False

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()

        self._enabled = False
        self._notify = False
        self._username = ""
        self._password = ""
        self._username2 = ""
        self._password2 = ""
        self._proxy = False
        self._interval_value = 6
        self._interval_unit = "hours"
        self._threshold = 0.0
        self._quota_tb = DEFAULT_QUOTA_TB
        self._fast_mbps = DEFAULT_FAST_MBPS
        self._slow_mbps = DEFAULT_SLOW_MBPS
        self._reset_day = DEFAULT_BILLING_RESET_DAY
        self._last_result = None
        self._last_check_time = None
        self._last_error = ""
        self._alerting = False
        self._refreshing = False

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._username2 = str(config.get("username2") or "").strip()
        self._password2 = str(config.get("password2") or "")
        self._proxy = bool(config.get("proxy"))
        try:
            self._interval_value = max(1, int(config.get("interval_value") or 6))
        except (TypeError, ValueError):
            self._interval_value = 6
        interval_unit = str(config.get("interval_unit") or "hours")
        self._interval_unit = interval_unit if interval_unit in ("minutes", "hours") else "hours"
        try:
            raw_threshold = config.get("threshold")
            self._threshold = max(0.0, float(raw_threshold)) if raw_threshold not in (None, "") else 0.0
        except (TypeError, ValueError):
            self._threshold = 0.0

        # 额度 / 速率档：非正数或非法值一律回落到默认
        def _pos_float(key: str, default: float) -> float:
            try:
                raw = config.get(key)
                val = float(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default
            return val if val > 0 else default

        self._quota_tb = _pos_float("quota_tb", DEFAULT_QUOTA_TB)
        self._fast_mbps = _pos_float("fast_mbps", DEFAULT_FAST_MBPS)
        self._slow_mbps = _pos_float("slow_mbps", DEFAULT_SLOW_MBPS)

        # 计费月重置日：非正数回落默认、>28 夹到 28（与 billing_period 同口径）
        try:
            raw_day = config.get("reset_day")
            day = int(float(raw_day)) if raw_day not in (None, "") else DEFAULT_BILLING_RESET_DAY
        except (TypeError, ValueError):
            day = DEFAULT_BILLING_RESET_DAY
        self._reset_day = DEFAULT_BILLING_RESET_DAY if day <= 0 else min(day, 28)

        # 恢复告警状态，避免重启后重复通知
        alert_state = self.get_data(ALERT_STATE_KEY) or {}
        self._alerting = bool(alert_state.get("alerting"))

        # 恢复最近一次查询结果缓存，用于详情页秒开
        cached = self.get_data(RESULT_CACHE_KEY) or {}
        if cached.get("result"):
            self._last_result = cached.get("result")
            self._last_check_time = cached.get("check_time")
            self._last_error = cached.get("error") or ""

        # 立即执行一次：执行后自动关闭开关
        if config.get("run_once"):
            logger.info("SCP流量监控：立即执行一次检查")
            self.check_traffic()
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "username": self._username,
                    "password": self._password,
                    "username2": self._username2,
                    "password2": self._password2,
                    "proxy": self._proxy,
                    "interval_value": self._interval_value,
                    "interval_unit": self._interval_unit,
                    "threshold": self._threshold,
                    "quota_tb": self._quota_tb,
                    "fast_mbps": self._fast_mbps,
                    "slow_mbps": self._slow_mbps,
                    "reset_day": self._reset_day,
                    "run_once": False,
                }
            )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "proxy", "label": "使用代理"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username",
                                            "label": "SCP 账号 1",
                                            "placeholder": "295820",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "SCP 密码 1",
                                            "type": "password",
                                            "placeholder": "账号 1 的登录密码",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username2",
                                            "label": "SCP 账号 2（可选）",
                                            "placeholder": "第二台机器的账号，留空则不监控",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password2",
                                            "label": "SCP 密码 2",
                                            "type": "password",
                                            "placeholder": "账号 2 的登录密码",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "quota_tb",
                                            "label": "高速额度（TB/月·每台）",
                                            "type": "number",
                                            "placeholder": "默认120",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fast_mbps",
                                            "label": "额度内速率（M）",
                                            "type": "number",
                                            "placeholder": "默认2500",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "slow_mbps",
                                            "label": "超量后限速（M）",
                                            "type": "number",
                                            "placeholder": "默认200",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "reset_day",
                                            "label": "计费月重置日（每月）",
                                            "type": "number",
                                            "placeholder": "默认25",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval_value",
                                            "label": "检查间隔数值",
                                            "type": "number",
                                            "placeholder": "默认6",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "interval_unit",
                                            "label": "间隔单位",
                                            "items": [
                                                {"title": "分钟", "value": "minutes"},
                                                {"title": "小时", "value": "hours"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "threshold",
                                            "label": "流量告警阈值（TB）",
                                            "type": "number",
                                            "placeholder": "默认0，超过该值通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_once",
                                            "label": "立即执行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件会按设定间隔登录 Server Control Panel 控制面板，"
                                                    "汇总每台服务器本计费月已用流量（Traffic current month），"
                                                    "统一换算为 TB 展示（GiB ÷ 1024），超过阈值时发送通知。"
                                                    "计费月按每月「重置日」00:00 起算（默认 25 日 → 次月 24 日），"
                                                    "流量由服务端清零、插件只读不写，跨周期后流量历史自动只保留本计费月。"
                                                    "账号 1 必填；若有多台机器分属不同账号，可在账号 2 填写，留空则只监控账号 1。"
                                                    "详情页按「每台一台卡片」展示高速额度进度（额度、占比、剩余、"
                                                    "上下行、速率档），额度按台独立计算，"
                                                    "超出额度后进度条转为红色并显示溢出段。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "username": "",
            "password": "",
            "username2": "",
            "password2": "",
            "proxy": False,
            "interval_value": 6,
            "interval_unit": "hours",
            "threshold": 0.0,
            "quota_tb": DEFAULT_QUOTA_TB,
            "fast_mbps": DEFAULT_FAST_MBPS,
            "slow_mbps": DEFAULT_SLOW_MBPS,
            "reset_day": DEFAULT_BILLING_RESET_DAY,
            "run_once": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页。

        性能设计：打开详情页时**不阻塞等待**远程登录抓取。
        - 有缓存：立即渲染缓存数据；若缓存超过 PAGE_CACHE_TTL，再丢到后台线程刷新。
        - 无缓存：才同步抓取一次（首次打开会慢几秒，之后都是秒开）。
        """
        if not self._enabled:
            return None

        if self._username and self._password:
            if self._last_result:
                # 已有缓存：秒出，过期则在后台静默刷新
                age = self.__cache_age_seconds()
                if age is None or age > PAGE_CACHE_TTL:
                    self.__refresh_in_background()
            else:
                # 无缓存（首次打开 / 重启后）：同步抓一次
                result, error = self.__fetch_traffic()
                self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if error:
                    self._last_error = error
                else:
                    self._last_error = ""
                    self._last_result = result
                    self.__record_history(result)
                self.__save_result_cache()

        page_content: List[dict] = []

        # 配置概览
        interval_unit_text = "分钟" if self._interval_unit == "minutes" else "小时"
        threshold_text = "不告警" if self._threshold <= 0 else f"{self._threshold:g} TB"
        period = billing_period(self._reset_day)
        page_content.append(
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": f"高速额度：{self._quota_tb:g} TB/月·每台"
                                            f"（额度内 {self._fast_mbps:g} M，超出后限速 {self._slow_mbps:g} M）；"
                                            f"计费周期：{period['label']}"
                                            f"（第 {period['day_index']}/{period['days_total']} 天，"
                                            f"{period['days_left']} 天后重置）；"
                                            f"检查间隔：{self._interval_value} {interval_unit_text}；"
                                            f"告警阈值：{threshold_text}；"
                                            f"最近检查：{self._last_check_time or '尚未检查'}",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # 查询错误提示
        if self._last_error:
            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "error",
                                        "variant": "tonal",
                                        "text": f"最近一次查询失败：{self._last_error}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # 部分账号失败的警告提示
        if self._last_result and self._last_result.get("warning"):
            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": f"部分账号查询失败：{self._last_result.get('warning')}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # 每台服务器：并列卡片（方案 2）
        #
        # 旧版这里是「一条合计 VAlert + 一张明细表」。2026-09-28 用户定板：
        # 不显示合计，只体现每台服务器，排版改为「逐台并列卡」——
        # 每张卡自带 已用 / 额度 / 占比 / 剩余 / 上下行 / 速率档，不依赖别的区块。
        servers = (self._last_result or {}).get("servers") or []
        if servers:
            page_content.append(
                {
                    "component": "div",
                    "props": {"style": "display: flex; flex-wrap: wrap; gap: 8px;"},
                    "content": [self.__server_card(srv) for srv in servers],
                }
            )
            # 一句话交代进度条口径：竖线是额度线、刻度是统一放大的，
            # 不解释的话「条只走到 88.9%」会被读成数据没抓全。
            page_content.append(
                {
                    "component": "div",
                    "props": {
                        "style": "font-size: 11px; line-height: 1.7; opacity: 0.7; "
                                 "margin: 2px 0 0 2px;",
                    },
                    "text": (
                        f"进度条统一按 {self._quota_tb + OVERFLOW_VISUAL_CAP_TB:g} TB 满刻度绘制，"
                        f"竖线为 {self._quota_tb:g} TB 额度线"
                        f"（{self._quota_tb / (self._quota_tb + OVERFLOW_VISUAL_CAP_TB) * 100:.1f}% 处），"
                        f"各服务器共用同一刻度以便横向比较；"
                        f"超出额度的部分用斜纹块表示，最多画 {OVERFLOW_VISUAL_CAP_TB:g} TB。"
                    ),
                }
            )
        else:
            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": "暂无流量数据，请等待首次检查或检查账号密码配置。",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # 流量历史（最近 TRAFFIC_HISTORY_LIMIT 条）
        history = self.get_data(TRAFFIC_HISTORY_KEY) or []
        if history:
            recent = history[-TRAFFIC_HISTORY_LIMIT:][::-1]
            rows = []
            for rec in recent:
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(rec.get("time") or "")},
                            {"component": "td", "text": f"{rec.get('total_tb', 0):.3f}"},
                            {"component": "td", "text": f"{rec.get('total_gib', 0):.1f}"},
                        ],
                    }
                )

            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": f"流量历史（本计费月 {period['label']}，"
                                                f"最近 {TRAFFIC_HISTORY_LIMIT} 条，倒序；"
                                                f"跨周期后自动只保留本计费月）",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
            page_content.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTable",
                                    "props": {"density": "compact"},
                                    "content": [
                                        {
                                            "component": "thead",
                                            "content": [
                                                {
                                                    "component": "tr",
                                                    "content": [
                                                        {"component": "th", "text": "时间"},
                                                        {"component": "th", "text": "已用流量 (TB)"},
                                                        {"component": "th", "text": "已用流量 (GiB)"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {"component": "tbody", "content": rows},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        return page_content

    def __server_card(self, srv: Dict[str, Any]) -> Dict[str, Any]:
        """构造单台服务器的卡片节点（方案 2「逐台并列卡」）。

        一张卡里自包含 6 项信息，不依赖任何别的区块、也不含合计：
            ① 状态点 + 主机名        ② 速率档徽章
            ③ 服务器名 + 账号        ④ 已用 / 额度 / 占比（22px 大号）
            ⑤ 额度进度条             ⑥ 上下行 + 剩余/已超

        配色即语义：<80% 主题主色、80–100% 橙、>=100% 红；
        超量后有 4 处联动变红 —— 状态点、大号数字、速率徽章、右侧「已超 X TB」。

        文字层级统一为「标签弱化 + 取值加粗」，弱化一律用带透明度的颜色而不是
        opacity（opacity 会把子树一起压暗）。第 ③ 行（机器名 + 账号）与第 ⑥ 行
        （下行 / 上行）都按这个规则拆节点，见各自下面的注释。
        """
        quota = self._quota_tb
        tb = float(srv.get("total_tb") or 0.0)
        pct = (tb / quota * 100.0) if quota > 0 else 0.0
        over = max(tb - quota, 0.0)
        color = _traffic_tone(pct)
        rx = float(srv.get("rx_gib") or 0.0)
        tx = float(srv.get("tx_gib") or 0.0)

        # 速率档徽章：达到额度 → 红底「已限速」；80–100% → 橙底；其余 → 紫底「高速」。
        # 🔴 判定用 pct >= 100 而不是 over > 0：恰好 120.00 TB 时 over 为 0，
        #    但此时进度条已转红、右侧已写「已达额度」，徽章若还挂「高速 2500 M」
        #    就会出现同屏自相矛盾的读数（触发限速的分界就在 120 TB 这一步）。
        if pct >= 100.0:
            badge_bg, badge_fg = "#FDE7E9", "#C0392B"
            badge_text = f"已限速 {self._slow_mbps:g} M"
        elif pct >= 80.0:
            badge_bg, badge_fg = "#FFF3E0", "#B26A00"
            badge_text = f"高速 {self._fast_mbps:g} M"
        else:
            badge_bg, badge_fg = "#EFE7FE", "#6B31D6"
            badge_text = f"高速 {self._fast_mbps:g} M"

        # 卡片右下角：剩余 / 已达额度 / 已超。
        # 阈值用 0.005（显示精度 0.01 的一半）而不是 0 —— 否则浮点误差下会出现
        # 「已超 0.00 TB」这种自相矛盾的读数。
        if over >= 0.005:
            tail_text, tail_style = f"已超 {over:.2f} TB", f"font-weight: 700; color: {COLOR_TAIL_BAD};"
        elif pct >= 100.0:
            tail_text, tail_style = "已达额度", f"font-weight: 700; color: {COLOR_TAIL_BAD};"
        else:
            tail_text = f"剩余 {quota - tb:.2f} TB"
            tail_style = f"font-weight: 700; color: {COLOR_TAIL_OK};"

        return {
            "component": "div",
            "props": {
                "style": (
                    # 两台并排；窗口收窄或超过 2 台时自动换行（最窄 240px 保底）
                    "flex: 1 1 calc(50% - 4px); min-width: 240px; box-sizing: border-box; "
                    "padding: 10px 12px; border-radius: 10px; "
                    "background: rgba(var(--v-theme-surface-variant), 0.18); "
                    "backdrop-filter: blur(10px) saturate(150%); "
                    "-webkit-backdrop-filter: blur(10px) saturate(150%); "
                    "border: 1px solid rgba(var(--v-theme-on-surface), 0.12); "
                    "box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);"
                ),
            },
            "content": [
                # ① 状态点 + 主机名 …… ② 速率档徽章
                {
                    "component": "div",
                    "props": {"style": "display: flex; align-items: center; gap: 7px; "
                                       "margin-bottom: 3px;"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "style": f"width: 8px; height: 8px; border-radius: 50%; "
                                         f"background: {color}; flex: 0 0 auto;",
                            },
                        },
                        {
                            "component": "div",
                            "props": {"style": "font-size: 13px; font-weight: 700; "
                                               "white-space: nowrap; overflow: hidden; "
                                               "text-overflow: ellipsis;"},
                            "text": str(srv.get("hostname") or srv.get("name") or "-"),
                        },
                        {"component": "div", "props": {"style": "flex: 1 1 auto;"}},
                        {
                            "component": "span",
                            "props": {
                                "style": f"font-size: 11px; font-weight: 700; padding: 2px 9px; "
                                         f"border-radius: 20px; white-space: nowrap; "
                                         f"background: {badge_bg}; color: {badge_fg};",
                            },
                            "text": badge_text,
                        },
                    ],
                },
                # ③ 服务器名 + 账号
                #
                # 🔴 旧写法是「整行一个节点 + opacity: 0.7 + 11px」：标签和取值一起被压暗，
                #    想单独加粗某一截都做不到，窄卡上也只剩一个省略号。
                # 现在拆成「机器名 / 账号」两段独立节点，取值加粗、标签用**颜色**弱化；
                # 两段都 nowrap，靠 flex-wrap 在窄卡上整段换行（「· 账号 X」绑成一体，
                # 不会出现行首孤零零一个「·」）。
                {
                    "component": "div",
                    "props": {"style": "display: flex; align-items: baseline; "
                                       "flex-wrap: wrap; gap: 2px 7px; "
                                       "margin-bottom: 7px; font-size: 12px; "
                                       "font-variant-numeric: tabular-nums;"},
                    "content": [
                        {
                            "component": "span",
                            "props": {"style": f"font-weight: 600; letter-spacing: 0.2px; "
                                               f"color: {COLOR_FIELD_NAME}; "
                                               "white-space: nowrap; max-width: 100%; "
                                               "overflow: hidden; text-overflow: ellipsis;"},
                            "text": str(srv.get("name") or "-"),
                        },
                        {
                            "component": "span",
                            "props": {"style": "white-space: nowrap;"},
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"style": f"font-weight: 600; "
                                                       f"color: {COLOR_FIELD_LABEL};"},
                                    "text": "· 账号 ",
                                },
                                {
                                    "component": "span",
                                    "props": {"style": "font-size: 12.5px; font-weight: 700; "
                                                       f"color: {COLOR_FIELD_VALUE};"},
                                    "text": str(srv.get("account") or "-"),
                                },
                            ],
                        },
                    ],
                },
                # ④ 已用 / 额度 / 占比
                {
                    "component": "div",
                    "props": {"style": f"font-size: 22px; font-weight: 700; "
                                       f"line-height: 1.15; color: {color};"},
                    "content": [
                        {"component": "span", "text": f"{tb:.2f} "},
                        {
                            "component": "span",
                            "props": {"style": "font-size: 12px; font-weight: 600;"},
                            "text": "TB",
                        },
                        {
                            "component": "span",
                            "props": {"style": "font-size: 11.5px; font-weight: 600; "
                                               "opacity: 0.75; margin-left: 4px;"},
                            "text": f"/ {quota:g} TB · {pct:.2f}%",
                        },
                    ],
                },
                # ⑤ 额度进度条
                {"component": "div", "props": {"style": "margin-top: 7px;"},
                 "content": [self.__progress_bar(tb)]},
                # ⑥ 上下行 + 剩余/已超
                #
                # 🔴 opacity 必须挂在**标签节点**上，不能挂容器：opacity 按子树整体
                #    合成，挂容器会把加粗的数值一起压暗（与 UHD 详情页卡片同一个坑）。
                # 🔴 数值单独拆成 span 才加得粗；「下行 + 数值 + 单位」各自 nowrap，
                #    窄卡（min-width 240px）时在两组之间换行，不会从中间断字。
                {
                    "component": "div",
                    "props": {"style": "display: flex; justify-content: space-between; "
                                       "align-items: baseline; flex-wrap: wrap; "
                                       "gap: 3px 10px; font-size: 11.5px; margin-top: 7px;"},
                    "content": [
                        {
                            "component": "span",
                            "props": {"style": "display: flex; align-items: baseline; "
                                               "flex-wrap: wrap; gap: 2px 9px;"},
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"style": "white-space: nowrap; "
                                                       "font-variant-numeric: tabular-nums;"},
                                    "content": [
                                        {"component": "span",
                                         "props": {"style": f"font-weight: 600; color: {COLOR_FIELD_LABEL};"},
                                         "text": "下行 "},
                                        {"component": "span",
                                         "props": {"style": "font-size: 12.5px; font-weight: 700;"},
                                         "text": f"{rx:,.1f}"},
                                        {"component": "span",
                                         "props": {"style": f"font-weight: 600; color: {COLOR_FIELD_LABEL};"},
                                         "text": " GiB"},
                                    ],
                                },
                                {
                                    "component": "span",
                                    "props": {"style": f"color: {COLOR_FIELD_LABEL}; "
                                                       "font-weight: 400; white-space: nowrap;"},
                                    "text": "·",
                                },
                                {
                                    "component": "span",
                                    "props": {"style": "white-space: nowrap; "
                                                       "font-variant-numeric: tabular-nums;"},
                                    "content": [
                                        {"component": "span",
                                         "props": {"style": f"font-weight: 600; color: {COLOR_FIELD_LABEL};"},
                                         "text": "上行 "},
                                        {"component": "span",
                                         "props": {"style": "font-size: 12.5px; font-weight: 700;"},
                                         "text": f"{tx:,.1f}"},
                                        {"component": "span",
                                         "props": {"style": f"font-weight: 600; color: {COLOR_FIELD_LABEL};"},
                                         "text": " GiB"},
                                    ],
                                },
                            ],
                        },
                        {
                            "component": "span",
                            "props": {"style": tail_style + " white-space: nowrap;"},
                            "text": tail_text,
                        },
                    ],
                },
            ],
        }

    def __progress_bar(self, tb: float) -> Dict[str, Any]:
        """构造额度进度条节点：填充段 + 额度线 + 超量斜纹溢出段。

        刻度口径（与效果图定板一致）：满刻度固定为「额度 + 溢出可视上限」，
        **所有服务器共用同一刻度**，所以 120 TB 额度线永远落在同一个相对位置，
        两台可以直接横向比较。代价是未超量的条看起来比「按自己的 120 TB 满刻度」短。

        超量时填充段在额度线处切断（圆角收成直角），右侧接一段斜纹块；
        斜纹块有 8px 最小宽度 —— 刚超一点点时按真实比例画几乎看不见，
        给个下限保证「已经越线」这件事至少看得出来。
        """
        quota = self._quota_tb
        scale = quota + OVERFLOW_VISUAL_CAP_TB
        qline_pct = quota / scale * 100.0 if scale > 0 else 0.0
        over = max(tb - quota, 0.0)
        color = _traffic_tone(tb / quota * 100.0 if quota > 0 else 0.0)
        fill_pct = (min(max(tb, 0.0), quota) / scale * 100.0) if scale > 0 else 0.0

        children: List[Dict[str, Any]] = [
            {
                "component": "div",
                "props": {
                    "style": "position: absolute; top: 0; left: 0; height: 100%; "
                             f"width: {fill_pct:.3f}%; background: {color}; "
                             + ("border-radius: 5px 0 0 5px;" if over > 0
                                else "border-radius: 5px;"),
                },
            }
        ]

        if over > 0:
            ov_pct = (min(over, OVERFLOW_VISUAL_CAP_TB) / scale * 100.0) if scale > 0 else 0.0
            children.append(
                {
                    "component": "div",
                    "props": {
                        "style": "position: absolute; top: 0; height: 100%; "
                                 f"left: {qline_pct:.3f}%; width: {ov_pct:.3f}%; "
                                 f"min-width: {OVERFLOW_MIN_WIDTH_PX}px; "
                                 f"background: {OVERFLOW_HATCH}; "
                                 "border-radius: 0 5px 5px 0;",
                    },
                }
            )

        # 额度线：竖线立在 100% 额度处，未超量时也显示，当「满额」参照物
        children.append(
            {
                "component": "div",
                "props": {
                    "style": "position: absolute; top: -3px; height: 16px; width: 2px; "
                             f"left: {qline_pct:.3f}%; margin-left: -1px; border-radius: 1px; "
                             "background: rgba(var(--v-theme-on-surface), 0.30);",
                },
            }
        )

        return {
            "component": "div",
            "props": {
                "style": "position: relative; height: 10px; border-radius: 5px; "
                         "background: rgba(var(--v-theme-on-surface), 0.10);",
            },
            "content": children,
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。"""
        if self._enabled and self._username and self._password:
            return [
                {
                    "id": "ScpTrafficMonitor",
                    "name": "SCP流量监控",
                    "trigger": "interval",
                    "func": self.check_traffic,
                    "kwargs": {self._interval_unit: self._interval_value},
                }
            ]
        return []

    def __cache_age_seconds(self) -> Optional[float]:
        """返回当前缓存数据距上次更新的秒数；无缓存或时间不可解析时返回 None。"""
        if not self._last_check_time:
            return None
        try:
            last = datetime.strptime(self._last_check_time, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
        return (datetime.now() - last).total_seconds()

    def __save_result_cache(self) -> None:
        """把最近一次查询结果落盘，供重启后详情页秒开。"""
        try:
            self.save_data(
                RESULT_CACHE_KEY,
                {
                    "result": self._last_result,
                    "check_time": self._last_check_time,
                    "error": self._last_error,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SCP流量监控：写入结果缓存失败，{e}")

    def __refresh_in_background(self) -> None:
        """后台线程静默刷新一次流量数据，不阻塞详情页渲染。"""
        # 上一次刷新还在跑就跳过，避免线程堆积
        if self._refreshing:
            return
        self._refreshing = True

        def _worker() -> None:
            try:
                result, error = self.__fetch_traffic()
                self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if error:
                    self._last_error = error
                    logger.warning(f"SCP流量监控：后台刷新失败，{error}")
                else:
                    self._last_error = ""
                    self._last_result = result
                    self.__record_history(result)
                self.__save_result_cache()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"SCP流量监控：后台刷新异常，{e}")
            finally:
                self._refreshing = False

        threading.Thread(target=_worker, name="ScpTrafficRefresh", daemon=True).start()

    def check_traffic(self) -> None:
        """登录并抓取流量，记录历史，超过阈值时发送通知。"""
        if not self._enabled or not self._username or not self._password:
            return

        result, error = self.__fetch_traffic()
        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if error:
            self._last_error = error
            self.__save_result_cache()
            logger.error(f"SCP流量监控：查询流量失败，{error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【SCP流量监控】",
                    text=f"查询流量失败：{error}",
                )
            return

        self._last_error = ""
        self._last_result = result
        self.__save_result_cache()
        rolled = self.__record_history(result)

        # 跨计费周期：显式清零告警状态，保证新周期能重新提醒一次。
        # （原有的「跌回阈值以下就复位」是启发式，这里用周期标识做确定性复位，
        #   两者并存 —— 后者还能覆盖「用户把阈值调高」这类同周期内的复位场景。）
        if rolled and self._alerting:
            self._alerting = False
            self.save_data(ALERT_STATE_KEY, {"alerting": False})

        if result.get("warning"):
            logger.warning(f"SCP流量监控：部分账号查询失败，{result.get('warning')}")

        period = billing_period(self._reset_day)
        total_mib = result.get("total_mib") or 0
        total_gib = total_mib / 1024.0
        total_tb = total_gib / 1024.0
        servers = result.get("servers") or []
        server_count = len(servers)

        logger.info(
            f"SCP流量监控：计费月 {period['label']} 已用流量 {total_tb:.3f} TB"
            f"（{total_gib:.1f} GiB，{server_count} 台服务器）"
        )

        # 超过阈值时通知（本计费月内只通知一次，重置后会重新提醒）
        alerting = self._threshold > 0 and total_tb >= self._threshold
        if self._notify and alerting and not self._alerting:
            lines = [f"本计费月（{period['label']}）已用流量 {total_tb:.3f} TB，"
                     f"已超过告警阈值 {self._threshold:g} TB。"]
            for srv in servers:
                lines.append(f"{srv.get('account', '')} · {srv.get('name')}：{srv.get('total_tb', 0):.3f} TB")
            lines.append(f"（计费月于每月 {self._reset_day} 日 00:00 重置，"
                         f"距今 {period['days_left']} 天；本次告警仅通知一次，重置后才会重新提醒）")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【SCP流量告警】",
                text="\n".join(lines),
            )

        # 记录告警状态变化
        if alerting != self._alerting:
            self._alerting = alerting
            self.save_data(ALERT_STATE_KEY, {"alerting": alerting})
            if not alerting:
                logger.info("SCP流量监控：流量已低于阈值，告警状态重置")

    def __record_history(self, result: Dict[str, Any]) -> bool:
        """记录流量历史，用于详情页展示趋势。

        **计费周期切分**：历史只保留「当前计费月」的记录。判据是**周期标识变化**
        （而不是「数值暴跌」这类启发式）—— 后者会把 `traffic_history` 里因为某个账号
        偶发查询失败导致的数值回落误判成重置，把历史整段清掉。周期标识由本地
        ``billing_period()`` 按重置日推算，是确定性的。

        :return: 本次是否发生了跨周期重置（调用方据此清零告警状态）
        """
        total_mib = result.get("total_mib") or 0
        total_gib = total_mib / 1024.0
        total_tb = total_gib / 1024.0

        history: List[Dict[str, Any]] = self.get_data(TRAFFIC_HISTORY_KEY) or []
        now = datetime.now()
        period = billing_period(self._reset_day, now)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        rolled = False
        if history:
            last_time_str = str(history[-1].get("time") or "")
            try:
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                last_time = None
            if last_time and billing_period(self._reset_day, last_time)["key"] != period["key"]:
                # 跨计费月：上周期记录整段丢弃，历史重新从本周期第一条开始
                history = []
                rolled = True
                logger.info(
                    f"SCP流量监控：进入新计费周期 {period['label']}，流量历史已重置"
                    f"（上一周期最后记录 {last_time_str}）"
                )

        # 若与上一条记录的 GiB 值几乎相同，跳过避免冗余
        # （必须放在跨周期判断之后：重置当轮即便数值接近也要记一条，作为新周期起点）
        if history:
            last = history[-1]
            if abs(float(last.get("total_gib") or 0) - total_gib) < 0.05:
                return rolled

        history.append(
            {
                "time": now_str,
                "cycle": period["key"],
                "total_mib": total_mib,
                "total_gib": total_gib,
                "total_tb": total_tb,
            }
        )

        if len(history) > TRAFFIC_HISTORY_LIMIT:
            history = history[-TRAFFIC_HISTORY_LIMIT:]

        self.save_data(TRAFFIC_HISTORY_KEY, history)
        return rolled

    def __fetch_traffic(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """登录 SCP 并抓取所有账号下所有服务器本计费月已用流量。

        流程：对账号 1、账号 2 分别走 Keycloak PKCE 授权码登录 →
        获取各自服务器列表 → 逐台抓详情 → 汇总流量。

        :return: (流量数据, 错误信息)；成功时错误信息为空字符串
        """
        # 组装账号列表（账号 2 留空则只监控账号 1）
        accounts: List[Tuple[str, str]] = []
        if self._username and self._password:
            accounts.append((self._username, self._password))
        if self._username2 and self._password2:
            accounts.append((self._username2, self._password2))
        if not accounts:
            return None, "未配置任何账号"

        def new_session() -> requests.Session:
            session = requests.Session()
            session.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36"
            )
            session.verify = False
            if self._proxy:
                from app.core.config import settings
                proxies = settings.PROXY
                if proxies:
                    session.proxies.update(proxies)
            return session

        servers: List[Dict[str, Any]] = []
        errors: List[str] = []

        for username, password in accounts:
            session = new_session()
            try:
                access_token, err = self.__login(session, username, password)
            except Exception as e:  # noqa: BLE001
                errors.append(f"账号 {username} 登录异常：{str(e)}")
                continue
            if err:
                errors.append(f"账号 {username}：{err}")
                continue

            api_headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

            # 服务器列表
            try:
                resp = session.get(f"{SCP_CORE_API}/v1/servers", headers=api_headers, timeout=30)
            except Exception as e:  # noqa: BLE001
                errors.append(f"账号 {username} 获取服务器列表异常：{str(e)}")
                continue
            if resp.status_code != 200:
                errors.append(f"账号 {username} 服务器列表返回状态码 {resp.status_code}")
                continue
            try:
                servers_list = resp.json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"账号 {username} 解析服务器列表失败：{str(e)}")
                continue

            for srv in servers_list:
                if srv.get("disabled"):
                    continue
                sid = srv.get("id")
                name = srv.get("name") or ""
                hostname = srv.get("hostname") or ""

                try:
                    dresp = session.get(f"{SCP_CORE_API}/v1/servers/{sid}", headers=api_headers, timeout=30)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"SCP流量监控：服务器 {name} 详情请求异常，{e}")
                    continue
                if dresp.status_code != 200:
                    logger.warning(f"SCP流量监控：服务器 {name} 详情返回 {dresp.status_code}")
                    continue
                try:
                    detail = dresp.json()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"SCP流量监控：服务器 {name} 详情解析失败，{e}")
                    continue

                ifaces = (detail.get("serverLiveInfo") or {}).get("interfaces") or []
                rx_mib = sum(i.get("rxMonthlyInMiB") or 0 for i in ifaces)
                tx_mib = sum(i.get("txMonthlyInMiB") or 0 for i in ifaces)
                total_mib = rx_mib + tx_mib

                servers.append(
                    {
                        "id": sid,
                        "name": name,
                        "hostname": hostname,
                        "account": username,
                        "rx_mib": rx_mib,
                        "tx_mib": tx_mib,
                        "total_mib": total_mib,
                        "rx_gib": rx_mib / 1024.0,
                        "tx_gib": tx_mib / 1024.0,
                        "total_gib": total_mib / 1024.0,
                        "total_tb": (total_mib / 1024.0) / 1024.0,
                    }
                )

        if not servers:
            if errors:
                return None, "；".join(errors)
            return None, "未获取到任何服务器流量数据"

        total_mib = sum(s.get("total_mib") or 0 for s in servers)
        # 若部分账号失败但仍有数据，把失败信息以警告形式返回（不阻断展示）
        warning = "；".join(errors) if errors else ""
        return {
            "servers": servers,
            "total_mib": total_mib,
            "total_gib": total_mib / 1024.0,
            "total_tb": (total_mib / 1024.0) / 1024.0,
            "warning": warning,
        }, ""

    def __login(self, session: requests.Session, username: str, password: str) -> Tuple[Optional[str], str]:
        """Keycloak PKCE 授权码登录，返回 access_token。

        :param session: 已配置好 headers / verify / proxies 的 requests.Session
        :param username: SCP 账号
        :param password: SCP 密码
        :return: (access_token, 错误信息)；成功时错误信息为空字符串
        """
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

        auth_url = (
            f"{SCP_AUTH_URL}?client_id={SCP_CLIENT_ID}&response_type=code&scope=openid"
            f"&redirect_uri={urllib.parse.quote(SCP_REDIRECT_URI, safe='')}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
        )
        try:
            r = session.get(auth_url, timeout=30)
        except Exception as e:  # noqa: BLE001
            return None, f"访问登录页异常：{str(e)}"

        m = re.search(r'<form id="kc-form-login"[^>]*action="([^"]+)"', r.text)
        if not m:
            return None, "未找到登录表单，站点可能已改版"
        action = m.group(1).replace("&amp;", "&")

        try:
            r2 = session.post(
                action,
                data={"username": username, "password": password, "credentialId": ""},
                allow_redirects=False,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"提交登录异常：{str(e)}"

        location = r2.headers.get("Location", "")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
        if not code:
            # 登录失败会停留在原页面，返回 200 且无 code
            if "Invalid username or password" in r2.text:
                return None, "账号或密码错误"
            return None, "登录失败，未获取到授权码"

        try:
            r3 = session.post(
                SCP_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": SCP_CLIENT_ID,
                    "code": code,
                    "redirect_uri": SCP_REDIRECT_URI,
                    "code_verifier": verifier,
                },
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"换取令牌异常：{str(e)}"

        try:
            token_data = r3.json()
        except Exception as e:  # noqa: BLE001
            return None, f"解析令牌响应失败：{str(e)}"

        access_token = token_data.get("access_token")
        if not access_token:
            return None, f"未获取到 access_token：{token_data.get('error', '未知错误')}"
        return access_token, ""

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
