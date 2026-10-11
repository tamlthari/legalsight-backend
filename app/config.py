from functools import cached_property, lru_cache
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_secret_key: str = Field(min_length=16)
    cors_origins: str = "http://localhost:3000"
    access_token_minutes: int = 480
    max_upload_mb: int = 200

    postgres_user: str
    postgres_password: str
    postgres_database: str
    postgres_host: str
    postgres_port: int = 5432
    postgres_sslmode: str = "disable"

    aws_region: str
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    s3_bucket: str
    s3_prefix: str = "legal-vag"
    s3_kms_key_id: str | None = None
    textract_completion_mode: str = "poll"
    textract_sns_topic_arn: str | None = None
    textract_sns_role_arn: str | None = None
    textract_sqs_queue_url: str | None = None

    openai_api_key: str
    openai_embedding_model: str
    openai_vision_model: str
    embedding_dimension: int

    pinecone_api_key: str
    pinecone_index: str
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    @field_validator(
        "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
        "s3_kms_key_id", "textract_sns_topic_arn", "textract_sns_role_arn",
        "textract_sqs_queue_url", mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_textract_mode(self) -> "Settings":
        if self.textract_completion_mode not in {"poll", "sqs"}:
            raise ValueError("TEXTRACT_COMPLETION_MODE must be poll or sqs")
        if self.textract_completion_mode == "sqs":
            required = [self.textract_sns_topic_arn, self.textract_sns_role_arn, self.textract_sqs_queue_url]
            if not all(required):
                raise ValueError("SNS topic, SNS role and SQS queue are required in sqs mode")
        return self

    @cached_property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @cached_property
    def async_database_url(self) -> str:
        user, password = quote_plus(self.postgres_user), quote_plus(self.postgres_password)
        ssl = "?ssl=require" if self.postgres_sslmode == "require" else ""
        return f"postgresql+asyncpg://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}{ssl}"

    @cached_property
    def sync_database_url(self) -> str:
        user, password = quote_plus(self.postgres_user), quote_plus(self.postgres_password)
        ssl = "?sslmode=require" if self.postgres_sslmode == "require" else ""
        return f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}{ssl}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
