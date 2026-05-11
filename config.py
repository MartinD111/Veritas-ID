"""
Centralna konfiguracija Veritas storitve – bere vrednosti iz okolja ali .env datoteke.
Poti do modelov se vedno razrešijo relativno na imenik tega modula,
ne glede na delovni imenik ob zagonu.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolutna pot do imenika, kjer leži ta datoteka (koren projekta)
_BASE_DIR = Path(__file__).parent.resolve()

# .env se išče v korenu projekta
_ENV_FILE = _BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Varnost ---
    admin_token: str = "change-me-in-production"

    # --- Modeli (privzete vrednosti ustrezajo dejanskim imenom datotek v korenu) ---
    llm_model_path: str = "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf"
    mmproj_model_path: str = "mmproj-F16.gguf"
    llm_context_size: int = 4096
    llm_gpu_layers: int = 0  # Nastavi višje, če imaš GPU z dovolj VRAM

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Celery naloge ---
    task_time_limit_seconds: int = 300

    def llm_path_absolute(self) -> Path:
        """Vrne absolutno pot do LLM modela, relativno na koren projekta."""
        p = Path(self.llm_model_path)
        return p if p.is_absolute() else _BASE_DIR / p

    def mmproj_path_absolute(self) -> Path:
        """Vrne absolutno pot do vision projektorja, relativno na koren projekta."""
        p = Path(self.mmproj_model_path)
        return p if p.is_absolute() else _BASE_DIR / p


settings = Settings()
