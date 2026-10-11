import asyncio
import logging
import uuid

from sqlalchemy import select

from app.aws_services import aws
from app.config import settings
from app.database import SessionLocal
from app.indexing import mark_failed, process_document
from app.models import Document, DocumentStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("legal-vag-worker")


async def document_for_job(job_id: str) -> Document | None:
    async with SessionLocal() as db:
        return await db.scalar(select(Document).where(Document.textract_job_id == job_id))


async def poll_once() -> None:
    async with SessionLocal() as db:
        documents = list((await db.scalars(
            select(Document).where(Document.status == DocumentStatus.TEXTRACT_SUBMITTED).limit(10)
        )).all())
    for document in documents:
        try:
            status, message = await asyncio.to_thread(aws.analysis_status, document.textract_job_id)
            if status == "SUCCEEDED":
                await process_document(document.id)
            elif status == "FAILED":
                await mark_failed(document.id, message or "Textract job failed")
        except Exception:
            logger.exception("Cannot inspect Textract job %s", document.textract_job_id)


async def run_poll_worker() -> None:
    logger.warning("Worker uses polling mode for local/demo use. Configure SNS/SQS in production.")
    while True:
        await poll_once()
        await asyncio.sleep(10)


async def run_sqs_worker() -> None:
    queue_url = settings.textract_sqs_queue_url
    while True:
        response = await asyncio.to_thread(
            aws.sqs.receive_message, QueueUrl=queue_url, MaxNumberOfMessages=5,
            WaitTimeSeconds=20, VisibilityTimeout=900,
        )
        for message in response.get("Messages", []):
            receipt = message["ReceiptHandle"]
            try:
                event = aws.notification(message["Body"])
                document = await document_for_job(event.get("JobId", ""))
                if document and event.get("Status") == "SUCCEEDED":
                    await process_document(document.id)
                elif document and event.get("Status") == "FAILED":
                    await mark_failed(document.id, event.get("StatusMessage") or "Textract job failed")
                await asyncio.to_thread(aws.sqs.delete_message, QueueUrl=queue_url, ReceiptHandle=receipt)
            except Exception:
                logger.exception("SQS message processing failed; it will become visible again")


async def main() -> None:
    await run_sqs_worker() if settings.textract_completion_mode == "sqs" else await run_poll_worker()


if __name__ == "__main__":
    asyncio.run(main())
