"""conftest.py - Pytest fixtures and environment configuration."""

import shutil
import sys
import tempfile
from pathlib import Path
import pytest

from old_rag.contracts import StorageConfig
from old_rag.storage.cas import CASEngine
from old_rag.storage.catalog import CatalogRepository


@pytest.fixture
def temp_app_dir():
    d = tempfile.mkdtemp(prefix="old_rag_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def storage_config(temp_app_dir):
    return StorageConfig(
        app_data_dir=str(temp_app_dir),
        copy_storage_emails_raw_msg=True,
        copy_storage_files=False,
    )


@pytest.fixture
def cas_engine(storage_config):
    return CASEngine(storage_config)


@pytest.fixture
def catalog_repo(temp_app_dir):
    return CatalogRepository(temp_app_dir / "catalog.db")
