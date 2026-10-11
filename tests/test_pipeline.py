import os

os.environ.update({
    "APP_SECRET_KEY": "test-secret-key-at-least-16",
    "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DATABASE": "test", "POSTGRES_HOST": "localhost",
    "AWS_REGION": "ap-southeast-1", "S3_BUCKET": "test-bucket",
    "OPENAI_API_KEY": "test", "OPENAI_EMBEDDING_MODEL": "text-embedding-3-small", "OPENAI_VISION_MODEL": "gpt-4.1-mini",
    "EMBEDDING_DIMENSION": "1536", "PINECONE_API_KEY": "test", "PINECONE_INDEX": "test-index",
})

from app.aws_services import AWS
from app.indexing import chunk_text, extract_pages


def test_extract_pages_orders_lines_by_geometry() -> None:
    result = {"Blocks": [
        {"BlockType": "LINE", "Text": "dòng hai", "Page": 1, "Geometry": {"BoundingBox": {"Top": .2, "Left": .1}}},
        {"BlockType": "WORD", "Text": "ignore", "Page": 1},
        {"BlockType": "LINE", "Text": "dòng một", "Page": 1, "Geometry": {"BoundingBox": {"Top": .1, "Left": .1}}},
    ]}
    assert extract_pages(result) == {1: "dòng một\ndòng hai"}


def test_chunk_text_preserves_overlap() -> None:
    chunks = chunk_text("A" * 2500, size=1000, overlap=100)
    assert len(chunks) == 3
    assert chunks[0][-100:] == chunks[1][:100]


def test_sns_envelope_is_unwrapped() -> None:
    body = '{"Type":"Notification","Message":"{\\"JobId\\":\\"abc\\",\\"Status\\":\\"SUCCEEDED\\"}"}'
    assert AWS.notification(body) == {"JobId": "abc", "Status": "SUCCEEDED"}


def test_direct_sqs_payload_is_supported() -> None:
    assert AWS.notification('{"JobId":"abc","Status":"FAILED"}')["Status"] == "FAILED"

