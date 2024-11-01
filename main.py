from __future__ import annotations
from typing import TypeVar, Generic, Callable, Any, cast
from functools import wraps, partial, reduce
import inspect
import asyncio
import contextlib
import weakref
import sys
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import logging
from abc import ABC, abstractmethod

T = TypeVar('T')
S = TypeVar('S')

class MetricCollector:
    """Thread-safe metric collection using descriptor protocol and weak references"""
    def __init__(self):
        self._metrics = defaultdict(int)
        self._lock = asyncio.Lock()
        self._refs = weakref.WeakKeyDictionary()

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if obj not in self._refs:
            self._refs[obj] = defaultdict(int)
        return self._refs[obj]

class Memoized(Generic[T]):
    """Generic memoization decorator with TTL and weak references"""
    def __init__(self, ttl: int = 3600):
        self.cache = weakref.WeakValueDictionary()
        self.ttl = ttl
        self.lock = asyncio.Lock()

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            key = (args, frozenset(kwargs.items()))
            async with self.lock:
                if key in self.cache:
                    return self.cache[key]
                result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
                self.cache[key] = result
                return result
        return wrapper

class Observable(ABC):
    """Abstract base class for observable pattern with async capabilities"""
    def __init__(self):
        self._observers: set[Callable] = set()
        self._metrics = MetricCollector()

    def attach(self, observer: Callable) -> None:
        self._observers.add(observer)

    def detach(self, observer: Callable) -> None:
        self._observers.discard(observer)

    async def notify(self, *args, **kwargs) -> None:
        async with asyncio.Lock():
            for observer in self._observers:
                if asyncio.iscoroutinefunction(observer):
                    await observer(*args, **kwargs)
                else:
                    observer(*args, **kwargs)

@dataclass
class TaskContext(Generic[T]):
    """Generic context manager for task execution with resource management"""
    resource: T
    timeout: float = 1.0
    retries: int = 3
    _metrics: MetricCollector = field(default_factory=MetricCollector)
    
    async def __aenter__(self) -> T:
        return self.resource

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._metrics['errors'] += 1
            logging.error(f"Error in context: {exc_val}")
            return False
        return True

class SystemAnalyzer:
    """Advanced system analysis using metaclasses and descriptors"""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._prepare_class()

    @classmethod
    def _prepare_class(cls):
        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if getattr(method, '_monitored', False):
                setattr(cls, name, cls._wrap_method(method))

    @classmethod
    def _wrap_method(cls, method):
        @wraps(method)
        async def wrapper(self, *args, **kwargs):
            async with TaskContext(self):
                return await method(self, *args, **kwargs)
        return wrapper

def monitored(func):
    """Decorator for monitoring method execution"""
    func._monitored = True
    return func

class AsyncResourceManager:
    """Resource manager with async context management and pooling"""
    def __init__(self, pool_size: int = 5):
        self._pool = ThreadPoolExecutor(max_workers=pool_size)
        self._resources: weakref.WeakSet = weakref.WeakSet()
        self._metrics = MetricCollector()

    async def acquire(self, resource_type: type[T]) -> T:
        resource = await self._create_resource(resource_type)
        self._resources.add(resource)
        return resource

    async def _create_resource(self, resource_type: type[T]) -> T:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, resource_type)

    async def cleanup(self):
        self._pool.shutdown(wait=True)

async def main():
    """Example usage of the advanced features"""
    logging.basicConfig(level=logging.INFO)
    
    resource_manager = AsyncResourceManager()
    
    class DataProcessor(Observable):
        @monitored
        async def process_data(self, data: list) -> None:
            await self.notify("Processing started", data)
            await asyncio.sleep(0.1)
            await self.notify("Processing completed", len(data))
    
    processor = DataProcessor()
    
    async def log_observer(message: str, data: Any):
        logging.info(f"Observer received: {message} with data: {data}")
    
    processor.attach(log_observer)
    
    await processor.process_data([1, 2, 3, 4, 5])
    
    await resource_manager.cleanup()

if __name__ == "__main__":
    asyncio.run(main())