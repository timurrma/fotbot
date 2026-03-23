from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    group_id: int
    rapidapi_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    llm_provider: str = "openrouter"  # "openai" or "openrouter"
    database_url: str = "sqlite+aiosqlite:///./football_bot.db"
    miniapp_url: str = "https://your-miniapp.vercel.app"
    api_port: int = 8080

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def async_database_url(self) -> str:
        """Конвертирует Railway postgres:// → postgresql+asyncpg://"""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
