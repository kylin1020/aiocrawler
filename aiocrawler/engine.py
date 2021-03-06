# coding: utf-8
import asyncio
import signal
import traceback
from inspect import iscoroutinefunction, isfunction
from random import uniform
from time import sleep
from typing import Iterator, List, Union

import aiojobs
from aiocrawler import BaseSettings, Field, Item, Request, Response, Spider, logger
from aiocrawler.downloaders import BaseDownloader
from aiocrawler.filters import BaseFilter
from aiocrawler.schedulers import BaseScheduler
from aiocrawler.collectors.base_collector import BaseCollector


class Engine(object):
    """
    The Engine schedules all components.
    """

    def __init__(self, spider: Spider,
                 settings: BaseSettings,
                 filters: BaseFilter = None,
                 scheduler: BaseScheduler = None,
                 downloader: BaseDownloader = None,
                 collector: BaseCollector = None,
                 job_scheduler: aiojobs.Scheduler = None
                 ):

        self._spider = spider
        self._scheduler = scheduler
        self._settings = settings

        self.__collector = collector if collector else BaseCollector()

        self._filters = filters
        self.__middlewares = []
        self.__downloader: BaseDownloader = downloader

        self.__signal_int_count = 0

        self.__startup_tasks = []
        self.__cleanup_tasks = []

        self.__job_scheduler: aiojobs.Scheduler = job_scheduler if job_scheduler else None
        self.__shutting_down = False
        self.__shutdown = False

        # noinspection PyBroadException
        try:
            # try import uvloop as Event Loop Policy

            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except Exception:
            pass

    def on_startup(self, target, *args, **kwargs):
        # prevent adding new target after startup tasks has been done
        if self.__collector.done_startup_tasks:
            logger.error('startup tasks has been done')
            return
        self.__startup_tasks.append((target, args, kwargs))

    def on_cleanup(self, target, *args, **kwargs):
        self.__cleanup_tasks.append((target, args, kwargs))

    @staticmethod
    async def __run_task(task):
        # noinspection PyBroadException
        try:
            if asyncio.iscoroutine(task):
                return await task
            else:
                return task

        except Exception:
            logger.error(traceback.format_exc())

    async def __initialize(self):
        """
        Initialize all necessary components.
        """
        logger.debug('Initializing...')

        if not self.__downloader:
            from aiocrawler.downloaders.aio_downloader import AioDownloader
            from aiohttp import ClientSession, CookieJar
            session = ClientSession(cookie_jar=CookieJar(unsafe=True))
            self.__downloader = AioDownloader(self._settings, session)
            self.on_cleanup(session.close)

        if not self._scheduler:
            if self._settings.REDIS_URL and not self._settings.DISABLE_REDIS:
                from aiocrawler.schedulers import RedisScheduler
                self._scheduler = RedisScheduler(settings=self._settings)
                await self._scheduler.create_redis_pool()

                if not self._filters:
                    from aiocrawler.filters import RedisFilter
                    self._filters = RedisFilter(self._settings, redis_pool=self._scheduler.redis_pool)

                self.on_cleanup(self._scheduler.close_redis_pool)

            else:
                from aiocrawler.schedulers import MemoryScheduler
                self._scheduler = MemoryScheduler(self._settings, self._spider)

        if not self._filters:
            if self._settings.REDIS_URL and not self._settings.DISABLE_REDIS:
                from aiocrawler.filters import RedisFilter
                self._filters = RedisFilter(self._settings, redis_pool=self._scheduler.redis_pool)
                await self._filters.create_redis_pool()
                self.on_cleanup(self._filters.close_redis_pool)

            else:
                from aiocrawler.filters import MemoryFilter
                self._filters = MemoryFilter(self._settings)

        from aiocrawler import middlewares

        for mw_name, key in self._settings.DEFAULT_MIDDLEWARES.items():
            if 0 <= key <= 1000 and mw_name in middlewares.__all__:
                self.__middlewares.append((getattr(middlewares, mw_name), key))

        for mw, key in self._settings.MIDDLEWARES:
            if 0 <= key <= 1000 and issubclass(middlewares.BaseMiddleware, mw):
                self.__middlewares.append((mw, key))
        self.__middlewares = sorted(self.__middlewares, key=lambda x: x[1])
        self.__middlewares = list(map(lambda x: x[0](self._settings, self), self.__middlewares))

        logger.debug('Initialized')

    async def __handle_downloader_output(self, request: Request, data: Union[Response, Exception, None]):
        """
        Handle the information returned by the downloader.
        :param request: Request
        :param data: Response or Exception
        """
        handled_data = None

        if isinstance(data, Exception):
            await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_downloader_exception()))
            handled_data = await self.__handle_downloader_exception(request, data)

        elif isinstance(data, Response):
            await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_response_received(response=data)))
            handled_data = await self.__handle_downloader_response(request, data)

        if not handled_data:
            return
        if not isinstance(handled_data, Iterator) and not isinstance(handled_data, List):
            handled_data = [handled_data]

        tasks = []
        for one in handled_data:
            if isinstance(one, Request):
                if iscoroutinefunction(self._scheduler.send_request):
                    tasks.append(asyncio.ensure_future(self._scheduler.send_request(one)))
                else:
                    self._scheduler.send_request(one)
            elif isinstance(one, Item):
                item = await self.__handle_spider_item(one)
                if item:
                    tasks.append(asyncio.ensure_future(
                        self.__filter_and_send(request, item)))

        if len(tasks):
            await asyncio.wait(tasks)

    async def __handle_downloader_exception(self, request: Request, exception: Exception):
        handled_data = None
        for middleware in self.__middlewares:
            handled_data = await self.__run_task(middleware.process_exception(request, exception))
            if handled_data:
                break

        if handled_data is None:
            await self.__job_scheduler.spawn(self.__run_task(self._scheduler.append_error_request(request)))
            if isfunction(request.err_callback) and hasattr(self._spider.__class__, request.err_callback.__name__):
                handled_data = request.err_callback(request, exception)

        return handled_data

    async def __handle_downloader_response(self, request: Request, response: Response):
        handled_data = None
        response = self.__downloader.__parse_html__(request, response)
        for middleware in self.__middlewares:
            handled_data = await self.__run_task(middleware.process_response(request, response))
            if handled_data:
                if isinstance(handled_data, Response):
                    response = handled_data
                break

        if isinstance(handled_data, Response) or handled_data is None:
            logger.success('Crawled ({status}) <{method} {url}>',
                           status=response.status,
                           method=request.method,
                           url=request.url
                           )

            response.meta = request.meta
            if hasattr(self._spider.__class__, request.callback.__name__):
                handled_data = request.callback(response)

        return handled_data

    async def __handle_spider_item(self, item: Item):
        for middleware in self.__middlewares:
            processed_item = await self.__run_task(middleware.process_item(item))
            if isinstance(processed_item, Item):
                item = processed_item
                break

        if item:
            item_copy = item.__class__()
            for field in self.get_fields(item):
                item_copy[field] = item.get(field, None)

            return item_copy

    async def __filter_and_send(self, request: Request, item: Item):
        item = await self.__run_task(self._filters.filter_item(item))
        if item:
            await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_item(item)))

            logger.success('Crawled from <{method} {url}> \n {item}',
                           method=request.method, url=request.url, item=item)

            await self.__run_task(self._scheduler.send_item(item))

    @staticmethod
    def get_fields(item: Item):
        for field_name in item.__class__.__dict__:
            if isinstance(getattr(item.__class__, field_name), Field):
                yield field_name

    async def __handle_scheduler_word(self):
        """
        Handle the word from the scheduler.
        """
        while not self.__shutting_down:
            await asyncio.sleep(self._settings.PROCESS_DALEY)
            word = await self.__run_task(self._scheduler.get_word())
            if word:
                await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_word()))

                logger.debug(
                    'Making Request from word <word: {word}>'.format(word=word))
                request = self._spider.make_request(word)
                if request:
                    await self.__run_task(self._scheduler.send_request(request))

    async def __handle_scheduler_request(self):
        """
        Handle the request from scheduler.
        """
        while not self.__shutting_down:
            await asyncio.sleep(self._settings.PROCESS_DALEY)
            request = await self.__run_task(self._scheduler.get_request())
            if request:
                request = await self.__run_task(self._filters.filter_request(request))
                if request:
                    await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_request(request)))

                    for downloader_middleware in self.__middlewares:
                        await self.__run_task(downloader_middleware.process_request(request))

                    sleep(self._settings.DOWNLOAD_DALEY * uniform(0.5, 1.5))
                    data = await self.__run_task(self.__downloader.download(request))
                    await self.__handle_downloader_output(request, data)

    def __shutdown_signal(self, _, __):
        self.__signal_int_count += 1

        if self.__signal_int_count == 1:
            logger.debug('Received SIGNAL INT, shutting down gracefully. Send again to force')
            self.close_crawler('Received SIGNAL INT')
        else:
            self.close_crawler('Received SIGNAL INT', force=True)
            logger.debug('Received SIGNAL INT Over 2 times, shutting down the Crawler by force...')

    def close_crawler(self, reason: str = 'Finished', force: bool = False):
        if force:
            self.__shutdown = True
        else:
            self.__shutting_down = True

        self.__collector.finish_reason = reason

    async def _main(self):
        await self.__initialize()

        tasks = []
        for target, args, kwargs in self.__startup_tasks:
            tasks.append(asyncio.ensure_future(self.__run_task(target(*args, **kwargs))))

        for _ in range(self._settings.CONCURRENT_WORDS):
            tasks.append(asyncio.ensure_future(self.__handle_scheduler_word()))

        for _ in range(self._settings.CONCURRENT_REQUESTS):
            tasks.append(asyncio.ensure_future(self.__handle_scheduler_request()))

        await self.__job_scheduler.spawn(self.__run_task(self.__collector.collect_start(
            self._spider.__class__.__name__, self._settings.DATETIME_FORMAT
        )))

        await asyncio.wait(tasks)

        tasks = []
        for target, args, kwargs in self.__cleanup_tasks:
            tasks.append(asyncio.ensure_future(self.__run_task(target(*args, **kwargs))))

        if len(tasks):
            await asyncio.wait(tasks)

        # collect finished information
        await self.__run_task(self.__collector.collect_finish(self._settings.DATETIME_FORMAT))
        await self.__run_task(self.__collector.output_stats())

        logger.debug('The Crawler is closed. <Reason {reason}>', reason=self.__collector.finish_reason)

    async def main(self):
        if self.__collector.running:
            logger.error('The Crawler already running')
            return

        if not self.__job_scheduler:
            self.__job_scheduler = await aiojobs.create_scheduler(limit=None)

        signal.signal(signal.SIGINT, self.__shutdown_signal)
        main_job = await self.__job_scheduler.spawn(self._main())

        while True:
            if self.__shutdown or main_job.closed:
                break
            await asyncio.sleep(0.1)
        await main_job.close()
        await self.__job_scheduler.close()

    def run(self):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self.main())
        finally:
            loop.close()
