from dataclasses import dataclass
from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    config_dir: Path
    prompts_dir: Path
    sources_dir: Path
    sources_config_path: Path
    summary_draft_prompt_path: Path
    instance_dir: Path
    data_dir: Path
    raw_dir: Path
    cleaned_dir: Path
    derived_dir: Path
    log_dir: Path
    database_path: Path
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    secret_key: str


def load_settings() -> Settings:
    data_dir = Path(os.getenv("AUTO_SCRAPY_DATA_DIR", DATA_DIR))
    config_dir = Path(os.getenv("AUTO_SCRAPY_CONFIG_DIR", ROOT_DIR / "config"))
    prompts_dir = Path(os.getenv("AUTO_SCRAPY_PROMPTS_DIR", config_dir / "prompts"))
    sources_dir = Path(os.getenv("AUTO_SCRAPY_SOURCES_DIR", config_dir / "sources"))
    sources_config_path = Path(
        os.getenv(
            "AUTO_SCRAPY_SOURCES_CONFIG_PATH",
            sources_dir / "demo_sources.toml",
        )
    )
    summary_draft_prompt_path = Path(
        os.getenv(
            "AUTO_SCRAPY_SUMMARY_DRAFT_PROMPT_PATH",
            prompts_dir / "summary_draft_v1.txt",
        )
    )
    instance_dir = Path(os.getenv("AUTO_SCRAPY_INSTANCE_DIR", ROOT_DIR / "instance"))
    database_path = Path(
        os.getenv("AUTO_SCRAPY_DATABASE_PATH", instance_dir / "auto_scrapy.sqlite3")
    )

    return Settings(
        root_dir=ROOT_DIR,
        config_dir=config_dir,
        prompts_dir=prompts_dir,
        sources_dir=sources_dir,
        sources_config_path=sources_config_path,
        summary_draft_prompt_path=summary_draft_prompt_path,
        instance_dir=instance_dir,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        cleaned_dir=data_dir / "cleaned",
        derived_dir=data_dir / "derived",
        log_dir=data_dir / "logs",
        database_path=database_path,
        ollama_base_url=os.getenv("AUTO_SCRAPY_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        ollama_model=os.getenv("AUTO_SCRAPY_OLLAMA_MODEL", "llama3.2:3b"),
        ollama_timeout_seconds=int(os.getenv("AUTO_SCRAPY_OLLAMA_TIMEOUT_SECONDS", "120")),
        secret_key=os.getenv("AUTO_SCRAPY_SECRET_KEY", "dev"),
    )
