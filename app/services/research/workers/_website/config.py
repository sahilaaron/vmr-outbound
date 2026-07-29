"""Configuration loading with documented defaults.

Settings live in a YAML file (config.yaml / config.example.yaml). Environment
variables are used only for the optional interpreter API key, never for
ordinary settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class CrawlerConfig:
    max_pages: int = 25
    max_depth: int = 3
    request_timeout_seconds: float = 20.0
    delay_between_requests_seconds: float = 1.0
    max_retries: int = 2
    max_redirects: int = 5
    max_response_size_mb: float = 8.0
    use_playwright_fallback: bool = True
    playwright_min_text_chars: int = 400  # fall back when HTTP text is shorter
    min_useful_text_chars: int = 80       # below this a page is flagged thin


@dataclass
class DiscoveryConfig:
    use_sitemap: bool = True
    use_robots_txt: bool = True
    include_subdomains: bool = True
    max_sitemap_urls: int = 500
    max_candidates: int = 200


@dataclass
class OutputConfig:
    root_directory: str = "output"
    save_clean_text: bool = True
    save_raw_html: bool = False


@dataclass
class InterpreterConfig:
    # Extension point for a future LLM interpretation stage.
    # "none" keeps the pipeline fully deterministic and offline.
    provider: str = "none"
    model: str = ""
    # API keys are the one thing that belongs in an environment variable:
    api_key_env_var: str = "COMPANY_RESEARCH_INTERPRETER_API_KEY"


@dataclass
class AppConfig:
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    interpreter: InterpreterConfig = field(default_factory=InterpreterConfig)
    database_path: str = "company_research.db"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _apply(dc: Any, values: dict[str, Any], section: str) -> None:
    for key, val in (values or {}).items():
        if hasattr(dc, key):
            setattr(dc, key, val)
        else:
            raise ValueError(f"Unknown config key '{section}.{key}'")


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load YAML config; missing file or keys fall back to defaults."""
    cfg = AppConfig()
    candidates = [path] if path else ["config.yaml", "config.example.yaml"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            with open(candidate, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            _apply(cfg.crawler, raw.get("crawler", {}), "crawler")
            _apply(cfg.discovery, raw.get("discovery", {}), "discovery")
            _apply(cfg.output, raw.get("output", {}), "output")
            _apply(cfg.interpreter, raw.get("interpreter", {}), "interpreter")
            if "database_path" in raw:
                cfg.database_path = raw["database_path"]
            break
        if path:  # explicit path that doesn't exist is an error
            raise FileNotFoundError(f"Config file not found: {path}")
    return cfg


def interpreter_api_key(cfg: AppConfig) -> Optional[str]:
    return os.environ.get(cfg.interpreter.api_key_env_var) or None
