"""Local-only defaults and resource boundaries."""
import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(os.path.relpath(Path(__file__).parent.parent))


@dataclass(frozen=True)
class Settings:
    root: Path
    max_upload_bytes: int = 24 * 1024 * 1024
    max_pixels: int = 16_000_000
    max_dimension: int = 8192
    max_batch: int = 24
    max_steps: int = 12
    max_bundle_assets: int = 24
    max_bundle_expanded_bytes: int = 64 * 1024 * 1024
    workers: int = 1

    @classmethod
    def from_environment(cls, root=None):
        location = root or os.environ.get("STUDIO_DATA_DIR")
        return cls(root=Path(os.path.relpath(location or PROJECT_ROOT / "data")))

    @property
    def media(self):
        return self.root / "media"

    @property
    def exports(self):
        return self.root / "exports"

    @property
    def database(self):
        return self.root / "json"

    def prepare(self):
        for directory in (self.root, self.media, self.exports, self.database):
            directory.mkdir(parents=True, exist_ok=True)

    def public(self):
        return {
            "max_upload_mb": self.max_upload_bytes // (1024 * 1024),
            "max_pixels": self.max_pixels,
            "max_dimension": self.max_dimension,
            "max_batch": self.max_batch,
            "max_steps": self.max_steps,
            "formats": ["PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"],
            "storage": "JSON",
        }

