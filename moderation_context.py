# -*- coding: utf-8 -*-
import asyncio
import hashlib
import re
import time
from collections import deque
from typing import Tuple

from astrbot.api import logger


CONTEXT_MESSAGE_MAX_CHARS = 600
CONTEXT_IMAGE_EVIDENCE_MAX_CHARS = 400
CONTEXT_TOTAL_MAX_CHARS = 6000
LOCAL_CONTEXT_MAX_ENTRIES = 50
LOCAL_CONTEXT_PROMPT_MAX_ENTRIES = 20
LOCAL_CONTEXT_RETENTION_SECONDS = 900
LOCAL_CONTEXT_MESSAGE_MAX_CHARS = 1000
LOCAL_CONTEXT_MAX_USERS = 2048
LOCAL_CONTEXT_CLEANUP_INTERVAL_SECONDS = 300
CONTEXT_EVENT_KEY_FALLBACK_MAX = 4096
ONEBOT_HISTORY_TIMEOUT = 20.0
ONEBOT_HISTORY_QUEUE_TIMEOUT = 60.0
HISTORY_CACHE_TTL_SECONDS = 1.5
HISTORY_CACHE_MAX_GROUPS = 256
HISTORY_FETCH_COUNT = 100
HISTORY_MAX_CONCURRENCY = 8
COMBINED_HANDLED_MAX_ENTRIES = 8192
COMBINED_HANDLED_CLEANUP_INTERVAL_SECONDS = 60

_CONTEXT_EVENT_KEY_MARKER = object()
_CONTEXT_EVENT_KEY_ATTR = "_group_guardian_context_key"

_COMBINED_SEMANTIC_SUSPECT_RE = re.compile(
    r"(?:日抛|周抛|月抛|plus|福利|资源|上车|接单|兼职|代理|代购|推广|引流|"
    r"联系|私聊|加(?:我|q|v|vx|wx)|微信|微\s*信|v\s*x|w\s*x|扫码|"
    r"频道|返利|低价|出售|购买|免费领|/\s*[a-z0-9_.-]{3,}|"
    r"(?:qq|群)\s*[:：]?\s*\d{5,}|@\s*[a-z0-9_.-]{3,}|"
    r"https?://|www\.|[a-z0-9-]+\.(?:com|cn|net|top|xyz))",
    re.IGNORECASE,
)


class ModerationContextMixin:
    """Ordered remote/local context and split-message candidate state."""

    def _init_moderation_context_resources(self, llm_concurrency: int = 12) -> None:
        """Initialize context buffers and bounded OneBot history resources."""
        self._moderation_context_data = {}
        self._moderation_context_last_cleanup = 0.0
        self._moderation_context_arrival_counter = 0
        self._context_event_key_counter = 0
        self._context_event_key_fallback = deque(
            maxlen=CONTEXT_EVENT_KEY_FALLBACK_MAX
        )
        self._combined_handled = {}
        self._combined_handled_last_cleanup = 0.0
        history_concurrency = max(
            1, min(HISTORY_MAX_CONCURRENCY, int(llm_concurrency or 1))
        )
        self._history_semaphore = asyncio.Semaphore(history_concurrency)
        self._history_cache = {}
        self._history_inflight = {}

    async def _close_moderation_context_resources(self) -> None:
        """Cancel shared history tasks when the plugin is unloaded."""
        inflight = getattr(self, "_history_inflight", None) or {}
        tasks = list({task for task in inflight.values() if not task.done()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        inflight.clear()
        cache = getattr(self, "_history_cache", None)
        if isinstance(cache, dict):
            cache.clear()

    @staticmethod
    def _positive_int(value) -> int:
        try:
            result = int(value or 0)
            return result if result > 0 else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _context_source_value(source, key):
        if isinstance(source, dict):
            return source.get(key)
        try:
            return getattr(source, key, None)
        except Exception:
            return None

    @staticmethod
    def _context_identifier(value) -> str:
        """Normalize an adapter message ID while rejecting empty sentinel values."""
        if value is None or isinstance(value, bool):
            return ""
        value = getattr(value, "value", value)
        text = str(value).strip()
        if text.casefold() in {"", "none", "null", "nil", "0"}:
            return ""
        return text

    @classmethod
    def _context_nested_mappings(cls, payload):
        """Yield the bounded OneBot envelopes that can carry message metadata."""
        queue = deque([payload])
        seen = set()
        while queue and len(seen) < 32:
            current = queue.popleft()
            if not isinstance(current, dict):
                continue
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            yield current
            for key in ("data", "event", "raw_event", "raw_message", "payload"):
                nested = current.get(key)
                if isinstance(nested, dict):
                    queue.append(nested)

    def _context_event_sources(self, event) -> list:
        sources = []
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            sources.append(msg_obj)
            sources.extend(self._context_nested_mappings(
                self._context_source_value(msg_obj, "raw_message")
            ))
        sources.append(event)
        try:
            raw = getattr(event, "raw_event", None)
        except Exception:
            raw = None
        sources.extend(self._context_nested_mappings(raw))
        return sources

    def _context_message_id(self, event) -> str:
        sources = self._context_event_sources(event)
        for key in ("message_id", "messageId", "msg_id", "msgId"):
            for source in sources:
                value = self._context_identifier(
                    self._context_source_value(source, key)
                )
                if value:
                    return value
        return ""

    def _context_event_positive_value(self, event, keys) -> int:
        for key in keys:
            for source in self._context_event_sources(event):
                value = self._positive_int(self._context_source_value(source, key))
                if value:
                    return value
        return 0

    def _event_message_order(self, event) -> Tuple[int, int]:
        """Return ``(message_seq, timestamp)`` without assuming one adapter shape."""
        seq = self._context_event_positive_value(
            event, ("message_seq", "messageSeq", "seq")
        )
        timestamp = self._context_event_positive_value(
            event, ("time", "timestamp")
        )
        return seq, timestamp

    def _context_message_key(self, event) -> str:
        msg_id = self._context_message_id(event)
        if msg_id:
            return f"message:{msg_id}"

        message_seq = self._context_event_positive_value(
            event, ("message_seq", "messageSeq", "seq")
        )
        if message_seq:
            return f"sequence:{message_seq}"

        try:
            cached = getattr(event, _CONTEXT_EVENT_KEY_ATTR, None)
        except Exception:
            cached = None
        if (isinstance(cached, tuple) and len(cached) == 2
                and cached[0] is _CONTEXT_EVENT_KEY_MARKER):
            return cached[1]

        try:
            raw = getattr(event, "raw_event", None)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            cached = raw.get(_CONTEXT_EVENT_KEY_ATTR)
            if isinstance(cached, str):
                return cached

        fallback = getattr(self, "_context_event_key_fallback", None)
        if fallback is not None:
            for cached_event, cached_key in fallback:
                if cached_event is event:
                    return cached_key

        counter = int(getattr(self, "_context_event_key_counter", 0) or 0) + 1
        self._context_event_key_counter = counter
        key = f"event:{counter}"
        cache_value = (_CONTEXT_EVENT_KEY_MARKER, key)
        try:
            setattr(event, _CONTEXT_EVENT_KEY_ATTR, cache_value)
            if getattr(event, _CONTEXT_EVENT_KEY_ATTR, None) is cache_value:
                return key
        except Exception:
            pass
        if isinstance(raw, dict):
            raw[_CONTEXT_EVENT_KEY_ATTR] = key
            return key

        if fallback is None:
            fallback = deque(maxlen=CONTEXT_EVENT_KEY_FALLBACK_MAX)
            self._context_event_key_fallback = fallback
        fallback.append((event, key))
        return key

    @classmethod
    def _history_message_order(cls, message: dict) -> Tuple[int, int]:
        if not isinstance(message, dict):
            return 0, 0
        return (
            cls._positive_int(message.get("message_seq") or message.get("seq")),
            cls._positive_int(message.get("time") or message.get("timestamp")),
        )

    def _ensure_history_resources(self) -> None:
        if getattr(self, "_history_semaphore", None) is None:
            self._history_semaphore = asyncio.Semaphore(HISTORY_MAX_CONCURRENCY)
        if getattr(self, "_history_cache", None) is None:
            self._history_cache = {}
        if getattr(self, "_history_inflight", None) is None:
            self._history_inflight = {}

    def _cache_history_snapshot(self, group_id: str, messages: list) -> None:
        cache = self._history_cache
        cache.pop(group_id, None)
        cache[group_id] = (
            time.monotonic() + HISTORY_CACHE_TTL_SECONDS,
            tuple(messages),
        )
        while len(cache) > HISTORY_CACHE_MAX_GROUPS:
            cache.pop(next(iter(cache)))

    async def _load_history_snapshot(self, group_id: str, gid: int) -> list:
        """Fetch one raw snapshot; callers apply their own message cutoff."""
        messages = []
        acquired = False
        try:
            client = await self._get_client(None)
            if not client:
                return []
            await asyncio.wait_for(
                self._history_semaphore.acquire(),
                timeout=ONEBOT_HISTORY_QUEUE_TIMEOUT,
            )
            acquired = True
            result = await asyncio.wait_for(
                client.call_action(
                    "get_group_msg_history",
                    group_id=gid,
                    message_seq=0,
                    count=HISTORY_FETCH_COUNT,
                ),
                timeout=ONEBOT_HISTORY_TIMEOUT,
            )
            ok, error = self._check_api_result(result, "获取群消息历史")
            if not ok:
                logger.debug(f"[GroupMgr] 获取上下文消息 API 失败: {error}")
            else:
                result = self._extract_data_result(result)
                raw_messages = (
                    result.get("messages", []) if isinstance(result, dict) else []
                )
                messages = [item for item in raw_messages if isinstance(item, dict)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[GroupMgr] 获取上下文消息失败: {exc}")
        finally:
            if acquired:
                self._history_semaphore.release()
        self._cache_history_snapshot(group_id, messages)
        return messages

    def _discard_history_task(self, group_id: str, task) -> None:
        inflight = getattr(self, "_history_inflight", None)
        if isinstance(inflight, dict) and inflight.get(group_id) is task:
            inflight.pop(group_id, None)

    async def _shared_history_snapshot(self, group_id: str, gid: int) -> list:
        self._ensure_history_resources()
        cached = self._history_cache.get(group_id)
        if cached:
            expires_at, messages = cached
            if time.monotonic() < expires_at:
                return list(messages)
            self._history_cache.pop(group_id, None)

        task = self._history_inflight.get(group_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._load_history_snapshot(group_id, gid)
            )
            self._history_inflight[group_id] = task
            task.add_done_callback(
                lambda done, key=group_id: self._discard_history_task(key, done)
            )
        # One caller timing out/cancelling must not cancel the request shared by
        # other messages from the same burst.
        return list(await asyncio.shield(task))

    async def _fetch_context_messages(
        self,
        group_id: str,
        current_msg_id: str,
        count: int = 30,
        current_seq: int = 0,
        current_time: int = 0,
    ) -> list:
        gid = self._positive_int(group_id)
        if not gid:
            return []
        try:
            messages = await self._shared_history_snapshot(str(group_id), gid)
            filtered = []
            for index, message in enumerate(messages):
                if (current_msg_id
                        and str(message.get("message_id", "")) == str(current_msg_id)):
                    continue
                msg_seq, msg_time = self._history_message_order(message)
                if current_seq and msg_seq and msg_seq >= current_seq:
                    continue
                if (not (current_seq and msg_seq)
                        and current_time and msg_time and msg_time >= current_time):
                    continue
                filtered.append((message, msg_seq, msg_time, index))

            if filtered and all(seq for _, seq, _, _ in filtered):
                filtered.sort(key=lambda item: (item[1], item[3]))
            elif any(timestamp for _, _, timestamp, _ in filtered):
                filtered.sort(key=lambda item: (
                    item[2] if item[2] else 0,
                    item[1] if item[1] else 0,
                    item[3],
                ))
            return [message for message, _, _, _ in filtered][-count:]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[GroupMgr] 获取上下文消息失败: {exc}")
            return []

    def _record_moderation_context(
        self,
        event,
        group_id: str,
        user_id: str,
        user_name: str,
        text: str,
        pending: bool = None,
    ) -> None:
        text = str(text or "").strip()
        if not group_id or not user_id or not text:
            return
        store = getattr(self, "_moderation_context_data", None)
        if store is None:
            store = {}
            self._moderation_context_data = store
        now = time.time()
        last_cleanup = float(
            getattr(self, "_moderation_context_last_cleanup", 0.0) or 0.0
        )
        if now - last_cleanup >= LOCAL_CONTEXT_CLEANUP_INTERVAL_SECONDS:
            self._moderation_context_last_cleanup = now
            expired = now - LOCAL_CONTEXT_RETENTION_SECONDS
            for store_key, entries in list(store.items()):
                discarded = [
                    entry for entry in entries
                    if float(entry.get("recorded_at", 0.0) or 0.0) < expired
                ]
                for entry in discarded:
                    ready_event = entry.get("ready_event")
                    if ready_event is not None:
                        ready_event.set()
                retained = [
                    entry for entry in entries
                    if float(entry.get("recorded_at", 0.0) or 0.0) >= expired
                ]
                if retained:
                    store[store_key] = deque(
                        retained, maxlen=LOCAL_CONTEXT_MAX_ENTRIES
                    )
                else:
                    store.pop(store_key, None)

        key = (str(group_id), str(user_id))
        queue = store.get(key)
        if queue is None:
            while len(store) >= LOCAL_CONTEXT_MAX_USERS:
                removed = store.pop(next(iter(store)))
                for entry in removed:
                    ready_event = entry.get("ready_event")
                    if ready_event is not None:
                        ready_event.set()
            queue = deque(maxlen=LOCAL_CONTEXT_MAX_ENTRIES)
            store[key] = queue
        else:
            # Keep recently active users at the end so capacity eviction is LRU-like.
            store[key] = store.pop(key)

        msg_id = self._context_message_id(event)
        context_key = self._context_message_key(event)
        seq, event_time = self._event_message_order(event)
        for entry in queue:
            if entry.get("context_key") != context_key:
                continue
            entry.update({
                "message_seq": seq or entry.get("message_seq", 0),
                "event_time": event_time or entry.get("event_time", 0),
                "user_name": str(user_name or entry.get("user_name") or "未知"),
                "text": self._bounded_audit_text(
                    text, LOCAL_CONTEXT_MESSAGE_MAX_CHARS
                ),
            })
            self._set_context_entry_pending(entry, pending)
            return

        arrival_id = int(
            getattr(self, "_moderation_context_arrival_counter", 0) or 0
        ) + 1
        self._moderation_context_arrival_counter = arrival_id
        if len(queue) >= LOCAL_CONTEXT_MAX_ENTRIES:
            removed = queue[0]
            ready_event = removed.get("ready_event")
            if ready_event is not None:
                ready_event.set()
        entry = {
            "context_key": context_key,
            "message_id": msg_id,
            "message_seq": seq,
            "event_time": event_time,
            "arrival_id": arrival_id,
            "recorded_at": now,
            "user_name": str(user_name or "未知"),
            "text": self._bounded_audit_text(text, LOCAL_CONTEXT_MESSAGE_MAX_CHARS),
        }
        self._set_context_entry_pending(entry, pending)
        queue.append(entry)

    @staticmethod
    def _set_context_entry_pending(entry: dict, pending: bool) -> None:
        if pending is None:
            return
        ready_event = entry.get("ready_event")
        if pending:
            if ready_event is None or ready_event.is_set():
                ready_event = asyncio.Event()
                entry["ready_event"] = ready_event
            entry["pending"] = True
            return
        entry["pending"] = False
        if ready_event is not None:
            ready_event.set()

    async def _wait_for_prior_context_ready(
        self, event, group_id: str, user_id: str
    ) -> None:
        """Wait for older same-sender image OCR so bursts retain causal order."""
        store = getattr(self, "_moderation_context_data", None) or {}
        queue = store.get((str(group_id), str(user_id)))
        if not queue:
            return
        current_key = self._context_message_key(event)
        current = next(
            (entry for entry in queue if entry.get("context_key") == current_key),
            None,
        )
        if current is None:
            return
        current_seq = self._positive_int(current.get("message_seq"))
        current_time = self._positive_int(current.get("event_time"))
        current_arrival = self._positive_int(current.get("arrival_id"))
        entries = self._recent_sender_entries(
            group_id,
            user_id,
            current_context_key=current_key,
            count=LOCAL_CONTEXT_MAX_ENTRIES,
            window=LOCAL_CONTEXT_RETENTION_SECONDS,
            before_seq=current_seq,
            before_time=current_time,
            before_arrival=current_arrival,
        )
        ready_events = []
        seen = set()
        for entry in entries:
            ready_event = entry.get("ready_event")
            if (not entry.get("pending") or ready_event is None
                    or ready_event.is_set() or id(ready_event) in seen):
                continue
            seen.add(id(ready_event))
            ready_events.append(ready_event)
        if not ready_events:
            return
        # 每个图片分支自身已有下载/Provider 执行边界；这里不再设置更短的
        # 二次超时，否则多图分批处理时后续消息会越过尚未完成的 OCR。
        await asyncio.gather(*(ready.wait() for ready in ready_events))

    @staticmethod
    def _local_context_order(entry: dict) -> tuple:
        seq = ModerationContextMixin._positive_int(entry.get("message_seq"))
        event_time = ModerationContextMixin._positive_int(entry.get("event_time"))
        arrival = ModerationContextMixin._positive_int(entry.get("arrival_id"))
        recorded_at = float(entry.get("recorded_at", 0.0) or 0.0)
        if seq:
            return 0, seq, event_time, arrival, recorded_at
        if event_time:
            return 1, event_time, arrival, 0, recorded_at
        return 2, arrival, 0, 0, recorded_at

    def _recent_sender_entries(
        self,
        group_id: str,
        user_id: str,
        current_context_key: str = "",
        count: int = 20,
        window: int = LOCAL_CONTEXT_RETENTION_SECONDS,
        before_seq: int = 0,
        before_time: int = 0,
        before_arrival: int = 0,
    ) -> list:
        store = getattr(self, "_moderation_context_data", None) or {}
        queue = store.get((str(group_id), str(user_id)))
        if not queue:
            return []
        now = time.time()

        def is_before(entry: dict) -> bool:
            entry_seq = self._positive_int(entry.get("message_seq"))
            if before_seq and entry_seq:
                return entry_seq < before_seq

            entry_time = self._positive_int(entry.get("event_time"))
            if before_time and entry_time and entry_time != before_time:
                return entry_time < before_time

            entry_arrival = self._positive_int(entry.get("arrival_id"))
            return not before_arrival or entry_arrival < before_arrival

        entries = [
            entry for entry in queue
            if now - float(entry.get("recorded_at", 0.0) or 0.0) <= window
            and (
                not current_context_key
                or entry.get("context_key") != current_context_key
            )
            and is_before(entry)
        ]
        entries.sort(key=self._local_context_order)
        return entries[-max(1, count):]

    def _format_recent_sender_context(
        self,
        group_id: str,
        user_id: str,
        current_context_key: str,
        current_seq: int = 0,
        current_time: int = 0,
        current_arrival: int = 0,
    ) -> str:
        # 提示词始终保留完整的 20 条本地兜底上下文；组合处罚条数仍由
        # combine_detect_count 控制，避免远端历史不可用时长拆分丢失开头。
        count = LOCAL_CONTEXT_PROMPT_MAX_ENTRIES
        window = max(5, min(
            self._cfg_int("combine_detect_window_seconds", 60, group_id=group_id),
            600,
        ))
        entries = self._recent_sender_entries(
            group_id,
            user_id,
            current_context_key=current_context_key,
            count=count,
            window=window,
            before_seq=current_seq,
            before_time=current_time,
            before_arrival=current_arrival,
        )
        lines = []
        for entry in entries:
            content = str(entry.get("text", "") or "").strip()
            if not content:
                continue
            if len(content) > CONTEXT_MESSAGE_MAX_CHARS:
                content = content[:CONTEXT_MESSAGE_MAX_CHARS] + "..."
            identifier = (
                entry.get("message_id") or entry.get("message_seq") or "本地"
            )
            lines.append(f"  [消息 {identifier}] {content}")
        result = "\n".join(lines)
        if len(result) > CONTEXT_TOTAL_MAX_CHARS:
            return result[-CONTEXT_TOTAL_MAX_CHARS:]
        return result

    def _set_moderation_combine_state(
        self,
        event,
        group_id: str,
        user_id: str,
        extra_message_ids: list,
        state: str,
    ) -> None:
        store = getattr(self, "_moderation_context_data", None) or {}
        queue = store.get((str(group_id), str(user_id)))
        if not queue:
            return
        current_id = self._context_message_id(event)
        current_context_key = self._context_message_key(event)
        target_ids = {
            str(value) for value in (extra_message_ids or []) if str(value)
        }
        if current_id:
            target_ids.add(current_id)
        current_seq, _ = self._event_message_order(event)
        for entry in queue:
            entry_id = str(entry.get("message_id", "") or "")
            entry_seq = self._positive_int(entry.get("message_seq"))
            if (entry_id in target_ids
                    or entry.get("context_key") == current_context_key
                    or (not current_id and current_seq and entry_seq == current_seq)):
                if not state:
                    if entry.get("combine_state") == "pending":
                        entry.pop("combine_state", None)
                else:
                    entry["combine_state"] = state

    def _combined_in_cooldown(
        self, group_id: str, user_id: str, signature: str
    ) -> bool:
        """Deduplicate one exact candidate without suppressing newer fragments."""
        if not signature:
            return False
        store = getattr(self, "_combined_handled", None)
        if not store:
            return False
        key = (str(group_id), str(user_id), str(signature))
        until = store.get(key, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            store.pop(key, None)
            return False
        return True

    def _mark_combined_handled(
        self, group_id: str, user_id: str, signature: str,
        seconds: int = 60,
    ) -> None:
        if not signature:
            return
        store = getattr(self, "_combined_handled", None)
        if store is None:
            store = {}
            self._combined_handled = store
        now = time.time()
        last_cleanup = float(
            getattr(self, "_combined_handled_last_cleanup", 0.0) or 0.0
        )
        if now - last_cleanup >= COMBINED_HANDLED_CLEANUP_INTERVAL_SECONDS:
            self._combined_handled_last_cleanup = now
            for key in [key for key, expires_at in store.items()
                        if now >= expires_at]:
                store.pop(key, None)

        key = (str(group_id), str(user_id), str(signature))
        # Refreshing a signature also refreshes its insertion order.
        store.pop(key, None)
        store[key] = now + seconds
        while len(store) > COMBINED_HANDLED_MAX_ENTRIES:
            store.pop(next(iter(store)))

    def _collect_combined_text(
        self, event, group_id: str, user_id: str, current_text: str,
    ) -> Tuple[str, list, str]:
        """Build one ordered, bounded same-sender candidate for split evasion."""
        if not self._cfg("combine_detect_enabled", True, group_id=group_id):
            return "", [], ""
        if not user_id:
            return "", [], ""
        count = max(2, min(
            self._cfg_int("combine_detect_count", 5, group_id=group_id), 20
        ))
        window = max(5, min(
            self._cfg_int(
                "combine_detect_window_seconds", 60, group_id=group_id
            ),
            600,
        ))
        current_id = self._context_message_id(event)
        current_context_key = self._context_message_key(event)
        self._record_moderation_context(
            event,
            group_id,
            user_id,
            event.get_sender_name() if hasattr(event, "get_sender_name") else "未知",
            current_text,
        )
        entries = self._recent_sender_entries(
            group_id, user_id, count=count, window=window
        )
        current_seq, current_time = self._event_message_order(event)
        if current_seq:
            entries = [
                entry for entry in entries
                if not self._positive_int(entry.get("message_seq"))
                or self._positive_int(entry.get("message_seq")) <= current_seq
            ]
        elif current_time:
            entries = [
                entry for entry in entries
                if not self._positive_int(entry.get("event_time"))
                or self._positive_int(entry.get("event_time")) <= current_time
            ]

        current_indexes = [
            index for index, entry in enumerate(entries)
            if entry.get("context_key") == current_context_key
        ]
        if current_indexes:
            entries = entries[:current_indexes[-1] + 1]
        barrier_indexes = [
            index for index, entry in enumerate(entries)
            if entry.get("combine_state") == "consumed"
        ]
        if barrier_indexes:
            entries = entries[barrier_indexes[-1] + 1:]
        entries = entries[-count:]
        if len(entries) < 2:
            return "", [], ""

        parts = []
        message_ids = []
        signature_parts = []
        for entry in entries:
            raw_text = str(entry.get("text", "") or "").strip()
            compacted = re.sub(r"\s+", " ", raw_text).strip()
            if not compacted:
                continue
            # 审核正文必须保留大小写，Base16/32/58/62/64 等编码对大小写
            # 敏感；去重签名再单独做小写归一化即可。
            parts.append(compacted)
            normalized = compacted.lower()
            message_id = str(entry.get("message_id", "") or "")
            if message_id and message_id != current_id:
                message_ids.append(message_id)
            signature_parts.append(
                f"{entry.get('context_key', message_id)}|"
                f"{entry.get('message_seq', 0)}|{normalized}"
            )
        if len(parts) < 2:
            return "", [], ""

        seamless = "".join(parts)
        spaced = " ".join(parts)
        signature = hashlib.sha256(
            "\x1f".join(signature_parts).encode("utf-8", "ignore")
        ).hexdigest()
        return f"{seamless}\n{spaced}", message_ids, signature

    @staticmethod
    def _combined_needs_semantic_review(combined_text: str) -> bool:
        """筛出值得 LLM 复核的零规则命中组合，控制普通模式调用量。"""
        seamless, _, spaced = str(combined_text or "").partition("\n")
        if not seamless:
            return False
        if _COMBINED_SEMANTIC_SUSPECT_RE.search(combined_text):
            return True

        # 逐字发送仍要复核。两条正常短句或长句不满足该形态，避免普通模式
        # 从第二次发言起几乎每条都调用 LLM。
        parts = [part for part in spaced.split(" ") if part]
        if len(parts) < 2:
            return False
        single_char_parts = sum(len(part) == 1 for part in parts)
        return (
            len(seamless) <= 32
            and single_char_parts >= max(2, (len(parts) * 2 + 2) // 3)
        )
