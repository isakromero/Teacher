"""Application settings: where Teacher keeps its database and files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "TEACHER_HOME"


@dataclass(frozen=True)
class Settings:
    """Locations of all on-disk data. Every path stored in the database is relative to `root`."""

    root: Path

    def __post_init__(self) -> None:
        # Normalise to an absolute, symlink-free path so relative-path maths is stable.
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @classmethod
    def from_env(cls) -> Settings:
        """Use `$TEACHER_HOME` if set, else the current working directory."""
        home = os.environ.get(ENV_HOME)
        return cls(root=Path(home) if home else Path.cwd())

    @property
    def db_path(self) -> Path:
        return self.root / "teacher.db"

    @property
    def sources_dir(self) -> Path:
        return self.root / "knowledge_sources"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


# -- model selection ----------------------------------------------------------------------

ENV_GENERATION_MODEL = "TEACHER_GENERATION_MODEL"
ENV_JUDGE_MODEL = "TEACHER_JUDGE_MODEL"
ENV_METADATA_MODEL = "TEACHER_METADATA_MODEL"
ENV_TUTOR_MODEL = "TEACHER_TUTOR_MODEL"
ENV_EMBEDDING_MODEL = "TEACHER_EMBEDDING_MODEL"
ENV_CONCEPT_MODEL = "TEACHER_CONCEPT_MODEL"
DEFAULT_GENERATION_MODEL = "claude-sonnet-5-5"
# The models a reader may pick for one study page (id, name shown), most capable first. The
# configured default ($TEACHER_GENERATION_MODEL) is always allowed as well. Every model here
# accepts the generator's request shape: adaptive thinking, `effort` and structured output.
GENERATION_MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("claude-fable-5-2", "Claude Fable 5.2"),
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
)
DEFAULT_JUDGE_MODEL = "claude-haiku-5-5"
DEFAULT_METADATA_MODEL = "claude-haiku-5-5"
DEFAULT_TUTOR_MODEL = "claude-opus-5-5"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# The same default as the generation model: the concept stage of a page generation can then
# read the document from the prompt cache the page's other stages wrote (caches are per model).
DEFAULT_CONCEPT_MODEL = DEFAULT_GENERATION_MODEL


@dataclass(frozen=True)
class ModelSettings:
    """Which model each job uses. The one place the model environment variables are read.

    Claude models: `$TEACHER_GENERATION_MODEL` (default `claude-sonnet-5-5`) writes study pages;
    `$TEACHER_JUDGE_MODEL` (default `claude-haiku-5-5`) grades blurb answers;
    `$TEACHER_METADATA_MODEL` (default `claude-haiku-5-5`) detects an uploaded paper's title,
    authors and other details; `$TEACHER_TUTOR_MODEL` (default `claude-opus-5-5`) answers the
    reader in the study page's side chat; `$TEACHER_CONCEPT_MODEL` (default: the generation
    model's default, `claude-sonnet-5-5`) extracts a source's concepts and judges whether each is
    one already in the concept graph. OpenAI model: `$TEACHER_EMBEDDING_MODEL` (default
    `text-embedding-3-small`) embeds concepts for similarity search. A variable that is set but
    blank is an error rather than a silent fallback to the default.
    """

    generation: str = DEFAULT_GENERATION_MODEL
    judge: str = DEFAULT_JUDGE_MODEL
    metadata: str = DEFAULT_METADATA_MODEL
    tutor: str = DEFAULT_TUTOR_MODEL
    embedding: str = DEFAULT_EMBEDDING_MODEL
    concept: str = DEFAULT_CONCEPT_MODEL

    @classmethod
    def from_env(cls) -> ModelSettings:
        return cls(
            generation=_model_from_env(ENV_GENERATION_MODEL, DEFAULT_GENERATION_MODEL),
            judge=_model_from_env(ENV_JUDGE_MODEL, DEFAULT_JUDGE_MODEL),
            metadata=_model_from_env(ENV_METADATA_MODEL, DEFAULT_METADATA_MODEL),
            tutor=_model_from_env(ENV_TUTOR_MODEL, DEFAULT_TUTOR_MODEL),
            embedding=_model_from_env(ENV_EMBEDDING_MODEL, DEFAULT_EMBEDDING_MODEL),
            concept=_model_from_env(ENV_CONCEPT_MODEL, DEFAULT_CONCEPT_MODEL),
        )


def _model_from_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    if not value.strip():
        raise ValueError(f"${name} is set but empty; unset it to use {default!r}")
    return value.strip()
