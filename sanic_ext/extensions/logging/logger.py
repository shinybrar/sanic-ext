from collections import defaultdict
from logging import LogRecord
from logging.handlers import QueueHandler
from multiprocessing import Manager
from queue import Empty, Full
from signal import SIGINT, SIGTERM
from signal import signal as signal_func

from sanic import Sanic
from sanic.log import access_logger, error_logger
from sanic.log import logger as root_logger
from sanic.log import server_logger


async def prepare_logger(app: Sanic, *_):
    Logger.prepare(app)


async def setup_logger(app: Sanic, *_):
    logger = Logger()
    app.manager.manage(
        "Logger",
        logger,
        {
            "queue": app.shared_ctx.logger_queue,
        },
    )


class SanicQueueHandler(QueueHandler):
    def emit(self, record: LogRecord) -> None:
        try:
            return super().enqueue(record)
        except Full:
            server_logger.warning(
                "Background logger is full. Emitting log in process."
            )
            server_logger.handle(record)


async def setup_server_logging(app: Sanic):
    qhandler = SanicQueueHandler(app.shared_ctx.logger_queue)
    app.ctx._logger_handlers = defaultdict(list)
    app.ctx._qhandler = qhandler

    for logger_instance in (root_logger, access_logger, error_logger):
        for handler in logger_instance.handlers:
            logger_instance.removeHandler(handler)
        logger_instance.addHandler(qhandler)


async def remove_server_logging(app: Sanic):
    for logger, handlers in app.ctx._logger_handlers.items():
        logger.removeHandler(app.ctx._qhandler)
        for handler in handlers:
            logger.addHandler(handler)


class Logger:
    def __init__(self):
        self.run = True
        self.loggers = {
            logger.name: logger
            for logger in (root_logger, access_logger, error_logger)
        }

    def __call__(self, queue) -> None:
        signal_func(SIGINT, self.stop)
        signal_func(SIGTERM, self.stop)

        while self.run:
            try:
                record: LogRecord = queue.get_nowait()
            except Empty:
                continue
            logger = self.loggers.get(record.name)
            logger.handle(record)

    def stop(self, *_):
        if self.run:
            self.run = False

    @classmethod
    def prepare(cls, app: Sanic):
        sync_manager = Manager()
        logger_queue = sync_manager.Queue(
            maxsize=app.config.LOGGING_QUEUE_MAX_SIZE
        )
        app.shared_ctx.logger_queue = logger_queue

    @classmethod
    def setup(cls, app: Sanic):
        app.main_process_start(prepare_logger)
        app.main_process_ready(setup_logger)
        app.before_server_start(setup_server_logging)
        app.before_server_stop(remove_server_logging)
