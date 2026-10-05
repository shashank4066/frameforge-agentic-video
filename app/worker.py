"""Dedicated worker for QUEUE_BACKEND=redis deployments."""
import asyncio
import logging

from .config import Settings
from .pipeline import Pipeline
from .queue import Coordinator, QueueSignal
from .store import Store


async def main():
    settings = Settings.from_env()
    signal = QueueSignal(settings)
    store = Store(settings.database_path)
    coordinator = Coordinator(store, Pipeline(store, settings), settings, signal)
    await coordinator.start()
    try:
        await coordinator.task
    finally:
        await coordinator.stop()
        await signal.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
