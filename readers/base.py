"""Abstract base class for NFC readers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TagRead:
    """Result from reading an NFC tag."""

    uid: bytes  # Tag hardware UID
    protocol: str  # "iso14443a" or "iso15693"
    data: bytes  # Raw tag memory


class NfcReader(ABC):
    """Abstract NFC reader interface."""

    @abstractmethod
    def open(self) -> bool:
        """Initialize the reader. Returns True on success."""

    @abstractmethod
    def close(self) -> None:
        """Release the reader."""

    @abstractmethod
    def poll(self, timeout: float = 1.0) -> TagRead | None:
        """Poll for a tag and read its memory. Returns TagRead or None."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable reader name for logging."""

    def write_blocks(self, uid: bytes, offset: int, data: bytes) -> bool:
        """Write data to tag memory at the given byte offset.

        Optional — not all readers or tag types support writing.
        Returns True on success.
        """
        return False
