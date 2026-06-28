from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SQLEnum, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database.base import Base


class ExtractionStatus(str, Enum):
    PENDING = "pending"         # document received, extraction not yet run
    PROCESSING = "processing"   # extraction in progress
    COMPLETED = "completed"     # extraction succeeded, awaiting human review
    FAILED = "failed"           # extraction failed (bad scan, unsupported format, etc.)
    REVIEWED = "reviewed"       # human has confirmed or corrected the output


class ExtractedData(Base):
    """
    Staging layer between raw Document OCR text and a verified Expense record.

    When a document (receipt, invoice, etc.) is uploaded, an AI extraction
    pipeline populates this model with its best guess at the structured data.
    A human reviewer can then confirm or correct the fields before the record
    graduates to an Expense.

    Keeping this separate from Expense means:
      - You preserve the raw AI output even after corrections.
      - You can track extraction confidence and failure modes over time.
      - Manual expenses (entered without a document) don't need to carry
        extraction-related fields.
    """

    __tablename__ = "extracted_data"

    # Primary Key
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # Foreign Keys
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Pipeline state
    status: Mapped[ExtractionStatus] = mapped_column(
        SQLEnum(ExtractionStatus),
        default=ExtractionStatus.PENDING,
        nullable=False,
    )

    # --- AI-extracted fields ---
    # These mirror the core Expense fields. They're nullable because extraction
    # may only partially succeed (e.g. vendor found but date missing).

    vendor: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    total_amount: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    invoice_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    invoice_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # AI's suggested category (from ExpenseCategory enum values).
    # Stored as a plain string so it remains readable even if the enum changes.
    suggested_category: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    # Line items as a JSON array, e.g.:
    # [{"description": "Labor", "quantity": 2, "unit_price": 75.00, "amount": 150.00}, ...]
    # Rich enough for invoices with multiple line items; ignored for simple receipts.
    line_items: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # 0.0–1.0 confidence score from the extraction model.
    # Useful for auto-approving high-confidence extractions in the future.
    confidence_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # Raw JSON dump of whatever the AI model returned — preserved for debugging
    # and reprocessing without needing to re-run OCR.
    raw_extraction: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # If extraction failed, the error message.
    error_message: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    document: Mapped["Document"] = relationship(
        back_populates="extracted_data",
    )

    # One-to-one: once reviewed, this extraction becomes one Expense.
    expense: Mapped["Expense | None"] = relationship(
        back_populates="extracted_data",
        foreign_keys="Expense.extracted_data_id",
    )

    def __repr__(self) -> str:
        return (
            f"<ExtractedData(id={self.id}, "
            f"status={self.status}, "
            f"vendor='{self.vendor}', "
            f"amount={self.total_amount})>"
        )
