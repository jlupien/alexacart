from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

_BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Comma-separated list of Alexa list names to pull items from.
    # Stored as str so pydantic-settings reads it as a plain string (not JSON).
    # Use the alexa_list_names property for the parsed list[str] form.
    alexa_list_names_csv: str = Field("Grocery List", validation_alias="alexa_list_names")

    @property
    def alexa_list_names(self) -> list[str]:
        return [x.strip() for x in self.alexa_list_names_csv.split(",") if x.strip()]
    instacart_store: str = "Wegmans"
    skip_alexa_checkoff: bool = False
    debug_clear_amazon_cookies: bool = False
    debug_clear_instacart_cookies: bool = False
    data_dir: str = ""
    local_data_dir: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def base_dir(self) -> Path:
        return _BASE_DIR

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir)
        return _BASE_DIR / "data"

    @property
    def resolved_local_data_dir(self) -> Path:
        if self.local_data_dir:
            return Path(self.local_data_dir)
        return _BASE_DIR / "data"

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / "alexacart.db"

    @property
    def cookies_path(self) -> Path:
        return self.resolved_local_data_dir / "cookies.json"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
