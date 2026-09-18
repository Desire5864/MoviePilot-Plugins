import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

# 完成记录持久化键名
COMPLETED_DATA_KEY = "hr_completed_map"


class ChdbitsHrMonitor(_PluginBase):
    """彩虹岛 H&R 种子监控插件。

    定时抓取彩虹岛 H&R 页面，解析未完成的 HR 任务，与本地 QB 指定分类的
    任务比对。站点 H&R 页面已不存在（即已完成保种要求）的任务，在等待
    设定时间后自动从 QB 删除，并同时删除本地文件。
    """

    # 插件名称
    plugin_name = "彩虹岛HR监控"
    plugin_desc = "监控彩虹岛H&R任务，完成后延迟自动删除QB任务与本地文件。"
    # 插件图标
    plugin_icon = "CHDBits.png"
    # 插件版本
    plugin_version = "1.6.1"
    # 插件作者
    plugin_author = "Desire5864"
    # 作者主页
    author_url = "https://github.com/Desire5864"
    # 插件配置项ID前缀
    plugin_config_prefix = "chdbitshrmonitor_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloader: str = ""
    _category: str = "彩虹岛&HR"
    _hr_url: str = ""
    _delete_delay_hours: int = 4
    _interval_minutes: int = 30
    _run_once: bool = False
    # 最近一次检查时间
    _last_check_time: Optional[str] = None
    # 最近一次检查错误
    _last_error: str = ""
    # 最近一次站点 HR 任务
    _last_hr_tasks: List[Dict[str, Any]] = []
    # 最近一次 QB 与站点比对明细
    _last_compare_items: List[Dict[str, Any]] = []

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._downloader = ""
        self._category = "彩虹岛&HR"
        self._hr_url = ""
        self._delete_delay_hours = 4
        self._interval_minutes = 30
        self._run_once = False
        self._last_check_time = None
        self._last_error = ""
        self._last_hr_tasks = []
        self._last_compare_items = []

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloader = str(config.get("downloader") or "").strip()
        self._category = str(config.get("category") or "彩虹岛&HR").strip()
        self._hr_url = str(config.get("hr_url") or "").strip()
        try:
            self._delete_delay_hours = max(0, int(config.get("delete_delay_hours") or 4))
        except (TypeError, ValueError):
            self._delete_delay_hours = 4
        try:
            self._interval_minutes = max(5, int(config.get("interval_minutes") or 30))
        except (TypeError, ValueError):
            self._interval_minutes = 30
        self._run_once = bool(config.get("run_once"))

        # 立即执行一次：执行后自动关闭开关
        if self._run_once:
            logger.info("彩虹岛HR监控：立即执行一次检查")
            self.check_hr()
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "downloader": self._downloader,
                    "category": self._category,
                    "hr_url": self._hr_url,
                    "delete_delay_hours": self._delete_delay_hours,
                    "interval_minutes": self._interval_minutes,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader",
                                            "label": "下载器",
                                            "items": [
                                                {"title": config.name, "value": config.name}
                                                for config in DownloaderHelper().get_configs().values()
                                            ],
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
                                            "model": "category",
                                            "label": "QB任务分类",
                                            "placeholder": "默认：彩虹岛&HR",
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
                                            "model": "hr_url",
                                            "label": "站点H&R页面地址",
                                            "placeholder": "https://ptchdbits.co/hnr.php?id=138574",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delete_delay_hours",
                                            "label": "完成后延迟删除（小时）",
                                            "placeholder": "默认4，最小0",
                                            "type": "number",
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
                                            "model": "interval_minutes",
                                            "label": "检查间隔（分钟）",
                                            "placeholder": "默认30，最小5",
                                            "type": "number",
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
                                            "text": "插件会定时抓取彩虹岛 H&R 页面，与本地 QB 指定分类的任务比对。"
                                                    "站点 H&R 页面已不存在（即已完成保种要求）的任务，"
                                                    "在等待设定时间后自动从 QB 删除，并同时删除本地文件。"
                                                    "H&R 周期 5 天对应 120 小时，3 天对应 72 小时。",
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
            "downloader": "",
            "category": "彩虹岛&HR",
            "hr_url": "",
            "delete_delay_hours": 4,
            "interval_minutes": 30,
            "run_once": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        page_content: List[dict] = []

        # 配置概览
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
                                    "text": f"下载器：{self._downloader or '未配置'}；"
                                            f"QB分类：{self._category}；"
                                            f"延迟删除：{self._delete_delay_hours} 小时；"
                                            f"检查间隔：{self._interval_minutes} 分钟；"
                                            f"最近检查：{self._last_check_time or '尚未检查'}",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # 错误提示
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
                                        "text": f"最近一次检查失败：{self._last_error}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        # 站点未完成的 HR 任务
        if self._last_hr_tasks:
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
                                        "text": f"站点未完成 H&R 任务：{len(self._last_hr_tasks)} 个",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            # 使用卡片式布局，避免 VTable 单元格强制 nowrap 导致标题截断
            card_items = []
            for task in self._last_hr_tasks:
                # 计算做种进度与达标状态
                cycle_hours = self.__parse_hr_cycle_hours(task.get("hr_cycle") or "")
                seeding_hours = self.__parse_seeding_hours(task.get("seeding_time") or "")
                if cycle_hours and seeding_hours is not None:
                    progress = min(100.0, seeding_hours / cycle_hours * 100)
                    remain_hours = max(0.0, cycle_hours - seeding_hours)
                    progress_text = f"{seeding_hours:.1f}h / {cycle_hours:.0f}h（{progress:.1f}%）"
                    if seeding_hours >= cycle_hours:
                        status_text = "✅ 已达标"
                    else:
                        status_text = f"⏳ 未达标，还差 {remain_hours:.1f}h"
                else:
                    progress_text = "-"
                    status_text = "-"

                card_items.append(
                    {
                        "component": "div",
                        "props": {
                            "style": "padding: 10px 12px; margin-bottom: 8px; "
                                     "border-radius: 10px; "
                                     "background: rgba(var(--v-theme-surface-variant), 0.18); "
                                     "backdrop-filter: blur(10px) saturate(150%); "
                                     "-webkit-backdrop-filter: blur(10px) saturate(150%); "
                                     "border: 1px solid rgba(var(--v-theme-on-surface), 0.12); "
                                     "box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);",
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "style": "white-space: normal; word-break: break-all; "
                                             "font-size: 14px; font-weight: 600; line-height: 1.5;",
                                },
                                "text": str(task.get("title") or ""),
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 4px;",
                                },
                                "text": f"H&R百分比：{task.get('hr_percent') or '-'}　|　"
                                        f"剩余时间：{task.get('remain_time') or '-'}",
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 2px;",
                                },
                                "text": f"H&R周期：{task.get('hr_cycle') or '-'}　|　"
                                        f"做种时间：{task.get('seeding_time') or '-'}",
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 2px;",
                                },
                                "text": f"做种进度：{progress_text}　|　{status_text}",
                            },
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
                            "content": card_items,
                        }
                    ],
                }
            )

        # 已完成待删除任务
        completed_map: Dict[str, Dict[str, Any]] = self.get_data(COMPLETED_DATA_KEY) or {}
        if completed_map:
            now_ts = datetime.now().timestamp()
            card_items = []
            for torrent_hash, record in completed_map.items():
                completed_at = record.get("completed_at") or 0
                elapsed_hours = (now_ts - completed_at) / 3600 if completed_at else 0
                remain_hours = max(0, self._delete_delay_hours - elapsed_hours)
                card_items.append(
                    {
                        "component": "div",
                        "props": {
                            "style": "padding: 10px 12px; margin-bottom: 8px; "
                                     "border-radius: 10px; "
                                     "background: rgba(var(--v-theme-surface-variant), 0.18); "
                                     "backdrop-filter: blur(10px) saturate(150%); "
                                     "-webkit-backdrop-filter: blur(10px) saturate(150%); "
                                     "border: 1px solid rgba(var(--v-theme-on-surface), 0.12); "
                                     "box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);",
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "style": "white-space: normal; word-break: break-all; "
                                             "font-size: 14px; font-weight: 600; line-height: 1.5;",
                                },
                                "text": str(record.get("name") or ""),
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 4px;",
                                },
                                "text": f"完成时间：{record.get('completed_time') or '-'}　|　"
                                        f"已等待：{elapsed_hours:.1f} 小时　|　"
                                        f"剩余：{remain_hours:.1f} 小时",
                            },
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
                                        "type": "success",
                                        "variant": "tonal",
                                        "text": f"已完成待删除任务：{len(completed_map)} 个"
                                                f"（延迟 {self._delete_delay_hours} 小时后删除）",
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
                            "content": card_items,
                        }
                    ],
                }
            )

        # QB 与站点比对明细
        if self._last_compare_items:
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
                                        "text": f"QB 分类「{self._category}」与站点比对明细："
                                                f"{len(self._last_compare_items)} 个任务",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            card_items = []
            for item in self._last_compare_items:
                card_items.append(
                    {
                        "component": "div",
                        "props": {
                            "style": "padding: 10px 12px; margin-bottom: 8px; "
                                     "border-radius: 10px; "
                                     "background: rgba(var(--v-theme-surface-variant), 0.18); "
                                     "backdrop-filter: blur(10px) saturate(150%); "
                                     "-webkit-backdrop-filter: blur(10px) saturate(150%); "
                                     "border: 1px solid rgba(var(--v-theme-on-surface), 0.12); "
                                     "box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);",
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "style": "white-space: normal; word-break: break-all; "
                                             "font-size: 14px; font-weight: 600; line-height: 1.5;",
                                },
                                "text": str(item.get("name") or ""),
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 4px;",
                                },
                                "text": f"站点状态：{item.get('status') or '-'}　|　"
                                        f"H&R周期：{item.get('hr_cycle') or '-'}　|　"
                                        f"做种时间：{item.get('seeding_time') or '-'}",
                            },
                            {
                                "component": "div",
                                "props": {
                                    "style": "font-size: 12px; opacity: 0.85; margin-top: 2px;",
                                },
                                "text": f"剩余做种时间：{item.get('remain_time') or '-'}　|　"
                                        f"说明：{item.get('detail') or '-'}",
                            },
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
                            "content": card_items,
                        }
                    ],
                }
            )

        return page_content

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        if self._enabled and self._downloader and self._hr_url:
            return [
                {
                    "id": "ChdbitsHrMonitor",
                    "name": "彩虹岛HR监控",
                    "trigger": "interval",
                    "func": self.check_hr,
                    "kwargs": {"minutes": self._interval_minutes},
                }
            ]
        return []

    def check_hr(self) -> None:
        """检查站点 H&R 任务并与本地 QB 任务比对，处理已完成任务。"""
        if not self._enabled:
            return

        if not self._downloader or not self._hr_url:
            logger.warning("彩虹岛HR监控：未配置下载器或H&R页面地址，跳过检查")
            return

        self._last_check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 抓取站点 H&R 页面
        hr_tasks, error = self.__fetch_hr_tasks()
        if error:
            self._last_error = error
            logger.error(f"彩虹岛HR监控：抓取H&R页面失败，{error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text=f"抓取H&R页面失败：{error}",
                )
            return

        self._last_error = ""
        self._last_hr_tasks = hr_tasks
        logger.info(f"彩虹岛HR监控：站点未完成 H&R 任务 {len(hr_tasks)} 个")

        # 2. 获取本地 QB 任务
        downloader_obj = self.__get_downloader()
        if not downloader_obj:
            self._last_error = "获取下载器失败"
            logger.error("彩虹岛HR监控：获取下载器失败")
            return

        torrents, err = downloader_obj.get_torrents()
        if err:
            self._last_error = "获取QB任务失败"
            logger.error("彩虹岛HR监控：获取QB任务失败")
            return

        # 筛选指定分类的任务
        local_torrents = [
            torrent for torrent in torrents
            if (torrent.get("category") or "") == self._category
        ]
        logger.info(f"彩虹岛HR监控：本地分类 {self._category} 任务 {len(local_torrents)} 个")

        # 安全校验：站点解析出 0 个任务但本地有任务时，视为页面异常（如 cookie 失效、
        # 页面改版返回登录页），跳过本次比对，避免误判为已完成而删除任务。
        if not hr_tasks and local_torrents:
            self._last_error = "站点 H&R 页面解析到 0 个任务，疑似页面异常，已跳过本次比对"
            logger.warning(f"彩虹岛HR监控：{self._last_error}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【彩虹岛HR监控】",
                    text="站点 H&R 页面解析到 0 个任务，疑似页面异常（cookie 失效或页面改版），"
                         "已跳过本次比对以避免误删任务。",
                )
            return

        # 3. 比对：站点未完成任务（含完整数据，用于关联做种时间/剩余时间）
        site_tasks = hr_tasks

        # 4. 处理已完成任务
        self.__process_completed(downloader_obj, local_torrents, site_tasks)

    def __process_completed(self, downloader_obj: Any, local_torrents: List[Any],
                            site_tasks: List[Dict[str, Any]]) -> None:
        """处理已完成（站点 H&R 页面已不存在）的任务。

        :param downloader_obj: 下载器实例
        :param local_torrents: 本地指定分类的任务列表
        :param site_tasks: 站点未完成 H&R 任务列表
        """
        completed_map: Dict[str, Dict[str, Any]] = self.get_data(COMPLETED_DATA_KEY) or {}
        now_ts = datetime.now().timestamp()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        delay_seconds = self._delete_delay_hours * 3600
        site_titles = {self.__normalize_title(task.get("title") or "") for task in site_tasks}

        # 本地任务哈希集合，用于清理已不存在的记录
        local_hashes = {torrent.get("hash") for torrent in local_torrents if torrent.get("hash")}

        # 清理已不在本地的记录
        for torrent_hash in list(completed_map.keys()):
            if torrent_hash not in local_hashes:
                completed_map.pop(torrent_hash, None)

        delete_hashes: List[str] = []
        delete_names: List[str] = []
        compare_items: List[Dict[str, Any]] = []

        for torrent in local_torrents:
            torrent_hash = torrent.get("hash")
            if not torrent_hash:
                continue
            title = torrent.get("name") or ""
            # 查找对应的站点任务，获取做种时间与 H&R 周期
            site_task = self.__find_site_task(title, site_tasks)
            seeding_time = str(site_task.get("seeding_time") or "") if site_task else ""
            hr_cycle = str(site_task.get("hr_cycle") or "") if site_task else ""
            # 剩余时间 = H&R周期 - 做种时间（还差多少做种时长才达标）
            remain_time = self.__calc_remain_time(hr_cycle, seeding_time)

            # 站点 H&R 页面仍存在该任务，说明未完成，跳过
            if site_task:
                completed_map.pop(torrent_hash, None)
                compare_items.append(
                    {
                        "name": title,
                        "status": "未完成",
                        "seeding_time": seeding_time,
                        "remain_time": remain_time,
                        "hr_cycle": hr_cycle,
                        "detail": "站点 H&R 页面仍存在，保种中",
                    }
                )
                continue

            # 站点已无该任务，视为已完成
            record = completed_map.get(torrent_hash)
            if not record:
                # 首次发现完成，记录时间
                completed_map[torrent_hash] = {
                    "name": title,
                    "completed_at": now_ts,
                    "completed_time": now_str,
                }
                logger.info(f"彩虹岛HR监控：任务已完成，开始计时 {title[:60]}")
                compare_items.append(
                    {
                        "name": title,
                        "status": "已完成",
                        "seeding_time": "-",
                        "remain_time": "-",
                        "hr_cycle": "-",
                        "detail": f"站点已无该任务，开始计时（{self._delete_delay_hours} 小时后删除）",
                    }
                )
                continue

            # 已记录，判断是否达到删除延迟
            completed_at = record.get("completed_at") or 0
            elapsed_hours = (now_ts - completed_at) / 3600
            if now_ts - completed_at >= delay_seconds:
                delete_hashes.append(torrent_hash)
                delete_names.append(title)
                compare_items.append(
                    {
                        "name": title,
                        "status": "待删除",
                        "seeding_time": "-",
                        "remain_time": "-",
                        "hr_cycle": "-",
                        "detail": f"已完成 {elapsed_hours:.1f} 小时，本次删除",
                    }
                )
            else:
                remain_hours = max(0, self._delete_delay_hours - elapsed_hours)
                compare_items.append(
                    {
                        "name": title,
                        "status": "已完成",
                        "seeding_time": "-",
                        "remain_time": "-",
                        "hr_cycle": "-",
                        "detail": f"已完成 {elapsed_hours:.1f} 小时，剩余 {remain_hours:.1f} 小时删除",
                    }
                )

        self._last_compare_items = compare_items

        # 执行删除
        if delete_hashes:
            logger.info(f"彩虹岛HR监控：删除 {len(delete_hashes)} 个已完成任务（含文件）")
            if downloader_obj.delete_torrents(delete_file=True, ids=delete_hashes):
                for torrent_hash in delete_hashes:
                    completed_map.pop(torrent_hash, None)
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【彩虹岛HR监控】",
                        text=f"已删除 {len(delete_hashes)} 个已完成 H&R 任务（含本地文件）：\n"
                             + "\n".join(f"- {name[:60]}" for name in delete_names[:20]),
                    )
            else:
                logger.error("彩虹岛HR监控：删除任务失败")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【彩虹岛HR监控】",
                        text="删除已完成任务失败，请检查下载器状态。",
                    )

        self.save_data(COMPLETED_DATA_KEY, completed_map)

    def __fetch_hr_tasks(self) -> Tuple[List[Dict[str, Any]], str]:
        """抓取并解析站点 H&R 页面。

        :return: (HR 任务列表, 错误信息)；成功时错误信息为空字符串
        """
        site = self.__get_site_config()
        if not site:
            return [], "未找到彩虹岛站点配置"

        try:
            res = RequestUtils(
                ua=site.get("ua"),
                cookies=site.get("cookie"),
                proxies=settings.PROXY if site.get("proxy") else None,
                timeout=site.get("timeout") or 20,
            ).get_res(url=self._hr_url)
        except Exception as err:
            return [], f"请求异常：{str(err)}"

        if res is None:
            return [], "无法连接站点"
        if res.status_code != 200:
            return [], f"站点返回状态码 {res.status_code}"

        return self.__parse_hr_page(res.text), ""

    @staticmethod
    def __parse_hr_page(html: str) -> List[Dict[str, Any]]:
        """解析 H&R 页面，提取未完成的 HR 任务。

        :param html: 页面 HTML
        :return: HR 任务列表
        """
        tasks: List[Dict[str, Any]] = []
        # 定位 H&R 数据表格（表头含"H&R百分比"）
        table_match = re.search(
            r'<table[^>]*>(?:(?!</table>).)*?H&R百分比(?:(?!</table>).)*?</table>',
            html, re.S
        )
        if not table_match:
            return tasks

        table_html = table_match.group(0)
        # 逐行解析
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(cells) < 6:
                continue

            # 标题：优先取 details.php 链接的 title 属性
            title = ""
            title_match = re.search(r'details\.php\?id=\d+[^>]*title="([^"]+)"', cells[1])
            if title_match:
                title = title_match.group(1).strip()
            else:
                title = re.sub(r'<[^>]+>', '', cells[1]).strip()

            # 跳过表头行
            if not title or title == "标题":
                continue

            def clean(cell: str) -> str:
                """清理单元格文本。"""
                return re.sub(r'<[^>]+>', '', cell).strip()

            tasks.append(
                {
                    "title": title,
                    "hr_percent": clean(cells[2]),
                    "remain_time": clean(cells[3]),
                    "hr_cycle": clean(cells[4]),
                    "seeding_time": clean(cells[5]),
                }
            )
        return tasks

    @staticmethod
    def __parse_hr_cycle_hours(hr_cycle: str) -> Optional[float]:
        """解析 H&R 周期文本为小时数。

        支持 "5天"、"3天"、"120小时"、"72h" 等格式。

        :param hr_cycle: H&R 周期文本
        :return: 小时数；无法解析返回 None
        """
        if not hr_cycle:
            return None
        text = str(hr_cycle).strip()
        # 天
        match = re.search(r'([\d.]+)\s*天', text)
        if match:
            return float(match.group(1)) * 24
        # 小时
        match = re.search(r'([\d.]+)\s*(?:小时|h|H)', text)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def __parse_seeding_hours(seeding_time: str) -> Optional[float]:
        """解析做种时间文本为小时数。

        支持 "3天02:48:29"、"14:38:16"、"2天01:15:50" 等格式。

        :param seeding_time: 做种时间文本
        :return: 小时数；无法解析返回 None
        """
        if not seeding_time:
            return None
        text = str(seeding_time).strip()
        total_hours = 0.0
        # 天数部分
        day_match = re.search(r'([\d.]+)\s*天', text)
        if day_match:
            total_hours += float(day_match.group(1)) * 24
            text = text[day_match.end():]
        # 时分秒部分
        time_match = re.search(r'(\d+):(\d+)(?::(\d+))?', text)
        if time_match:
            total_hours += int(time_match.group(1))
            total_hours += int(time_match.group(2)) / 60
            if time_match.group(3):
                total_hours += int(time_match.group(3)) / 3600
        elif not day_match:
            return None
        return total_hours

    @staticmethod
    def __normalize_title(title: str) -> str:
        """规范化标题，便于比对。

        去除中文、扩展名、多余空格，仅保留英文与数字特征，转小写。

        :param title: 原始标题
        :return: 规范化后的标题
        """
        # 去除扩展名
        text = re.sub(r'\.(mkv|mp4|iso|ts|avi)$', '', title.strip(), flags=re.I)
        # 去除中文字符（QB 任务标题常带中文前缀，站点标题为纯英文）
        text = re.sub(r'[\u4e00-\u9fff]+', ' ', text)
        # 统一分隔符为空格
        text = re.sub(r'[._\-]+', ' ', text)
        # 压缩空格
        text = re.sub(r'\s+', ' ', text).strip()
        return text.lower()

    def __calc_remain_time(self, hr_cycle: str, seeding_time: str) -> str:
        """计算剩余时间（H&R周期 - 做种时间）。

        :param hr_cycle: H&R 周期文本，如 "5天"
        :param seeding_time: 做种时间文本，如 "3天02:48:29"
        :return: 剩余时间文本，如 "45.2h"；无法计算返回 "-"
        """
        cycle_hours = self.__parse_hr_cycle_hours(hr_cycle)
        seeding_hours = self.__parse_seeding_hours(seeding_time)
        if cycle_hours is None or seeding_hours is None:
            return "-"
        remain_hours = cycle_hours - seeding_hours
        if remain_hours <= 0:
            return "已达标"
        return f"{remain_hours:.1f}h"

    def __find_site_task(self, local_title: str,
                         site_tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """根据本地任务标题查找对应的站点 H&R 任务。

        优先按片名（标题开头的英文词）匹配，避免因通用词（分辨率、编码等）
        导致误匹配。

        :param local_title: 本地任务标题
        :param site_tasks: 站点未完成 H&R 任务列表
        :return: 匹配的站点任务；未匹配返回 None
        """
        normalized = self.__normalize_title(local_title)
        if not normalized:
            return None

        # 提取片名特征：取规范化标题的前 3 个词（通常是片名）
        local_tokens = normalized.split()
        local_name = " ".join(local_tokens[:3])

        best_task = None
        best_score = 0.0
        for task in site_tasks:
            site_title = self.__normalize_title(task.get("title") or "")
            if not site_title:
                continue
            # 完全一致直接返回
            if site_title == normalized:
                return task
            site_tokens = site_title.split()
            site_name = " ".join(site_tokens[:3])
            # 片名完全一致，视为同一任务
            if local_name and local_name == site_name:
                return task
            # 片名互相包含，视为同一任务
            if local_name and site_name and (
                local_name in site_name or site_name in local_name
            ):
                return task
            # 兜底：按 token 交集比例评分，取最高分且需超过阈值
            local_set = set(local_tokens)
            site_set = set(site_tokens)
            if not local_set or not site_set:
                continue
            common = local_set & site_set
            score = len(common) / min(len(local_set), len(site_set))
            if score > best_score:
                best_score = score
                best_task = task

        # 兜底匹配需达到较高阈值，避免误匹配
        if best_task is not None and best_score >= 0.8:
            return best_task
        return None

    def __match_site_title(self, local_title: str, site_titles: set) -> bool:
        """判断本地任务标题是否匹配站点未完成任务。

        :param local_title: 本地任务标题
        :param site_titles: 站点未完成任务标题集合
        :return: 是否匹配
        """
        normalized = self.__normalize_title(local_title)
        if not normalized:
            return False
        if normalized in site_titles:
            return True
        # 模糊匹配：比较规范化后的英文特征片段
        local_tokens = set(normalized.split())
        for site_title in site_titles:
            if not site_title:
                continue
            site_tokens = set(site_title.split())
            if not site_tokens:
                continue
            # 交集占比达到 60% 视为同一任务
            common = local_tokens & site_tokens
            if len(common) / min(len(local_tokens), len(site_tokens)) >= 0.6:
                return True
        return False

    def __get_downloader(self) -> Optional[Any]:
        """获取下载器实例。

        :return: 下载器实例；获取失败返回 None
        """
        services = DownloaderHelper().get_services(name_filters=[self._downloader])
        if not services:
            return None
        for service_name, service_info in services.items():
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"彩虹岛HR监控：下载器 {service_name} 不是 QB 类型")
                continue
            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"彩虹岛HR监控：下载器 {service_name} 未连接")
                continue
            return downloader_obj
        return None

    @staticmethod
    def __get_site_config() -> Optional[dict]:
        """获取彩虹岛站点配置。

        :return: 站点配置字典；未找到返回 None
        """
        from app.db.site_oper import SiteOper
        site = SiteOper().get_by_domain("ptchdbits.co")
        if not site:
            return None
        return {
            "name": site.name,
            "domain": site.domain,
            "url": site.url,
            "cookie": site.cookie,
            "ua": site.ua,
            "proxy": site.proxy,
            "timeout": site.timeout,
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None
