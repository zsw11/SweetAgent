"""进程内 TTL 缓存（OPT-07 查询 / RAG 缓存底座）。

设计决策（为什么是进程内、为什么类级共享、为什么深拷贝）：
1. **进程内内存缓存**：本项目单机部署（FastAPI 为主请求链路，Streamlit 只是前端），
   DB 往返本身比内存慢几个数量级；引入 Redis / DB 缓存表反而让"查缓存"变成一次 IO，
   还要处理序列化与一致性，收益为负。代价：进程重启即失效——可接受，
   查询缓存只是加速层，不是持久状态。
2. **模块级共享实例**：`execute_readonly_sql` 工具每次注册都 `ReadOnlyExecutor()` 新建实例，
   缓存若挂在实例上等于零命中（每个实例一份空缓存）；挂在模块级单例上，
   同一进程内所有实例共享一份缓存。
3. **线程安全**：部门子图 query 已改 ThreadPoolExecutor 并行（考点三十八），
   SQL 执行与 RAG 检索会并发访问缓存，必须加锁；数据量小（≤ max_entries），
   一把互斥锁足够，不引入读写锁复杂度。
4. **深拷贝语义**：set 存副本、get 返副本，缓存内容与外部完全隔离——
   SQL rows / RAG hits 都是共享引用的 dict 列表，任何一方修改都不污染另一方。
5. **可观测**：hit / miss / evict / invalidate 全部走结构化日志（cache.* 事件），
   命中率 = hits / (hits + misses)，可被监控消费。
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("cache")


class TTLCache:
    """线程安全的 TTL + LRU 缓存。

    语义：
    - ``set(key, value, ttl_seconds)``：ttl <= 0 视为不缓存（no-op 返回 False）；
    - ``get(key)``：命中返回深拷贝并刷新 LRU 热度；过期 / 缺失返回 None（惰性清过期项）；
    - ``invalidate(prefix)``：删除 key 以 prefix 开头的所有条目（表级失效用）；空串 = 清空；
    - ``stats()``：{hits, misses, entries, evictions, invalidations, hit_rate}。
    """

    def __init__(self, max_entries: int = 512, name: str = "default"):
        self._max = max(1, int(max_entries))
        self._name = name
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(self, key: str) -> Optional[Any]:
        if not key:
            return None
        with self._lock:
            now = time.monotonic()
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                logger.debug("cache.miss", cache=self._name, key=key[:40])
                return None
            expire_at, value = item
            if expire_at <= now:
                del self._data[key]
                self._misses += 1
                logger.debug("cache.miss", cache=self._name, key=key[:40], reason="expired")
                return None
            self._data.move_to_end(key)  # LRU：刚被访问 → 排最热
            self._hits += 1
            logger.debug("cache.hit", cache=self._name, key=key[:40])
            return copy.deepcopy(value)

    def set(self, key: str, value: Any, ttl_seconds: int) -> bool:
        if not key or ttl_seconds <= 0:
            return False
        with self._lock:
            now = time.monotonic()
            self._data[key] = (now + ttl_seconds, copy.deepcopy(value))
            self._data.move_to_end(key)
            # 淘汰：先清过期项，仍超限再按 LRU 弹出最久未用
            self._purge(now)
            return True

    # ------------------------------------------------------------------
    # 失效
    # ------------------------------------------------------------------
    def invalidate(self, key_prefix: str = "") -> int:
        """删除 key 以 key_prefix 开头的所有条目；空串 = 清空全部。

        Returns: 删除条数。
        """
        with self._lock:
            if not key_prefix:
                n = len(self._data)
                self._data.clear()
                self._invalidations += n
                if n:
                    logger.info("cache.clear", cache=self._name, entries=n)
                return n
            keys = [k for k in self._data if k.startswith(key_prefix)]
            for k in keys:
                del self._data[k]
            if keys:
                self._invalidations += len(keys)
                logger.info(
                    "cache.invalidate", cache=self._name, prefix=key_prefix[:60],
                    entries=len(keys),
                )
            return len(keys)

    def clear(self) -> int:
        """清空全部条目，返回清除条数。"""
        return self.invalidate("")

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "cache": self._name,
                "entries": len(self._data),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "invalidations": self._invalidations,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _purge(self, now: float) -> None:
        """清理过期项；仍超 max_entries 则按 LRU 弹出最久未用。"""
        expired = [k for k, (exp, _) in self._data.items() if exp <= now]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._max:
            self._data.popitem(last=False)  # OrderedDict 头部 = 最久未用
            self._evictions += 1


# ----------------------------------------------------------------------
# 模块级共享实例（跨进程内所有 executor / retriever 实例共享）
# ----------------------------------------------------------------------
def _build(name: str) -> TTLCache:
    if settings.CACHE_ENABLED:
        return TTLCache(max_entries=settings.CACHE_MAX_ENTRIES, name=name)
    # 关闭缓存时返回一个"拒绝一切写入"的空缓存（max=1，写入即被淘汰，逻辑等价禁用）
    return TTLCache(max_entries=1, name=name)


_sql_cache: Optional[TTLCache] = None
_rag_cache: Optional[TTLCache] = None


def get_sql_cache() -> TTLCache:
    """SQL 结果缓存（ReadOnlyExecutor 类级共享）。"""
    global _sql_cache
    if _sql_cache is None:
        _sql_cache = _build("sql")
    return _sql_cache


def get_rag_cache() -> TTLCache:
    """RAG 检索缓存（KnowledgeRetriever 类级共享）。"""
    global _rag_cache
    if _rag_cache is None:
        _rag_cache = _build("rag")
    return _rag_cache


def cache_stats() -> dict[str, Any]:
    """全量缓存监控快照：{cache: stats}，命中率可观测（验收标准之一）。"""
    return {
        "sql": get_sql_cache().stats(),
        "rag": get_rag_cache().stats(),
    }
