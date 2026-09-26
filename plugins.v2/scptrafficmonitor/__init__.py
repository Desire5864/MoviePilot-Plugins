import base64
import hashlib
import re
import secrets
import threading
import urllib.parse
import urllib3
from datetime import datetime
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
# 流量历史最多保留条数
TRAFFIC_HISTORY_LIMIT = 300
# 告警状态持久化键名（避免重复通知）
ALERT_STATE_KEY = "alert_state"
# 最近一次查询结果缓存键名（用于详情页秒开）
RESULT_CACHE_KEY = "last_result_cache"
# 打开详情页时缓存的最大容忍年龄（秒）；超过则同步刷新，否则后台刷新
PAGE_CACHE_TTL = 180


class ScpTrafficMonitor(_PluginBase):
    """Server Control Panel（servercontrolpanel.de）流量监控插件。

    定时登录 SCP 控制面板，抓取每台服务器本月已用流量（Traffic current month），
    统一换算为 TB 展示（GiB ÷ 1024），超过设定阈值时发送通知提醒。
    """

    # 插件名称
    plugin_name = "SCP流量监控"
    # 插件描述
    plugin_desc = "监控Server Control Panel服务器本月已用流量，按TB展示，超阈值时通知。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.2.0"
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
                                                    "汇总每台服务器本月已用流量（Traffic current month），"
                                                    "统一换算为 TB 展示（GiB ÷ 1024），超过阈值时发送通知。"
                                                    "账号 1 必填；若有多台机器分属不同账号，可在账号 2 填写，留空则只监控账号 1。",
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
                                    "text": f"检查间隔：{self._interval_value} {interval_unit_text}；"
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

        # 流量汇总
        if self._last_result:
            result = self._last_result
            servers = result.get("servers") or []
            total_mib = result.get("total_mib") or 0
            total_gib = total_mib / 1024.0
            total_tb = total_gib / 1024.0

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
                                        "type": "success",
                                        "variant": "tonal",
                                        "text": f"本月已用流量：{total_tb:.2f} TB（{total_gib:.0f} GiB）",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            # 每台服务器明细
            rows = []
            for srv in servers:
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(srv.get("account") or "")},
                            {"component": "td", "text": str(srv.get("name") or "")},
                            {"component": "td", "text": str(srv.get("hostname") or "")},
                            {"component": "td", "text": f"{srv.get('rx_gib', 0):.1f}"},
                            {"component": "td", "text": f"{srv.get('tx_gib', 0):.1f}"},
                            {"component": "td", "text": f"{srv.get('total_gib', 0):.1f}"},
                            {"component": "td", "text": f"{srv.get('total_tb', 0):.3f}"},
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
                                                        {"component": "th", "text": "账号"},
                                                        {"component": "th", "text": "服务器"},
                                                        {"component": "th", "text": "主机名"},
                                                        {"component": "th", "text": "下行 (GiB)"},
                                                        {"component": "th", "text": "上行 (GiB)"},
                                                        {"component": "th", "text": "合计 (GiB)"},
                                                        {"component": "th", "text": "合计 (TB)"},
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

        # 流量历史（最近 30 条）
        history = self.get_data(TRAFFIC_HISTORY_KEY) or []
        if history:
            recent = history[-30:][::-1]
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
                                        "text": "流量历史（最近 30 条，倒序）",
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
        self.__record_history(result)

        if result.get("warning"):
            logger.warning(f"SCP流量监控：部分账号查询失败，{result.get('warning')}")

        total_mib = result.get("total_mib") or 0
        total_gib = total_mib / 1024.0
        total_tb = total_gib / 1024.0
        servers = result.get("servers") or []
        server_count = len(servers)

        logger.info(f"SCP流量监控：本月已用流量 {total_tb:.3f} TB（{total_gib:.1f} GiB，{server_count} 台服务器）")

        # 超过阈值时通知（流量单调递增，本月内只通知一次，月初重置后重新通知）
        alerting = self._threshold > 0 and total_tb >= self._threshold
        if self._notify and alerting and not self._alerting:
            lines = [f"本月已用流量 {total_tb:.3f} TB，已超过告警阈值 {self._threshold:g} TB。"]
            for srv in servers:
                lines.append(f"{srv.get('account', '')} · {srv.get('name')}：{srv.get('total_tb', 0):.3f} TB")
            lines.append("（本次告警仅通知一次，本月流量重置后才会重新提醒）")
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

    def __record_history(self, result: Dict[str, Any]) -> None:
        """记录流量历史，用于详情页展示趋势。"""
        total_mib = result.get("total_mib") or 0
        total_gib = total_mib / 1024.0
        total_tb = total_gib / 1024.0

        history: List[Dict[str, Any]] = self.get_data(TRAFFIC_HISTORY_KEY) or []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 若与上一条记录的 GB 值相同（取整后），跳过避免冗余
        if history:
            last = history[-1]
            if abs(float(last.get("total_gib") or 0) - total_gib) < 0.05:
                return

        history.append(
            {
                "time": now_str,
                "total_mib": total_mib,
                "total_gib": total_gib,
                "total_tb": total_tb,
            }
        )

        if len(history) > TRAFFIC_HISTORY_LIMIT:
            history = history[-TRAFFIC_HISTORY_LIMIT:]

        self.save_data(TRAFFIC_HISTORY_KEY, history)

    def __fetch_traffic(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """登录 SCP 并抓取所有账号下所有服务器本月已用流量。

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
