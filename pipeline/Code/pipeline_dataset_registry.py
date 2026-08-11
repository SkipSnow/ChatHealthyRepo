# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""PipelineDatasetRegistry: immutable, fully validated view over one env's
`dataset_versions[]` config array.

The registry parses every raw dict into a frozen DatasetEntry and runs the
six structural invariants the JSON Schema cannot express. Every one of the
six invariant check sites first calls pipeline_fatal_recorder.record_fatal_
discrepancy(...) so a fatal registry construction leaves a record on the
pipeline cluster before the ChatHealthyException reaches the caller.

Invariants (each raises a distinct mode):
  1. dataset_registry_duplicate_source_name
  2. dataset_registry_duplicate_staging_name
  3. dataset_registry_duplicate_public_data_name
  4. dataset_registry_bundle_fetch_count
  5. dataset_registry_dangling_dependency
  6. dataset_registry_dependency_cycle

All lookup methods raise fatally on unknown names. No silent fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline_fatal_recorder import record_fatal_discrepancy

_log = ChatHealthyLoggingService()


@dataclass(frozen=True)
class DatasetEntry:
    source_name: str
    staging_db: str
    staging_coll_base: str
    public_data_db: str
    public_data_coll_base: str
    file_format: Optional[str] = None
    fetch: Optional[dict] = None
    bundled_with: Optional[str] = None
    archive_member: Optional[str] = None
    depends_on: Optional[tuple[str, ...]] = None

    @property
    def is_derived(self) -> bool:
        return (
            self.fetch is None
            and self.bundled_with is None
            and self.file_format is None
        )

    @property
    def is_fetched(self) -> bool:
        return self.fetch is not None

    @property
    def is_bundled(self) -> bool:
        return self.bundled_with is not None


def _split_db_coll(source_name: str, qualified: object, field_name: str) -> tuple[str, str]:
    if not isinstance(qualified, str):
        raise ChatHealthyException(
            mode="dataset_registry_field_wrong_type",
            message=(
                f"pipeline_dataset_registry[{source_name}]: {field_name} MUST be "
                f"a 'DB.coll' string, got {type(qualified).__name__}."
            ),
            source_name=source_name,
            field_name=field_name,
        )
    parts = qualified.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ChatHealthyException(
            mode="dataset_registry_field_malformed",
            message=(
                f"pipeline_dataset_registry[{source_name}]: {field_name} MUST be "
                f"'DB.coll' with a single dot separator; got {qualified!r}."
            ),
            source_name=source_name,
            field_name=field_name,
        )
    return parts[0], parts[1]


def _coerce_entry(raw: dict) -> DatasetEntry:
    source_name = raw.get("source_name")
    if not source_name or not isinstance(source_name, str):
        raise ChatHealthyException(
            mode="dataset_registry_missing_source_name",
            message=(
                "pipeline_dataset_registry: dataset_versions[] entry is missing "
                f"source_name or it is not a string: {raw!r}"
            ),
        )
    staging_db, staging_coll_base = _split_db_coll(
        source_name, raw.get("staging_name"), "staging_name"
    )
    public_db, public_coll_base = _split_db_coll(
        source_name, raw.get("public_data_name"), "public_data_name"
    )
    depends_on_raw = raw.get("depends_on")
    depends_on: Optional[tuple[str, ...]]
    if depends_on_raw is None:
        depends_on = None
    else:
        if not isinstance(depends_on_raw, list) or not depends_on_raw:
            raise ChatHealthyException(
                mode="dataset_registry_depends_on_wrong_shape",
                message=(
                    f"pipeline_dataset_registry[{source_name}]: depends_on MUST be "
                    f"a non-empty list of strings; got {depends_on_raw!r}."
                ),
                source_name=source_name,
            )
        for dep in depends_on_raw:
            if not isinstance(dep, str) or not dep:
                raise ChatHealthyException(
                    mode="dataset_registry_depends_on_wrong_shape",
                    message=(
                        f"pipeline_dataset_registry[{source_name}]: every "
                        f"depends_on member must be a non-empty string; got {dep!r}."
                    ),
                    source_name=source_name,
                )
        depends_on = tuple(depends_on_raw)
    return DatasetEntry(
        source_name=source_name,
        staging_db=staging_db,
        staging_coll_base=staging_coll_base,
        public_data_db=public_db,
        public_data_coll_base=public_coll_base,
        file_format=raw.get("file_format"),
        fetch=raw.get("fetch"),
        bundled_with=raw.get("bundled_with"),
        archive_member=raw.get("archive_member"),
        depends_on=depends_on,
    )


class PipelineDatasetRegistry:
    """Immutable, fully validated view over one env's dataset_versions[]."""

    _STEP = "pipeline_dataset_registry.__init__"

    def __init__(self, config: dict, data_version: int, pipeline_mongo) -> None:
        self._pipeline_mongo = pipeline_mongo
        if not isinstance(config, dict):
            self._fatal(ChatHealthyException(
                mode="dataset_registry_config_not_dict",
                message=(
                    "pipeline_dataset_registry: config must be the env record "
                    f"dict returned by load_pipeline_config; got {type(config).__name__}."
                ),
            ))
        raw_entries = config.get("dataset_versions")
        if not isinstance(raw_entries, list) or not raw_entries:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_dataset_versions_missing",
                message=(
                    "pipeline_dataset_registry: config['dataset_versions'] must "
                    "be a non-empty list."
                ),
            ))
        if not isinstance(data_version, int) or data_version < 1:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_bad_data_version",
                message=(
                    "pipeline_dataset_registry: data_version must be a positive "
                    f"integer; got {data_version!r}."
                ),
            ))
        self._data_version: int = data_version

        try:
            entries: list[DatasetEntry] = [_coerce_entry(r) for r in raw_entries]
        except ChatHealthyException as exc:
            self._record_only(exc)
            raise

        # Invariant 1: source_name uniqueness across the array.
        seen_names: dict[str, int] = {}
        for idx, e in enumerate(entries):
            if e.source_name in seen_names:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_duplicate_source_name",
                    message=(
                        f"pipeline_dataset_registry: source_name {e.source_name!r} "
                        f"appears at both index {seen_names[e.source_name]} and "
                        f"index {idx}."
                    ),
                    source_name=e.source_name,
                    first_index=seen_names[e.source_name],
                    duplicate_index=idx,
                ))
            seen_names[e.source_name] = idx

        # Invariant 2: staging (db, coll_base) uniqueness across the array.
        seen_staging: dict[tuple[str, str], str] = {}
        for e in entries:
            key = (e.staging_db, e.staging_coll_base)
            if key in seen_staging:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_duplicate_staging_name",
                    message=(
                        f"pipeline_dataset_registry: staging_name "
                        f"{e.staging_db}.{e.staging_coll_base} is used by both "
                        f"{seen_staging[key]!r} and {e.source_name!r}."
                    ),
                    staging_name=f"{e.staging_db}.{e.staging_coll_base}",
                    first_owner=seen_staging[key],
                    duplicate_owner=e.source_name,
                ))
            seen_staging[key] = e.source_name

        # Invariant 3: public_data (db, coll_base) uniqueness across the array.
        seen_public: dict[tuple[str, str], str] = {}
        for e in entries:
            key = (e.public_data_db, e.public_data_coll_base)
            if key in seen_public:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_duplicate_public_data_name",
                    message=(
                        f"pipeline_dataset_registry: public_data_name "
                        f"{e.public_data_db}.{e.public_data_coll_base} is used by "
                        f"both {seen_public[key]!r} and {e.source_name!r}."
                    ),
                    public_data_name=f"{e.public_data_db}.{e.public_data_coll_base}",
                    first_owner=seen_public[key],
                    duplicate_owner=e.source_name,
                ))
            seen_public[key] = e.source_name

        # Invariant 4: each bundled_with group has exactly one fetcher.
        bundle_members: dict[str, list[DatasetEntry]] = {}
        for e in entries:
            if e.bundled_with is not None:
                bundle_members.setdefault(e.bundled_with, []).append(e)
        bundle_fetchers: dict[str, str] = {}
        for bundle_name, members in bundle_members.items():
            fetchers = [m for m in members if m.fetch is not None]
            if len(fetchers) != 1:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_bundle_fetch_count",
                    message=(
                        f"pipeline_dataset_registry: bundle {bundle_name!r} has "
                        f"{len(fetchers)} fetch owners "
                        f"({[m.source_name for m in fetchers]}); exactly one "
                        f"member of a bundle MUST own the fetch."
                    ),
                    bundle_name=bundle_name,
                    members=[m.source_name for m in members],
                    fetchers=[m.source_name for m in fetchers],
                ))
            bundle_fetchers[bundle_name] = fetchers[0].source_name

        # Invariant 5: every depends_on name resolves to a known entry.
        for e in entries:
            if e.depends_on is None:
                continue
            for dep in e.depends_on:
                if dep not in seen_names:
                    self._fatal(ChatHealthyException(
                        mode="dataset_registry_dangling_dependency",
                        message=(
                            f"pipeline_dataset_registry[{e.source_name}]: "
                            f"depends_on references unknown source_name {dep!r}. "
                            f"Known: {sorted(seen_names.keys())}."
                        ),
                        source_name=e.source_name,
                        missing_dependency=dep,
                    ))

        # Invariant 6: depends_on graph is acyclic; else name the cycle path.
        cycle_path = _find_cycle(entries)
        if cycle_path is not None:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_dependency_cycle",
                message=(
                    "pipeline_dataset_registry: depends_on graph contains a "
                    "cycle: " + " -> ".join(cycle_path)
                ),
                cycle=list(cycle_path),
            ))

        self._entries: tuple[DatasetEntry, ...] = tuple(entries)
        self._by_name: dict[str, DatasetEntry] = {e.source_name: e for e in entries}
        self._bundle_fetchers: dict[str, str] = dict(bundle_fetchers)
        self._bundle_members: dict[str, tuple[str, ...]] = {
            b: tuple(m.source_name for m in ms)
            for b, ms in bundle_members.items()
        }
        self._topological_order: tuple[DatasetEntry, ...] = _topological_order(entries)

    # ------------------------------------------------------------------
    # fatal helpers
    # ------------------------------------------------------------------

    def _fatal(self, exc: ChatHealthyException):
        record_fatal_discrepancy(
            self._pipeline_mongo, run_id=None, step=self._STEP, exc=exc,
        )
        raise exc

    def _record_only(self, exc: ChatHealthyException) -> None:
        record_fatal_discrepancy(
            self._pipeline_mongo, run_id=None, step=self._STEP, exc=exc,
        )

    # ------------------------------------------------------------------
    # lookup surface: every unknown-name path raises fatally
    # ------------------------------------------------------------------

    def entries(self) -> list[DatasetEntry]:
        return list(self._entries)

    def by_source_name(self, name: str) -> DatasetEntry:
        e = self._by_name.get(name)
        if e is None:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_unknown_source_name",
                message=(
                    f"pipeline_dataset_registry: no entry for source_name "
                    f"{name!r}. Known: {sorted(self._by_name.keys())}."
                ),
                source_name=name,
            ))
        return e

    def dependencies_of(self, name: str) -> list[DatasetEntry]:
        entry = self.by_source_name(name)
        if entry.depends_on is None:
            return []
        return [self._by_name[d] for d in entry.depends_on]

    def fetcher_of_bundle(self, bundle_name: str) -> DatasetEntry:
        fetcher_name = self._bundle_fetchers.get(bundle_name)
        if fetcher_name is None:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_unknown_bundle",
                message=(
                    f"pipeline_dataset_registry: no bundle {bundle_name!r}. "
                    f"Known bundles: {sorted(self._bundle_fetchers.keys())}."
                ),
                bundle_name=bundle_name,
            ))
        return self._by_name[fetcher_name]

    def bundle_members(self, bundle_name: str) -> list[DatasetEntry]:
        names = self._bundle_members.get(bundle_name)
        if names is None:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_unknown_bundle",
                message=(
                    f"pipeline_dataset_registry: no bundle {bundle_name!r}. "
                    f"Known bundles: {sorted(self._bundle_members.keys())}."
                ),
                bundle_name=bundle_name,
            ))
        return [self._by_name[n] for n in names]

    def topological_order(self) -> list[DatasetEntry]:
        return list(self._topological_order)

    def staging_collection_name(self, name: str) -> str:
        e = self.by_source_name(name)
        return f"{e.staging_coll_base}_v_{self._data_version}"

    def public_data_collection_name(self, name: str) -> str:
        e = self.by_source_name(name)
        return f"{e.public_data_coll_base}_v_{self._data_version}"

    def is_fetched(self, name: str) -> bool:
        return self.by_source_name(name).is_fetched

    def is_bundled(self, name: str) -> bool:
        return self.by_source_name(name).is_bundled

    def is_derived(self, name: str) -> bool:
        return self.by_source_name(name).is_derived

    def resolve_source_url(self, name: str) -> str:
        entry = self.by_source_name(name)
        if entry.fetch is None:
            self._fatal(ChatHealthyException(
                mode="dataset_registry_resolve_url_not_a_fetcher",
                message=(
                    f"pipeline_dataset_registry: resolve_source_url({name!r}) is "
                    f"invalid; entry has no fetch block. Fetchers only."
                ),
                source_name=name,
            ))
        source_url = entry.fetch.get("source_url")
        if isinstance(source_url, str) and source_url:
            return source_url
        if isinstance(source_url, dict):
            page_url = source_url.get("page_url")
            instructions = source_url.get("instructions")
            if not isinstance(page_url, str) or not page_url:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_discovery_missing_page_url",
                    message=(
                        f"pipeline_dataset_registry[{name}]: source_url is a "
                        f"discovery object but page_url is missing or empty."
                    ),
                    source_name=name,
                ))
            if not isinstance(instructions, str) or not instructions:
                self._fatal(ChatHealthyException(
                    mode="dataset_registry_discovery_missing_instructions",
                    message=(
                        f"pipeline_dataset_registry[{name}]: source_url is a "
                        f"discovery object but instructions is missing or empty."
                    ),
                    source_name=name,
                ))
            from source_url_discovery import find_latest_data_url
            return find_latest_data_url(
                source_name=name,
                page_url=page_url,
                instructions=instructions,
            )
        self._fatal(ChatHealthyException(
            mode="dataset_registry_source_url_wrong_shape",
            message=(
                f"pipeline_dataset_registry[{name}]: fetch.source_url must be a "
                f"non-empty string or a discovery dict; got {source_url!r}."
            ),
            source_name=name,
        ))


def _find_cycle(entries: list[DatasetEntry]) -> Optional[tuple[str, ...]]:
    """Three color DFS. Returns the offending cycle path
    (start ... start) or None if the depends_on graph is acyclic.
    A node that references itself is reported as (name, name).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {e.source_name: WHITE for e in entries}
    parents: dict[str, Optional[str]] = {e.source_name: None for e in entries}
    deps: dict[str, tuple[str, ...]] = {
        e.source_name: (e.depends_on or ()) for e in entries
    }

    def _reconstruct(seed_name: str, cycle_start: str) -> tuple[str, ...]:
        path = [seed_name]
        walker = parents[seed_name]
        while walker is not None and walker != cycle_start:
            path.append(walker)
            walker = parents[walker]
        path.append(cycle_start)
        path.reverse()
        path.append(cycle_start)
        return tuple(path)

    def _dfs(node: str) -> Optional[tuple[str, ...]]:
        color[node] = GRAY
        for dep in deps.get(node, ()):
            if color.get(dep, WHITE) == GRAY:
                if node == dep:
                    return (dep, dep)
                return _reconstruct(node, dep)
            if color.get(dep, WHITE) == WHITE:
                parents[dep] = node
                sub = _dfs(dep)
                if sub is not None:
                    return sub
        color[node] = BLACK
        return None

    for e in entries:
        if color[e.source_name] == WHITE:
            found = _dfs(e.source_name)
            if found is not None:
                return found
    return None


def _topological_order(entries: list[DatasetEntry]) -> tuple[DatasetEntry, ...]:
    """Kahn's algorithm on the depends_on graph. Deps first order. Caller
    has already run _find_cycle so a cycle here is a defect."""
    by_name = {e.source_name: e for e in entries}
    indeg: dict[str, int] = {e.source_name: 0 for e in entries}
    reverse: dict[str, list[str]] = {e.source_name: [] for e in entries}
    for e in entries:
        for dep in (e.depends_on or ()):
            indeg[e.source_name] += 1
            reverse[dep].append(e.source_name)
    ready = [name for name in indeg if indeg[name] == 0]
    ready.sort(key=lambda n: next(i for i, e in enumerate(entries) if e.source_name == n))
    ordered: list[DatasetEntry] = []
    while ready:
        node = ready.pop(0)
        ordered.append(by_name[node])
        for downstream in reverse[node]:
            indeg[downstream] -= 1
            if indeg[downstream] == 0:
                ready.append(downstream)
        ready.sort(key=lambda n: next(i for i, e in enumerate(entries) if e.source_name == n))
    if len(ordered) != len(entries):
        raise ChatHealthyException(
            mode="dataset_registry_topological_incomplete",
            message=(
                "pipeline_dataset_registry: topological order incomplete; a "
                "cycle slipped past cycle detection. This is a defect."
            ),
        )
    return tuple(ordered)
