from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Iterable


DEFAULT_NOTE_PROMPT = """你将把一篇文章重写成"阅读版本" ，输出简体中文和英文两个版本，按内容主题分成若干小节；目标是让读者通过阅读就能完整理解文章讲了什么，就好像是在读一篇 Blog 版的文章一样。

输出要求：
1. Metadata
- Title
- Author
- URL

2. Overview
用一段话点明文章的核心论题与结论。

3. 按照主题来梳理
- 每个小节都需要根据文章中的内容详细展开，让我不需要再二次查看文章了解详情，每个小节不少于 500 字。
- 若出现方法/框架/流程，将其重写为条理清晰的步骤或段落。
- 若有关键数字、定义、原话，请如实保留核心词，并在括号内补充注释。

4. 框架 & 心智模型（Framework & Mindset）
可以从文章中抽象出什么 framework & mindset，将其重写为条理清晰的步骤或段落，每个 framework & mindset 不少于 500 字。

风格与限制：
- 永远不要高度浓缩！
- 不新增事实；若出现含混表述，请保持原意并注明不确定性。
- 专有名词保留原文，并在括号给出中文释义（若转录中出现或能直译）。
- 要求类的问题不用体现出来（例如 > 500 字）。
- 避免一个段落的内容过多，可以拆解成多个逻辑段落（使用 bullet points）。

-回答的任何部分都不要出现繁体中文"""


DEFAULT_CONFIG = {
    "max_pages_per_chunk": 50,
    "chunk_threshold": 50,
    "default_artifacts": [
        "report",
        "slide_deck",
        "video",
        "note",
    ],
    "extended_artifacts": [
        "audio",
        "quiz",
        "flashcards",
        "mind_map",
        "infographic",
    ],
    "report_format": "Study Guide",
    "note_prompt": DEFAULT_NOTE_PROMPT,
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
    "note",
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
    default_artifacts: list[str] = field(default_factory=lambda: ["report", "slide_deck", "video", "note"])
    extended_artifacts: list[str] = field(
        default_factory=lambda: ["audio", "quiz", "flashcards", "mind_map", "infographic"]
    )
    report_format: str = "Study Guide"
    note_prompt: str = DEFAULT_NOTE_PROMPT
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
            "note_prompt": self.note_prompt,
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
    if token in {"note", "notes", "reading_note", "reading_notes"}:
        return "note"
    return token


def remote_artifact_type(value: str) -> str:
    token = normalize_artifact_type(value)
    if token == "note":
        return "report"
    return token
