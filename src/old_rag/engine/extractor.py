"""extractor.py - Document Extractor delegating to Scryer with Template-Aware Catalog Caching."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from old_rag.contracts import (
    ExtractedAttachmentMeta,
    NormalizedDocumentJSON,
    RawIngestPayload,
    safe_json_dumps,
    safe_json_loads,
)
from old_rag.protocols import CatalogProtocol, ExtractorProtocol, VLMClientProtocol
from scryer.contracts import (
    ScryerExtractionOptions,
    ScryerResult,
    compute_canonical_hash,
)
from scryer.extractor import ScryerExtractor

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
OFFICE_OR_TEXT_EXTENSIONS = {".docx", ".xlsx", ".pdf", ".pptx", ".txt", ".md", ".csv", ".json", ".eml", ".msg"}


def strip_html_tags(html: str) -> str:
    """Basic deterministic HTML tag stripper."""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _flatten_structured_fields(data: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Flatten nested dicts/lists into readable key-value pairs for FTS5 and vector indexing."""
    pairs: List[Tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            key_path = f"{prefix}.{k}" if prefix else str(k)
            pairs.extend(_flatten_structured_fields(v, key_path))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            if isinstance(item, (dict, list)):
                pairs.extend(_flatten_structured_fields(item, f"{prefix}[{idx}]"))
            elif item is not None and str(item).strip():
                pairs.append((prefix or "item", str(item).strip()))
    elif data is not None and str(data).strip():
        pairs.append((prefix or "value", str(data).strip()))
    return pairs


def format_structured_context_text(
    structured_data: Dict[str, Any],
    metadata_dict: Optional[Dict[str, Any]] = None,
    annotations: Optional[List[str]] = None,
) -> str:
    """Render Scryer structured fields, metadata, and annotations into searchable text."""
    lines: List[str] = []
    if metadata_dict:
        meta_bits = []
        for field_name in ("author", "title", "subject", "mime_type", "dimensions", "sheet_names"):
            val = metadata_dict.get(field_name)
            if val:
                meta_bits.append(f"{field_name}: {val}")
        if meta_bits:
            lines.append("Document Metadata: " + " | ".join(meta_bits))

    if structured_data:
        lines.append("<!-- Scryer Structured Context -->")
        for k, v in _flatten_structured_fields(structured_data):
            lines.append(f"{k}: {v}")

    if annotations:
        for note in annotations:
            if note and str(note).strip():
                lines.append(f"Note: {str(note).strip()}")

    return "\n".join(lines).strip()


class DocumentExtractor(ExtractorProtocol):
    """Document parser leveraging Scryer with composite template-keyed VLM caching."""

    def __init__(
        self,
        catalog: Optional[CatalogProtocol] = None,
        vlm_client: Optional[VLMClientProtocol] = None,
        scryer_extractor: Optional[ScryerExtractor] = None,
        default_options: Optional[ScryerExtractionOptions] = None,
        templates_by_ext: Optional[Dict[str, Any]] = None,
    ):
        self.catalog = catalog
        self.vlm_client = vlm_client
        self.scryer = scryer_extractor or ScryerExtractor()
        self.default_options = default_options or ScryerExtractionOptions()
        self.templates_by_ext = {k.lower(): v for k, v in (templates_by_ext or {}).items()}

    def _resolve_options_and_identity(
        self, ext: str, *, force_phase2_override: Optional[bool] = None, enable_phase2_override: Optional[bool] = None
    ) -> Tuple[ScryerExtractionOptions, str, str]:
        template_obj = self.templates_by_ext.get(ext, self.default_options.template)
        updates: Dict[str, Any] = {}
        if template_obj is not self.default_options.template:
            updates["template"] = template_obj
        if force_phase2_override is not None:
            updates["force_phase2"] = force_phase2_override
        if enable_phase2_override is not None:
            updates["enable_phase2"] = enable_phase2_override
        opts = self.default_options.model_copy(update=updates) if updates else self.default_options

        if template_obj is not None:
            schema_dict, _ = self.scryer.template_compiler.compile(template_obj)
            template_key = (
                getattr(template_obj, "name", None)
                or (template_obj.get("name") if isinstance(template_obj, dict) else None)
                or getattr(template_obj, "__name__", None)
                or "custom"
            )
            schema_hash = compute_canonical_hash(schema_dict)
        else:
            template_key = "default"
            schema_hash = ""
        return opts, template_key, schema_hash

    def _read_vlm_cache(
        self, blob_hash: str, template_key: str, schema_hash: str
    ) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Any]]:
        if not self.catalog:
            return None, {}, {}
        if hasattr(self.catalog, "get_vlm_cache_entry"):
            entry = self.catalog.get_vlm_cache_entry(
                blob_hash, template_key=template_key, schema_hash=schema_hash
            )
            if not entry:
                return None, {}, {}
            structured: Dict[str, Any] = {}
            metadata: Dict[str, Any] = {}
            if entry.get("structured_json"):
                try:
                    structured = safe_json_loads(entry["structured_json"])
                except Exception:
                    structured = {}
            if entry.get("metadata_json"):
                try:
                    metadata = safe_json_loads(entry["metadata_json"])
                except Exception:
                    metadata = {}
            return entry.get("markdown"), structured, metadata

        try:
            md = self.catalog.get_vlm_cache(blob_hash, template_key=template_key, schema_hash=schema_hash)
        except TypeError:
            md = self.catalog.get_vlm_cache(blob_hash)
        return md, {}, {}

    def _write_vlm_cache(
        self,
        blob_hash: str,
        markdown: str,
        template_key: str,
        schema_hash: str,
        structured_data: Optional[Dict[str, Any]] = None,
        document_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.catalog or not markdown:
            return
        structured_json = safe_json_dumps(structured_data) if structured_data else None
        metadata_json = safe_json_dumps(document_metadata) if document_metadata else None
        try:
            self.catalog.put_vlm_cache(
                blob_hash,
                markdown,
                template_key=template_key,
                schema_hash=schema_hash,
                structured_json=structured_json,
                metadata_json=metadata_json,
            )
        except TypeError:
            self.catalog.put_vlm_cache(blob_hash, markdown)

    def can_extract(self, filename: str) -> bool:
        return self.scryer.parser_registry.can_parse(filename)

    def extract_docx(self, data: bytes) -> str:
        opts, _, _ = self._resolve_options_and_identity(
            ".docx",
            enable_phase2_override=self.default_options.enable_phase2 and (
                self.templates_by_ext.get(".docx") is not None
                or self.default_options.template is not None
                or self.default_options.force_phase2
            ),
        )
        res = self.scryer.extract(data, filename="document.docx", options=opts)
        summary = format_structured_context_text(res.structured_data, None, res.unstructured_annotations)
        return f"{res.raw_text}\n\n{summary}".strip() if summary else res.raw_text

    def extract_xlsx(self, data: bytes) -> str:
        opts, _, _ = self._resolve_options_and_identity(
            ".xlsx",
            enable_phase2_override=self.default_options.enable_phase2 and (
                self.templates_by_ext.get(".xlsx") is not None
                or self.default_options.template is not None
                or self.default_options.force_phase2
            ),
        )
        res = self.scryer.extract(data, filename="workbook.xlsx", options=opts)
        summary = format_structured_context_text(res.structured_data, None, res.unstructured_annotations)
        return f"{res.raw_text}\n\n{summary}".strip() if summary else res.raw_text

    def extract_pdf(self, data: bytes) -> str:
        opts, _, _ = self._resolve_options_and_identity(".pdf")
        if self.vlm_client:
            pdf_opts = opts.model_copy(update={"min_text_threshold": opts.min_text_threshold, "extract_images": True})
            res = self.scryer.parser_registry.parse(data, "document.pdf", options=pdf_opts)
            if res.low_text_pages and res.visual_elements:
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

        scryer_res = self.scryer.extract(data, filename="document.pdf", options=opts)
        summary = format_structured_context_text(
            scryer_res.structured_data, None, scryer_res.unstructured_annotations
        )
        return f"{scryer_res.raw_text}\n\n{summary}".strip() if summary else scryer_res.raw_text

    def _run_scryer_for_attachment(
        self,
        data: bytes,
        filename: str,
        ext: str,
        blob_hash: str,
        opts: ScryerExtractionOptions,
        template_key: str,
        schema_hash: str,
    ) -> Tuple[str, Optional[str], Dict[str, Any], Dict[str, Any], List[str], Dict[str, Any]]:
        """Extract attachment using Scryer two-phase pipeline + catalog VLM cache."""
        cached_md, cached_structured, cached_meta = self._read_vlm_cache(
            blob_hash, template_key, schema_hash
        )

        is_image = ext in IMAGE_EXTENSIONS
        should_skip = self.catalog.is_vlm_skipped(blob_hash) if (is_image and self.catalog) else False

        if is_image:
            if cached_md is not None:
                return "", cached_md, cached_structured, cached_meta, [], {"cache_hit": True}
            if should_skip:
                return "", None, {}, {}, [], {"vlm_skipped": True}
            if self.vlm_client:
                vlm_md = self.vlm_client.extract_image_markdown(data, filename)
                self._write_vlm_cache(blob_hash, vlm_md, template_key, schema_hash)
                return "", vlm_md, {}, {}, [], {"legacy_vlm_client": True}

            scryer_res = self.scryer.extract(data, filename=filename, options=opts)
            meta_dict = scryer_res.metadata.model_dump(mode="json")
            summary = format_structured_context_text(
                scryer_res.structured_data, meta_dict, scryer_res.unstructured_annotations
            )
            parts = [p for p in (summary, scryer_res.raw_text) if p]
            vlm_md = "\n\n".join(parts).strip() or None
            if vlm_md:
                self._write_vlm_cache(
                    blob_hash,
                    vlm_md,
                    template_key,
                    schema_hash,
                    structured_data=scryer_res.structured_data,
                    document_metadata=meta_dict,
                )
            return (
                "",
                vlm_md,
                scryer_res.structured_data,
                meta_dict,
                scryer_res.unstructured_annotations,
                scryer_res.route_telemetry,
            )

        # Document / Office / PDF / Text formats
        if self.vlm_client and ext == ".pdf":
            return self.extract_pdf(data), cached_md, cached_structured, cached_meta, [], {}

        has_template = template_key != "default" or opts.force_phase2
        if ext in {".docx", ".xlsx"} and not has_template:
            # Run Phase 1 first; only invoke Phase 2 if embedded visual elements exist.
            p1 = self.scryer.parser_registry.parse(data, filename, options=opts)
            doc_opts = opts if (opts.enable_phase2 and p1.visual_elements) else opts.model_copy(
                update={"enable_phase2": False}
            )
        else:
            doc_opts = opts

        if cached_md is not None and not has_template:
            # Cached visual enrichment exists; avoid redundant Phase 2 call unless template changed
            doc_opts = doc_opts.model_copy(update={"enable_phase2": False})

        scryer_res: ScryerResult = self.scryer.extract(data, filename=filename, options=doc_opts)
        meta_dict = scryer_res.metadata.model_dump(mode="json")
        structured = scryer_res.structured_data or cached_structured
        annotations = scryer_res.unstructured_annotations
        telemetry = scryer_res.route_telemetry

        summary = format_structured_context_text(structured, meta_dict, annotations)
        att_text = f"{scryer_res.raw_text}\n\n{summary}".strip() if summary else scryer_res.raw_text
        vlm_md = cached_md
        if (structured or (ext == ".pdf" and telemetry.get("phase2_invoked"))) and not cached_md:
            vlm_md = summary or scryer_res.raw_text
            if vlm_md:
                self._write_vlm_cache(
                    blob_hash,
                    vlm_md,
                    template_key,
                    schema_hash,
                    structured_data=structured,
                    document_metadata=meta_dict,
                )
        return att_text, vlm_md, structured, meta_dict, annotations, telemetry

    def extract(self, payload: RawIngestPayload) -> NormalizedDocumentJSON:
        body_text = payload.body_text or ""
        if not body_text and payload.body_html:
            body_text = strip_html_tags(payload.body_html)

        extracted_attachments: List[ExtractedAttachmentMeta] = []
        doc_structured_context: Dict[str, Any] = {}
        doc_metadata: Dict[str, Any] = {}
        all_annotations: List[str] = []

        for att in payload.attachments:
            blob_hash = hashlib.sha256(att.data).hexdigest()
            ext = Path(att.filename).suffix.lower()
            eff_opts, template_key, schema_hash = self._resolve_options_and_identity(ext)
            att_text = ""
            vlm_markdown: Optional[str] = None
            structured_data: Dict[str, Any] = {}
            att_metadata: Dict[str, Any] = {}
            annotations: List[str] = []
            telemetry: Dict[str, Any] = {}

            if ext in IMAGE_EXTENSIONS or ext in OFFICE_OR_TEXT_EXTENSIONS or self.can_extract(att.filename):
                (
                    att_text,
                    vlm_markdown,
                    structured_data,
                    att_metadata,
                    annotations,
                    telemetry,
                ) = self._run_scryer_for_attachment(
                    att.data, att.filename, ext, blob_hash, eff_opts, template_key, schema_hash
                )

            if att_text:
                body_text += f"\n\n--- Attachment: {att.filename} ---\n{att_text}"
            elif vlm_markdown and ext in IMAGE_EXTENSIONS:
                body_text += f"\n\n--- Image Context: {att.filename} ---\n{vlm_markdown}"

            if structured_data:
                doc_structured_context[att.filename] = structured_data
            if att_metadata:
                doc_metadata[att.filename] = att_metadata
            all_annotations.extend(annotations)

            extracted_attachments.append(
                ExtractedAttachmentMeta(
                    filename=att.filename,
                    content_type=att.content_type,
                    blob_hash=blob_hash,
                    vlm_markdown=vlm_markdown,
                    template_key=template_key,
                    structured_data=structured_data,
                    document_metadata=att_metadata,
                    unstructured_annotations=annotations,
                    route_telemetry=telemetry,
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
            structured_context=doc_structured_context,
            document_metadata=doc_metadata,
            unstructured_annotations=all_annotations,
        )
