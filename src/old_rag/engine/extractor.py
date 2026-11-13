"""extractor.py - Document Extractor delegating to Scryer with Catalog VLM Caching."""

from __future__ import annotations

import base64
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
    safe_json_dumps,
)
from old_rag.protocols import CatalogProtocol, ExtractorProtocol, VLMClientProtocol
from scryer.contracts import ScryerExtractionOptions
from scryer.extractor import ScryerExtractor


def strip_html_tags(html: str) -> str:
    """Basic deterministic HTML tag stripper."""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


class DocumentExtractor(ExtractorProtocol):
    """Document parser leveraging Scryer with Catalog VLM caching and backward-compatible fallback."""

    def __init__(
        self,
        catalog: Optional[CatalogProtocol] = None,
        vlm_client: Optional[VLMClientProtocol] = None,
        scryer_extractor: Optional[ScryerExtractor] = None,
    ):
        self.catalog = catalog
        self.vlm_client = vlm_client
        self.scryer = scryer_extractor or ScryerExtractor()

    def can_extract(self, filename: str) -> bool:
        return self.scryer.parser_registry.can_parse(filename)

    def extract_docx(self, data: bytes) -> str:
        res = self.scryer.parser_registry.parse(data, "document.docx")
        return res.raw_text

    def extract_xlsx(self, data: bytes) -> str:
        res = self.scryer.parser_registry.parse(data, "workbook.xlsx")
        return res.raw_text

    def extract_pdf(self, data: bytes) -> str:
        opts = ScryerExtractionOptions(min_text_threshold=30, extract_images=True)
        res = self.scryer.parser_registry.parse(data, "document.pdf", options=opts)

        # If low-text pages were detected and vlm_client is provided, use vlm_client for low text pages
        if res.low_text_pages and self.vlm_client and res.visual_elements:
            merged_pages = []
            page_text_map = {idx + 1: t for idx, t in enumerate(res.page_texts)}
            rendered_map = {
                elem.page_number: elem
                for elem in res.visual_elements
                if elem.element_type == "rendered_page"
            }
            for p_num in range(1, res.metadata.page_count + 1):
                if p_num in res.low_text_pages and p_num in rendered_map:
                    elem = rendered_map[p_num]
                    if elem.data_base64:
                        img_bytes = base64.b64decode(elem.data_base64)
                        vlm_markdown = self.vlm_client.extract_image_markdown(
                            img_bytes, f"page_{p_num}.png"
                        )
                        merged_pages.append(vlm_markdown)
                        continue
                merged_pages.append(page_text_map.get(p_num, ""))
            return "\n\n".join(merged_pages)

        return res.raw_text

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
            elif ext in {".png", ".jpg", ".jpeg"} and not vlm_markdown:
                should_skip = self.catalog.is_vlm_skipped(blob_hash) if self.catalog else False
                if not should_skip:
                    if self.vlm_client:
                        vlm_markdown = self.vlm_client.extract_image_markdown(att.data, att.filename)
                    else:
                        scryer_res = self.scryer.extract(att.data, filename=att.filename)
                        if scryer_res.structured_data:
                            vlm_markdown = "<!-- Scryer Structured Context -->\n" + safe_json_dumps(
                                scryer_res.structured_data, indent=2
                            )
                        elif scryer_res.raw_text:
                            vlm_markdown = scryer_res.raw_text

                    if self.catalog and vlm_markdown:
                        self.catalog.put_vlm_cache(blob_hash, vlm_markdown)
            elif ext in {".txt", ".md", ".csv", ".json", ".pptx", ".eml", ".msg"}:
                res = self.scryer.extract(att.data, filename=att.filename)
                att_text = res.raw_text

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
        )
