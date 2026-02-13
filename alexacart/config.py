from pathlib import Path

from pydantic_settings import BaseSettings

_BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    alexa_list_name: str = "Grocery List"
    browser_use_api_key: str = ""
    instacart_store: str = "Wegmans"
    search_concurrency: int = 4
    data_dir: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def base_dir(self) -> Path:
        return _BASE_DIR

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir)
        return _BASE_DIR / "data"

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / "alexacart.db"

    @property
    def cookies_path(self) -> Path:
        return self.resolved_data_dir / "cookies.json"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
