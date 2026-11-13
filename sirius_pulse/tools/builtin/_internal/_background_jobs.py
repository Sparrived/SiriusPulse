"""后台任务的进行中句柄：让「停止」能打断一个正在等待的慢操作。

后台轮询任务（例如生图）在等上游返回时只是挂在一个 future 上。停止请求来自另一条
调用链（``background_task`` 工具），两边通过这里按任务键会合：

- 轮询方开始慢操作前 :func:`track`，结束后 :func:`untrack`；
- 停止方 :func:`request_stop` 记下请求，并取消正在等待的 future；
- 轮询方在动手前和被取消后用 :func:`stop_requested` 区分「被要求停止」与
  「进程关停」，前者记成已停止，后者照常向上抛。

只放内存：句柄本身活不过进程重启，重启后的残留任务由各工具自己的恢复逻辑处理。
"""

from __future__ import annotations

import asyncio

_inflight: dict[str, asyncio.Future] = {}
_stop_requested: set[str] = set()


def track(key: str, future: asyncio.Future) -> None:
    _inflight[key] = future


def untrack(key: str) -> None:
    _inflight.pop(key, None)


def request_stop(key: str) -> bool:
    """记下停止请求；正在等待的慢操作会被立刻取消。返回是否打断了进行中的操作。"""
    _stop_requested.add(key)
    future = _inflight.get(key)
    if future is not None and not future.done():
        future.cancel()
        return True
    return False


def stop_requested(key: str) -> bool:
    return key in _stop_requested


def clear(key: str) -> None:
    _stop_requested.discard(key)
    _inflight.pop(key, None)


def cancelled_by_request(key: str) -> bool:
    """当前这次取消来自停止请求，而不是外层任务（关停）被取消。"""
    task = asyncio.current_task()
    outer_cancelling = bool(task is not None and task.cancelling())
    return stop_requested(key) and not outer_cancelling
