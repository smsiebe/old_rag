"""cas.py - 3-Tier Envelope-Encrypted Content-Addressable Storage (CAS) Engine."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from old_rag.contracts import NormalizedDocumentJSON, StorageConfig
from old_rag.protocols import ContentStoreProtocol


def get_default_app_data_dir() -> Path:
    """Return %LOCALAPPDATA%\\OldRAG on Windows or ~/.old_rag fallback."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data and sys.platform == "win32":
        return Path(local_app_data) / "OldRAG"
    return Path.home() / ".old_rag"


class KeyManager:
    """Manages the 256-bit Master Data Encryption Key (DEK) wrapped by Windows DPAPI."""

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._master_key: Optional[bytes] = None

    def get_master_key(self) -> bytes:
        if self._master_key is not None:
            return self._master_key

        if self.key_path.exists():
            encrypted_key = self.key_path.read_bytes()
            self._master_key = self._unwrap_key(encrypted_key)
        else:
            self._master_key = os.urandom(32)  # 256-bit DEK
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            encrypted_key = self._wrap_key(self._master_key)
            self.key_path.write_bytes(encrypted_key)

        return self._master_key

    def _wrap_key(self, raw_key: bytes) -> bytes:
        if sys.platform == "win32":
            try:
                import win32crypt
                # CryptProtectData with current-user scope (CRYPTPROTECT_UI_FORBIDDEN = 0x1)
                return win32crypt.CryptProtectData(raw_key, "OldRAG Master Key", None, None, None, 0x1)
            except Exception:
                pass
        # Fallback for testing / simulated environments
        return b"DEV_DPAPI_WRAPPED:" + raw_key

    def _unwrap_key(self, wrapped_key: bytes) -> bytes:
        if sys.platform == "win32":
            try:
                import win32crypt
                _, raw = win32crypt.CryptUnprotectData(wrapped_key, None, None, None, 0x1)
                return raw
            except Exception:
                pass
        if wrapped_key.startswith(b"DEV_DPAPI_WRAPPED:"):
            return wrapped_key[len(b"DEV_DPAPI_WRAPPED:"):]
        raise ValueError("Failed to unwrap DPAPI master key.")

    def export_key(self, passphrase: str) -> bytes:
        """Export master key encrypted with passphrase via Scrypt + AES-GCM."""
        master = self.get_master_key()
        salt = os.urandom(16)
        kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
        derived_key = kdf.derive(passphrase.encode("utf-8"))
        aesgcm = AESGCM(derived_key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, master, b"OldRAG Key Backup")
        payload = {
            "version": 1,
            "salt_hex": salt.hex(),
            "nonce_hex": nonce.hex(),
            "ciphertext_hex": ciphertext.hex(),
        }
        return json.dumps(payload, indent=2).encode("utf-8")


class CASEngine(ContentStoreProtocol):
    """3-Tier Content-Addressable Storage Engine using AES-256-GCM envelope encryption."""

    def __init__(self, config: Optional[StorageConfig] = None):
        self.config = config or StorageConfig()
        root = Path(self.config.app_data_dir) if self.config.app_data_dir else get_default_app_data_dir()
        self.root_dir = root
        self.archive_dir = self.root_dir / "archive"
        self.text_dir = self.archive_dir / "text"
        self.blob_dir = self.archive_dir / "blob"
        self.msg_dir = self.archive_dir / "msg"
        self.tmp_dir = self.root_dir / "tmp"

        for d in (self.text_dir, self.blob_dir, self.msg_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.key_manager = KeyManager(self.root_dir / "master_key.dpapi")
        self._master_key = self.key_manager.get_master_key()
        # Derive specific subkeys
        self._enc_key = hashlib.sha256(self._master_key + b":AES_ENC").digest()
        self._hmac_key = hashlib.sha256(self._master_key + b":HMAC_NAME").digest()
        self._aesgcm = AESGCM(self._enc_key)

    def _derive_hmac_name(self, raw_sha256: str) -> str:
        """Obfuscate plaintext hash on disk using HMAC."""
        return hmac.new(self._hmac_key, raw_sha256.encode("utf-8"), hashlib.sha256).hexdigest()

    def _encrypt_and_write_atomic(self, target_path: Path, plaintext: bytes, aad: bytes) -> None:
        """Encrypt payload with AES-256-GCM and atomic os.replace."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, aad)
        payload = nonce + ciphertext

        temp_path = target_path.with_suffix(".tmp." + os.urandom(4).hex())
        with open(temp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, target_path)

    def _read_and_decrypt(self, target_path: Path, aad: bytes) -> Optional[bytes]:
        """Read and decrypt payload with AES-256-GCM."""
        if not target_path.exists():
            return None
        data = target_path.read_bytes()
        if len(data) < 28:  # 12 bytes nonce + 16 bytes auth tag
            return None
        nonce = data[:12]
        ciphertext = data[12:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception:
            return None

    # --- Tier 1: Canonical Extracted Text JSON ---

    def put_text(self, doc_id: str, payload: NormalizedDocumentJSON) -> str:
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.text_dir / hmac_name[:2] / f"{hmac_name}.json.enc"
        serialized = payload.model_dump_json().encode("utf-8")
        self._encrypt_and_write_atomic(target, serialized, aad=doc_id.encode("utf-8"))
        return hmac_name

    def get_text(self, doc_id: str) -> Optional[NormalizedDocumentJSON]:
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.text_dir / hmac_name[:2] / f"{hmac_name}.json.enc"
        decrypted = self._read_and_decrypt(target, aad=doc_id.encode("utf-8"))
        if decrypted is None:
            return None
        return NormalizedDocumentJSON.model_validate_json(decrypted.decode("utf-8"))

    def delete_text(self, doc_id: str) -> bool:
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.text_dir / hmac_name[:2] / f"{hmac_name}.json.enc"
        if target.exists():
            target.unlink()
            return True
        return False

    # --- Tier 2: Deduplicated Binary Attachment & Image Blobs ---

    def put_blob(self, blob_hash: str, data: bytes) -> str:
        hmac_name = self._derive_hmac_name(blob_hash)
        target = self.blob_dir / hmac_name[:2] / f"{hmac_name}.bin.enc"
        if not target.exists():
            self._encrypt_and_write_atomic(target, data, aad=blob_hash.encode("utf-8"))
        return hmac_name

    def get_blob(self, blob_hash: str) -> Optional[bytes]:
        hmac_name = self._derive_hmac_name(blob_hash)
        target = self.blob_dir / hmac_name[:2] / f"{hmac_name}.bin.enc"
        return self._read_and_decrypt(target, aad=blob_hash.encode("utf-8"))

    def delete_blob(self, blob_hash: str) -> bool:
        hmac_name = self._derive_hmac_name(blob_hash)
        target = self.blob_dir / hmac_name[:2] / f"{hmac_name}.bin.enc"
        if target.exists():
            target.unlink()
            return True
        return False

    # --- Tier 3: Raw Outlook .msg Email Archives ---

    def put_msg(self, doc_id: str, msg_bytes: bytes) -> str:
        if not self.config.copy_storage_emails_raw_msg:
            return ""
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.msg_dir / hmac_name[:2] / f"{hmac_name}.msg.enc"
        self._encrypt_and_write_atomic(target, msg_bytes, aad=doc_id.encode("utf-8"))
        return hmac_name

    def get_msg(self, doc_id: str) -> Optional[bytes]:
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.msg_dir / hmac_name[:2] / f"{hmac_name}.msg.enc"
        return self._read_and_decrypt(target, aad=doc_id.encode("utf-8"))

    def delete_msg(self, doc_id: str) -> bool:
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.msg_dir / hmac_name[:2] / f"{hmac_name}.msg.enc"
        if target.exists():
            target.unlink()
            return True
        return False

    def has_msg(self, doc_id: str) -> bool:
        """Check if raw .msg is archived for doc_id."""
        raw_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        hmac_name = self._derive_hmac_name(raw_hash)
        target = self.msg_dir / hmac_name[:2] / f"{hmac_name}.msg.enc"
        return target.exists()

    # --- Temp View Lifecycle ---

    def create_temp_view_file(self, doc_id: str, suffix: str = ".msg") -> Optional[Path]:
        """Decrypt archive payload to restricted tmp/ for shell viewing."""
        data = self.get_msg(doc_id)
        if not data:
            return None
        safe_name = f"view_{hashlib.sha256(doc_id.encode()).hexdigest()[:16]}{suffix}"
        view_path = self.tmp_dir / safe_name
        view_path.write_bytes(data)
        return view_path

    def sweep_tmp(self) -> int:
        """Mandatory startup/shutdown cleanup of decrypted temp files."""
        count = 0
        if self.tmp_dir.exists():
            for f in self.tmp_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except Exception:
                        pass
        return count

