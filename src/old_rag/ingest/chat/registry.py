"""registry.py - Extensible Chat Plugin Registry and discovery host."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import os
from pathlib import Path
from typing import Dict, List, Optional, Type

from old_rag.contracts import (
    ChatAccountConfig,
    ChatPluginDescriptor,
    SecurityViolationError,
    ServiceUnavailableError,
)
from old_rag.ingest.chat.base import BaseChatAdapter
from old_rag.ingest.chat.google_chat import GoogleChatAdapter
from old_rag.ingest.chat.jabber import JabberAdapter
from old_rag.ingest.chat.rocketchat import RocketChatAdapter
from old_rag.ingest.chat.teams import TeamsGraphAdapter
from old_rag.protocols import ChatAdapterProtocol, ChatPluginRegistryProtocol
from old_rag.storage.cas import get_default_app_data_dir


class ChatPluginRegistry(ChatPluginRegistryProtocol):
    """Host registry managing built-in and third-party chat adapter plugins."""

    def __init__(self, plugin_dir: Optional[Path] = None):
        self._adapters: Dict[str, Type[ChatAdapterProtocol]] = {}
        self.plugin_dir = plugin_dir or (get_default_app_data_dir() / "plugins" / "chat")
        # Register built-ins
        self.register(RocketChatAdapter)
        self.register(GoogleChatAdapter)
        self.register(TeamsGraphAdapter)
        self.register(JabberAdapter)

    def register(self, adapter_cls: Type[ChatAdapterProtocol]) -> None:
        """Register a chat adapter class."""
        adapter_type = getattr(adapter_cls, "adapter_type", "")
        if not adapter_type:
            dummy = adapter_cls.__new__(adapter_cls)
            desc = dummy.get_descriptor() if hasattr(dummy, "get_descriptor") else None
            adapter_type = desc.adapter_type if desc else adapter_cls.__name__.lower()
        self._adapters[adapter_type] = adapter_cls

    def discover_plugins(self, plugin_dir: Optional[Path] = None) -> List[ChatPluginDescriptor]:
        """Discover external plugins via entry_points and local plugin directory."""
        # 1. Discover via entry points
        try:
            eps = importlib.metadata.entry_points(group="old_rag.chat_plugins")
            for ep in eps:
                try:
                    cls = ep.load()
                    if inspect.isclass(cls):
                        self.register(cls)
                except Exception:
                    pass
        except Exception:
            pass

        # 2. Discover via local directory
        target_dir = plugin_dir or self.plugin_dir
        if target_dir:
            resolved_root = self.plugin_dir.resolve()
            if not target_dir.resolve().is_relative_to(resolved_root):
                raise SecurityViolationError(f"Plugin path traversal attempt: {target_dir}")
            if target_dir.exists():
                for root, _, files in os.walk(str(target_dir)):
                    for file in files:
                        if file.endswith(".py") and not file.startswith("__"):
                            candidate = (Path(root) / file).resolve()
                            # Path traversal and symlink rejection (METHODOLOGY.md §V)
                            if not candidate.is_relative_to(resolved_root):
                                raise SecurityViolationError(f"Plugin path traversal attempt: {candidate}")
                            if candidate.is_symlink():
                                continue

                        try:
                            spec = importlib.util.spec_from_file_location(f"ext_chat_{candidate.stem}", str(candidate))
                            if spec and spec.loader:
                                mod = importlib.util.module_from_spec(spec)
                                spec.loader.exec_module(mod)
                                for _, obj in inspect.getmembers(mod, inspect.isclass):
                                    if (
                                        issubclass(obj, (BaseChatAdapter, ChatAdapterProtocol))
                                        and obj not in (BaseChatAdapter, ChatAdapterProtocol)
                                        and hasattr(obj, "adapter_type")
                                    ):
                                        self.register(obj)
                        except SecurityViolationError:
                            raise
                        except Exception:
                            pass

        return self.list_plugins()

    def list_plugins(self) -> List[ChatPluginDescriptor]:
        """List descriptors for all currently registered chat adapters."""
        descriptors: List[ChatPluginDescriptor] = []
        for atype, cls in sorted(self._adapters.items()):
            try:
                # Instantiate with dummy config to obtain descriptor
                dummy = cls(ChatAccountConfig(id="desc", name="desc", adapter_type=atype))
                descriptors.append(dummy.get_descriptor())
            except Exception:
                pass
        return descriptors

    def create_adapter(self, config: ChatAccountConfig) -> ChatAdapterProtocol:
        """Instantiate a configured ChatAdapterProtocol."""
        cls = self._adapters.get(config.adapter_type)
        if not cls:
            raise ServiceUnavailableError(f"No chat adapter registered for type '{config.adapter_type}'")
        return cls(config)
