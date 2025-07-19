import os.path

import attr
import configparser
import argparse
from pathlib import Path

import requests


class WHConfig:
    api_key: str = attr.ib()
    login: str = attr.ib()
    collection: str = attr.ib()
    cache_dir: str = attr.ib()

    def __init__(self):
        file_conf = configparser.ConfigParser()
        file_conf.read(f"{Path.home()}/.config/wallhalla/config")

        arg_conf = argparse.ArgumentParser()
        arg_conf.add_argument('--api-key', help='Override API key') # TODO: request from stdin for security
        arg_conf.add_argument('--login', help='Override login')
        arg_conf.add_argument('--cache-dir', help='Override cache directory')
        arg_conf.add_argument('--collection', help='Override collection')
        args = arg_conf.parse_args()

        self.api_key = args.api_key or file_conf["DEFAULT"]["api.key"]
        self.login = args.login or file_conf["DEFAULT"]["login"]
        self.collection = args.collection or file_conf["DEFAULT"]["collection"]
        self.cache_dir = args.cache_dir or file_conf["CACHE"]["cache.dir"]


class Wallpaper:
    def __init__(self, config: WHConfig):
        self.__cache_dir = Path(config.cache_dir)
        if not self.__cache_dir.exists():
            self.__cache_dir.mkdir(parents=True)

    def set_wallpaper(self, url: str, file_name: str):
        path_str = os.path.join(self.__cache_dir, file_name)
        with open(path_str, 'wb') as f:
            f.write(requests.get(url).content)

class Wallhalla:
    def __init__(self, config: WHConfig, wallpaper: Wallpaper):
        self.__config = config
        self.__wallpaper = wallpaper
        self.__base_uri = 'https://wallhaven.cc/api/v1'

    def collections(self):
        response = requests.get(f'{self.__base_uri}/collections', params={'apikey': self.__config.api_key})

        if not response.ok:
            raise RuntimeError(response.text)

        return response.json()['data']

    def set_random(self):
        collections = self.collections()
        collection = next(filter(lambda c: c['label'] == self.__config.collection, collections), None)
        response = requests.get(f'{self.__base_uri}/collections/{self.__config.login}/{collection["id"]}', params={'apikey': self.__config.api_key})

        if not response.ok:
            raise RuntimeError(response.text)

        first_wallpaper = response.json()['data'][0]

        file_url = first_wallpaper['path']
        file_name = file_url.split('/')[-1]
        self.__wallpaper.set_wallpaper(file_url, file_name)
        print(first_wallpaper)

if __name__ == '__main__':
    conf = WHConfig()
    wh = Wallhalla(conf, Wallpaper(conf))
    print(wh.collections())

    wh.set_random()
