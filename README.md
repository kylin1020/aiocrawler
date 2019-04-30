
![version](https://img.shields.io/badge/version-v1.15-green.svg)
![support](https://img.shields.io/badge/python-3.6%20%7C%203.7-blue.svg)
![license](https://img.shields.io/badge/license-MIT-yellow.svg)

#### Aiocrawler is an asynchronous, distributed crawler framework, based on redis, asyncio, aiohttp.
**Different from scrapy, aiocrawler starts with a series of words,
generates urls based on word and starts crawling instead of
starting_urls**
- ### Installation
```bash
pip3 install aiocrawler
```
Or you can install by this way
```bash
python3 setup.py install
```

- ### start project
```bash
aiocrawler startproject demo
```
**This will create a demo directory with the following contents**
```
+-- demo/
|    +-- items.py
|    +-- middlewares.py
|    +-- run.py
|    +-- settings.py
|    +-- spiders.py
```
**This is the code for your first Spider**
```python
# coding: utf-8
from aiocrawler import BaseSettings, Spider, Request


class DemoSpider(Spider):
    name = "demo"

    def __init__(self, settings: BaseSettings):
        Spider.__init__(self, settings)

    def make_request(self, word):
        return Request(word, callback=self.parse)

    def parse(self, response):
        pass
```
**Configure your settings.py**
```python
# coding: utf-8
from aiocrawler import BaseSettings
from demo.middlewares import DemoMiddleware


class DemoSettings(BaseSettings):
    PROJECT_NAME = 'demo'

    """
    If you use the redis server as the scheduler, the REDIS_URL must be configured.
    """
    REDIS_URL = 'redis://user:password@redis_address:port'
    REDIS_PROJECT_NAME = 'demo'

    CONCURRENT_REQUESTS = 32
    CONCURRENT_WORDS = 32
    DEFAULT_TIMEOUT = 20
    DEFAULT_HEADERS = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN',
    }

    DOWNLOAD_DALEY = 0
    PROCESS_DALEY = 0.01
    DOWNLOADER_MIDDLEWARES = [
        (DemoMiddleware, 300),

    ]
```

### run the project
```bash
aiocrawler run demo
```
