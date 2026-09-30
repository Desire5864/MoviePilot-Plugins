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

# 迁移任务「进行中」的状态集合（这些任务永远不会被裁剪）
MIGRATE_ACTIVE_STATES = ("queued", "copying", "verifying", "swapping")

# 迁移条目「状态」列的徽章配色（Vuetify 色名）。
# done 绿 / failed 红 / 进行中紫（面板主色）/ 排队灰；表里没有的取值兜底成 grey。
MIGRATE_STATE_COLORS = {
    "done": "success",
    "failed": "error",
    "queued": "grey",
    "copying": "primary",
    "verifying": "primary",
    "swapping": "primary",
}

# 迁移队列里最多保留多少条「已结束」（done / failed）历史记录。
# 渲染层本来就只展示最近 20 条，存储层也要收敛，否则队列会随版本无限膨胀。
MIGRATE_KEEP_FINISHED = 50

# 🔴 迁移前的空间校验余量（字节）。
# 判断「装得下」用的是 `剩余空间 - 本次需要 >= MIGRATE_SPACE_RESERVE`，
# 而不是简单的 `剩余空间 > 本次需要`：文件系统元数据、目录项、以及复制过程中
# 目标侧的临时膨胀都要占地方，恰好卡满会把盘写爆 —— 写爆的后果是复制中途失败
# 且目标目录留下半份残文件。
MIGRATE_SPACE_RESERVE = 1024 ** 3  # 1 GiB

# 备用迁移目录的默认值（qB 视角路径）。
# 升级前的旧配置里**没有**这个键，此时按默认值启用备用目录；用户在面板里把它
# 清空成 "" 才是「明确不要备用」。这样「加备用路径」这个需求不需要用户再手动配一次。
DEFAULT_MIGRATE_TARGET_ALT = "/下载"

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
    plugin_version = "1.5.8"
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
    # 备用目标根目录（qB 容器视角路径，如 /下载）：主目录剩余空间不足时自动改用
    _migrate_target_alt: str = ""
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
    # 🔴 迁移队列的读写锁：`__enqueue`（用户点按钮）与 worker（复制中的进度回写）
    # 都会「读队列 → 改 → 写回」，没有这把锁两个线程会互相覆盖。
    # 用 RLock（可重入）：`__load_jobs` / `__save_jobs` 自己也上锁，
    # 于是「一整段读-改-写」既能被锁保护、又不会自己把自己锁死。
    # ⚠️ 复制/校验这种长耗时的动作**绝不能**持锁，否则用户点迁移会卡住整个线程。
    _migrate_lock: threading.RLock = threading.RLock()

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
        self._migrate_target_alt = ""
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
        # 键缺失（老配置）→ 用默认备用目录；显式空串 → 尊重「关闭备用」的意图
        alt_raw = config.get("migrate_target_alt")
        self._migrate_target_alt = (
            DEFAULT_MIGRATE_TARGET_ALT if alt_raw is None else str(alt_raw).strip()
        )
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
                                            "label": "迁移主目标目录（qB 视角路径）",
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
                                            "model": "migrate_target_alt",
                                            "label": "迁移备用目标目录（主目录空间不足时自动使用）",
                                            "placeholder": "例如 /下载，留空则空间不足时不迁移",
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
            "migrate_target_alt": DEFAULT_MIGRATE_TARGET_ALT,
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
                                                f" → 分类 {self._migrate_category or '（未设置）'}"
                                                + (f"；空间不足时自动改用 "
                                                   f"{self._migrate_target_alt}"
                                                   if self._migrate_target_alt else ""),
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

    @staticmethod
    def __fmt_size(num: Any) -> str:
        """把字节数格式化成人类可读文本（用于任务说明与日志）。"""
        try:
            size = float(num or 0)
        except (TypeError, ValueError):
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PB"

    @staticmethod
    def __disk_free(path: str) -> Optional[Tuple[int, int, str]]:
        """取 path 所在文件系统的剩余空间。

        :param path: 允许尚不存在（例如首次迁移到某个新分类目录），
                     这时上溯到最近存在的祖先目录再探测 —— 同一分区结论一致。
        :return: (剩余字节, 总量字节, 实际用于探测的路径)；完全探测不到时返回 None
        """
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if not parent or parent == probe:
                break
            probe = parent
        try:
            usage = shutil.disk_usage(probe)
            return usage.free, usage.total, probe
        except Exception as e:
            logger.warning(f"QB分类活动暂停：读取 {path} 所在分区剩余空间失败：{e}")
            return None

    @staticmethod
    def __path_dev(path: str) -> Optional[int]:
        """取路径所在文件系统的设备号（用于判断两个目录是否同一分区）。"""
        try:
            return os.stat(path).st_dev
        except OSError:
            return None

    @staticmethod
    def __same_fs(dev: Optional[int], total: int, free: int,
                  primary: Optional[Tuple[Optional[int], int, int]]) -> bool:
        """判断候选目录与主目录是否落在同一文件系统。

        同一文件系统 = 备用目录带不来额外空间，配了也没用（还可能误导用户
        以为「换了个地方就有地方了」）。两个判据取或：

        1. 总量与剩余都一致 —— 同一文件系统在同一时刻读出来必然一致。
           🔴 这个判据不能省：本环境实测 `/` 与 `/下载` 的 `st_dev` **不同**
           （1048618 vs 124）却是同一块盘，只看设备号会漏判。
        2. 设备号相同。
        """
        if primary is None:
            return False
        primary_dev, primary_total, primary_free = primary
        if total == primary_total and abs(free - primary_free) < 1024 * 1024:
            return True
        return dev is not None and primary_dev is not None and dev == primary_dev

    @staticmethod
    def __scan_tree(path: str) -> Tuple[int, List[Tuple[str, str]]]:
        """统计待复制内容的**真实**字节数，顺带产出文件清单供复制阶段复用。

        用真实文件大小而不是种子报告的 size：稀疏文件、部分下载、硬链接都会
        让两者不一致，而空间校验必须按实际要写过去的字节算。
        清单一起返回是为了避免复制时再遍历一遍目录（大目录遍历很贵）。

        :return: (总字节数, [(源文件绝对路径, 相对路径)；单文件时为空列表])
        """
        if os.path.isdir(path):
            pairs: List[Tuple[str, str]] = []
            total = 0
            for root, _dirs, names in os.walk(path):
                for filename in names:
                    full = os.path.join(root, filename)
                    try:
                        total += os.path.getsize(full)
                    except OSError:
                        # 单个文件取不到大小不中断统计，复制阶段会报出具体失败
                        continue
                    pairs.append((full, os.path.relpath(full, path)))
            return total, pairs
        try:
            return os.path.getsize(path), []
        except OSError:
            return 0, []

    def __pick_target(self, src_mp: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """迁移前挑一个装得下的目标目录：主目录优先，不够就改用备用目录。

        判断标准是 `剩余空间 - 本次需要 >= MIGRATE_SPACE_RESERVE`（留安全余量，
        不是恰好卡满）。两边都不够、没配备用、或备用与主目录同分区时返回 None，
        说明文本直接写进任务记录，让用户看得见差多少。

        :return: (选中方案 或 None, 说明文本)
        """
        need, pairs = self.__scan_tree(src_mp)
        notes: List[str] = []
        candidates: List[Dict[str, Any]] = []
        primary: Optional[Tuple[Optional[int], int, int]] = None

        # 配置项本身就是 qB 视角路径，要换算成 MP 视角才能探测剩余空间
        # （两个容器挂载点不同名）。
        for index, (label, qb_path) in enumerate((("主目录", self._migrate_target),
                                                  ("备用目录", self._migrate_target_alt))):
            if not qb_path:
                continue
            mp_path = self.__map_qb_path(qb_path)
            probe = self.__disk_free(mp_path)
            if not probe:
                notes.append(f"{label} {qb_path} 读不到剩余空间")
                continue
            free, total, real = probe
            dev = self.__path_dev(real)
            if index > 0 and self.__same_fs(dev, total, free, primary):
                # 同一块盘：换个名字并不会多出空间，留着只会误导用户
                notes.append(f"{label} {qb_path} 与主目录在同一分区，按无备用处理")
                continue
            if index == 0:
                primary = (dev, total, free)
            candidates.append({"label": label, "qb": qb_path, "mp": mp_path,
                               "free": free, "total": total, "real": real,
                               "need": need})

        if not candidates:
            return None, "；".join(notes) or "没有可用的迁移目标目录"

        for candidate in candidates:
            spare = candidate["free"] - need
            if spare >= MIGRATE_SPACE_RESERVE:
                detail = (f"{candidate['label']} {candidate['qb']} 剩余 "
                          f"{self.__fmt_size(candidate['free'])}，本次需要 "
                          f"{self.__fmt_size(need)}")
                if not os.path.exists(candidate["mp"]):
                    detail += f"（目标目录尚不存在，按 {candidate['real']} 估算）"
                candidate["plan"] = (need, pairs)
                return candidate, detail
            notes.append(f"{candidate['label']} {candidate['qb']} 剩余 "
                         f"{self.__fmt_size(candidate['free'])}，需要 "
                         f"{self.__fmt_size(need)}，差 {self.__fmt_size(-spare)}")

        return None, (f"空间不足：{'；'.join(notes)}"
                      f"（另需预留 {self.__fmt_size(MIGRATE_SPACE_RESERVE)}）")

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
        with self._migrate_lock:
            return self.get_data(MIGRATE_DATA_KEY) or {}

    def __save_jobs(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        with self._migrate_lock:
            self.save_data(MIGRATE_DATA_KEY, jobs)

    def __get_job(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """读取队列里的单条任务（副本，改了不会影响存储）。"""
        if not torrent_hash:
            return None
        job = self.__load_jobs().get(torrent_hash)
        return dict(job) if job else None

    def __update_job(self, torrent_hash: str, **patch) -> None:
        """只更新队列里的**一条**任务，其余记录原样保留。

        🔴 必须「写前重新读取 + 只合并自己那几个字段」，绝不能拿一份旧快照整表回写 ——
        worker 在复制开始前 load 的那份快照不含后来新入队的任务，
        整表写回会把它们抹掉：1.5.3 及之前「复制期间点迁移，任务活不过 1 秒」就是此因。
        整个读-改-写在 `_migrate_lock` 内完成，同时点多个迁移也不会互相覆盖。
        """
        if not torrent_hash:
            return
        if patch.get("state") in ("done", "failed") and "end_ts" not in patch:
            # 记下结束时刻，供裁剪时按「真正结束的先后」保留最近若干条
            patch["end_ts"] = datetime.now().timestamp()
        with self._migrate_lock:
            jobs = self.__load_jobs()
            job = jobs.get(torrent_hash)
            if not job:
                job = {"hash": torrent_hash}
                jobs[torrent_hash] = job
            job.update(patch)
            self.__save_jobs(self.__trim_jobs(jobs, keep_hash=torrent_hash))

    @staticmethod
    def __trim_jobs(jobs: Dict[str, Dict[str, Any]],
                    keep_hash: str = None) -> Dict[str, Dict[str, Any]]:
        """收敛队列体积：进行中的任务全部保留，已结束的只留最近 MIGRATE_KEEP_FINISHED 条。

        :param keep_hash: 本条任务一定会保留（刚结束的那条，别让它被自己挤掉）
        """
        active: Dict[str, Dict[str, Any]] = {}
        finished: List[Tuple[int, Dict[str, Any]]] = []
        for index, (torrent_hash, job) in enumerate(jobs.items()):
            if job.get("state") in MIGRATE_ACTIVE_STATES:
                active[torrent_hash] = job
            else:
                finished.append((index, job))
        # 时间相同时（同一轮里批量结束）用入队次序兜底：后入队的算更新。
        # 少了这个兜底，稳定排序会把「并列里最旧的」当成最新的留下。
        finished.sort(key=lambda item: (item[1].get("end_ts")
                                        or item[1].get("ts") or 0, item[0]),
                      reverse=True)
        kept: Dict[str, Dict[str, Any]] = {}
        for _index, job in finished[:MIGRATE_KEEP_FINISHED]:
            torrent_hash = job.get("hash")
            if torrent_hash:
                kept[torrent_hash] = job
        kept.update(active)
        if keep_hash and keep_hash in jobs:
            kept[keep_hash] = jobs[keep_hash]
        return kept

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

    def __resolve_source(self, brief: Dict[str, Any], name: str) -> str:
        """定位种子内容在磁盘上的**根**（映射成 MoviePilot 容器视角）。

        🔴 为什么不直接用 `content_path`：qB 对「种子内只有 1 个文件」的种子返回的
        是*文件*路径（哪怕该文件在磁盘上还带一层以种子名命名的顶层目录）。直接拿它
        当源根，复制阶段会被判成「单文件」而丢掉顶层目录，校验阶段再按
        `<目标>/<种子内相对路径>` 去找就报「N 个文件缺失」。

        取法（三形态通吃）：
        1. 优先 `save_path/种子名` —— 多文件种子、以及「单文件但带顶层目录」的种子，
           这一层都是磁盘上真实存在的目录；
        2. 取不到时退回 `content_path` —— 对应「文件直接躺在 save_path 下」的真单文件种子。

        :return: 源根路径；两者都取不到时返回空串
        """
        save_path = self.__map_qb_path(brief.get("save_path") or "")
        if save_path and name:
            root = os.path.join(save_path, name)
            if os.path.isdir(root):
                return root
        return self.__map_qb_path(brief.get("content_path") or "")

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
        """写入迁移队列，并确保后台线程在跑。

        🔴 整段「读队列 → 判断 → 写回」都在 `_migrate_lock` 内：没有锁时
        「同时点好几个迁移」的并发请求会各自读到同一份旧值，后写的赢（前面的任务消失）。
        """
        added = skipped = 0
        with self._migrate_lock:
            jobs = self.__load_jobs()
            for torrent_hash in hashes:
                if not torrent_hash:
                    continue
                exist = jobs.get(torrent_hash)
                if exist and exist.get("state") in MIGRATE_ACTIVE_STATES:
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
            if added:
                self.__save_jobs(self.__trim_jobs(jobs))
                self.__start_worker()
        return added, skipped

    def __start_worker(self) -> None:
        """启动后台迁移线程（已有线程在跑时不重复启动）。"""
        with self._migrate_lock:
            if self._migrate_running:
                return
            self._migrate_running = True
        threading.Thread(target=self.__migrate_worker,
                         name="QbCategoryPauseMigrate", daemon=True).start()

    def __migrate_worker(self) -> None:
        """后台工作线程：按入队顺序逐个迁移。

        🔴 「没待办 → 退出」的判断与把 `_migrate_running` 置回 False 必须在同一把锁内，
        否则中间那一小段窗口里入队的任务会两边都漏：worker 判定队列为空准备退出，
        而入队的线程此刻看到 `_migrate_running` 还是 True 就不再另起线程。
        （复制期间入队的任务由此能正常续跑，不会再丢。）
        """
        try:
            while True:
                with self._migrate_lock:
                    pending = [job for job in self.__load_jobs().values()
                               if job.get("state") == "queued"]
                    if not pending:
                        self._migrate_running = False
                        return
                pending.sort(key=lambda job: job.get("ts") or 0)
                job = pending[0]
                torrent_hash = job.get("hash")
                try:
                    self.__migrate_one(torrent_hash)
                except Exception as e:
                    logger.error(f"QB分类活动暂停：迁移 {job.get('name')} 异常：{e}")
                    self.__update_job(torrent_hash, state="failed",
                                      message=f"异常：{e}")
        finally:
            with self._migrate_lock:
                self._migrate_running = False

    def __migrate_one(self, torrent_hash: str) -> None:
        """迁移单个种子。任一步失败都保留原任务，不制造"两头空"。

        🔴 只认 hash，**不接收也不持有队列快照**：每一步写入都走 `__update_job`
        （写前重读、只改自己那条），否则会把复制期间新入队的任务整表抹掉。
        """
        job = self.__get_job(torrent_hash) or {}
        name = job.get("name") or torrent_hash

        brief = self.__torrent_brief(torrent_hash)
        if not brief:
            self.__update_job(torrent_hash, state="failed",
                              message="找不到该种子，可能已被删除")
            return

        client = self.__qb_client(brief["downloader"])
        if not client:
            self.__update_job(torrent_hash, state="failed",
                              message=f"无法连接下载器 {brief['downloader']}")
            return

        # 🔴 源根不能直接用 content_path：qB 对「种子内只有 1 个文件」的种子返回的是
        #    *文件*路径，而不是它所在的那层目录。照搬会让复制走「单文件」分支、丢掉
        #    顶层目录，随后校验按「<目标>/<种子内相对路径>」找不到文件，报「N 个文件缺失」。
        #    所以优先取「save_path/种子名」这层真目录，取不到才退回 content_path。
        src_mp = self.__resolve_source(brief, name)
        if not src_mp or not os.path.exists(src_mp):
            self.__update_job(torrent_hash, state="failed",
                              message=f"源路径不存在：{src_mp or '无法定位种子内容'}")
            return

        # 源路径正好就是主目标下的同名位置时，搬了等于原地不动
        primary_mp = self.__map_qb_path(self._migrate_target)
        if os.path.abspath(src_mp) == os.path.abspath(
                os.path.join(primary_mp, os.path.basename(src_mp.rstrip("/")))):
            self.__update_job(torrent_hash, state="failed",
                              message="源路径与主目标路径相同，无需迁移")
            return

        # ① 空间校验：剩余空间必须大于本次迁移所需（并留安全余量），
        #    主目录装不下就自动改用备用目录；两边都装不下就干脆不动 ——
        #    宁可这次不迁，也不能写爆目标盘或留下半份残文件。
        chosen, space_detail = self.__pick_target(src_mp)
        if not chosen:
            self.__update_job(torrent_hash, state="failed",
                              message=f"{space_detail}，本次未迁移")
            return
        target_qb = chosen["qb"]
        target_mp = chosen["mp"]
        # 目标名必须跟源的实际形态对齐：目录型用种子名，真单文件型用文件名。
        # 一律用种子名的话，真单文件会变成「无扩展名的裸文件」，校验必然失败。
        dst_mp = os.path.join(target_mp, os.path.basename(src_mp.rstrip("/")))
        if os.path.abspath(src_mp) == os.path.abspath(dst_mp):
            self.__update_job(torrent_hash, state="failed",
                              message="源路径与选中的目标路径相同，无需迁移")
            return

        # ② 复制（复用空间校验时已扫好的文件清单，不重复遍历目录）
        used_alt = chosen["label"] == "备用目录"
        self.__update_job(
            torrent_hash, state="copying", target=target_mp, used_alt=used_alt,
            space_need=chosen["need"], space_free=chosen["free"],
            message=f"复制到 {dst_mp}（{chosen['label']}：{space_detail}）")
        if not self.__copy_tree(src_mp, dst_mp, torrent_hash,
                                chosen.get("plan")):
            return

        # ③ 逐块校验（只比大小验不出静默损坏）
        if self._migrate_verify:
            self.__update_job(torrent_hash, state="verifying", message="逐块校验中")
            ok, detail = self.__verify_pieces(client, torrent_hash, target_mp)
            if not ok:
                self.__update_job(torrent_hash, state="failed",
                                  message=f"校验未通过（{detail}），原任务未动")
                return
            self.__update_job(torrent_hash, message=f"校验通过：{detail}")

        # ④ 导出 .torrent 备份 —— 删掉原任务后这是唯一的回滚凭据
        try:
            torrent_bytes = client.torrents_export(torrent_hash=torrent_hash)
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"导出种子失败：{e}")
            return
        if not torrent_bytes:
            self.__update_job(torrent_hash, state="failed",
                              message="导出种子为空，原任务未动")
            return
        try:
            backup_dir = os.path.join(self.get_data_path(), "torrents")
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f"{torrent_hash}.torrent"), "wb") as fp:
                fp.write(torrent_bytes)
        except Exception as e:
            logger.warning(f"QB分类活动暂停：备份种子文件失败（不阻断流程）：{e}")

        # ⑤ 确保目标分类存在
        if self._migrate_category:
            try:
                client.torrents_create_category(
                    name=self._migrate_category, save_path=target_qb)
            except Exception as e:
                logger.warning(f"QB分类活动暂停：创建分类 {self._migrate_category} "
                               f"失败（可能已存在）：{e}")

        # ⑥ 删除原任务
        self.__update_job(torrent_hash, state="swapping", message="删除原任务")
        try:
            client.torrents_delete(delete_files=self._migrate_delete_files,
                                   torrent_hashes=torrent_hash)
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"删除原任务失败：{e}（内容已复制，原任务未动）")
            return

        # ⑦ 以新分类重新添加
        self.__update_job(torrent_hash, message="重新添加中")
        try:
            client.torrents_add(
                torrent_files=torrent_bytes,
                save_path=target_qb,
                category=self._migrate_category or None,
                is_skip_checking=self._migrate_skip_check,
                is_paused=False,
            )
        except Exception as e:
            self.__update_job(
                torrent_hash, state="failed",
                message=(f"重新添加失败：{e}；种子已备份到插件数据目录 "
                         f"torrents/{torrent_hash}.torrent，可手动恢复"))
            return

        # ⑧ 复查生效状态
        # 🔴 结尾的说明必须带上「实际落在哪个目录、是主目录还是备用目录」：
        # 这四个阶段（copying/verifying/swapping/重新添加）会反复覆盖 message，
        # 不在这里补一句，用户翻队列就只看到「已重新添加」，根本不知道换过盘。
        where = f"{chosen['label']} {target_mp}"
        time.sleep(2)
        try:
            infos = client.torrents_info(torrent_hashes=torrent_hash)
        except Exception:
            infos = []
        if infos:
            info = infos[0]
            progress = float(info.get("progress") or 0)
            # 🔴 括号里只留「主目录 / 备用目录」这个标签：`save_path` 已经写在
            #    前一段，再把 `where`（标签 **+ 路径**）整段塞进来，页面上就会
            #    出现 `/download（主目录 /download）` 这种同一路径写两遍的冗余
            #    （2026-09-30 在真机截图上发现的）。
            self.__update_job(
                torrent_hash, state="done",
                message=(f"{info.get('state')} / {progress * 100:.0f}% / "
                         f"{info.get('category')} / {info.get('save_path')}"
                         f"（{chosen['label']}）"))
        else:
            self.__update_job(torrent_hash, state="done",
                              message=f"已重新添加（{where}），请刷新页面确认")
        logger.info(f"QB分类活动暂停：迁移完成 {name} → {dst_mp}"
                    f"（{chosen['label']}）")

    def __copy_tree(self, src: str, dst: str, torrent_hash: str,
                    plan: Optional[Tuple[int, List[Tuple[str, str]]]] = None) -> bool:
        """把源目录（或单文件）复制到目标位置，并把进度写进任务记录。

        :param plan: 空间校验阶段已扫好的 (总字节数, 文件清单)，传进来就不再重复
                     遍历目录；为 None 时自己扫一遍（留作兜底）。
        🔴 进度只回写**自己那一条**任务的 total / done（`__update_job` 写前重读）。
        不要持有队列快照整表回写 —— 那正是「复制期间点迁移，任务活不过 1 秒」的成因。
        """
        saved_at = [0.0]

        def flush(force: bool = False, **patch) -> None:
            now = time.time()
            if force or now - saved_at[0] >= 1.0:
                saved_at[0] = now
                self.__update_job(torrent_hash, **patch)

        try:
            total, pairs = plan if plan else self.__scan_tree(src)
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                flush(force=True, total=total, done=0)
                copied = 0
                for source, rel in pairs:
                    target = os.path.join(dst, rel)
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if not self.__copy_file(source, target):
                        raise IOError(f"复制失败：{source}")
                    copied += os.path.getsize(target)
                    flush(done=copied)
                flush(force=True, done=copied)
            else:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if not self.__copy_file(src, dst):
                    raise IOError(f"复制失败：{src}")
                flush(force=True, total=total, done=os.path.getsize(dst))
            return True
        except Exception as e:
            self.__update_job(torrent_hash, state="failed",
                              message=f"复制失败：{e}")
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

    @staticmethod
    def __job_summary(job: Dict[str, Any]) -> str:
        """迁移条目第二行的小字摘要。

        「状态」「进度」已经各自成列，完成态 message 开头那两段
        （`stoppedUP / 100%`）就是重复信息，这里剥掉只留分类与落地位置：

            `stoppedUP / 100% / 本地保种 / /download（主目录）`
                -> `本地保种 · /download（主目录）`

        其余形态（排队中、逐块校验中、各种失败原因）原样保留。
        """
        msg = str(job.get("message") or "").strip()
        parts = msg.split(" / ")
        if len(parts) >= 3 and parts[1].endswith("%"):
            # 分类为空时上游会写出字面量 `None`，这里一并滤掉，别让它在页面上露脸。
            parts = [part for part in parts[2:] if part and part != "None"]
            # 🔴 1.5.8 之前写进队列的**老记录**，末段是 `/download（主目录 /download）`
            #    —— 路径写了两遍（新写入的已经只留标签）。历史记录不改库，在渲染层折叠：
            #    括号内形如「标签 路径」、且路径与括号外那段重合时，只留标签。
            if parts:
                head, sep, tail = parts[-1].rpartition("（")
                if sep and tail.endswith("）"):
                    label, _, inner = tail[:-1].partition(" ")
                    if inner and head.endswith(inner):
                        parts[-1] = f"{head}（{label}）"
            msg = " · ".join(parts)
        return msg or "—"

    def __render_migrate_jobs(self) -> List[dict]:
        """渲染迁移队列区块。

        排版取向（方案 3「徽章双行」，2026-09-30 起）：
        「种子名称」独占第一行并单行省略（`title` 悬浮看全名），第二行是灰色小字摘要；
        「状态」独立成列用徽章表示，「进度」只留百分比。

        旧排版四列平铺、说明列只占 16% 宽，长种子名会折成三行并把**整行**撑到
        64px —— 行高由最高的那一列决定，于是名称列右边一片空白、最右侧却挤成一团。
        改成两行后行高由内容决定（真机实测 44px，旧版 64px），一屏能多看好几条。
        """
        jobs = self.__load_jobs()
        if not jobs:
            return []

        ordered = sorted(jobs.values(),
                         key=lambda job: job.get("ts") or 0, reverse=True)
        active = sum(1 for job in ordered
                     if job.get("state") in MIGRATE_ACTIVE_STATES)
        finished = sum(1 for job in ordered if job.get("state") == "done")
        failed = sum(1 for job in ordered if job.get("state") == "failed")

        rows = []
        for job in ordered[:20]:
            total = job.get("total") or 0
            done_bytes = job.get("done") or 0
            percent = f"{done_bytes * 100.0 / total:.1f}%" if total else "-"
            state = str(job.get("state") or "")
            name = str(job.get("name") or "")
            summary = self.__job_summary(job)
            rows.append({
                "component": "tr",
                "content": [
                    {
                        # 🔴 `max-width:0` 不能去掉：表格是自适应布局，单元格不加这个
                        #    约束时会被 nowrap 的长名字撑开，`text-truncate` 的省略号
                        #    不生效，表现为整张表横向溢出。
                        "component": "td",
                        "props": {"style": "max-width:0;"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-truncate", "title": name},
                                "text": name,
                            },
                            {
                                "component": "div",
                                "props": {
                                    "class": "text-truncate text-caption "
                                             "text-medium-emphasis",
                                    "style": "margin-top:2px;",
                                    "title": summary,
                                },
                                "text": summary,
                            },
                        ],
                    },
                    {
                        "component": "td",
                        "props": {"style": "white-space:nowrap;"},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {
                                    "size": "x-small",
                                    "variant": "tonal",
                                    "color": MIGRATE_STATE_COLORS.get(state, "grey"),
                                },
                                "text": state or "—",
                            }
                        ],
                    },
                    {
                        "component": "td",
                        "props": {"style": "white-space:nowrap;"},
                        "text": percent,
                    },
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
                                                    {"component": "th",
                                                     "props": {"style": "width:100%;"},
                                                     "text": "种子名称"},
                                                    {"component": "th",
                                                     "props": {"style": "width:96px;"},
                                                     "text": "状态"},
                                                    {"component": "th",
                                                     "props": {"style": "width:84px;"},
                                                     "text": "进度"},
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
