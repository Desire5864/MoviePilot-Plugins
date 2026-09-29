from re import search
from typing import Any, Dict, List, Optional, Tuple

from clouddrive2_client.proto import clouddrive_pb2
from google.protobuf import empty_pb2
from grpc import RpcError, StatusCode

from app.log import logger


def convert_bytes(size_in_bytes: float) -> str:
    """
    将字节转换为最合适的单位

    :param size_in_bytes (float): 字节数

    :return str: 转换后的字符串
    """
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while size_in_bytes >= 1024 and unit_index < len(units) - 1:
        size_in_bytes /= 1024
        unit_index += 1
    return f"{size_in_bytes:.2f} {units[unit_index]}"


def convert_seconds(seconds: float) -> str:
    """
    将秒数转换为天时分秒格式

    :param seconds (float): 秒数

    :return str: 格式化后的时间字符串
    """
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{int(days)}天")
    if hours > 0:
        parts.append(f"{int(hours)}小时")
    if minutes > 0:
        parts.append(f"{int(minutes)}分钟")
    if seconds > 0 or not parts:
        parts.append(f"{seconds:.0f}秒")
    return "".join(parts)


def check_cookie(client, black_dir: str = "") -> Optional[str]:
    """
    检查云盘cookie是否过期

    通过遍历根目录下的挂载点，尝试列出每个云盘的内容
    如果列出失败或结果为空，则认为该云盘的cookie已过期

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param black_dir (str): 黑名单目录，逗号分隔

    :return str: 错误信息，无错误返回 None
    """
    if not client:
        logger.error("CloudDrive2 客户端未初始化")
        return "CloudDrive2 客户端未初始化"

    black_list = [d.strip() for d in black_dir.split(",") if d.strip()]

    try:
        drives = client.get_sub_files("/", force_refresh=True)
    except Exception as e:
        logger.error("获取云盘列表失败: %s", e)
        return f"获取云盘列表失败: {e}"

    for drive in drives:
        name = getattr(drive, "name", "") or ""
        full_path = getattr(drive, "fullPathName", "") or ""
        is_dir = getattr(drive, "isDirectory", False)

        if not is_dir or not name:
            continue
        if name in black_list:
            continue

        try:
            sub_files = list(client.get_sub_files(full_path, force_refresh=True))
            if not sub_files:
                logger.warning("云盘 %s 为空", name)
                return f"云盘 {name} cookie过期"
        except Exception as e:
            logger.error("云盘 %s 检查失败: %s", name, e)
            err_str = str(e)
            if "429" in err_str:
                return f"云盘 {name} 访问频率过高，请稍后再试"
            return f"云盘 {name} cookie过期"

    return None


def check_upload_tasks(client, keyword: str = "") -> Optional[str]:
    """
    检查上传任务是否有异常

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param keyword (str): 检测关键字正则表达式

    :return str: 异常任务错误信息，无异常返回 None
    """
    if not client:
        logger.error("CloudDrive2 客户端未初始化")
        return None

    try:
        resp = client.get_upload_file_list(get_all=True)
    except Exception as e:
        logger.error("获取上传任务列表失败: %s", e)
        return None

    upload_files = getattr(resp, "uploadFiles", None) or []
    if not upload_files:
        logger.info("没有发现上传任务")
        return None

    for task in upload_files:
        status = getattr(task, "status", "") or ""
        error_message = getattr(task, "errorMessage", "") or ""

        if status == "FatalError" and keyword and search(keyword, error_message):
            logger.info("发现异常上传任务: %s", error_message)
            return error_message

    return None


def get_cloud_space(client, black_dir: str = "") -> str:
    """
    获取云盘空间信息

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param black_dir (str): 黑名单目录，逗号分隔

    :return str: 云盘空间信息字符串
    """
    if not client:
        return "\n"

    black_list = [d.strip() for d in black_dir.split(",") if d.strip()]
    space_info = "\n"

    try:
        drives = client.get_sub_files("/", force_refresh=True)
    except Exception as e:
        logger.error("获取云盘列表失败: %s", e)
        return "\n"

    for drive in drives:
        name = getattr(drive, "name", "") or ""
        full_path = getattr(drive, "fullPathName", "") or ""
        is_dir = getattr(drive, "isDirectory", False)

        if not is_dir or not name:
            continue
        if name in black_list:
            continue

        try:
            info = client.get_space_info(full_path)
            # 本地文件夹挂载：挂载根可能是陈旧缓存，改取所在文件系统的实时值
            if _is_local_mount(drive):
                local_info = _local_mount_space_info(client, full_path, info)
                if local_info:
                    info = local_info
            total = getattr(info, "totalSpace", 0) or 0
            used = getattr(info, "usedSpace", 0) or 0
            space_info += f"{name}：{convert_bytes(used)}/{convert_bytes(total)}\n"
        except Exception as e:
            logger.error("获取云盘 %s 空间信息失败: %s", name, e)

    return space_info


# ---------------------------------------------------------------------------
# 云盘 / 硬件品牌图标（base64 内嵌）
#
# 面板常部署在内网，卡片里外链外网图片会加载失败变裂图，所以图标一律内嵌。
# 来源（均为各站真实 favicon，2026-09-27 抓取）：
#   115  -> https://115.com/favicon.ico
#   123  -> https://statics.123957.com/static-by-custom/favicon.ico
#   189  -> https://cloud.dlife.cn/web/main/logo.ico
#   acer -> https://www.acer.com/favicon.ico
#   wd   -> https://www.westerndigital.com（经 favicon 服务取 256px 高清版）
# ---------------------------------------------------------------------------

_SPACE_LOGOS = {
    '115': (
        "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"
    ),
    '123': (
        "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"
        "RU5ErkJggg=="
    ),
    '189': (
        "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"
    ),
    'acer': (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAEHUlEQVR42u3VW0zbVRwH8HP+117/"
        "bWkp0FLKbVAuctucGAYzdYb5Aru+qAvG+GB0DxoVSUzUiHvgQRazxHnJYmKmi2QGL+DmBpoNF0AI"
        "DoXaEu4UaFdaKL23/4sPw0iiT0jCw36f15PfyTnf8/vlIAQAAAAAAAAAAAAAAAAAAAAAeBDgHRcS"
        "GEmihAoO6NPqW6ytjJwyyxSUkaAIVSom+Da80dHp4cD3Y70r46m4gJQ6hrDVpRcW16Wf5oxsKc0S"
        "WglJvmsfTL06MxLwm0s4ZeWTpkbTPpVdrqGLEEIoEkhOrDhD/b92u28ElqMpc4lG+dCRTLu5lDvK"
        "KiizJElCMsx7Lp0dfWmn96B2HJ10Pz5bffoTKg1rvjcfGYtHUn4+IcY4o6yg+FH9mweas9tlKqp+"
        "xRVynXyr7Fq6VbnfMxd2BpZiA+H15Kw2U5ZrzFNbSxqM9oYzeV3e2fDQ0kSwyzsTGaQYQmmt0rWU"
        "2zNeyavWfuUa9H9qfy6/h6QJmWc61M8LYpymCDYRloQ97YD/QstIdOrt8raqxsx271x0XKVnS+Qq"
        "UvF1+2TZWO+yQ+T/qTv9TvnLh5/NP9/zvuv49Quub7bvU38mr+5YW8kvkfUkIhkCYYzRl23j1ol+"
        "z+JujcCOO0ASJYQJjAoP6g251bqazH1quzqNzlVwdBGrZqysnEiLbaZ4jZHdL1NR6KdLs8dHv3U7"
        "EEaIpAgkihKy1RmyDp6wnF91bS7P310frjyaVZBmklv0FmW5pYw7ZbJxh/+8de9jISXFig8Zzq5O"
        "he9M/uxdxHjr6bZylKQ9CCCvJk3X1Gr7zGBVNi87gpf9S7HRVWewz++OLeiy5JYjLxT28xhTfEpI"
        "JuMi9cdNzw2CxPc7Z+vEpY9lHBN5kRclRDS9VtyX4sVQdIOfjWyk5hy3fB09na6TC+PrgecvPtzN"
        "KChqesR/URIlRJAYiYK0tx3wVEeF05CtNF59d7L2zpX54e1rTa22WopCvG8uOqQzKQ5JkphkFCQj"
        "ClIUbTs4q6KMjIKi5n/zfNh9bvJcPMz/61Y5FVrOVMw1R4Mp0Tng69vtX2DHASTjYiAZF4z1z+R8"
        "kVul/Ty0Fp/DBEEptLSptMH4HqZIdPOjmacfOZH9RtnjGS+2dFYveWcj30U3U25MYJozsNZUQkhs"
        "eOIblY1Z7XqLvDbsTzolhAmEENJlyYumh9YuI4yQJoNFC78HJ9yTwcDf47dbyJ0WOm/7PklGhEGC"
        "xAnOIKvQmRU1aj2TjxBGbsfmlaGuxbax3mXX1ODa9URYGEAYheQcncsoKCNFYiYeERZ/vDD1+ljP"
        "SidBoSVWyRgUajqHZkkNSWNlMsovOm77upVaVo+QFLr7w2rHzIh/AeP/N/MAAAAAAAAAAAAAAAAA"
        "AAAAgAfTXwTjs87cEvn9AAAAAElFTkSuQmCC"
    ),
    'wd': (
        "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"
        "AElFTkSuQmCC"
    ),
}


def space_logo_data_uri(name: str) -> str:
    """
    按云盘 / 目录名匹配图标，返回可直接塞进 `<img src>` 的 data URI。

    匹配顺序（大小写无关）：

    1. 名字含 `115` → 115 网盘；
    2. 含 `123` → 123 网盘；
    3. 含 `天翼` / `189` → 天翼云盘；
    4. 名字恰为 `storage` / `downloads` → Acer（同一块宏基固态的两个挂载目录）；
    5. 名字恰为 `download` → Western Digital；
    6. 都不匹配 → 返回空串（调用方回退到首字母色块）。

    图标以 base64 内嵌，不依赖外网 —— 面板常在内网，外链图片会加载失败变裂图。

    :param name (str): 云盘或目录名

    :return str: `data:image/png;base64,...`；无匹配时返回 ""
    """
    raw = (name or "").strip()
    if not raw:
        return ""

    low = raw.lower()
    key = ""
    if "115" in low:
        key = "115"
    elif "123" in low:
        key = "123"
    elif "天翼" in raw or "189" in low:
        key = "189"
    elif low in ("storage", "downloads"):
        key = "acer"
    elif low == "download":
        key = "wd"

    b64 = _SPACE_LOGOS.get(key)
    if not b64:
        return ""
    return "data:image/png;base64," + "".join(b64)


LOCAL_FS_CLOUD = "LocalFsAPI"


def _is_local_mount(drive: Any) -> bool:
    """
    判断某个挂载点是否为「本地文件夹」类型（LocalFsAPI）。

    这类挂载不是云盘，而是把本机目录挂进 CD2；CD2 对它们的空间信息有一套特殊处理，
    详见 _local_mount_space_info。

    :param drive: get_sub_files 返回的挂载点对象

    :return bool: 是本地文件夹挂载返回 True
    """
    if getattr(drive, "isLocal", False):
        return True
    cloud_api = getattr(drive, "CloudAPI", None)
    return str(getattr(cloud_api, "name", "") or "") == LOCAL_FS_CLOUD


def _local_mount_space_info(client, mount_path: str, root_info: Any) -> Optional[Any]:
    """
    取本地文件夹挂载（LocalFsAPI）**可信**的空间信息。

    🔴 为什么要绕这一下（2026-09-29 实测）：
    CD2 对本地文件夹挂载的**根路径**返回的空间信息会陈旧，与所在文件系统严重不符，
    直接用它会让详情页「剩余」虚高。实测同一时刻、同一块盘（NAS volume2 上的 /download）：
      · 挂载根 get_space_info("/download")        → used 84.14 GB / free 800.01 GB  ❌
      · 子路径 get_space_info("/download/SSD")    → used 465.44 GB / free 418.70 GB ✅
        （与 NAS 上 `df -B1 /volume2` 的 Used / Available **逐字节相同**）
    也就是说：挂载根那份是缓存值，**挂载根以下的任意路径返回的是实时 statvfs**。

    做法：任取一个子目录查一次；只有它与挂载根属于**同一个文件系统**（totalSpace 相等）
    才采用，避免本地目录里嵌了别的云盘挂载时取错盘。

    :param client (CloudDriveClient): 已认证的 CloudDrive 客户端
    :param mount_path (str): 本地挂载根路径，如 "/download"
    :param root_info: 挂载根的空间信息（取不到子路径时的回退值）

    :return: 可信的空间信息；拿不到时返回 None，调用方沿用挂载根的值
    """
    root_total = getattr(root_info, "totalSpace", 0) or 0
    try:
        children = list(client.get_sub_files(mount_path))
    except Exception as e:
        logger.warning(
            f"【CloudDrive】列举本地挂载 {mount_path} 失败，沿用挂载根空间信息：{e}"
        )
        return None

    for child in children:
        if not getattr(child, "isDirectory", False):
            continue
        child_name = (getattr(child, "name", "") or "").strip()
        if not child_name:
            continue
        try:
            info = client.get_space_info(
                f"{mount_path.rstrip('/')}/{child_name}"
            )
        except Exception:
            continue
        if not info:
            continue
        child_total = getattr(info, "totalSpace", 0) or 0
        if root_total and child_total != root_total:
            # 不是同一个文件系统（本地目录里嵌了别的挂载），不能用
            continue
        return info
    return None


def get_cloud_space_items(client, black_dir: str = "") -> List[Dict[str, Any]]:
    """
    获取云盘空间明细（结构化），供详情页图形化排版使用。

    与 get_cloud_space 的区别：那个返回拼好的多行字符串（通知消息用），
    这个返回列表，每项形如
        {"name": str, "used": int, "total": int, "used_text": str,
         "total_text": str, "logo": str}
    容量单位已用 convert_bytes 格式化，`used`/`total` 保留原始字节数便于算占用率；
    `logo` 是该云盘的图标 data URI（按名称匹配，取不到时为空串）。

    本地文件夹挂载（LocalFsAPI）会走 _local_mount_space_info 取实时值，
    避免 CD2 挂载根的陈旧数据把「剩余」算虚。

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param black_dir (str): 黑名单目录，逗号分隔

    :return List[Dict]: 各云盘空间明细；取不到时返回空列表
    """
    if not client:
        return []

    black_list = [d.strip() for d in black_dir.split(",") if d.strip()]
    items: List[Dict[str, Any]] = []

    try:
        drives = client.get_sub_files("/", force_refresh=True)
    except Exception as e:
        logger.error("获取云盘列表失败: %s", e)
        return []

    for drive in drives:
        name = getattr(drive, "name", "") or ""
        full_path = getattr(drive, "fullPathName", "") or ""
        is_dir = getattr(drive, "isDirectory", False)

        if not is_dir or not name:
            continue
        if name in black_list:
            continue

        try:
            info = client.get_space_info(full_path)
            # 本地文件夹挂载：挂载根可能是陈旧缓存，改取所在文件系统的实时值
            if _is_local_mount(drive):
                local_info = _local_mount_space_info(client, full_path, info)
                if local_info:
                    info = local_info
            total = getattr(info, "totalSpace", 0) or 0
            used = getattr(info, "usedSpace", 0) or 0
            items.append(
                {
                    "name": name,
                    "used": used,
                    "total": total,
                    "used_text": convert_bytes(used),
                    "total_text": convert_bytes(total),
                    "logo": space_logo_data_uri(name),
                }
            )
        except Exception as e:
            logger.error("获取云盘 %s 空间信息失败: %s", name, e)

    return items


def get_cd2_system_info(client, black_dir: str = "") -> Dict[str, Any]:
    """
    获取CloudDrive2系统信息

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param black_dir (str): 黑名单目录，逗号分隔

    :return Dict: 系统信息字典
    """
    result: Dict[str, Any] = {
        "cpuUsage": None,
        "memUsageKB": None,
        "uptime": None,
        "fhTableCount": None,
        "dirCacheCount": None,
        "tempFileCount": None,
        "upload_count": 0,
        "download_count": 0,
        "download_speed": "0KB/s",
        "upload_speed": "0KB/s",
        "cloud_space": "\n",
        "cloud_space_items": [],
    }

    if not client:
        return result

    # 运行信息
    try:
        run_info = client.stub.GetRunningInfo(
            empty_pb2.Empty(),
            metadata=client._create_authorized_metadata(),
        )
        if run_info:
            result["cpuUsage"] = f"{getattr(run_info, 'cpuUsage', 0):.2f}%"
            mem_kb = getattr(run_info, "memUsageKB", 0) or 0
            result["memUsageKB"] = f"{mem_kb / 1024:.2f}MB"
            uptime = getattr(run_info, "uptime", 0) or 0
            result["uptime"] = convert_seconds(uptime)
            result["fhTableCount"] = getattr(run_info, "fhTableCount", 0) or 0
            result["dirCacheCount"] = getattr(run_info, "dirCacheCount", 0) or 0
            result["tempFileCount"] = getattr(run_info, "tempFileCount", 0) or 0
    except Exception as e:
        logger.error("获取CloudDrive2运行信息失败: %s", e)

    # 任务数量
    try:
        task_count = client.get_all_tasks_count()
        if task_count:
            result["upload_count"] = getattr(task_count, "uploadCount", 0) or 0
            result["download_count"] = getattr(task_count, "downloadCount", 0) or 0
    except Exception as e:
        logger.error("获取CloudDrive2任务数量失败: %s", e)

    # 下载速度
    try:
        download_resp = client.get_download_file_list()
        if download_resp:
            dl_speed = getattr(download_resp, "globalBytesPerSecond", 0) or 0
            if dl_speed:
                result["download_speed"] = f"{dl_speed / 1024 / 1024:.2f}MB/s"
            else:
                result["download_speed"] = "0KB/s"
    except Exception as e:
        logger.error("获取CloudDrive2下载速度失败: %s", e)

    # 上传速度
    try:
        upload_resp = client.get_upload_file_list(get_all=True)
        if upload_resp:
            ul_speed = getattr(upload_resp, "globalBytesPerSecond", 0) or 0
            if ul_speed:
                result["upload_speed"] = f"{ul_speed / 1024 / 1024:.2f}MB/s"
            else:
                result["upload_speed"] = "0KB/s"
    except Exception as e:
        logger.error("获取CloudDrive2上传速度失败: %s", e)

    # 云盘空间
    result["cloud_space"] = get_cloud_space(client, black_dir)
    # 结构化明细：详情页图形化排版用（get_cloud_space 的字符串版仍供通知消息使用）
    result["cloud_space_items"] = get_cloud_space_items(client, black_dir)

    return result


def restart_cd2(client) -> bool:
    """
    重启CloudDrive2服务

    :param client (CloudDriveClient): CloudDriveClient 实例

    :return bool: 成功返回 True，失败返回 False
    """
    if not client:
        logger.error("CloudDrive2 客户端未初始化")
        return False

    try:
        client.stub.RestartService(
            empty_pb2.Empty(),
            metadata=client._create_authorized_metadata(),
        )
        logger.info("CloudDrive2 重启命令已发送")
        return True
    except RpcError as e:
        if e.code() == StatusCode.UNAVAILABLE and "Socket closed" in str(e):
            logger.info("CloudDrive2 重启命令已发送（服务端已断开连接，正在重启中）")
            return True
        logger.error("CloudDrive2 重启失败: %s", e)
        return False
    except Exception as e:
        logger.error("CloudDrive2 重启失败: %s", e)
        return False


def add_offline_files(client, urls: str, to_folder: str) -> Tuple[bool, Optional[str]]:
    """
    添加离线下载任务

    :param client (CloudDriveClient): CloudDriveClient 实例
    :param urls (str): 下载链接，多个链接用换行分隔
    :param to_folder (str): 保存路径

    :return Tuple: (是否成功, 错误信息)
    """
    if not client:
        logger.error("CloudDrive2 客户端未初始化")
        return False, "CloudDrive2 客户端未初始化"

    try:
        request = clouddrive_pb2.AddOfflineFileRequest(
            urls=urls,
            toFolder=to_folder,
        )
        result = client.stub.AddOfflineFiles(
            request,
            metadata=client._create_authorized_metadata(),
        )
        if result and getattr(result, "success", False):
            logger.info("离线下载成功: %s -> %s", urls, to_folder)
            return True, None
        error_message = getattr(result, "errorMessage", "") or "未知错误"
        logger.error("离线下载失败: %s", error_message)
        return False, error_message
    except Exception as e:
        logger.error("离线下载异常: %s", e)
        return False, str(e)
