# -*- coding: utf-8 -*-
"""同 key 并发查询合并（single-flight）。

消息热路径上的协议端查询（机器人群角色、成员角色、is_robot）在缓存过期瞬间会被
同群/同用户的每条消息各打一次；协议端变慢时这些请求互相拖慢、各自等满超时，
整个消息处理看起来就像卡死。这里让同一 key 只保留一个进行中的任务，其余调用方
共享它的结果；调用方可以只等一小段时间，超时后任务继续在后台跑完并写缓存。

只依赖标准库，便于单元测试独立加载。
"""

import asyncio
from typing import Any, Awaitable, Callable, Hashable, Optional, Tuple


def shared_task(owner: Any, attr: str, key: Hashable,
                factory: Callable[[], Awaitable[Any]]) -> "asyncio.Future":
    """返回 owner.<attr>[key] 上进行中的任务；没有则用 factory() 新建。

    任务结束后自动从字典移除，字典本身持有强引用，避免任务被提前回收；
    插件卸载时可遍历该字典取消残留任务。
    """
    store = getattr(owner, attr, None)
    if store is None:
        store = {}
        setattr(owner, attr, store)
    task = store.get(key)
    if task is not None and not task.done():
        return task
    task = asyncio.ensure_future(factory())
    store[key] = task

    def _cleanup(done_task, _key=key, _store=store):
        if _store.get(_key) is done_task:
            _store.pop(_key, None)
        if not done_task.cancelled():
            # 取走异常，避免无人等待时出现 "Task exception was never retrieved"
            done_task.exception()

    task.add_done_callback(_cleanup)
    return task


async def wait_shared(task: "asyncio.Future", max_wait: Optional[float] = None) -> Tuple[bool, Any]:
    """等待共享任务。返回 (是否拿到结果, 结果)。

    max_wait 为 None 时等到任务结束；超时返回 (False, None)，任务不会被取消。
    调用方被取消时同样不影响共享任务（shield）。任务自身抛错按未拿到结果处理。
    """
    try:
        if max_wait is None:
            return True, await asyncio.shield(task)
        return True, await asyncio.wait_for(asyncio.shield(task), max_wait)
    except asyncio.TimeoutError:
        return False, None
    except asyncio.CancelledError:
        if task.cancelled():
            return False, None
        raise
    except Exception:
        return False, None
