from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Кадастровая карта Республики Беларусь"
    app_version: str = "1.0.0"
    database_url: str = "sqlite+aiosqlite:///./cadastral_rb.db"
    enable_live_osm_enrichment: bool = True
    enable_live_reverse_geocoding: bool = True
    enable_live_egr_verification: bool = False
    enable_egr_enrichment: bool = True
    overpass_api_url: str = "https://overpass-api.de/api/interpreter"
    geofabrik_belarus_pbf_url: str = "https://download.geofabrik.de/europe/belarus-latest.osm.pbf"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


settings = Settings()
