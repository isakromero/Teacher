"""Source discovery: find candidate sources for a topic or a concept, without ingesting any.

    finder = build_source_finder(ModelSettings.from_env())
    query = DiscoveryQuery("Diffie-Hellman key exchange", level="intro")
    # or: query = concept_query(settings, "diffie-hellman", "intro")
    result = find_sources(settings, finder, query)   # a DiscoveryResult; .to_dict() is the JSON

The pipeline (`service.SourceFinder.find`): providers in parallel (`providers`: Claude with web
search, and arXiv), merged and matched against the library (`registry`), each kept candidate
fetched without storing anything (`probe`), the readable ones scored by one Haiku call
(`triage`, the fetched text fenced as untrusted data by `untrusted`), then ranked. Failures are
loud: a failed provider is listed with its error next to the others; every provider failing,
or a triage reply that does not validate, raises a `DiscoveryError`. Every model call is
recorded in the cost ledger (`teacher.costs`; purpose `source_discovery`, or `exploration` inside
an exploration) and priced in the result too (`common.DiscoveryUsage`).

Surfaces: `teacher discover "<topic>"` (`teacher.cli`), `POST /api/sources/discover`
(`teacher.web.routers.discovery`) and the "Find sources" box on the Add page.
"""

from teacher.discovery.common import (
    DEFAULT_LEVEL,
    LEDGER_PURPOSE,
    LEVELS,
    AllProvidersFailedError,
    DiscoveryError,
    DiscoveryQuery,
    DiscoveryUnavailableError,
    DiscoveryUsage,
    ModelCall,
    ProviderError,
    TriageError,
    TriageUnavailableError,
)
from teacher.discovery.probe import Probe
from teacher.discovery.providers import (
    ArxivProvider,
    ProviderOutput,
    SourceProvider,
    WebSearchProvider,
)
from teacher.discovery.registry import FoundBy, LibraryCatalog, LibraryMatch, ProviderHit
from teacher.discovery.service import (
    Candidate,
    DiscoveryResult,
    NotKept,
    ProviderStatus,
    SourceFinder,
    TriageStatus,
    build_source_finder,
    concept_query,
    find_sources,
)
from teacher.discovery.triage import TriageItem, TriageOutcome, Triager, TriageScore

__all__ = [
    "DEFAULT_LEVEL",
    "LEDGER_PURPOSE",
    "LEVELS",
    "AllProvidersFailedError",
    "ArxivProvider",
    "Candidate",
    "DiscoveryError",
    "DiscoveryQuery",
    "DiscoveryResult",
    "DiscoveryUnavailableError",
    "DiscoveryUsage",
    "FoundBy",
    "LibraryCatalog",
    "LibraryMatch",
    "ModelCall",
    "NotKept",
    "Probe",
    "ProviderError",
    "ProviderHit",
    "ProviderOutput",
    "ProviderStatus",
    "SourceFinder",
    "SourceProvider",
    "TriageError",
    "TriageItem",
    "TriageOutcome",
    "TriageScore",
    "TriageStatus",
    "TriageUnavailableError",
    "Triager",
    "WebSearchProvider",
    "build_source_finder",
    "concept_query",
    "find_sources",
]
