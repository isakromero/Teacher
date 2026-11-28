"""Manager classes: plain Python objects doing the database and file I/O for each table group.

Every manager takes `(session, settings)`, flushes but never commits, and raises the exceptions
from `teacher.managers.errors` for expected failures.
"""

from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.artifacts import ArtifactManager
from teacher.managers.block_concepts import BlockConceptManager, ReplaceResult, TagSpec
from teacher.managers.block_responses import BlockProgress, BlockResponseManager
from teacher.managers.chat import (
    ChatBlockDraft,
    ChatBlockProgress,
    ChatManager,
    ThreadSummary,
)
from teacher.managers.concept_embeddings import ConceptEmbeddingManager
from teacher.managers.concept_judgements import ConceptJudgementManager
from teacher.managers.concept_relations import ConceptRelationManager
from teacher.managers.concept_standing import ConceptStanding, ConceptStandingManager
from teacher.managers.concepts import (
    ConceptManager,
    DroppedRelation,
    MergeResult,
    RetargetedRelation,
)
from teacher.managers.errors import (
    CycleError,
    DuplicateError,
    EmbeddingIndexError,
    IntegrityCheckError,
    Issue,
    ManagerError,
    NotFoundError,
    RelationConflictError,
    SourceInUseError,
    StaleEmbeddingsError,
    StateConflictError,
    VectorIndexError,
)
from teacher.managers.explorations import ExplorationManager
from teacher.managers.generation_runs import GenerationRunManager
from teacher.managers.glossary import GlossaryManager
from teacher.managers.highlights import HighlightManager
from teacher.managers.knowledge_sources import KnowledgeSourceManager
from teacher.managers.labs import LabManager, StepStatus
from teacher.managers.source_concepts import LinkSpec, SourceConceptManager
from teacher.managers.source_references import SourceReferenceManager
from teacher.managers.source_segments import SourceSegmentManager
from teacher.managers.study_history import (
    SessionPage,
    SessionSummary,
    StudyHistoryManager,
)
from teacher.managers.users import SignInError, UserManager

__all__ = [
    "ArtifactBlockManager",
    "ArtifactManager",
    "BlockConceptManager",
    "BlockProgress",
    "BlockResponseManager",
    "ChatBlockDraft",
    "ChatBlockProgress",
    "ChatManager",
    "ConceptEmbeddingManager",
    "ConceptJudgementManager",
    "ConceptManager",
    "ConceptRelationManager",
    "ConceptStanding",
    "ConceptStandingManager",
    "CycleError",
    "DroppedRelation",
    "DuplicateError",
    "EmbeddingIndexError",
    "ExplorationManager",
    "GenerationRunManager",
    "GlossaryManager",
    "HighlightManager",
    "IntegrityCheckError",
    "Issue",
    "KnowledgeSourceManager",
    "LabManager",
    "LinkSpec",
    "ManagerError",
    "MergeResult",
    "NotFoundError",
    "RelationConflictError",
    "ReplaceResult",
    "RetargetedRelation",
    "SessionPage",
    "SessionSummary",
    "SignInError",
    "SourceConceptManager",
    "SourceInUseError",
    "SourceReferenceManager",
    "SourceSegmentManager",
    "StaleEmbeddingsError",
    "StateConflictError",
    "StepStatus",
    "StudyHistoryManager",
    "TagSpec",
    "ThreadSummary",
    "UserManager",
    "VectorIndexError",
]
