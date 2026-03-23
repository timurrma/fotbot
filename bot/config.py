from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    group_id: int
    rapidapi_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str
    database_url: str = "sqlite+aiosqlite:///./football_bot.db"
    miniapp_url: str = "https://your-miniapp.vercel.app"
    api_port: int = 8080

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
