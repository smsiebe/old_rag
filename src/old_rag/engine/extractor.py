"""extractor.py - Deterministic Document Extractors with Conditional VLM Gating."""

from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from old_rag.contracts import (
    ExtractedAttachmentMeta,
    NormalizedDocumentJSON,
    RawIngestPayload,
    SourceType,
)
from old_rag.protocols import CatalogProtocol, ExtractorProtocol, VLMClientProtocol


def strip_html_tags(html: str) -> str:
    """Basic deterministic HTML tag stripper."""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


class DocumentExtractor(ExtractorProtocol):
    """Deterministic document parser for PDF, DOCX, XLSX, PPTX, EML with VLM fallback."""

    def __init__(
        self,
        catalog: Optional[CatalogProtocol] = None,
        vlm_client: Optional[VLMClientProtocol] = None,
    ):
        self.catalog = catalog
        self.vlm_client = vlm_client

    def can_extract(self, filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in {".pdf", ".docx", ".xlsx", ".pptx", ".eml", ".txt", ".md", ".png", ".jpg", ".jpeg"}

    def extract_docx(self, data: bytes) -> str:
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_text:
                        parts.append(row_text)
            return "\n\n".join(parts)
        except Exception:
            return ""

    def extract_xlsx(self, data: bytes) -> str:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            parts = []
            for sheetname in wb.sheetnames:
                sheet = wb[sheetname]
                parts.append(f"# Sheet: {sheetname}")
                for row in sheet.iter_rows(values_only=True):
                    row_vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                    if row_vals:
                        parts.append(" | ".join(row_vals))
            return "\n\n".join(parts)
        except Exception:
            return ""

    def extract_pdf(self, data: bytes) -> str:
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(data)
            pages = []
            for i in range(len(pdf)):
                page = pdf[i]
                textpage = page.get_textpage()
                page_text = textpage.get_text_range()
                if len(page_text.strip()) < 30 and self.vlm_client:
                    # Gated VLM inference for scanned / visual page
                    try:
                        pil_image = page.render(scale=2.0).to_pil()
                        img_buf = io.BytesIO()
                        pil_image.save(img_buf, format="PNG")
                        vlm_markdown = self.vlm_client.extract_image_markdown(
                            img_buf.getvalue(), f"page_{i+1}.png"
                        )
                        pages.append(vlm_markdown)
                        continue
                    except Exception:
                        pass
                pages.append(page_text)
            return "\n\n".join(pages)
        except Exception:
            return ""

    def extract(self, payload: RawIngestPayload) -> NormalizedDocumentJSON:
        body_text = payload.body_text or ""
        if not body_text and payload.body_html:
            body_text = strip_html_tags(payload.body_html)

        extracted_attachments: List[ExtractedAttachmentMeta] = []

        for att in payload.attachments:
            blob_hash = hashlib.sha256(att.data).hexdigest()
            ext = Path(att.filename).suffix.lower()
            att_text = ""
            vlm_markdown = None

            # Check VLM cache if catalog is available
            if self.catalog:
                vlm_markdown = self.catalog.get_vlm_cache(blob_hash)

            if ext == ".docx":
                att_text = self.extract_docx(att.data)
            elif ext == ".xlsx":
                att_text = self.extract_xlsx(att.data)
            elif ext == ".pdf":
                att_text = self.extract_pdf(att.data)
            elif ext in {".png", ".jpg", ".jpeg"} and not vlm_markdown and self.vlm_client:
                # Check VLM skip list
                should_skip = self.catalog.is_vlm_skipped(blob_hash) if self.catalog else False
                if not should_skip:
                    vlm_markdown = self.vlm_client.extract_image_markdown(att.data, att.filename)
                    if self.catalog and vlm_markdown:
                        self.catalog.put_vlm_cache(blob_hash, vlm_markdown)

            if att_text:
                body_text += f"\n\n--- Attachment: {att.filename} ---\n{att_text}"

            extracted_attachments.append(
                ExtractedAttachmentMeta(
                    filename=att.filename,
                    content_type=att.content_type,
                    blob_hash=blob_hash,
                    vlm_markdown=vlm_markdown,
                )
            )

        return NormalizedDocumentJSON(
            doc_id=payload.doc_id,
            source_type=payload.source_type,
            original_path=payload.original_path,
            entry_id=payload.entry_id,
            internet_message_id=payload.internet_message_id,
            conversation_id=payload.conversation_id,
            sender=payload.sender,
            subject=payload.subject,
            folder=payload.folder,
            received_ts_utc=payload.received_ts_utc,
            clean_body_text=body_text.strip(),
            attachments=extracted_attachments,
            extracted_at_utc=datetime.now(timezone.utc),
        )
