"""In-process fanout of real Python log records to authorized Dashboard clients."""

import asyncio
import collections
import datetime
import logging
import threading


class LiveTerminalHandler(logging.Handler):
    def __init__(self, history_limit=500):
        super().__init__(level=logging.INFO)
        self.history = collections.deque(maxlen=history_limit)
        self.queues = set()
        self._queue_loops = {}
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            event = {
                "timestamp": datetime.datetime.fromtimestamp(
                    record.created, datetime.timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()[:4000],
            }
            with self._lock:
                self.history.append(event)
                queue_targets = [(queue, loop) for queue, loop in self._queue_loops.items() if not loop.is_closed()]
            for queue, loop in queue_targets:
                loop.call_soon_threadsafe(self._publish_one, queue, event)
        except Exception:
            self.handleError(record)

    def subscribe(self):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self.queues.add(queue)
            self._queue_loops[queue] = loop
            history = list(self.history)[-100:]
        for event in history:
            queue.put_nowait(event)
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            self.queues.discard(queue)
            self._queue_loops.pop(queue, None)


    def _publish(self, event):
        """Compatibility helper for in-loop callers; publishes to current subscriptions."""
        with self._lock:
            queues = tuple(self.queues)
        for queue in queues:
            self._publish_one(queue, event)

    @staticmethod
    def _publish_one(queue, event):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


live_terminal = LiveTerminalHandler()


def install_live_terminal():
    root = logging.getLogger()
    if live_terminal not in root.handlers:
        root.addHandler(live_terminal)