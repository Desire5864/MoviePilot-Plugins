from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from qbittorrentapi import TorrentState

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# QB 活动状态：仅做种上传中（uploading）
# 命中监控分类且处于该状态的种子会被立即暂停（暂停后状态变为 stoppedUP）
ACTIVE_STATES = {
    TorrentState.UPLOADING.value,
}

# QB 已停止（暂停）状态
STOPPED_STATES = {
    TorrentState.STOPPED_UPLOAD.value,
    TorrentState.STOPPED_DOWNLOAD.value,
    TorrentState.PAUSED_UPLOAD.value,
    TorrentState.PAUSED_DOWNLOAD.value,
}

# 持久化停止时间记录的键名
STOP_TIME_DATA_KEY = "stopped_time_map"


class QbCategoryPause(_PluginBase):
    """QB 分类做种上传监控暂停插件。

    监控指定 QB 下载器中指定分类的种子，一旦发现处于做种上传中
    （uploading）状态的种子，立即将其暂停（暂停后状态变为 stoppedUP）。
    """

    # 插件名称
    plugin_name = "QB分类活动暂停"
    # 插件描述
    plugin_desc = "监控QB指定分类，一旦有做种上传中（uploading）的种子立即暂停。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.3.0"
    # 插件作者
    plugin_author = "local"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "qbcategorypause_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _notify: bool = False
    _downloaders: List[str] = []
    _categories: List[str] = []
    _interval: int = 30
    # 是否启用自动恢复
    _resume_enabled: bool = False
    # 停止多久后自动恢复（分钟）
    _resume_minutes: int = 60
    # 记录上一次暂停的种子哈希，避免重复通知
    _last_paused_hashes: List[str] = []

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        # 停止现有任务
        self.stop_service()

        # 重置状态
        self._enabled = False
        self._notify = False
        self._downloaders = []
        self._categories = []
        self._interval = 30
        self._resume_enabled = False
        self._resume_minutes = 60
        self._last_paused_hashes = []

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._downloaders = config.get("downloaders") or []
        # 分类支持逗号或换行分隔
        categories_raw = config.get("categories") or ""
        self._categories = [
            category.strip()
            for category in str(categories_raw).replace("\n", ",").split(",")
            if category.strip()
        ]
        try:
            self._interval = max(5, int(config.get("interval") or 30))
        except (TypeError, ValueError):
            self._interval = 30
        self._resume_enabled = bool(config.get("resume_enabled"))
        try:
            self._resume_minutes = max(1, int(config.get("resume_minutes") or 60))
        except (TypeError, ValueError):
            self._resume_minutes = 60

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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval",
                                            "label": "检查间隔（秒）",
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
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": [
                                                {"title": config.name, "value": config.name}
                                                for config in DownloaderHelper().get_configs().values()
                                            ],
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "categories",
                                            "label": "监控分类",
                                            "placeholder": "用,分隔多个分类，例如：刷流,PT下载",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "resume_enabled",
                                            "label": "启用自动恢复",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resume_minutes",
                                            "label": "停止多久后自动恢复（分钟）",
                                            "placeholder": "默认60，最小1",
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
                                            "text": "插件会按设定间隔检查所选下载器中指定分类的种子，"
                                                    "一旦发现处于做种上传中（uploading）状态的种子，"
                                                    "立即将其暂停（暂停后状态变为 stoppedUP）。"
                                                    "启用自动恢复后，所有处于 stoppedUP 状态的任务"
                                                    "在停止超过设定时间后会自动重新开始。",
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
            "downloaders": [],
            "categories": "",
            "interval": 30,
            "resume_enabled": False,
            "resume_minutes": 60,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: Vuetify 详情页结构
        """
        if not self._enabled:
            return None

        # 展示当前配置概览
        downloaders_text = "、".join(self._downloaders) if self._downloaders else "未配置"
        categories_text = "、".join(self._categories) if self._categories else "未配置"
        resume_text = (
            f"已启用（停止超过 {self._resume_minutes} 分钟自动恢复）"
            if self._resume_enabled
            else "未启用"
        )

        page_content: List[dict] = [
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
                                    "text": f"监控下载器：{downloaders_text}；"
                                            f"监控分类：{categories_text}；"
                                            f"检查间隔：{self._interval} 秒；"
                                            f"自动恢复：{resume_text}",
                                },
                            }
                        ],
                    }
                ],
            }
        ]

        # 实时查询各下载器中 uploading（正在上传）的种子
        uploading_info = self.__collect_uploading_torrents()
        if uploading_info is None:
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
                                        "text": "未能获取下载器数据，请检查下载器配置与连接状态。",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
            return page_content

        total_count = sum(len(items) for items in uploading_info.values())
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
                                    "type": "success" if total_count else "info",
                                    "variant": "tonal",
                                    "text": f"当前 uploading（正在上传）任务总数：{total_count}",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        if not total_count:
            return page_content

        # 按下载器展示分类统计与明细
        for downloader_name, items in uploading_info.items():
            if not items:
                continue

            # 分类统计
            category_counter: Dict[str, int] = {}
            for item in items:
                category_counter[item["category"]] = category_counter.get(item["category"], 0) + 1
            category_summary = "；".join(
                f"{category}：{count}" for category, count in sorted(
                    category_counter.items(), key=lambda pair: pair[1], reverse=True
                )
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
                                        "text": f"【{downloader_name}】共 {len(items)} 个，"
                                                f"分类统计：{category_summary}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            # 明细表格
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
                                                        {"component": "th", "text": "分类"},
                                                        {"component": "th", "text": "状态"},
                                                        {"component": "th", "text": "种子名称"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "component": "tbody",
                                            "content": [
                                                {
                                                    "component": "tr",
                                                    "content": [
                                                        {"component": "td", "text": item["category"]},
                                                        {"component": "td", "text": item["state"]},
                                                        {"component": "td", "text": item["name"]},
                                                    ],
                                                }
                                                for item in items
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        # 展示已停止（stoppedUP）任务及自动恢复倒计时
        stopped_info = self.__collect_stopped_torrents()
        if stopped_info:
            stop_time_map: Dict[str, float] = self.get_data(STOP_TIME_DATA_KEY) or {}
            now_ts = datetime.now().timestamp()
            total_stopped = sum(len(items) for items in stopped_info.values())
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
                                        "text": f"当前已停止（stoppedUP）任务总数：{total_stopped}"
                                                + (
                                                    f"；自动恢复已启用，停止超过 {self._resume_minutes} 分钟将自动恢复"
                                                    if self._resume_enabled
                                                    else "；自动恢复未启用"
                                                ),
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

            for downloader_name, items in stopped_info.items():
                if not items:
                    continue
                rows = []
                for item in items:
                    stop_ts = stop_time_map.get(item["hash"])
                    if stop_ts:
                        elapsed_minutes = int((now_ts - stop_ts) / 60)
                        remain_minutes = max(0, self._resume_minutes - elapsed_minutes)
                        countdown = f"已停止 {elapsed_minutes} 分钟，剩余 {remain_minutes} 分钟"
                    else:
                        countdown = "未记录"
                    rows.append(
                        {
                            "component": "tr",
                            "content": [
                                {"component": "td", "text": item["category"]},
                                {"component": "td", "text": item["state"]},
                                {"component": "td", "text": countdown},
                                {"component": "td", "text": item["name"]},
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
                                                            {"component": "th", "text": "分类"},
                                                            {"component": "th", "text": "状态"},
                                                            {"component": "th", "text": "恢复倒计时"},
                                                            {"component": "th", "text": "种子名称"},
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

    def __collect_stopped_torrents(self) -> Optional[Dict[str, List[Dict[str, str]]]]:
        """收集各下载器中已停止（stoppedUP）状态的种子。

        :return: 下载器名称到种子明细列表的映射；获取失败时返回 None
        """
        if not self._downloaders:
            return {}

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            return None

        result: Dict[str, List[Dict[str, str]]] = {}
        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                continue

            items: List[Dict[str, str]] = []
            for torrent in torrents:
                if torrent.get("state") not in STOPPED_STATES:
                    continue
                items.append(
                    {
                        "category": torrent.get("category") or "(无分类)",
                        "state": torrent.get("state") or "",
                        "name": torrent.get("name") or "",
                        "hash": torrent.get("hash") or "",
                    }
                )
            result[service_name] = items

        return result

    def __collect_uploading_torrents(self) -> Optional[Dict[str, List[Dict[str, str]]]]:
        """收集各下载器中 uploading（正在上传）状态的种子。

        :return: 下载器名称到种子明细列表的映射；获取失败时返回 None
        """
        if not self._downloaders:
            return {}

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            return None

        result: Dict[str, List[Dict[str, str]]] = {}
        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                continue

            items: List[Dict[str, str]] = []
            for torrent in torrents:
                if torrent.get("state") not in ACTIVE_STATES:
                    continue
                items.append(
                    {
                        "category": torrent.get("category") or "(无分类)",
                        "state": torrent.get("state") or "",
                        "name": torrent.get("name") or "",
                    }
                )
            result[service_name] = items

        return result

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。

        :return: 定时服务定义列表
        """
        services: List[Dict[str, Any]] = []
        if not self._enabled or not self._downloaders:
            return services

        # 暂停服务：需要配置监控分类
        if self._categories:
            services.append(
                {
                    "id": "QbCategoryPause",
                    "name": "QB分类活动监控暂停",
                    "trigger": "interval",
                    "func": self.check_and_pause,
                    "kwargs": {"seconds": self._interval},
                }
            )

        # 自动恢复服务：需要启用自动恢复
        if self._resume_enabled:
            services.append(
                {
                    "id": "QbCategoryResume",
                    "name": "QB已停止任务自动恢复",
                    "trigger": "interval",
                    "func": self.check_and_resume,
                    "kwargs": {"seconds": self._interval},
                }
            )

        return services

    def check_and_pause(self) -> None:
        """检查指定分类的活动种子并立即暂停。"""
        if not self._enabled:
            return

        if not self._downloaders or not self._categories:
            logger.warning("QB分类活动暂停：未配置下载器或监控分类，跳过检查")
            return

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("QB分类活动暂停：获取下载器实例失败，请检查配置")
            return

        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"QB分类活动暂停：下载器 {service_name} 不是 QB 类型，跳过")
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"QB分类活动暂停：下载器 {service_name} 未连接，跳过")
                continue

            self.__pause_active_torrents(service_name, downloader_obj)

    def check_and_resume(self) -> None:
        """检查所有已停止（stoppedUP）任务，超过设定时间后自动恢复。"""
        if not self._enabled or not self._resume_enabled:
            return

        if not self._downloaders:
            logger.warning("QB已停止任务自动恢复：未配置下载器，跳过检查")
            return

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("QB已停止任务自动恢复：获取下载器实例失败，请检查配置")
            return

        # 读取持久化的停止时间记录
        stop_time_map: Dict[str, float] = self.get_data(STOP_TIME_DATA_KEY) or {}
        now_ts = datetime.now().timestamp()
        threshold_seconds = self._resume_minutes * 60

        for service_name, service_info in services.items():
            # 仅处理 QB 下载器
            if not DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info):
                continue

            downloader_obj = service_info.instance
            if not downloader_obj or downloader_obj.is_inactive():
                logger.warning(f"QB已停止任务自动恢复：下载器 {service_name} 未连接，跳过")
                continue

            torrents, error = downloader_obj.get_torrents()
            if error:
                logger.error(f"QB已停止任务自动恢复：获取下载器 {service_name} 种子失败")
                continue

            stopped_hashes: List[str] = []
            resume_hashes: List[str] = []
            resume_names: List[str] = []
            for torrent in torrents:
                torrent_hash = torrent.get("hash")
                if not torrent_hash:
                    continue
                if torrent.get("state") not in STOPPED_STATES:
                    continue

                stopped_hashes.append(torrent_hash)
                # 首次发现该停止任务时记录时间；已存在则沿用原记录
                if torrent_hash not in stop_time_map:
                    # 优先使用 QB 的最后活动时间作为停止起始时间
                    last_activity = torrent.get("last_activity")
                    stop_time_map[torrent_hash] = (
                        float(last_activity) if last_activity and last_activity > 0 else now_ts
                    )

                # 停止时间超过阈值则恢复
                if now_ts - stop_time_map[torrent_hash] >= threshold_seconds:
                    resume_hashes.append(torrent_hash)
                    resume_names.append(torrent.get("name") or torrent_hash)

            # 清理已不再处于停止状态的记录
            for torrent_hash in list(stop_time_map.keys()):
                if torrent_hash not in stopped_hashes:
                    stop_time_map.pop(torrent_hash, None)

            if not resume_hashes:
                continue

            logger.info(
                f"QB已停止任务自动恢复：下载器 {service_name} 有 {len(resume_hashes)} 个任务"
                f"停止超过 {self._resume_minutes} 分钟，准备恢复"
            )
            if downloader_obj.start_torrents(ids=resume_hashes):
                logger.info(f"QB已停止任务自动恢复：下载器 {service_name} 已恢复 {len(resume_hashes)} 个任务")
                for torrent_hash in resume_hashes:
                    stop_time_map.pop(torrent_hash, None)
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【QB已停止任务自动恢复】",
                        text=f"下载器：{service_name}\n"
                             f"停止超过 {self._resume_minutes} 分钟，已恢复 {len(resume_hashes)} 个任务：\n"
                             + "\n".join(f"- {name}" for name in resume_names[:20]),
                    )
            else:
                logger.error(f"QB已停止任务自动恢复：下载器 {service_name} 恢复任务失败")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【QB已停止任务自动恢复】",
                        text=f"下载器 {service_name} 恢复任务失败，请检查下载器状态。",
                    )

        # 持久化停止时间记录
        self.save_data(STOP_TIME_DATA_KEY, stop_time_map)

    def __pause_active_torrents(self, downloader_name: str, downloader_obj: Any) -> None:
        """暂停指定下载器中监控分类的活动种子。

        :param downloader_name: 下载器名称
        :param downloader_obj: 下载器实例
        """
        torrents, error = downloader_obj.get_torrents()
        if error:
            logger.error(f"QB分类活动暂停：获取下载器 {downloader_name} 种子失败")
            return

        if not torrents:
            return

        # 筛选出监控分类中处于做种上传中（uploading）状态的种子
        active_hashes: List[str] = []
        active_names: List[str] = []
        for torrent in torrents:
            category = torrent.get("category") or ""
            if category not in self._categories:
                continue
            # 仅做种上传中（uploading）视为活动状态
            if torrent.get("state") in ACTIVE_STATES:
                active_hashes.append(torrent.get("hash"))
                active_names.append(torrent.get("name"))

        if not active_hashes:
            # 无活动种子时清空记录
            self._last_paused_hashes = []
            return

        logger.info(
            f"QB分类活动暂停：下载器 {downloader_name} 发现 {len(active_hashes)} 个活动种子，准备暂停"
        )

        if downloader_obj.stop_torrents(ids=active_hashes):
            logger.info(f"QB分类活动暂停：下载器 {downloader_name} 已暂停 {len(active_hashes)} 个种子")
            if self._notify and active_hashes != self._last_paused_hashes:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【QB分类活动暂停】",
                    text=f"下载器：{downloader_name}\n"
                         f"监控分类：{'、'.join(self._categories)}\n"
                         f"已暂停 {len(active_hashes)} 个活动种子：\n"
                         + "\n".join(f"- {name}" for name in active_names[:20]),
                )
            self._last_paused_hashes = active_hashes
        else:
            logger.error(f"QB分类活动暂停：下载器 {downloader_name} 暂停种子失败")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【QB分类活动暂停】",
                    text=f"下载器 {downloader_name} 暂停种子失败，请检查下载器状态。",
                )

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._last_paused_hashes = []
