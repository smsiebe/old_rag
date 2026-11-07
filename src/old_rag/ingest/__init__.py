"""ingest package for old_rag."""

from old_rag.ingest.outlook import ClassicMAPIConnector, is_new_outlook_active, probe_object_model_guard
from old_rag.ingest.watcher import DirectoryWatcher, is_cloud_placeholder, is_temporary_office_file, parse_eml_file

__all__ = [
    "ClassicMAPIConnector",
    "is_new_outlook_active",
    "probe_object_model_guard",
    "DirectoryWatcher",
    "is_cloud_placeholder",
    "is_temporary_office_file",
    "parse_eml_file",
]
