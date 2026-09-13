"""配置加载：yaml 默认值 + 环境变量覆盖（系统唯一的全局配置入口，仅被工厂函数使用）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "config" / "settings.yaml"


@dataclass
class DBSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    name: str = "ods"


@dataclass
class BacktestSettings:
    init_cash: float = 1_000_000
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    slippage: float = 0.002
    price_limit: float = 0.095


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class Settings:
    db: DBSettings = field(default_factory=DBSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    data_source: str = "baostock"


def load_settings() -> Settings:
    raw: dict = {}
    if SETTINGS_FILE.exists():
        raw = yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
    db = raw.get("database", {})
    bt = raw.get("backtest", {})
    sv = raw.get("server", {})
    settings = Settings(
        db=DBSettings(
            host=os.getenv("QUANT_DB_HOST", db.get("host", "127.0.0.1")),
            port=int(os.getenv("QUANT_DB_PORT", db.get("port", 3306))),
            user=os.getenv("QUANT_DB_USER", db.get("user", "root")),
            password=os.getenv("QUANT_DB_PASSWORD", db.get("password", "")),
            name=os.getenv("QUANT_DB_NAME", db.get("name", "ods")),
        ),
        backtest=BacktestSettings(**{**BacktestSettings().__dict__, **bt}),
        server=ServerSettings(**{**ServerSettings().__dict__, **sv}),
        data_source=raw.get("data_source", "baostock"),
    )
    return settings
