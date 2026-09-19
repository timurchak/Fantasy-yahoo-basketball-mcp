from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    client_id: SecretStr = Field(default=SecretStr(""), validation_alias="YAHOO_CLIENT_ID")
    client_secret: SecretStr = Field(default=SecretStr(""), validation_alias="YAHOO_CLIENT_SECRET")
    redirect_uri: str = Field(
        default="https://localhost:8765/callback", validation_alias="YAHOO_REDIRECT_URI"
    )
    league_id: str = Field(default="30166", validation_alias="FANTASY_LEAGUE_ID")
    season: int = Field(default=2026, validation_alias="FANTASY_SEASON", ge=2000, le=2100)
    league_key: str | None = Field(default=None, validation_alias="FANTASY_LEAGUE_KEY")
    team_key: str | None = Field(default=None, validation_alias="FANTASY_TEAM_KEY")
    data_dir: Path = Field(
        default=Path.home() / ".fantasy-mcp", validation_alias="FANTASY_DATA_DIR"
    )
    player_data_file: Path | None = Field(default=None, validation_alias="FANTASY_PLAYER_DATA_FILE")
    min_games: int = Field(default=10, ge=0, validation_alias="FANTASY_MIN_GAMES")
    min_minutes: float = Field(default=10, ge=0, validation_alias="FANTASY_MIN_MINUTES")
    population_size: int = Field(
        default=200, ge=2, le=1000, validation_alias="FANTASY_POPULATION_SIZE"
    )
    context_weight: float = Field(
        default=0.25, ge=0, le=1, validation_alias="FANTASY_CONTEXT_WEIGHT"
    )
    settings_ttl: int = 86400
    metadata_ttl: int = 86400
    stats_ttl: int = 3600
    ownership_ttl: int = 120
    historical_ttl: int = 2592000
    stale_max_age: int = 604800
    debug: bool = False
    replacement_weight: float = Field(default=0.1, ge=0, le=1)
    scarcity_weight: float = Field(default=0.1, ge=0, le=1)
    bdl_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="BALLDONTLIE_API_KEY")
