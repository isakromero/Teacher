"""Application settings: which database Teacher uses and where it keeps its files, which models
it uses, the YouTube Data API key (`youtube_api_key_from_env`) and the access code that
registering an account needs (`teacher_password_from_env`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ENV_HOME = "TEACHER_HOME"
ENV_DB = "TEACHER_DB"
ENV_SUPABASE_DB_URL = "SUPABASE_DB_URL"

#: The database kinds `$TEACHER_DB` (and `teacher --db`) accept: Supabase's Postgres, or the
#: local SQLite file. Re-exported by `teacher.db.providers`.
DB_KINDS = ("supabase", "sqlite")
#: What `Settings.from_env()` uses when `$TEACHER_DB` is not set: production is Supabase.
DEFAULT_DB_KIND = "supabase"
#: The SQLAlchemy scheme a Supabase connection string is normalised to (psycopg 3).
POSTGRES_SCHEME = "postgresql+psycopg"
_POSTGRES_SCHEMES = ("postgresql", "postgres", POSTGRES_SCHEME)


class ConfigError(ValueError):
    """Required configuration is missing or invalid. The message says what to set and where; it
    never contains a secret (a connection string is never echoed back)."""


def default_db_kind() -> str:
    """`$TEACHER_DB`, else `DEFAULT_DB_KIND` (supabase). A variable that is set but blank, or
    names no known kind, is an error listing the choices (never a silent fallback)."""
    value = os.environ.get(ENV_DB)
    if value is None:
        return DEFAULT_DB_KIND
    kind = value.strip().lower()
    if not kind:
        raise ConfigError(
            f"${ENV_DB} is set but empty; set it to one of: {', '.join(DB_KINDS)} "
            f"(or unset it to use {DEFAULT_DB_KIND})"
        )
    return check_db_kind(kind, source=f"${ENV_DB}")


def check_db_kind(kind: str, *, source: str = "the database kind") -> str:
    if kind not in DB_KINDS:
        raise ConfigError(f"{source} is {kind!r}; choose one of: {', '.join(DB_KINDS)}")
    return kind


def supabase_db_url_from_env() -> str:
    """`$SUPABASE_DB_URL`, required in supabase mode; a missing or blank variable is an error
    that says where to put it and how to use the local database instead."""
    value = os.environ.get(ENV_SUPABASE_DB_URL, "").strip()
    if not value:
        raise ConfigError(
            f"${ENV_SUPABASE_DB_URL} is not set, and the database is supabase (the default). "
            f"Put the Supabase Postgres connection string (postgresql://...) after "
            f"{ENV_SUPABASE_DB_URL}= in .env, or set {ENV_DB}=sqlite (or pass --db sqlite) to "
            "use the local SQLite database."
        )
    return value


def normalise_postgres_url(url: str) -> str:
    """`url` with its scheme replaced by `postgresql+psycopg`; `postgresql://` and `postgres://`
    are accepted. Anything else is a ConfigError that names only the scheme (the string holds
    the password)."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in _POSTGRES_SCHEMES:
        shown = f"{scheme!r}" if sep else "no scheme"
        raise ConfigError(
            f"${ENV_SUPABASE_DB_URL} must be a Postgres connection string starting with "
            f"postgresql:// or postgres:// (it has {shown})"
        )
    normalised = f"{POSTGRES_SCHEME}://{rest}"
    try:
        parts = urlsplit(normalised)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise ConfigError(
            f"${ENV_SUPABASE_DB_URL} is not a valid connection string (its host or port "
            "cannot be read)"
        ) from None
    if not parts.hostname:
        raise ConfigError(f"${ENV_SUPABASE_DB_URL} names no host")
    return normalised


@dataclass(frozen=True)
class Settings:
    """Which database to use and where the files live.

    `db` is the database kind: "sqlite" (the file `root/teacher.db`) or "supabase" (Supabase's
    Postgres at `supabase_db_url`, a secret: it is left out of `repr` and never logged; use
    `teacher.db.providers.create_db_provider(settings).describe()` to name the database).

    Every path stored in the database is relative to `data_root`, where the files of that
    database live: `root` itself in sqlite mode (the layout Teacher always had), `root/supabase`
    in supabase mode, so the two databases never share or overwrite each other's files.

    A directly constructed `Settings(root=...)` is SQLite (what the tests use); the production
    default, supabase, applies only in `from_env()`.
    """

    root: Path
    db: str = "sqlite"
    supabase_db_url: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Normalise to an absolute, symlink-free path so relative-path maths is stable.
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())
        check_db_kind(self.db)
        if self.db == "supabase":
            if not self.supabase_db_url:
                raise ConfigError(
                    "Settings(db='supabase') needs supabase_db_url (from "
                    f"${ENV_SUPABASE_DB_URL})"
                )
            object.__setattr__(
                self, "supabase_db_url", normalise_postgres_url(self.supabase_db_url)
            )
        elif self.supabase_db_url is not None:
            raise ConfigError("supabase_db_url is only used with db='supabase'")

    @classmethod
    def from_env(cls, db: str | None = None) -> Settings:
        """`$TEACHER_HOME` (else the current working directory) as the root, the database kind
        from `$TEACHER_DB` (default supabase) and, in supabase mode, `$SUPABASE_DB_URL`
        (required). This is the only place the supabase default applies. `db` names the kind
        instead of `$TEACHER_DB` (`teacher db copy`, which uses both databases)."""
        home = os.environ.get(ENV_HOME)
        root = Path(home) if home else Path.cwd()
        db = default_db_kind() if db is None else check_db_kind(db)
        if db == "supabase":
            return cls(root=root, db=db, supabase_db_url=supabase_db_url_from_env())
        return cls(root=root, db=db)

    @property
    def data_root(self) -> Path:
        """Where this database's files live; every stored path is relative to it."""
        return self.root / "supabase" if self.db == "supabase" else self.root

    @property
    def db_path(self) -> Path:
        """The SQLite database file (sqlite mode only; an error in supabase mode)."""
        if self.db != "sqlite":
            raise ConfigError(f"db_path is the SQLite file; this Settings uses {self.db}")
        return self.root / "teacher.db"

    @property
    def sources_dir(self) -> Path:
        return self.data_root / "knowledge_sources"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_root / "artifacts"

    @property
    def derived_dir(self) -> Path:
        return self.data_root / "derived"

    @property
    def backups_dir(self) -> Path:
        return self.data_root / "backups"

    @property
    def db_url(self) -> str:
        """The SQLite database URL (sqlite mode only; an error in supabase mode, whose URL is a
        secret: the database provider builds the engine)."""
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


# -- the YouTube Data API -----------------------------------------------------------------

ENV_YOUTUBE_API_KEY = "YOUTUBE_API_KEY"


def youtube_api_key_from_env() -> str | None:
    """`$YOUTUBE_API_KEY`: the YouTube Data API v3 key that reads a video's title, channel, date
    and description when a YouTube video is added as a source (`teacher.youtube_sources`). The
    one place the variable is read. None when it is not set (the caller says how to set it); a
    variable that is set but blank is an error rather than being treated as unset."""
    value = os.environ.get(ENV_YOUTUBE_API_KEY)
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"${ENV_YOUTUBE_API_KEY} is set but empty; put the key after "
            f"{ENV_YOUTUBE_API_KEY}= in .env and restart the server"
        )
    return value.strip()


# -- the account gate ---------------------------------------------------------------------

ENV_PASSWORD = "TEACHER_PASSWORD"


def teacher_password_from_env() -> str | None:
    """`$TEACHER_PASSWORD`: with it set, `teacher serve` puts the account gate
    (`teacher.web.auth`) in front of every page and API route, and the value is the access code
    that registering an account needs (nobody signs in with it: everyone signs in with their own
    account). The one place the variable is read. None when it is not set (no gate, no accounts:
    the app is open, as on 127.0.0.1); a variable that is set but blank is a ConfigError rather
    than an open app. The value is used exactly as given (not stripped) and is never logged or
    echoed."""
    value = os.environ.get(ENV_PASSWORD)
    if value is None:
        return None
    if not value.strip():
        raise ConfigError(
            f"${ENV_PASSWORD} is set but empty; set it to the access code for registering "
            "accounts, or unset it to run without sign-in (only on 127.0.0.1)"
        )
    return value
