import hashlib
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import qbittorrentapi
from qbittorrentapi import TorrentState

from app import schemas
from app.core.config import settings
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

# 持久化迁移任务队列的键名
MIGRATE_DATA_KEY = "migrate_jobs"

# qB 容器路径前缀 -> MoviePilot 容器路径前缀
# （两个容器的挂载点不同名，插件跑在 MP 容器里要靠这张表换算真实路径）
QB_TO_MP_PATH_MAP = (
    ("/保种", "/CloudNAS/CloudDrive/115open/保种"),
    ("/下载", "/下载"),
    ("/原盘", "/原盘"),
    ("/完结", "/完结"),
    ("/热更", "/热更"),
    ("/ISO", "/ISO"),
    ("/download", "/download"),
)


class QbCategoryPause(_PluginBase):
    """QB 分类做种上传监控暂停插件。

    监控指定 QB 下载器中指定分类的种子，一旦发现处于做种上传中
    （uploading）状态的种子，立即将其暂停（暂停后状态变为 stoppedUP）。
    """

    # 插件名称
    plugin_name = "QB分类活动暂停"
    # 插件描述
    plugin_desc = "监控QB指定分类并暂停活动种子，支持把已停止种子迁移到本地做种。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.5.3"
    # 插件作者
    plugin_author = "Desire5864"
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

    # ---- 迁移相关（把种子内容搬到另一处存储后重新做种）----
    # 目标根目录（qB 容器视角路径，如 /download）
    _migrate_target: str = ""
    # 迁移后归入的新分类
    _migrate_category: str = ""
    # 重新添加时跳过哈希校验
    _migrate_skip_check: bool = False
    # 删除原任务时是否连文件一起删
    _migrate_delete_files: bool = False
    # 复制完成后是否逐块校验完整性
    _migrate_verify: bool = True
    # 迁移工作线程是否在跑
    _migrate_running: bool = False

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
        self._migrate_target = ""
        self._migrate_category = ""
        self._migrate_skip_check = False
        self._migrate_delete_files = False
        self._migrate_verify = True

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

        # 迁移配置
        self._migrate_target = str(config.get("migrate_target") or "").strip()
        self._migrate_category = str(config.get("migrate_category") or "").strip()
        self._migrate_skip_check = bool(config.get("migrate_skip_check"))
        self._migrate_delete_files = bool(config.get("migrate_delete_files"))
        self._migrate_verify = bool(config.get("migrate_verify", True))

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
        return [
            {
                "path": "/migrate_torrent",
                "endpoint": self.api_migrate_torrent,
                "methods": ["GET"],
                "summary": "迁移单个种子",
                "description": "把指定已停止种子的内容复制到目标目录，"
                               "校验通过后删除原任务并以新分类重新添加。",
            },
            {
                "path": "/migrate_all",
                "endpoint": self.api_migrate_all,
                "methods": ["GET"],
                "summary": "迁移全部已停止种子",
                "description": "把当前所有已停止的种子批量加入迁移队列。",
            },
        ]

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
                                            "text": "【种子迁移】填好目标目录与新分类后，"
                                                    "插件详情页的已停止列表会多出「迁移」按钮。"
                                                    "点击后先把内容复制到目标目录，"
                                                    "校验通过再删除原任务并以新分类重新添加，"
                                                    "过程在后台进行，不阻塞页面。",
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
                                            "model": "migrate_target",
                                            "label": "迁移目标目录（qB 视角路径）",
                                            "placeholder": "例如 /download，留空则不显示迁移按钮",
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
                                            "model": "migrate_category",
                                            "label": "迁移后归入的分类",
                                            "placeholder": "例如 本地保种，不存在会自动创建",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "migrate_verify",
                                            "label": "复制后逐块校验",
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
                                            "model": "migrate_skip_check",
                                            "label": "重加时跳过哈希校验",
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
                                            "model": "migrate_delete_files",
                                            "label": "删除原任务时连文件一起删",
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
            "migrate_target": "",
            "migrate_category": "",
            "migrate_skip_check": False,
            "migrate_delete_files": False,
            "migrate_verify": True,
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
        fetch_failed = uploading_info is None
        if fetch_failed:
            # 取不到数据时只降级提示，不要 return —— 否则「已停止」列表与
            # 迁移队列也会被一起吞掉，下载器短暂掉线就看不到迁移入口了。
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
            uploading_info = {}

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
                                    "text": (
                                        f"当前 uploading（正在上传）任务总数：{total_count}"
                                        if not fetch_failed
                                        else "下载器数据获取失败，uploading 数量未知"
                                    ),
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # 按下载器展示分类统计与明细
        # 注意：没有 uploading 任务时不能提前 return —— 否则下面的「已停止」列表
        # 与迁移队列会被一起吞掉，页面只剩两条提示，迁移入口直接消失。
        for downloader_name, items in (uploading_info.items() if total_count else ()):
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

            show_migrate = bool(self._migrate_target)

            # 批量迁移入口
            if show_migrate:
                page_content.append(
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "size": "small",
                                            "color": "primary",
                                            "variant": "flat",
                                            "prepend-icon": "mdi-content-duplicate",
                                        },
                                        "text": f"迁移全部已停止（{total_stopped} 个）",
                                        "events": {
                                            "click": {
                                                "api": f"plugin/{self.__class__.__name__}"
                                                       f"/migrate_all",
                                                "method": "get",
                                                "params": {"apikey": settings.API_TOKEN},
                                            }
                                        },
                                    },
                                    {
                                        "component": "span",
                                        "props": {
                                            "class": "text-caption ms-3",
                                            "style": "opacity:.7;",
                                        },
                                        "text": f"目标目录 {self._migrate_target}"
                                                f" → 分类 {self._migrate_category or '（未设置）'}",
                                    },
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
                    cells = [
                        {"component": "td", "text": item["category"]},
                        {"component": "td", "text": item["state"]},
                        {"component": "td", "text": countdown},
                        {"component": "td", "text": item["name"]},
                    ]
                    if show_migrate:
                        cells.append({
                            "component": "td",
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "size": "x-small",
                                        "color": "primary",
                                        "variant": "tonal",
                                    },
                                    "text": "迁移",
                                    "events": {
                                        "click": {
                                            "api": f"plugin/{self.__class__.__name__}"
                                                   f"/migrate_torrent",
                                            "method": "get",
                                            "params": {
                                                "apikey": settings.API_TOKEN,
                                                "hash": item["hash"],
                                            },
                                        }
                                    },
                                }
                            ],
                        })
                    rows.append({"component": "tr", "content": cells})

                head_cells = [
                    {"component": "th", "text": "分类"},
                    {"component": "th", "text": "状态"},
                    {"component": "th", "text": "恢复倒计时"},
                    {"component": "th", "text": "种子名称"},
                ]
                if show_migrate:
                    head_cells.append({"component": "th", "text": "操作"})

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
                                                        "content": head_cells,
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

        # 迁移队列状态
        page_content.extend(self.__render_migrate_jobs())

        # 面板在对话框右下角内侧 12px 固定着一个 56px 的齿轮悬浮按钮（VFab），它不在
        # 插件页面树里、插件改不了它，实测它恒定压住「内容最下 46.6px × 最右 48px」。
        # 所以让页面最下面那一块往左让出一点 —— 选「让宽」而不是补底部空白：
        # 不占高度，也不受视口高矮影响（矮窗口补空白会顶穿）。
        self.__avoid_fab(page_content)

        return page_content

    @staticmethod
    def __avoid_fab(page_content: List[dict], gap: int = 56) -> None:
        """把页面最下面那一块往左让出 gap 像素，避开右下角的齿轮悬浮按钮。

        只处理最后一块：按钮锚定的是内容右下角，只有滚到底时最后一块的右下角
        才会被压。中间那些块滚动时位置随意，不需要动。

        :param page_content: get_page 的页面结构（原地修改）
        :param gap: 让出的宽度；实测压住区宽 48px，取 56 留 8px 余量
        """
        if not page_content:
            return
        block = page_content[-1]
        if not isinstance(block, dict):
            return
        # 一块的结构是 VRow -> content[VCol]，VCol 直接就是 VRow 的孩子，
        # 别再往 VCol 的 content 里找一层（那里是 VTable/VAlert）。
        for col in block.get("content") or []:
            if not isinstance(col, dict) or col.get("component") != "VCol":
                continue
            props = col.setdefault("props", {})
            style = (props.get("style") or "").strip()
            if "padding-right" not in style:
                props["style"] = (style + f";padding-right:{gap}px;").lstrip(";")
            return

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
        active_categories: List[str] = []
        for torrent in torrents:
            category = torrent.get("category") or ""
            if category not in self._categories:
                continue
            # 仅做种上传中（uploading）视为活动状态
            if torrent.get("state") in ACTIVE_STATES:
                active_hashes.append(torrent.get("hash"))
                active_names.append(torrent.get("name"))
                active_categories.append(category)

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
                # 按分类统计暂停数量
                category_counter: Dict[str, int] = {}
                for category in active_categories:
                    category_counter[category] = category_counter.get(category, 0) + 1
                category_summary = "；".join(
                    f"已暂停{category} {count} 个活动种子"
                    for category, count in sorted(
                        category_counter.items(), key=lambda pair: pair[1], reverse=True
                    )
                )
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【QB分类活动暂停】",
                    text=f"下载器：{downloader_name}\n"
                         f"监控分类：{'、'.join(self._categories)}\n"
                         f"{category_summary}：\n"
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

    # ------------------------------------------------------------------
    # 种子迁移：复制内容到目标目录 → 校验 → 删原任务 → 以新分类重加
    # ------------------------------------------------------------------

    @staticmethod
    def __map_qb_path(path: str) -> str:
        """把 qB 容器视角的路径换算成 MoviePilot 容器里的真实路径。

        两个容器的挂载点不同名（qB 里是 /保种，MP 里是 /CloudNAS/CloudDrive/...），
        插件跑在 MP 容器里，必须换算后才能读写。
        """
        if not path:
            return path
        for qb_prefix, mp_prefix in QB_TO_MP_PATH_MAP:
            if path == qb_prefix or path.startswith(qb_prefix + "/"):
                return mp_prefix + path[len(qb_prefix):]
        return path

    def __qb_client(self, downloader_name: str) -> Optional[qbittorrentapi.Client]:
        """构造指定下载器的原生 qbittorrentapi 客户端。

        用原生客户端而不是 MP 的封装，是因为本流程需要两个它没暴露的能力：
        导出 .torrent（torrents_export）与跳过哈希校验（is_skip_checking）。
        """
        try:
            conf = DownloaderHelper().get_configs().get(downloader_name)
        except Exception as e:
            logger.error(f"QB分类活动暂停：读取下载器配置失败：{e}")
            return None
        if not conf:
            return None
        cfg = conf.config or {}
        try:
            client = qbittorrentapi.Client(
                host=cfg.get("host"),
                username=cfg.get("username"),
                password=cfg.get("password"),
            )
            client.auth_log_in()
            return client
        except Exception as e:
            logger.error(f"QB分类活动暂停：连接下载器 {downloader_name} 失败：{e}")
            return None

    def __load_jobs(self) -> Dict[str, Dict[str, Any]]:
        return self.get_data(MIGRATE_DATA_KEY) or {}

    def __save_jobs(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.save_data(MIGRATE_DATA_KEY, jobs)

    def __torrent_brief(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """在已配置的下载器里定位种子，返回它所属下载器与关键字段。"""
        services = DownloaderHelper().get_services(
            name_filters=self._downloaders or None)
        for service_name, service_info in (services or {}).items():
            if not DownloaderHelper().is_downloader(
                    service_type="qbittorrent", service=service_info):
                continue
            obj = service_info.instance
            if not obj or obj.is_inactive():
                continue
            torrents, error = obj.get_torrents(ids=[torrent_hash])
            if error or not torrents:
                continue
            torrent = torrents[0]
            return {
                "downloader": service_name,
                "name": torrent.get("name"),
                "category": torrent.get("category"),
                "size": torrent.get("size"),
                "save_path": torrent.get("save_path"),
                "content_path": torrent.get("content_path"),
            }
        return None

    def api_migrate_torrent(self, hash: str = None,
                            apikey: str = None) -> schemas.Response:
        """API：把单个种子加入迁移队列。"""
        if not self._migrate_target:
            return schemas.Response(success=False, message="未配置迁移目标目录")
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        added, _skipped = self.__enqueue([hash])
        if not added:
            return schemas.Response(success=False,
                                    message="未加入队列：该种子已在队列中，或已不存在")
        return schemas.Response(success=True,
                                message=f"已加入迁移队列，共 {added} 个任务")

    def api_migrate_all(self, apikey: str = None) -> schemas.Response:
        """API：把所有已停止的种子加入迁移队列。"""
        if not self._migrate_target:
            return schemas.Response(success=False, message="未配置迁移目标目录")
        stopped = self.__collect_stopped_torrents() or {}
        hashes = [item["hash"] for items in stopped.values()
                  for item in items if item.get("hash")]
        if not hashes:
            return schemas.Response(success=False, message="当前没有已停止的种子")
        added, skipped = self.__enqueue(hashes)
        return schemas.Response(
            success=bool(added),
            message=f"新加入 {added} 个，跳过 {skipped} 个（已在队列或已不存在）")

    def __enqueue(self, hashes: List[str]) -> Tuple[int, int]:
        """写入迁移队列，并确保后台线程在跑。"""
        jobs = self.__load_jobs()
        added = skipped = 0
        for torrent_hash in hashes:
            if not torrent_hash:
                continue
            exist = jobs.get(torrent_hash)
            if exist and exist.get("state") in (
                    "queued", "copying", "verifying", "swapping"):
                skipped += 1
                continue
            brief = self.__torrent_brief(torrent_hash)
            if not brief:
                skipped += 1
                continue
            jobs[torrent_hash] = {
                "hash": torrent_hash,
                "name": brief.get("name") or torrent_hash,
                "downloader": brief.get("downloader") or "",
                "category": brief.get("category") or "",
                "state": "queued",
                "total": int(brief.get("size") or 0),
                "done": 0,
                "message": "排队中",
                "ts": datetime.now().timestamp(),
            }
            added += 1
        self.__save_jobs(jobs)
        if added:
            self.__start_worker()
        return added, skipped

    def __start_worker(self) -> None:
        """启动后台迁移线程（已有线程在跑时不重复启动）。"""
        if self._migrate_running:
            return
        self._migrate_running = True
        threading.Thread(target=self.__migrate_worker,
                         name="QbCategoryPauseMigrate", daemon=True).start()

    def __migrate_worker(self) -> None:
        """后台工作线程：按入队顺序逐个迁移。"""
        try:
            while True:
                jobs = self.__load_jobs()
                pending = [job for job in jobs.values()
                           if job.get("state") == "queued"]
                if not pending:
                    break
                pending.sort(key=lambda job: job.get("ts") or 0)
                job = pending[0]
                try:
                    self.__migrate_one(job, jobs)
                except Exception as e:
                    logger.error(f"QB分类活动暂停：迁移 {job.get('name')} 异常：{e}")
                    job["state"] = "failed"
                    job["message"] = f"异常：{e}"
                    self.__save_jobs(jobs)
        finally:
            self._migrate_running = False

    def __migrate_one(self, job: Dict[str, Any],
                      jobs: Dict[str, Dict[str, Any]]) -> None:
        """迁移单个种子。任一步失败都保留原任务，不制造"两头空"。"""
        torrent_hash = job.get("hash")
        name = job.get("name") or torrent_hash

        brief = self.__torrent_brief(torrent_hash)
        if not brief:
            job["state"] = "failed"
            job["message"] = "找不到该种子，可能已被删除"
            self.__save_jobs(jobs)
            return

        client = self.__qb_client(brief["downloader"])
        if not client:
            job["state"] = "failed"
            job["message"] = f"无法连接下载器 {brief['downloader']}"
            self.__save_jobs(jobs)
            return

        target_qb = self._migrate_target
        target_mp = self.__map_qb_path(target_qb)
        src_mp = self.__map_qb_path(brief.get("content_path") or "")
        dst_mp = os.path.join(target_mp, name)

        if not os.path.exists(src_mp):
            job["state"] = "failed"
            job["message"] = f"源路径不存在：{src_mp}"
            self.__save_jobs(jobs)
            return
        if os.path.abspath(src_mp) == os.path.abspath(dst_mp):
            job["state"] = "failed"
            job["message"] = "源路径与目标路径相同，无需迁移"
            self.__save_jobs(jobs)
            return

        # ① 复制
        job["state"] = "copying"
        job["message"] = f"复制到 {dst_mp}"
        self.__save_jobs(jobs)
        if not self.__copy_tree(src_mp, dst_mp, job, jobs):
            return

        # ② 逐块校验（只比大小验不出静默损坏）
        if self._migrate_verify:
            job["state"] = "verifying"
            job["message"] = "逐块校验中"
            self.__save_jobs(jobs)
            ok, detail = self.__verify_pieces(client, torrent_hash, target_mp)
            if not ok:
                job["state"] = "failed"
                job["message"] = f"校验未通过（{detail}），原任务未动"
                self.__save_jobs(jobs)
                return
            job["message"] = f"校验通过：{detail}"
            self.__save_jobs(jobs)

        # ③ 导出 .torrent 备份 —— 删掉原任务后这是唯一的回滚凭据
        try:
            torrent_bytes = client.torrents_export(torrent_hash=torrent_hash)
        except Exception as e:
            job["state"] = "failed"
            job["message"] = f"导出种子失败：{e}"
            self.__save_jobs(jobs)
            return
        if not torrent_bytes:
            job["state"] = "failed"
            job["message"] = "导出种子为空，原任务未动"
            self.__save_jobs(jobs)
            return
        try:
            backup_dir = os.path.join(self.get_data_path(), "torrents")
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f"{torrent_hash}.torrent"), "wb") as fp:
                fp.write(torrent_bytes)
        except Exception as e:
            logger.warning(f"QB分类活动暂停：备份种子文件失败（不阻断流程）：{e}")

        # ④ 确保目标分类存在
        if self._migrate_category:
            try:
                client.torrents_create_category(
                    name=self._migrate_category, save_path=target_qb)
            except Exception as e:
                logger.warning(f"QB分类活动暂停：创建分类 {self._migrate_category} "
                               f"失败（可能已存在）：{e}")

        # ⑤ 删除原任务
        job["state"] = "swapping"
        job["message"] = "删除原任务"
        self.__save_jobs(jobs)
        try:
            client.torrents_delete(delete_files=self._migrate_delete_files,
                                   torrent_hashes=torrent_hash)
        except Exception as e:
            job["state"] = "failed"
            job["message"] = f"删除原任务失败：{e}（内容已复制，原任务未动）"
            self.__save_jobs(jobs)
            return

        # ⑥ 以新分类重新添加
        job["message"] = "重新添加中"
        self.__save_jobs(jobs)
        try:
            client.torrents_add(
                torrent_files=torrent_bytes,
                save_path=target_qb,
                category=self._migrate_category or None,
                is_skip_checking=self._migrate_skip_check,
                is_paused=False,
            )
        except Exception as e:
            job["state"] = "failed"
            job["message"] = (f"重新添加失败：{e}；种子已备份到插件数据目录 "
                              f"torrents/{torrent_hash}.torrent，可手动恢复")
            self.__save_jobs(jobs)
            return

        # ⑦ 复查生效状态
        time.sleep(2)
        try:
            infos = client.torrents_info(torrent_hashes=torrent_hash)
        except Exception:
            infos = []
        if infos:
            info = infos[0]
            progress = float(info.get("progress") or 0)
            job["state"] = "done"
            job["message"] = (f"{info.get('state')} / {progress * 100:.0f}% / "
                              f"{info.get('category')} / {info.get('save_path')}")
        else:
            job["state"] = "done"
            job["message"] = "已重新添加，请刷新页面确认"
        self.__save_jobs(jobs)
        logger.info(f"QB分类活动暂停：迁移完成 {name} → {dst_mp}")

    def __copy_tree(self, src: str, dst: str, job: Dict[str, Any],
                    jobs: Dict[str, Dict[str, Any]]) -> bool:
        """把源目录（或单文件）复制到目标位置，并把进度写进任务记录。"""
        saved_at = [0.0]

        def flush(force: bool = False) -> None:
            now = time.time()
            if force or now - saved_at[0] >= 1.0:
                saved_at[0] = now
                self.__save_jobs(jobs)

        try:
            if os.path.isdir(src):
                pairs: List[Tuple[str, str]] = []
                for root, _dirs, names in os.walk(src):
                    for filename in names:
                        full = os.path.join(root, filename)
                        pairs.append((full, os.path.relpath(full, src)))
                os.makedirs(dst, exist_ok=True)
                total = sum(os.path.getsize(s) for s, _ in pairs)
                job["total"] = total
                copied = 0
                for source, rel in pairs:
                    target = os.path.join(dst, rel)
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if not self.__copy_file(source, target):
                        raise IOError(f"复制失败：{source}")
                    copied += os.path.getsize(target)
                    job["done"] = copied
                    flush()
            else:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if not self.__copy_file(src, dst):
                    raise IOError(f"复制失败：{src}")
                job["done"] = os.path.getsize(dst)
                job["total"] = job.get("total") or job["done"]
            flush(force=True)
            return True
        except Exception as e:
            job["state"] = "failed"
            job["message"] = f"复制失败：{e}"
            self.__save_jobs(jobs)
            return False

    @staticmethod
    def __copy_file(src: str, dst: str) -> bool:
        """复制单个文件。网盘挂载点上 copystat 可能失败，降级为纯内容复制。"""
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            try:
                shutil.copyfile(src, dst)
                return True
            except Exception as e:
                logger.error(f"QB分类活动暂停：复制 {src} → {dst} 失败：{e}")
                return False

    @staticmethod
    def __verify_pieces(client: qbittorrentapi.Client, torrent_hash: str,
                        files_root: str) -> Tuple[bool, str]:
        """按种子的 piece 哈希逐块校验目标目录里的文件。

        :param files_root: 目标父目录（种子内文件名是相对它的路径）
        :return: (是否通过, 说明)
        """
        try:
            props = client.torrents_properties(torrent_hash=torrent_hash)
            piece_size = int(props.get("piece_size") or 0)
            hashes = list(client.torrents_piece_hashes(torrent_hash=torrent_hash))
            files = sorted(client.torrents_files(torrent_hash=torrent_hash),
                           key=lambda f: f.get("index", 0))
        except Exception as e:
            return False, f"读取校验数据失败：{e}"
        if not piece_size or not hashes:
            return False, "种子没有可用的 piece 信息"

        buf = bytearray()
        index = missing = bad = 0
        for finfo in files:
            path = os.path.join(files_root, finfo.get("name") or "")
            if not os.path.exists(path):
                missing += 1
                continue
            with open(path, "rb") as fp:
                while True:
                    chunk = fp.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= piece_size and index < len(hashes):
                        if hashlib.sha1(bytes(buf[:piece_size])).hexdigest() \
                                != str(hashes[index]).lower():
                            bad += 1
                        del buf[:piece_size]
                        index += 1
        if buf and index < len(hashes):
            if hashlib.sha1(bytes(buf)).hexdigest() != str(hashes[index]).lower():
                bad += 1
            index += 1

        if missing:
            return False, f"{missing} 个文件缺失"
        if bad:
            return False, f"{bad} / {len(hashes)} 个数据块不一致"
        return True, f"{index} 个数据块全部一致"

    def __render_migrate_jobs(self) -> List[dict]:
        """渲染迁移队列区块。"""
        jobs = self.__load_jobs()
        if not jobs:
            return []

        ordered = sorted(jobs.values(),
                         key=lambda job: job.get("ts") or 0, reverse=True)
        active = sum(1 for job in ordered
                     if job.get("state") in ("queued", "copying", "verifying", "swapping"))
        finished = sum(1 for job in ordered if job.get("state") == "done")
        failed = sum(1 for job in ordered if job.get("state") == "failed")

        rows = []
        for job in ordered[:20]:
            total = job.get("total") or 0
            done_bytes = job.get("done") or 0
            percent = f"{done_bytes * 100.0 / total:.1f}%" if total else "-"
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": job.get("name") or ""},
                    {"component": "td", "text": job.get("state") or ""},
                    {"component": "td", "text": percent},
                    {"component": "td", "text": job.get("message") or ""},
                ],
            })

        return [
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
                                    "type": "error" if failed else "info",
                                    "variant": "tonal",
                                    "text": f"迁移队列：进行中 {active} 个，"
                                            f"已完成 {finished} 个，失败 {failed} 个"
                                            f"（最多显示最近 20 条）",
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
                                "component": "VTable",
                                "props": {"density": "compact"},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {"component": "th", "text": "种子名称"},
                                                    {"component": "th", "text": "阶段"},
                                                    {"component": "th", "text": "进度"},
                                                    {"component": "th", "text": "说明"},
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
            },
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._last_paused_hashes = []
