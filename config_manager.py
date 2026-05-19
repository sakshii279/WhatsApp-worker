"""
config_manager.py
=================
Singleton config loader with dot-notation + index access.

Usage:
    ConfigManager.init("config.yaml")
    ConfigManager.load()
    val = ConfigManager.getProperty("rabbitmq.host", "localhost")
    ConfigManager.setProperty("rabbitmq.port", 5672)
    ConfigManager.save()
    ConfigManager.clear()
"""

import yaml
from pathlib import Path
from functools import reduce


class ConfigManager:
    _path: str = None
    _data: dict = {}

    @classmethod
    def init(cls, path: str) -> None:
        cls._path = path
        cls._data = {}

    @classmethod
    def load(cls) -> None:
        if not cls._path:
            raise RuntimeError("ConfigManager not initialized. Call init() first.")
        with open(cls._path) as f:
            cls._data = yaml.safe_load(f) or {}

    @classmethod
    def _resolve(cls, keys: list, node: any) -> any:
        for k in keys:
            if isinstance(node, list):
                node = node[int(k)]
            elif isinstance(node, dict):
                node = node.get(k)
            else:
                return None
        return node

    @classmethod
    def getProperty(cls, key: str, default=None) -> any:
        try:
            return cls._resolve(key.split("."), cls._data) or default
        except Exception:
            return default

    @classmethod
    def _set_nested(cls, keys: list, value: any, node: dict) -> None:
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    @classmethod
    def setProperty(cls, key: str, value: any) -> None:
        cls._set_nested(key.split("."), value, cls._data)

    @classmethod
    def save(cls) -> None:
        if not cls._path:
            raise RuntimeError("ConfigManager not initialized.")
        with open(cls._path, "w") as f:
            yaml.dump(cls._data, f, default_flow_style=False)

    @classmethod
    def clear(cls) -> None:
        cls._data = {}
        cls._path = None