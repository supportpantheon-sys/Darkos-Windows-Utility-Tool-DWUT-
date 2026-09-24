"""
ThreadWorker — replaces the original ThreadManager.

Improvements over the original:
  • Tasks return typed results, not bare Any.
  • Errors are published on the EventBus (app.error) instead of silently logged.
  • `cancel(task_id)` attempts cancellation and marks the task.
  • `shutdown()` drains the pool cleanly on app exit.
  • `@task` decorator makes a function submit itself automatically.

Usage
─────
    # Direct submission
    task_id = worker.submit(my_func, arg1, arg2,
                            on_done=lambda r: bus.publish(Events.DASHBOARD_STATS, r),
                            on_error=lambda e: bus.publish(Events.APP_ERROR, str(e)))

    # Decorator style — the decorated function returns a Future when called
    @worker.task(on_done=handle_result)
    def fetch_proxies(region: str) -> list[ProxyRecord]:
        ...

    fetch_proxies("CA")   # runs in background, calls handle_result on completion
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from core.events import Events, bus

T = TypeVar("T")


class ThreadWorker:
    """
    Central background task runner.

    One global pool (max_workers=12) shared across the whole app.
    Use `submit()` for one-shot tasks, `submit_periodic()` for polling loops.
    """

    def __init__(self, max_workers: int = 12) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dwut")
        self._lock = threading.Lock()
        self._futures: dict[int, Future] = {}
        self._counter = 0
        self._periodic_stop_events: list[threading.Event] = []

    # ── one-shot tasks ────────────────────────────────────────────────────────

    def submit(
        self,
        func: Callable[..., T],
        *args: Any,
        on_done: Optional[Callable[[T], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        **kwargs: Any,
    ) -> int:
        """
        Submit `func(*args, **kwargs)` to the background pool.

        Returns a task_id that can be passed to `cancel()`.
        `on_done` and `on_error` are called on the worker thread — if they
        need to touch UI they must go through EventBus.publish() or
        widget.after().
        """
        with self._lock:
            task_id = self._counter
            self._counter += 1

        def _wrapped() -> None:
            try:
                result = func(*args, **kwargs)
                if on_done:
                    on_done(result)
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    bus.publish(Events.APP_ERROR, f"{func.__name__}: {exc}")
            finally:
                with self._lock:
                    self._futures.pop(task_id, None)

        future = self._pool.submit(_wrapped)
        with self._lock:
            self._futures[task_id] = future

        return task_id

    def cancel(self, task_id: int) -> bool:
        """Attempt to cancel a pending task.  Returns True if cancelled."""
        with self._lock:
            future = self._futures.get(task_id)
        if future:
            return future.cancel()
        return False

    # ── periodic tasks ────────────────────────────────────────────────────────

    def submit_periodic(
        self,
        func: Callable[[], None],
        interval_s: float,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> threading.Event:
        """
        Call `func()` repeatedly every `interval_s` seconds in a daemon thread.

        Returns the stop_event so the caller can cancel the loop:
            stop = worker.submit_periodic(refresh_stats, 2.0)
            ...
            stop.set()   # stop the loop

        The loop also halts if `AppState.shutting_down` is True.
        """
        if stop_event is None:
            stop_event = threading.Event()

        self._periodic_stop_events.append(stop_event)

        def _loop() -> None:
            from app.state import state  # late import to avoid circular dep
            while not stop_event.is_set() and not state.shutting_down:
                try:
                    func()
                except Exception as exc:
                    bus.publish(Events.APP_ERROR, f"Periodic task error: {exc}")
                stop_event.wait(timeout=interval_s)

        t = threading.Thread(target=_loop, daemon=True, name=f"periodic-{id(func)}")
        t.start()
        return stop_event

    # ── decorator ─────────────────────────────────────────────────────────────

    def task(
        self,
        on_done: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
    ) -> Callable:
        """
        Decorator factory.  Wraps a function so calling it submits it to the pool.

            @worker.task(on_done=handle_result)
            def expensive_work(x: int) -> str:
                ...

            expensive_work(42)   # returns int task_id immediately
        """
        def decorator(func: Callable) -> Callable:
            def wrapper(*args, **kwargs) -> int:
                return self.submit(
                    func, *args,
                    on_done=on_done,
                    on_error=on_error,
                    **kwargs,
                )
            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            return wrapper
        return decorator

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def shutdown(self, wait: bool = True) -> None:
        """Signal all periodic loops to stop and drain the thread pool."""
        for ev in self._periodic_stop_events:
            ev.set()
        self._pool.shutdown(wait=wait)


# Module-level singleton — import `worker` everywhere
worker: ThreadWorker = ThreadWorker()
