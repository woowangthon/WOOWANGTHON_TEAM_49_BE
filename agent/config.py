from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

ENGINE_VERSION = "agent-0.4.0"
# Extraction compatibility is deliberately independent of wage/report changes.
EXTRACTION_COMPATIBILITY_VERSION = "extraction-1.2"
SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
CHECKPOINT_VERSION = "1.0"


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    provider_mode: Literal["openai"] = "openai"
    openai_api_key: SecretStr | None = None
    extraction_model: str = "gpt-6-luna"
    explanation_model: str = "gpt-6-luna"
    model_config_version: str = "luna-v1"
    max_file_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_pages: int = Field(default=20, gt=0)
    max_stage_calls: int = Field(default=3, ge=1, le=3)
    max_output_tokens: int = Field(default=8192, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)
    retry_delay_seconds: float = Field(default=0.5, ge=0)
    font_path: Path = Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansKR-Regular.ttf"
    ocr_command: str = ""
    ocr_data_path: Path | None = None
    ocr_dpi: int = Field(default=300, ge=150, le=400)
    ocr_page_timeout: float = Field(default=60, gt=0)

    @classmethod
    def from_env(cls, path: Path, **overrides):
        # Explicit development entry point; never called on import.
        from dotenv import dotenv_values

        env = dotenv_values(path)
        values = {
            "openai_api_key": env.get("OPENAI_API_KEY") or None,
            "extraction_model": env.get("OPENAI_EXTRACTION_MODEL") or "gpt-6-luna",
            "explanation_model": env.get("OPENAI_EXPLANATION_MODEL") or "gpt-6-luna",
            "model_config_version": env.get("MODEL_CONFIG_VERSION") or "luna-v1",
        }
        if env.get("AGENT_FONT_PATH"):
            values["font_path"] = env["AGENT_FONT_PATH"]
        if env.get("AGENT_OCR_COMMAND"):
            values["ocr_command"] = env["AGENT_OCR_COMMAND"]
        if env.get("AGENT_OCR_DATA_PATH"):
            values["ocr_data_path"] = env["AGENT_OCR_DATA_PATH"]
        return cls(**(values | overrides))
