from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    host: str = '127.0.0.1'
    port: int = 8000
    data_dir: Path = Path('data')
    cors_origins: list[str] = ['http://localhost:3000', 'http://127.0.0.1:3000']
    ai_mode: Literal['direct'] = 'direct'
    openai_api_key: SecretStr | None = None
    openai_extraction_model: str = 'gpt-6-luna'
    openai_explanation_model: str = 'gpt-6-luna'
    openai_chat_model: str = 'gpt-6-luna'
    model_config_version: str = 'luna-v1'
    ai_timeout_seconds: float = Field(default=120, gt=0)
    agent_font_path: Path = Path('assets/fonts/NotoSansKR-Regular.ttf')
    agent_ocr_command: str = ''
    agent_ocr_data_path: Path | None = None
    worker_concurrency: int = Field(default=1, ge=1, le=4)
    workers_enabled: bool = True
    recompute_debounce_seconds: float = Field(default=0.3, ge=0)
    upload_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    cookie_secure: bool = False
    password_reset_delivery: Literal['file', 'disabled'] = 'file'
    frontend_base_url: str = 'http://localhost:3000'

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'app.sqlite3'

    def agent_settings(self):
        from agent.config import AgentSettings
        return AgentSettings(openai_api_key=self.openai_api_key,
            extraction_model=self.openai_extraction_model, explanation_model=self.openai_explanation_model,
            model_config_version=self.model_config_version, font_path=self.agent_font_path,
            ocr_command=self.agent_ocr_command, ocr_data_path=self.agent_ocr_data_path,
            timeout_seconds=self.ai_timeout_seconds, max_file_bytes=self.upload_max_bytes)
