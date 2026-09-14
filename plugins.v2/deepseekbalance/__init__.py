from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

# DeepSeek 余额查询接口
DEEPSEEK_BALANCE_API = "https://api.deepseek.com/user/balance"
# 余额历史记录持久化键名
BALANCE_HISTORY_KEY = "balance_history"
# 余额历史最多保留条数
BALANCE_HISTORY_LIMIT = 200


class DeepSeekBalance(_PluginBase):
    """DeepSeek 开放平台余额监控插件。

    定时查询 DeepSeek 账户余额，记录余额变化明细，
    当可用余额低于设定阈值时发送通知，并在插件详情页展示余额与消耗明细。
    """

    # 插件名称
    plugin_name = "DeepSeek余额监控"
    # 插件描述
    plugin_desc = "定时查询DeepSeek账户余额，记录消耗明细，低于阈值时通知提醒。"
    # 插件图标
    plugin_icon = "deepseek.png"
    # 插件版本
    plugin_version = "1.4.0"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "deepseekbalance_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _api_key: str = ""
    # 检查间隔数值
    _interval_value: int = 6
    # 检查间隔单位：minutes / hours
    _interval_unit: str = "hours"
    _threshold: float = 10.0
    _proxy: bool = False
    # 最近一次余额查询结果
    _last_balance: Optional[Dict[str, Any]] = None
    # 最近一次查询时间
    _last_check_time: Optional[str] = None
    # 最近一次查询错误
    _last_error: str = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._api_key = ""
        self._interval_value = 6
        self._interval_unit = "hours"
        self._threshold = 10.0
        self._proxy = False
        self._last_balance = None
        self._last_check_time = None
        self._last_error = ""

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._api_key = str(config.get("api_key") or "").strip()
        self._proxy = bool(config.get("proxy"))
        try:
            self._interval_value = max(1, int(config.get("interval_value") or 6))
        except (TypeError, ValueError):
            self._interval_value = 6
        interval_unit = str(config.get("interval_unit") or "hours")
        self._interval_unit = interval_unit if interval_unit in ("minutes", "hours") else "hours"
        try:
            self._threshold = max(0.0, float(config.get("threshold") or 10.0))
        except (TypeError, ValueError):
            self._threshold = 10.0

        # 立即执行一次：执行后自动关闭开关
        if config.get("run_once"):
            logger.info("DeepSeek余额监控：立即执行一次检查")
            self.check_balance()
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "proxy": self._proxy,
                    "api_key": self._api_key,
                    "interval_value": self._interval_value,
                    "interval_unit": self._interval_unit,
                    "threshold": self._threshold,
                    "run_once": False,
                }
            )

    def get_state(self) -> bool:
        """获取插件启用状态。

        :return: 插件是否启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。

        :return: 命令定义列表
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。

        :return: API 定义列表
        """
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: Vuetify 表单结构与默认配置
        """
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
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "proxy",
                                            "label": "使用代理",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_key",
                                            "label": "DeepSeek API Key",
                                            "placeholder": "sk-xxxxxxxxxxxxxxxx",
                                            "type": "password",
                                        },
                                    }
                                ],
                            }
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
                                            "placeholder": "默认6",
                                            "type": "number",
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
                                            "label": "余额告警阈值",
                                            "placeholder": "默认10，低于该值发送通知",
                                            "type": "number",
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
                                            "text": "插件会按设定间隔查询 DeepSeek 账户余额，"
                                                    "记录每次余额变化明细，"
                                                    "当可用余额低于设定阈值时发送通知提醒。"
                                                    "API Key 可在 DeepSeek 开放平台创建。",
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
            "proxy": False,
            "api_key": "",
            "interval_value": 6,
            "interval_unit": "hours",
            "threshold": 10.0,
            "run_once": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        # 打开详情页时实时查询余额，确保展示最新数据
        if self._api_key:
            balance_data, error = self.__query_balance()
            self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if error:
                self._last_error = error
            else:
                self._last_error = ""
                self._last_balance = balance_data
                # 记录余额历史
                self.__record_balance_history(balance_data)

        page_content: List[dict] = []

        # 配置概览
        interval_unit_text = "分钟" if self._interval_unit == "minutes" else "小时"
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
                                            f"告警阈值：{self._threshold}；"
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

        # 余额明细
        if self._last_balance:
            is_available = self._last_balance.get("is_available")
            balance_infos = self._last_balance.get("balance_infos") or []

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
                                        "type": "success" if is_available else "error",
                                        "variant": "tonal",
                                        "text": "账户余额充足，可正常调用 API"
                                                if is_available
                                                else "账户余额不足，无法调用 API",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            rows = []
            for info in balance_infos:
                currency = info.get("currency") or ""
                total_balance = info.get("total_balance") or "0"
                granted_balance = info.get("granted_balance") or "0"
                topped_up_balance = info.get("topped_up_balance") or "0"
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": currency},
                            {"component": "td", "text": total_balance},
                            {"component": "td", "text": granted_balance},
                            {"component": "td", "text": topped_up_balance},
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
                                                        {"component": "th", "text": "货币"},
                                                        {"component": "th", "text": "总可用余额"},
                                                        {"component": "th", "text": "赠金余额"},
                                                        {"component": "th", "text": "充值余额"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "tbody",
                                            "content": rows,
                                        },
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
                                        "text": "暂无余额数据，请等待首次检查或检查 API Key 配置。",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # 余额消耗明细（按日统计）
        history = self.get_data(BALANCE_HISTORY_KEY) or []
        if history:
            # 汇总消耗情况
            summary_text = self.__build_consumption_summary(history)
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
                                        "text": summary_text,
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            # 按日统计表格（倒序，最新在前）
            daily_stats = self.__build_daily_stats(history)
            rows = []
            for stat in daily_stats:
                consumed = stat.get("consumed") or 0
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(stat.get("date") or "")},
                            {"component": "td", "text": str(stat.get("currency") or "")},
                            {"component": "td", "text": f"{stat.get('first_balance') or 0:.4f}"},
                            {"component": "td", "text": f"{stat.get('last_balance') or 0:.4f}"},
                            {"component": "td", "text": f"{consumed:.4f}"},
                            {"component": "td", "text": str(stat.get("count") or 0)},
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
                                                        {"component": "th", "text": "日期"},
                                                        {"component": "th", "text": "货币"},
                                                        {"component": "th", "text": "当日首次余额"},
                                                        {"component": "th", "text": "当日末次余额"},
                                                        {"component": "th", "text": "当日消耗"},
                                                        {"component": "th", "text": "记录次数"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "tbody",
                                            "content": rows,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        if self._enabled and self._api_key:
            return [
                {
                    "id": "DeepSeekBalance",
                    "name": "DeepSeek余额监控",
                    "trigger": "interval",
                    "func": self.check_balance,
                    "kwargs": {self._interval_unit: self._interval_value},
                }
            ]
        return []

    def check_balance(self) -> None:
        """查询 DeepSeek 余额，记录消耗明细，低于阈值时发送通知。"""
        if not self._enabled or not self._api_key:
            return

        balance_data, error = self.__query_balance()
        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if error:
            self._last_error = error
            logger.error(f"DeepSeek余额监控：查询余额失败，{error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【DeepSeek余额监控】",
                    text=f"查询余额失败：{error}",
                )
            return

        self._last_error = ""
        self._last_balance = balance_data
        # 记录余额历史
        self.__record_balance_history(balance_data)

        # 汇总余额信息
        balance_infos = balance_data.get("balance_infos") or []
        is_available = balance_data.get("is_available")
        summary_lines = []
        low_balance = False
        for info in balance_infos:
            currency = info.get("currency") or ""
            total_balance = info.get("total_balance") or "0"
            granted_balance = info.get("granted_balance") or "0"
            topped_up_balance = info.get("topped_up_balance") or "0"
            summary_lines.append(
                f"{currency} 总余额：{total_balance}（赠金 {granted_balance} / 充值 {topped_up_balance}）"
            )
            try:
                if float(total_balance) < self._threshold:
                    low_balance = True
            except (TypeError, ValueError):
                pass

        logger.info(f"DeepSeek余额监控：当前余额 {'；'.join(summary_lines)}")

        # 余额不足或低于阈值时通知
        if self._notify and (low_balance or not is_available):
            title = "【DeepSeek余额不足】" if not is_available else "【DeepSeek余额告警】"
            text = "\n".join(summary_lines)
            if not is_available:
                text += "\n账户余额不足，已无法调用 API，请及时充值。"
            else:
                text += f"\n当前余额已低于告警阈值 {self._threshold}，请及时充值。"
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )

    def __record_balance_history(self, balance_data: Dict[str, Any]) -> None:
        """记录余额历史，用于展示消耗明细。

        :param balance_data: 余额接口返回数据
        """
        balance_infos = balance_data.get("balance_infos") or []
        if not balance_infos:
            return

        history: List[Dict[str, Any]] = self.get_data(BALANCE_HISTORY_KEY) or []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for info in balance_infos:
            currency = str(info.get("currency") or "")
            total_balance = info.get("total_balance") or "0"

            # 查找该货币上一次记录，计算变化
            change_text = "-"
            for record in reversed(history):
                if record.get("currency") == currency:
                    try:
                        change = float(total_balance) - float(record.get("total_balance") or 0)
                        change_text = f"{change:+.4f}"
                    except (TypeError, ValueError):
                        change_text = "-"
                    break

            history.append(
                {
                    "time": now_str,
                    "currency": currency,
                    "total_balance": str(total_balance),
                    "change": change_text,
                }
            )

        # 限制历史长度
        if len(history) > BALANCE_HISTORY_LIMIT:
            history = history[-BALANCE_HISTORY_LIMIT:]

        self.save_data(BALANCE_HISTORY_KEY, history)

    @staticmethod
    def __build_consumption_summary(history: List[Dict[str, Any]]) -> str:
        """根据余额历史构建消耗汇总文本。

        :param history: 余额历史记录列表
        :return: 消耗汇总文本
        """
        if not history:
            return "暂无消耗数据"

        first = history[0]
        last = history[-1]
        currency = last.get("currency") or ""
        try:
            total_change = float(last.get("total_balance") or 0) - float(first.get("total_balance") or 0)
        except (TypeError, ValueError):
            total_change = 0.0

        # 统计消耗（负变化）次数与金额
        consumed = 0.0
        consumed_count = 0
        for record in history:
            change_str = str(record.get("change") or "")
            if change_str.startswith("-"):
                try:
                    consumed += abs(float(change_str))
                    consumed_count += 1
                except (TypeError, ValueError):
                    pass

        return (
            f"余额消耗明细：共 {len(history)} 条记录，"
            f"统计区间 {first.get('time')} ~ {last.get('time')}；"
            f"区间净变化 {total_change:+.4f} {currency}，"
            f"累计消耗 {consumed:.4f} {currency}（{consumed_count} 次）"
        )

    @staticmethod
    def __build_daily_stats(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按日期聚合余额历史，统计每日消耗。

        每日消耗 = 当天最后一次余额 - 当天第一次余额（负值表示消耗）。

        :param history: 余额历史记录列表
        :return: 按日期倒序排列的每日统计列表
        """
        # 按日期分组，保留当天首末余额
        daily: Dict[str, Dict[str, Any]] = {}
        for record in history:
            time_str = str(record.get("time") or "")
            date_str = time_str.split(" ")[0] if " " in time_str else time_str
            if not date_str:
                continue
            currency = str(record.get("currency") or "")
            try:
                balance = float(record.get("total_balance") or 0)
            except (TypeError, ValueError):
                continue

            entry = daily.setdefault(
                date_str,
                {"date": date_str, "currency": currency, "first": balance, "last": balance, "count": 0},
            )
            entry["last"] = balance
            entry["count"] += 1

        # 计算每日消耗
        stats = []
        for date_str in sorted(daily.keys(), reverse=True):
            entry = daily[date_str]
            consumed = entry["first"] - entry["last"]
            stats.append(
                {
                    "date": date_str,
                    "currency": entry["currency"],
                    "first_balance": entry["first"],
                    "last_balance": entry["last"],
                    "consumed": consumed,
                    "count": entry["count"],
                }
            )
        return stats

    def __query_balance(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """调用 DeepSeek 余额接口查询账户余额。

        :return: (余额数据, 错误信息)；成功时错误信息为空字符串
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            res = RequestUtils(
                headers=headers,
                proxies=self.__get_proxies(),
                timeout=30,
            ).get_res(url=DEEPSEEK_BALANCE_API)
        except Exception as err:
            return None, f"请求异常：{str(err)}"

        if res is None:
            return None, "无法连接 DeepSeek 接口"
        if res.status_code == 401:
            return None, "API Key 无效或已失效（401）"
        if res.status_code != 200:
            return None, f"接口返回状态码 {res.status_code}"

        try:
            return res.json(), ""
        except Exception as err:
            return None, f"解析响应失败：{str(err)}"

    def __get_proxies(self) -> Optional[dict]:
        """获取请求代理配置。

        :return: 代理配置字典；未启用代理时返回 None
        """
        if not self._proxy:
            return None
        from app.core.config import settings

        return settings.PROXY

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
