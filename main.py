from __future__ import annotations
from typing import TypeVar, Generic, Callable, Any, cast, Protocol, runtime_checkable
from functools import wraps, partial, reduce
import inspect
import asyncio
import contextlib
import weakref
import sys
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import logging
from abc import ABC, abstractmethod
import traceback
from datetime import datetime, timedelta

T = TypeVar('T')
S = TypeVar('S')

@runtime_checkable
class Monitorable(Protocol):
    """Protocol for objects that can be monitored"""
    async def get_metrics(self) -> dict: ...

def monitored(func: Callable) -> Callable:
    """
    Decorator to mark methods for monitoring in SystemAnalyzer.
    Records execution time and error metrics for the decorated method.
    """
    func._monitored = True
    
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            execution_time = time.time() - start_time
            return result
        except Exception as e:
            raise e
    
    return wrapper

class CircuitBreaker:
    """Circuit breaker pattern implementation"""
    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure_time = None
        self.state = "CLOSED"
        self._lock = asyncio.Lock()

    async def __call__(self, func: Callable) -> Any:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with self._lock:
                if self.state == "OPEN":
                    if (datetime.now() - self.last_failure_time).seconds >= self.reset_timeout:
                        self.state = "HALF-OPEN"
                    else:
                        raise Exception("Circuit breaker is OPEN")

                try:
                    result = await func(*args, **kwargs)
                    if self.state == "HALF-OPEN":
                        self.state = "CLOSED"
                        self.failure_count = 0
                    return result
                except Exception as e:
                    self.failure_count += 1
                    self.last_failure_time = datetime.now()
                    if self.failure_count >= self.failure_threshold:
                        self.state = "OPEN"
                    raise e
        return wrapper

class MetricCollector:
    """Enhanced thread-safe metric collection with aggregation"""
    def __init__(self):
        self._metrics = defaultdict(lambda: {
            'count': 0,
            'sum': 0,
            'min': float('inf'),
            'max': float('-inf'),
            'last_update': None
        })
        self._lock = asyncio.Lock()
        self._refs = weakref.WeakKeyDictionary()

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if obj not in self._refs:
            self._refs[obj] = defaultdict(lambda: {
                'count': 0,
                'sum': 0,
                'min': float('inf'),
                'max': float('-inf'),
                'last_update': None
            })
        return self._refs[obj]

    async def record(self, metric_name: str, value: float):
        async with self._lock:
            metric = self._metrics[metric_name]
            metric['count'] += 1
            metric['sum'] += value
            metric['min'] = min(metric['min'], value)
            metric['max'] = max(metric['max'], value)
            metric['last_update'] = datetime.now()

    async def get_stats(self, metric_name: str) -> dict:
        async with self._lock:
            metric = self._metrics[metric_name]
            if metric['count'] == 0:
                return None
            return {
                'count': metric['count'],
                'avg': metric['sum'] / metric['count'],
                'min': metric['min'],
                'max': metric['max'],
                'last_update': metric['last_update']
            }

class Memoized(Generic[T]):
    """Enhanced memoization with TTL and size limits"""
    def __init__(self, ttl: int = 3600, max_size: int = 1000):
        self.cache = {}
        self.ttl = ttl
        self.max_size = max_size
        self.lock = asyncio.Lock()
        self.last_access = {}

    async def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self.last_access.items() if now - v > self.ttl]
        for k in expired:
            del self.cache[k]
            del self.last_access[k]

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            key = (args, frozenset(kwargs.items()))
            async with self.lock:
                await self._cleanup()
                
                if len(self.cache) >= self.max_size:
                    oldest_key = min(self.last_access, key=self.last_access.get)
                    del self.cache[oldest_key]
                    del self.last_access[oldest_key]

                if key in self.cache:
                    self.last_access[key] = time.time()
                    return self.cache[key]

                result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
                self.cache[key] = result
                self.last_access[key] = time.time()
                return result
        return wrapper

class Observable(ABC, Monitorable):
    """Enhanced observable pattern with metrics"""
    def __init__(self):
        self._observers: set[Callable] = set()
        self._metrics = MetricCollector()
        self._event_history = []
        self._max_history = 100

    def attach(self, observer: Callable) -> None:
        self._observers.add(observer)

    def detach(self, observer: Callable) -> None:
        self._observers.discard(observer)

    async def notify(self, event_type: str, *args, **kwargs) -> None:
        async with asyncio.Lock():
            start_time = time.time()
            
            event = {
                'type': event_type,
                'timestamp': datetime.now(),
                'args': args,
                'kwargs': kwargs
            }
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history.pop(0)

            for observer in self._observers:
                try:
                    if asyncio.iscoroutinefunction(observer):
                        await observer(*args, **kwargs)
                    else:
                        observer(*args, **kwargs)
                except Exception as e:
                    await self._metrics.record(f'observer_error_{observer.__name__}', 1)
                    logging.error(f"Observer {observer.__name__} failed: {str(e)}")

            execution_time = time.time() - start_time
            await self._metrics.record(f'notify_{event_type}_time', execution_time)

    async def get_metrics(self) -> dict:
        """Implement Monitorable protocol"""
        return {
            'total_observers': len(self._observers),
            'event_history_size': len(self._event_history),
            'last_event': self._event_history[-1] if self._event_history else None
        }

@dataclass
class TaskContext(Generic[T]):
    """Enhanced context manager with retry logic and metrics"""
    resource: T
    timeout: float = 1.0
    retries: int = 3
    backoff_factor: float = 1.5
    _metrics: MetricCollector = field(default_factory=MetricCollector)
    
    async def __aenter__(self) -> T:
        retry_count = 0
        last_exception = None
        
        while retry_count < self.retries:
            try:
                await asyncio.wait_for(self._initialize_resource(), self.timeout)
                await self._metrics.record('task_start', time.time())
                return self.resource
            except Exception as e:
                last_exception = e
                retry_count += 1
                await self._metrics.record('task_retry', retry_count)
                if retry_count < self.retries:
                    await asyncio.sleep(self.backoff_factor ** retry_count)
        
        await self._metrics.record('task_failure', 1)
        raise last_exception or Exception("Task initialization failed")

    async def _initialize_resource(self) -> None:
        if hasattr(self.resource, 'initialize') and callable(self.resource.initialize):
            await self.resource.initialize()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self._metrics.record('task_error', 1)
            logging.error(f"Error in context: {exc_val}")
            logging.debug(f"Traceback: {''.join(traceback.format_tb(exc_tb))}")
            return False
        
        await self._metrics.record('task_success', 1)
        return True

class SystemAnalyzer:
    """Enhanced system analysis with performance tracking"""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._prepare_class()
        cls._metrics = MetricCollector()

    @classmethod
    def _prepare_class(cls):
        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if getattr(method, '_monitored', False):
                setattr(cls, name, cls._wrap_method(method))

    @classmethod
    def _wrap_method(cls, method):
        @wraps(method)
        async def wrapper(self, *args, **kwargs):
            start_time = time.time()
            try:
                async with TaskContext(self):
                    result = await method(self, *args, **kwargs)
                    execution_time = time.time() - start_time
                    await cls._metrics.record(f'{method.__name__}_time', execution_time)
                    return result
            except Exception as e:
                await cls._metrics.record(f'{method.__name__}_error', 1)
                raise e
        return wrapper

    @classmethod
    async def get_performance_metrics(cls):
        metrics = {}
        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if getattr(method, '_monitored', False):
                metrics[name] = await cls._metrics.get_stats(f'{name}_time')
        return metrics

class AsyncResourceManager:
    """Enhanced resource manager with monitoring and limits"""
    def __init__(self, pool_size: int = 5, max_resources: int = 100):
        self._pool = ThreadPoolExecutor(max_workers=pool_size)
        self._resources: weakref.WeakSet = weakref.WeakSet()
        self._metrics = MetricCollector()
        self._max_resources = max_resources
        self._resource_semaphore = asyncio.Semaphore(max_resources)

    async def acquire(self, resource_type: type[T]) -> T:
        async with self._resource_semaphore:
            start_time = time.time()
            try:
                resource = await self._create_resource(resource_type)
                self._resources.add(resource)
                await self._metrics.record('resource_creation_time', time.time() - start_time)
                return resource
            except Exception as e:
                await self._metrics.record('resource_creation_error', 1)
                raise e

    async def _create_resource(self, resource_type: type[T]) -> T:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, resource_type)

    async def cleanup(self):
        self._pool.shutdown(wait=True)
        await self._metrics.record('cleanup_executed', 1)

    async def get_stats(self) -> dict:
        return {
            'active_resources': len(self._resources),
            'resource_creation_times': await self._metrics.get_stats('resource_creation_time'),
            'creation_errors': await self._metrics.get_stats('resource_creation_error')
        }

async def main():
    """Example usage of the advanced features"""
    logging.basicConfig(level=logging.INFO)
    
    resource_manager = AsyncResourceManager()
    
    class DataProcessor(Observable, SystemAnalyzer):
        @monitored
        async def process_data(self, data: list) -> None:
            await self.notify("Processing started", data)
            await asyncio.sleep(1)
            await self.notify("Processing completed", len(data))
    
    processor = DataProcessor()
    
    async def log_observer(event_type: str, *args: Any):
        logging.info(f"Observer received: {event_type} with args: {args}")
    
    processor.attach(log_observer)
    
    await processor.process_data([1, 2, 3, 4, 5])
    
    metrics = await processor.get_performance_metrics()
    logging.info(f"Performance metrics: {metrics}")
    
    await resource_manager.cleanup()

if __name__ == "__main__":
    asyncio.run(main())