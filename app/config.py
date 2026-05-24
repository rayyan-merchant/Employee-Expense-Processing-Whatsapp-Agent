from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    TWILIO_ACCOUNT_SID: str = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    TWILIO_AUTH_TOKEN: str = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    TWILIO_WHATSAPP_NUMBER: str = "whatsapp:+14155238886"

    GOOGLE_API_KEY: str = "AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    CELERY_BROKER_URL: str = "redis://127.0.0.1:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://127.0.0.1:6379/2"
    CELERY_TASK_ALWAYS_EAGER: bool = True

    DATABASE_URL: str = "sqlite+aiosqlite:///./expense_agent.db"

    PRIORITY_BASE_URL: str = "https://your-company.priority.co.il/odata/Priority/tabula.ini"
    PRIORITY_USERNAME: str = "api_user"
    PRIORITY_PASSWORD: str = "api_password"
    PRIORITY_USE_MOCK: bool = True

    APP_SECRET_KEY: str = "change-this-to-a-random-secret"
    ENVIRONMENT: str = "development"
    BASE_URL: str = "https://xxxx.ngrok.io"

    DEFAULT_MANAGER_PHONE: str = "whatsapp:+972501234567"
    EXPENSE_SUBMISSION_WINDOW_DAYS: int = 90
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def use_priority_mock(self) -> bool:
        return self.PRIORITY_USE_MOCK


settings = Settings()
