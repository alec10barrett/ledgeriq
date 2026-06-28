"""
app/services/extraction_service.py
 
Orchestrates the full document extraction pipeline:
  1. Upload raw file to S3
  2. Call Textract AnalyzeExpense
  3. Parse the Textract response into structured fields
  4. Persist an ExtractedData record (and update Document.storage_url)
 
Usage (e.g. from a FastAPI background task):
    service = ExtractionService(db, settings)
    extracted = await service.process(document_id=42, file_bytes=raw_bytes, filename="invoice.pdf")
"""
 
from __future__ import annotations
 
import asyncio
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
 
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session
 
from app.database.models.document import Document
from app.database.models.extracted_data import ExtractedData, ExtractionStatus
from app.services.category_mapper import CategoryMapper
from app.core.config import Settings
 
logger = logging.getLogger(__name__)
 
 
class ExtractionError(Exception):
    """Raised when extraction fails unrecoverably."""
 
 
class ExtractionService:
    """
    Handles S3 upload + Textract AnalyzeExpense for a single document.
 
    Keeps AWS clients as instance attributes so they can be injected/mocked
    in tests without patching globals.
    """
 
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.category_mapper = CategoryMapper()
 
        self._s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        self._textract = boto3.client(
            "textract",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
 
    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
 
    async def process(
        self,
        document_id: int,
        file_bytes: bytes,
        filename: str,
    ) -> ExtractedData:
        """
        Full pipeline for one document. Creates/updates an ExtractedData row
        and returns it. Always writes a row — status will be FAILED if
        anything goes wrong, so callers don't need to handle exceptions for
        normal failure modes.
        """
        document = self._get_document(document_id)
        extracted = self._get_or_create_extracted(document)
 
        try:
            extracted.status = ExtractionStatus.PROCESSING
            self.db.flush()
 
            # 1. Upload to S3
            s3_key = self._build_s3_key(document_id, filename)
            storage_url = await self._upload_to_s3(file_bytes, s3_key)
            document.storage_url = storage_url
 
            # 2. Call Textract
            raw_response = await self._analyze_expense(s3_key)
 
            # 3. Parse response
            parsed = self._parse_textract_response(raw_response)
 
            # 4. Persist
            extracted.status = ExtractionStatus.COMPLETED
            extracted.vendor = parsed["vendor"]
            extracted.total_amount = parsed["total_amount"]
            extracted.invoice_date = parsed["invoice_date"]
            extracted.invoice_number = parsed["invoice_number"]
            extracted.line_items = parsed["line_items"]
            extracted.confidence_score = parsed["confidence_score"]
            extracted.suggested_category = self.category_mapper.suggest(
                vendor=parsed["vendor"],
                line_items=parsed["line_items"],
            )
            extracted.raw_extraction = raw_response
            extracted.error_message = None
 
        except ExtractionError as exc:
            logger.warning("Extraction failed for document %s: %s", document_id, exc)
            extracted.status = ExtractionStatus.FAILED
            extracted.error_message = str(exc)
 
        except Exception as exc:
            logger.exception("Unexpected error extracting document %s", document_id)
            extracted.status = ExtractionStatus.FAILED
            extracted.error_message = f"Unexpected error: {exc}"
 
        finally:
            self.db.commit()
            self.db.refresh(extracted)
 
        return extracted
 
    # ------------------------------------------------------------------
    # S3
    # ------------------------------------------------------------------
 
    async def _upload_to_s3(self, file_bytes: bytes, s3_key: str) -> str:
        """Upload bytes to S3 and return the s3:// URI."""
        try:
            await asyncio.to_thread(
                self._s3.upload_fileobj,
                BytesIO(file_bytes),
                self.settings.s3_bucket,
                s3_key,
            )
        except (BotoCoreError, ClientError) as exc:
            raise ExtractionError(f"S3 upload failed: {exc}") from exc
 
        return f"s3://{self.settings.s3_bucket}/{s3_key}"
 
    def _build_s3_key(self, document_id: int, filename: str) -> str:
        return f"documents/{document_id}/{filename}"
 
    # ------------------------------------------------------------------
    # Textract
    # ------------------------------------------------------------------
 
    async def _analyze_expense(self, s3_key: str) -> dict[str, Any]:
        """
        Call Textract AnalyzeExpense synchronously (it's fast enough for
        single-page docs). For multi-page PDFs you'd use StartExpenseAnalysis
        + polling — add that path here when needed.
        """
        try:
            response = await asyncio.to_thread(
                self._textract.analyze_expense,
                Document={
                    "S3Object": {
                        "Bucket": self.settings.s3_bucket,
                        "Name": s3_key,
                    }
                },
            )
        except (BotoCoreError, ClientError) as exc:
            raise ExtractionError(f"Textract call failed: {exc}") from exc
 
        return response
 
    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
 
    def _parse_textract_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """
        Flatten a Textract AnalyzeExpense response into the fields we care
        about. Textract returns a list of ExpenseDocuments; we take the first
        (receipts/invoices are single-document).
 
        Textract field reference:
          https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html
        """
        expense_docs = response.get("ExpenseDocuments", [])
        if not expense_docs:
            raise ExtractionError("Textract returned no ExpenseDocuments.")
 
        doc = expense_docs[0]
        summary_fields = {
            f["Type"]["Text"]: f
            for f in doc.get("SummaryFields", [])
            if f.get("Type", {}).get("Text")
        }
 
        vendor = self._get_field_value(summary_fields, "VENDOR_NAME")
        total_amount = self._parse_amount(
            self._get_field_value(summary_fields, "TOTAL")
            or self._get_field_value(summary_fields, "AMOUNT_PAID")
        )
        invoice_date = self._parse_date(
            self._get_field_value(summary_fields, "INVOICE_RECEIPT_DATE")
        )
        invoice_number = self._get_field_value(summary_fields, "INVOICE_RECEIPT_ID")
 
        line_items = self._parse_line_items(doc.get("LineItemGroups", []))
        confidence_score = self._compute_confidence(summary_fields)
 
        return {
            "vendor": vendor,
            "total_amount": total_amount,
            "invoice_date": invoice_date,
            "invoice_number": invoice_number,
            "line_items": line_items,
            "confidence_score": confidence_score,
        }
 
    @staticmethod
    def _get_field_value(
        fields: dict[str, Any],
        key: str,
    ) -> str | None:
        field = fields.get(key)
        if not field:
            return None
        return field.get("ValueDetection", {}).get("Text") or None
 
    @staticmethod
    def _parse_amount(raw: str | None) -> float | None:
        if not raw:
            return None
        # Strip currency symbols and commas, then cast.
        cleaned = raw.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
 
    @staticmethod
    def _parse_date(raw: str | None) -> datetime | None:
        if not raw:
            return None
        # Textract returns dates in several formats; try the common ones.
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        logger.warning("Could not parse date string: %r", raw)
        return None
 
    @staticmethod
    def _parse_line_items(groups: list[dict]) -> list[dict] | None:
        items = []
        for group in groups:
            for row in group.get("LineItems", []):
                item: dict[str, Any] = {}
                for field in row.get("LineItemExpenseFields", []):
                    field_type = field.get("Type", {}).get("Text")
                    value = field.get("ValueDetection", {}).get("Text")
                    if field_type and value:
                        item[field_type.lower()] = value
                if item:
                    items.append(item)
        return items or None
 
    @staticmethod
    def _compute_confidence(summary_fields: dict[str, Any]) -> float | None:
        """
        Average the confidence scores Textract returns for each field.
        Returns None if no scores are present.
        """
        scores = []
        for field in summary_fields.values():
            score = field.get("ValueDetection", {}).get("Confidence")
            if score is not None:
                scores.append(score / 100.0)  # Textract gives 0–100
        return round(sum(scores) / len(scores), 4) if scores else None
 
    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
 
    def _get_document(self, document_id: int) -> Document:
        doc = self.db.get(Document, document_id)
        if not doc:
            raise ExtractionError(f"Document {document_id} not found.")
        return doc
 
    def _get_or_create_extracted(self, document: Document) -> ExtractedData:
        """
        Return existing ExtractedData for this document if present,
        otherwise create a fresh PENDING record.
        """
        existing = (
            self.db.query(ExtractedData)
            .filter(ExtractedData.document_id == document.id)
            .first()
        )
        if existing:
            return existing
 
        extracted = ExtractedData(
            document_id=document.id,
            status=ExtractionStatus.PENDING,
        )
        self.db.add(extracted)
        self.db.flush()
        return extracted
 
