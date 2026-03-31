from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Iterable


DEFAULT_CONFIG = {
    "max_pages_per_chunk": 50,
    "chunk_threshold": 50,
    "default_artifacts": [
        "report",
        "slide_deck",
    ],
    "extended_artifacts": [
        "audio",
        "video",
        "quiz",
        "flashcards",
        "mind_map",
        "infographic",
    ],
    "report_format": "Study Guide",
    "max_concurrent_generations": 2,
    "courtesy_delay_seconds": 5,
    "poll_interval_seconds": 30,
    "poll_max_wait_seconds": 600,
    "backoff_base_seconds": 30,
    "backoff_multiplier": 2,
    "max_retries": 3,
    "nlm_request_interval_range": [0.5, 1.5],
    "cleanup_failed_remote_artifacts": True,
}


ALL_ARTIFACT_TYPES = (
    "report",
    "slide_deck",
    "audio",
    "video",
    "quiz",
    "flashcards",
    "mind_map",
    "infographic",
)


@dataclass
class EngineConfig:
    max_pages_per_chunk: int = 50
    chunk_threshold: int = 50
    default_artifacts: list[str] = field(default_factory=lambda: ["report", "slide_deck"])
    extended_artifacts: list[str] = field(
        default_factory=lambda: ["audio", "video", "quiz", "flashcards", "mind_map", "infographic"]
    )
    report_format: str = "Study Guide"
    max_concurrent_generations: int = 2
    courtesy_delay_seconds: int = 5
    poll_interval_seconds: int = 30
    poll_max_wait_seconds: int = 600
    backoff_base_seconds: int = 30
    backoff_multiplier: int = 2
    max_retries: int = 3
    nlm_request_interval_range: list[float] = field(default_factory=lambda: [0.5, 1.5])
    cleanup_failed_remote_artifacts: bool = True

    @classmethod
    def from_dict(cls, payload: dict) -> "EngineConfig":
        data = dict(DEFAULT_CONFIG)
        data.update(payload or {})
        data["default_artifacts"] = list(data.get("default_artifacts", []))
        data["extended_artifacts"] = list(data.get("extended_artifacts", []))
        data["nlm_request_interval_range"] = list(data.get("nlm_request_interval_range", [0.5, 1.5]))
        allowed = {item.name for item in fields(cls)}
        filtered = {key: value for key, value in data.items() if key in allowed}
        return cls(**filtered)

    def to_dict(self) -> dict:
        return {
            "max_pages_per_chunk": self.max_pages_per_chunk,
            "chunk_threshold": self.chunk_threshold,
            "default_artifacts": list(self.default_artifacts),
            "extended_artifacts": list(self.extended_artifacts),
            "report_format": self.report_format,
            "max_concurrent_generations": self.max_concurrent_generations,
            "courtesy_delay_seconds": self.courtesy_delay_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_max_wait_seconds": self.poll_max_wait_seconds,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "max_retries": self.max_retries,
            "nlm_request_interval_range": list(self.nlm_request_interval_range),
            "cleanup_failed_remote_artifacts": self.cleanup_failed_remote_artifacts,
        }

    def resolve_artifacts(self, requested: Iterable[str] | None) -> list[str]:
        if requested is None:
            values = list(self.default_artifacts)
        else:
            values = [item.strip() for item in requested if item and item.strip()]
        normalized: list[str] = []
        for item in values:
            key = normalize_artifact_type(item)
            if key not in ALL_ARTIFACT_TYPES:
                raise ValueError(f"Unsupported artifact type: {item}")
            if key not in normalized:
                normalized.append(key)
        if not normalized:
            raise ValueError("At least one artifact type is required.")
        return normalized


def normalize_artifact_type(value: str) -> str:
    token = value.strip().lower().replace("-", "_")
    if token in {"slides", "slide", "slide_deck"}:
        return "slide_deck"
    if token in {"mindmap", "mind_map", "mind-map"}:
        return "mind_map"
    return token
