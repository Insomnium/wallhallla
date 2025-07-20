#!/usr/bin/env python3

import os.path
import subprocess
from enum import Enum
import logging
import os

import attr
import configparser
import argparse
import requests
from pathlib import Path
import schedule
import time

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

CONFIG_PATH = f"{Path.home()}/.config/wallhalla/config"

class WHConfig:
    api_key: str = attr.ib()
    login: str = attr.ib()
    collection: str = attr.ib()
    cache_dir: str = attr.ib()
    freq_sec: int = attr.ib()

    def __init__(self):
        self.__preserve_config()
        file_conf = configparser.ConfigParser()
        file_conf.read(CONFIG_PATH)

        arg_conf = argparse.ArgumentParser()
        arg_conf.add_argument('--api-key', help='Override API key') # TODO: request from stdin for security
        arg_conf.add_argument('--login', help='Override login')
        arg_conf.add_argument('--cache-dir', help='Override cache directory')
        arg_conf.add_argument('--collection', help='Override collection')
        arg_conf.add_argument('--freq-sec', help='Override frequency in seconds')
        arg_conf.add_argument('--freq-fetch-sec', help='Collection content fetching interval in seconds')
        args = arg_conf.parse_args()

        self.api_key = args.api_key or file_conf["DEFAULT"]["api.key"]
        self.login = args.login or file_conf["DEFAULT"]["login"]
        self.collection = args.collection or file_conf["DEFAULT"]["collection"]
        self.freq_sec = int(args.freq_sec or file_conf["DEFAULT"]["frequency.sec"])
        self.cache_dir = args.cache_dir or file_conf["CACHE"]["cache.dir"]
        self.fetch_freq = int(args.freq_fetch_sec or file_conf["CACHE"]["cache.fetch.sec"])

    @staticmethod
    def __preserve_config():
        if not Path(CONFIG_PATH).exists():
            logger.debug(f'Initializing default config file at {CONFIG_PATH}')
            conf_dir, _, _ = CONFIG_PATH.rpartition(os.sep)
            Path(conf_dir).mkdir(parents=True)
            config = configparser.ConfigParser()
            config['DEFAULT'] = {
                'api.key': '<API_KEY>',
                'login': '<LOGIN>',
                'collection': '<COLLECTION>',
                'freq.sec': '10',
            }
            config['CACHE'] = {
                'cache.dir': '/tmp/wallhalla',
                'cache.fetch.sec': '60',
            }
            with open(CONFIG_PATH, 'w') as f:
                config.write(f)

class Environment(Enum): # TODO: add support for other popular envs & test
    GNOME = 'GNOME'
    KDE = 'KDE'
    NONE = 'NONE'

    def build_changer(self, **kwargs):
        match self:
            case Environment.GNOME:
                return GnomeWallChanger(**kwargs)
            case _:
                return DefaultWallChanger(**kwargs)


class DefaultWallChanger:
    def __init__(self, config: WHConfig):
        self.__cache_dir = Path(config.cache_dir)
        if not self.__cache_dir.exists():
            self.__cache_dir.mkdir(parents=True)

    def set_wallpaper(self, path: Path):
        logger.debug(f'Setting wallpaper to {path}')
        subprocess.run(
            ['feh', '--bg-scale', path.absolute()],
            capture_output=True,
            text=True,
            check=True,
        )

class GnomeWallChanger(DefaultWallChanger):

    def set_wallpaper(self, path: Path):
        subprocess.run(
            ['gsettings', 'set', 'org.gnome.desktop.background', 'picture-uri', path.absolute()],
            capture_output=True,
            text=True,
            check=True,
        )


class WHClient:
    def __init__(self, config: WHConfig):
        self.__config = config
        self.__base_uri = 'https://wallhaven.cc/api/v1'

    def __get_json(self, resource: str, params: dict = {}) -> dict:
        logger.debug(f'Requesting resource {resource} with params {params}')
        params.update({'apikey': self.__config.api_key})
        response = requests.get(f'{self.__base_uri}{resource}', params=params)

        if not response.ok:
            raise RuntimeError(response.text)

        return response.json()

    def collections(self):
        return self.__get_json('/collections')['data']

    def wallpapers(self, page: int = 0) -> dict:
        collections = self.collections()
        collection = next(filter(lambda c: c['label'] == self.__config.collection, collections), None)
        return self.__get_json(resource=f'/collections/{self.__config.login}/{collection["id"]}', params={'page': page})

    def download_wallpaper(self, url: str, file_name: str) -> Path:
        path_str = os.path.join(self.__config.cache_dir, file_name)
        path = Path(path_str)
        if not path.exists():
            with open(path_str, 'wb') as f:
                f.write(requests.get(url).content)
        return path

class Wallhalla:
    def __init__(self, config: WHConfig, client: WHClient, changer: DefaultWallChanger):
        self.__config = config
        self.__client = client
        self.__changer = changer
        self.__wallpapers = []
        self.__last_fetched_at = 0
        self.__current_wallpaper_id = '0'
        self.__collection_size = 0
        self.__page_size = 0
        self.__page_index = 0
        self.__page_entry_index = 0
        self.__wallpaper_index = 0

    def set_next(self):
        self.__refetch()
        current_wallpaper = next(filter(lambda w: w['id'] > self.__current_wallpaper_id, self.__wallpapers), None)
        self.__current_wallpaper_id = current_wallpaper['id']
        self.__page_entry_index += 1
        self.__wallpaper_index += 1
        logger.debug(f'Next ID: {self.__current_wallpaper_id}; interation: {self.__wallpaper_index}')
        file_url = current_wallpaper['path']
        file_name = file_url.split('/')[-1]
        wallpaper_path = self.__client.download_wallpaper(file_url, file_name)
        self.__changer.set_wallpaper(wallpaper_path)

    def __fetch(self):
        walls_meta = self.__client.wallpapers(page=self.__page_index)
        self.__wallpapers = sorted(walls_meta['data'], key=lambda x: x['id'])
        self.__page_size = walls_meta['meta']['per_page']
        self.__collection_size = walls_meta['meta']['total']
        self.__last_fetched_at = time.time()

    def __refetch(self):
        if self.__is_eoc_reached(): # end of collection
            self.__page_entry_index = 0
            self.__page_index = 1
            self.__current_wallpaper_id = '0'
            self.__wallpaper_index = 0
            self.__fetch()
        elif self.__is_eop_reached():
            logger.debug('Reached end of page')
            self.__page_index += 1
            self.__page_entry_index = 0
            self.__current_wallpaper_id = '0'
            self.__fetch()
        elif self.__is_fetch_cache_expired():
            logger.debug('Fetch cache expired')
            self.__fetch()

    def __is_eoc_reached(self):
        """ End if the collection reached check """
        return self.__wallpaper_index > self.__collection_size - 1

    def __is_eop_reached(self):
        """ End if the page reached check """
        return self.__page_entry_index >= self.__page_size

    def __is_fetch_cache_expired(self):
        return time.time() - self.__last_fetched_at > self.__config.fetch_freq

    def schedule_collection(self):
        schedule.every(self.__config.freq_sec).seconds.do(self.set_next)

        while True:
            schedule.run_pending()
            time.sleep(1)


if __name__ == '__main__':
    conf = WHConfig()
    env_code = os.getenv('DESKTOP_SESSION') or os.getenv('XDG_SESSION_DESKTOP')
    env = Environment(env_code.upper())
    changer = env.build_changer(config=conf)
    wh = Wallhalla(conf, WHClient(conf), changer)

    wh.schedule_collection()

### TODO:
# 1. [ ] cache limit
# 2. [X] wall change timer
# 3. [X] extract wallhaven client
# 4. [ ] OPTIONAL: package for Archlinux
# 5. [ ] OPTIONAL: tests?
# 6. [ ] OPTIONAL: restructure project
# 7. [ ] init default config
