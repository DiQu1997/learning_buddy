from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


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


DEFAULT_ARTIFACTS = ["note", "slide_deck", "video", "audio", "mind_map"]


def _filter_known(cls: type, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Drop any keys not declared on the dataclass — lets us load legacy configs cleanly."""
    if not payload:
        return {}
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in allowed}


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


@dataclass
class SplitConfig:
    min_pages_to_split: int = 35
    max_pages_per_chunk: int = 25


@dataclass
class LLMConfig:
    model: str = "gpt-5-mini"


@dataclass
class NLMConfig:
    verify_interval_seconds: int = 30
    backoff_base_seconds: int = 30
    backoff_multiplier: int = 2
    max_retries: int = 5
    request_interval_range: list[float] = field(default_factory=lambda: [0.5, 1.5])


@dataclass
class AppConfig:
    inbox: str = ""
    library: str = ""
    metadata: str = ""
    bucket_capacity: int = 25
    artifacts: list[str] = field(default_factory=lambda: list(DEFAULT_ARTIFACTS))
    note_prompt: str = DEFAULT_NOTE_PROMPT
    split: SplitConfig = field(default_factory=SplitConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    nlm: NLMConfig = field(default_factory=NLMConfig)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "AppConfig":
        payload = payload or {}
        return cls(
            inbox=str(payload.get("inbox", "")),
            library=str(payload.get("library", "")),
            metadata=str(payload.get("metadata") or payload.get("database", "")),
            bucket_capacity=int(payload.get("bucket_capacity", 25)),
            artifacts=[normalize_artifact_type(x) for x in (payload.get("artifacts") or DEFAULT_ARTIFACTS)],
            note_prompt=str(payload.get("note_prompt") or DEFAULT_NOTE_PROMPT),
            split=SplitConfig(**_filter_known(SplitConfig, payload.get("split"))),
            llm=LLMConfig(**_filter_known(LLMConfig, payload.get("llm"))),
            nlm=NLMConfig(**_filter_known(NLMConfig, payload.get("nlm"))),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["artifacts"] = list(self.artifacts)
        return data

    def resolved_paths(self) -> dict[str, Path]:
        if not self.inbox or not self.library or not self.metadata:
            raise ValueError(
                "inbox, library, and metadata paths must all be set. "
                "Run `learning-buddy config set <key> <value>`."
            )
        return {
            "inbox": Path(self.inbox).expanduser(),
            "library": Path(self.library).expanduser(),
            "metadata": Path(self.metadata).expanduser(),
        }


def default_config_path() -> Path:
    override = os.environ.get("LEARNING_BUDDY_CONFIG")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return home / "learning-buddy" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    target = Path(path) if path else default_config_path()
    if not target.exists():
        return AppConfig.from_dict({})
    return AppConfig.from_dict(json.loads(target.read_text(encoding="utf-8")))


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return target


_NESTED_KEYS = {
    "split.min_pages_to_split": ("split", "min_pages_to_split", int),
    "split.max_pages_per_chunk": ("split", "max_pages_per_chunk", int),
    "llm.model": ("llm", "model", str),
    "nlm.verify_interval_seconds": ("nlm", "verify_interval_seconds", int),
    "nlm.max_retries": ("nlm", "max_retries", int),
}


def apply_kv_update(config: AppConfig, key: str, value: str) -> None:
    if key in {"inbox", "library", "metadata", "note_prompt"}:
        setattr(config, key, value)
        return
    if key == "database":
        # legacy alias from earlier design — treat as `metadata`
        config.metadata = value
        return
    if key == "bucket_capacity":
        config.bucket_capacity = int(value)
        return
    if key == "artifacts":
        config.artifacts = [normalize_artifact_type(item) for item in value.split(",") if item.strip()]
        return
    if key in _NESTED_KEYS:
        section, attr, caster = _NESTED_KEYS[key]
        setattr(getattr(config, section), attr, caster(value))
        return
    raise KeyError(f"Unknown config key: {key}")
