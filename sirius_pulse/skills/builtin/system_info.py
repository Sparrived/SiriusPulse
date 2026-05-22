"""Built-in skill for collecting local system information."""

from __future__ import annotations

import os
import platform
from datetime import datetime
from typing import Any

SKILL_META = {
    "name": "system_info",
    "description": "获取主机的系统信息",
    "version": "1.0.0",
    "tags": ["system", "info"],
    "dependencies": ["psutil"],
    "parameters": {
        "categories": {
            "type": "list[str]",
            "description": "要获取的信息类别，可选值: cpu, memory, disk, network, os。不传则返回全部",
            "required": False,
            "default": ["cpu", "memory", "disk", "os"],
        },
    },
}


def run(
    categories: list[str] | None = None,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if categories is None:
        categories = ["cpu", "memory", "disk", "os"]

    result: dict[str, Any] = {}

    if "os" in categories:
        result["os"] = _get_os_info()
    if "cpu" in categories:
        result["cpu"] = _get_cpu_info()
    if "memory" in categories:
        result["memory"] = _get_memory_info()
    if "disk" in categories:
        result["disk"] = _get_disk_info()
    if "network" in categories:
        result["network"] = _get_network_info()

    if data_store is not None:
        history = data_store.get("query_history", [])
        history.append(
            {
                "time": datetime.now().isoformat(),
                "categories": categories,
            }
        )
        data_store.set("query_history", history[-20:])

    return result


def _get_os_info() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python_version": platform.python_version(),
        "hostname": platform.node(),
    }


def _get_cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "logical_cores": os.cpu_count() or 0,
    }
    try:
        import psutil

        info["physical_cores"] = psutil.cpu_count(logical=False) or 0
        info["usage_percent"] = psutil.cpu_percent(interval=0.1)
        freq = psutil.cpu_freq()
        if freq:
            info["frequency_mhz"] = {
                "current": round(freq.current, 1),
                "min": round(freq.min, 1),
                "max": round(freq.max, 1),
            }
    except ImportError:
        info["note"] = "psutil未安装，仅显示基本信息"
    return info


def _get_memory_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil

        vm = psutil.virtual_memory()
        info["total_gb"] = round(vm.total / (1024**3), 2)
        info["available_gb"] = round(vm.available / (1024**3), 2)
        info["used_gb"] = round(vm.used / (1024**3), 2)
        info["usage_percent"] = vm.percent
    except ImportError:
        info["note"] = "psutil未安装，无法获取内存信息"
    return info


def _get_disk_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil

        partitions = psutil.disk_partitions()
        disks: list[dict[str, Any]] = []
        for part in partitions:
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append(
                    {
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024**3), 2),
                        "used_gb": round(usage.used / (1024**3), 2),
                        "free_gb": round(usage.free / (1024**3), 2),
                        "usage_percent": usage.percent,
                    }
                )
            except (PermissionError, OSError):
                continue
        info["partitions"] = disks
    except ImportError:
        info["note"] = "psutil未安装，无法获取磁盘信息"
    return info


def _get_network_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil

        addrs = psutil.net_if_addrs()
        interfaces: dict[str, list[str]] = {}
        for iface, addr_list in addrs.items():
            ips = [
                address.address
                for address in addr_list
                if address.family.name in ("AF_INET", "AF_INET6")
            ]
            if ips:
                interfaces[iface] = ips
        info["interfaces"] = interfaces
    except ImportError:
        info["note"] = "psutil未安装，无法获取网络信息"
    return info