from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import yaml


IMPLEMENTATION_STATUSES = frozenset({"implemented", "planned"})
EVIDENCE_STATUSES = frozenset({"tested_current", "tested_superseded", "not_tested"})
READINESS_STATUSES = frozenset({"runnable", "blocked"})
CONFIG_BINDINGS = frozenset({"strategy_space", "derivative_grid", "registry_only"})
INSTRUMENT_CLASSES = frozenset(
    {
        "a_share_equity",
        "cffex_index_futures",
        "cffex_index_options",
        "cffex_index_derivatives",
    }
)
INSTRUMENT_ROOTS_BY_CLASS = {
    "cffex_index_futures": frozenset({"IF", "IH", "IC", "IM"}),
    "cffex_index_options": frozenset({"IO", "HO", "MO"}),
    "cffex_index_derivatives": frozenset(
        {"IF", "IH", "IC", "IM", "IO", "HO", "MO"}
    ),
}
DATA_TIERS = frozenset(
    {
        "proxy_research",
        "free_real",
        "free_real_derived_limits",
        "official_exchange_daily",
        "strict_real",
    }
)
POSITION_FAMILIES = frozenset(
    {
        "cross_sectional_long_only",
        "stateful_long_only",
        "regime_gated_long_only",
        "event_driven_long_only",
        "whole_contract_overlay",
        "long_option_overlay",
        "futures_plus_long_option_overlay",
    }
)
SIZING_FAMILIES = frozenset(
    {
        "equal_weight",
        "concentrated_equal_weight",
        "equal_weight_with_cash_gate",
        "concentrated_equal_weight_with_cash_gate",
        "configurable_risk_weighting",
        "atr_risk_weighted",
        "whole_contract_budget",
        "premium_budget",
        "whole_contract_plus_premium_budget",
    }
)
SIZING_MODES = frozenset({"equal", "inverse_volatility", "atr_risk", "rank"})
SIGNAL_KERNELS = frozenset(
    {
        "stock_momentum",
        "stock_breakout",
        "smallcap_factor",
        "regime_selector",
        "short_term_reversal",
        "low_volatility",
        "residual_momentum",
        "volume_price_shock",
        "value_composite",
        "stateful_trend",
        "volatility_contraction",
        "regime_contraction",
        "gap_intraday",
        "momentum_acceleration",
        "signed_flow_accumulation",
        "beta_residual_shock_reversal",
        "idiosyncratic_strength",
        "risk_weighting_overlay",
        "post_limit_release",
        "cffex_index_overlay",
        "cffex_long_convexity",
        "cffex_combined_derivative_overlay",
        "cffex_dynamic_direction_overlay",
    }
)
NEXT_GATES = frozenset(
    {
        "corrected_panel_rerun",
        "corrected_panel_development_funnel",
        "weighted_target_order_interface",
        "post_limit_fill_clock",
        "whole_contract_event_loop",
        "official_option_chain_event_loop",
        "derivative_screen",
        "combined_overlay_screen",
        "derivative_timing_screen",
    }
)
PRIORITIES = frozenset({"critical", "high", "medium", "low"})
RELATION_TYPES = frozenset({"identical_signal_kernel_neighborhood"})

_FAMILY_ID_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


class RegistryValidationError(ValueError):
    """Raised when the search registry cannot be audited safely."""


@dataclass(frozen=True)
class SearchFamilyRecord:
    family_id: str
    instrument_class: str
    instrument_roots: tuple[str, ...]
    signal_kernel: str
    position_family: str
    sizing_family: str
    sizing_modes: tuple[str, ...]
    data_tier: str
    config_path: str
    config_binding: str
    relation_group: str
    implementation_status: str
    evidence_status: str
    readiness_status: str
    blocking_dependency: str | None
    next_gate: str
    priority: str
    estimated_config_budget: int
    relation_note: str = ""


@dataclass(frozen=True)
class RelationGroupAudit:
    relation_type: str
    members: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class SearchConfigFamily:
    family_id: str
    config_path: str
    signal_kernel: str
    data_tier: str
    config_budget: int
    sizing_modes: tuple[str, ...]


@dataclass(frozen=True)
class SearchRegistry:
    version: int
    registry_id: str
    search_config_glob: str
    families: tuple[SearchFamilyRecord, ...]
    relation_group_audit: Mapping[str, RelationGroupAudit]
    registry_path: Path
    repo_root: Path
    derivative_config_glob: str = ""

    @property
    def config_backed_families(self) -> tuple[SearchFamilyRecord, ...]:
        return tuple(
            family for family in self.families if family.config_binding == "strategy_space"
        )

    @property
    def planned_families(self) -> tuple[SearchFamilyRecord, ...]:
        return tuple(
            family for family in self.families if family.implementation_status == "planned"
        )

    @property
    def derivative_grid_families(self) -> tuple[SearchFamilyRecord, ...]:
        return tuple(
            family
            for family in self.families
            if family.config_binding == "derivative_grid"
        )


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise RegistryValidationError(f"cannot read {label} {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RegistryValidationError(f"invalid safe YAML in {label} {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryValidationError(f"{label} must be a YAML mapping: {path}")
    return raw


def load_search_registry(path: str | Path) -> dict[str, Any]:
    """Load a registry with PyYAML's safe loader."""

    return _load_yaml_mapping(Path(path).resolve(), "search registry")


def _required_string(raw: Mapping[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _enum_value(
    raw: Mapping[str, Any],
    key: str,
    allowed: frozenset[str],
    context: str,
) -> str:
    value = _required_string(raw, key, context)
    if value not in allowed:
        raise RegistryValidationError(
            f"{context}.{key} has unsupported value {value!r}; expected one of {sorted(allowed)}"
        )
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RegistryValidationError(f"{context} must be a positive integer")
    return value


def _string_tuple(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RegistryValidationError(f"{context} must be a list")
    normalized = tuple(
        _required_string({"value": item}, "value", context) for item in value
    )
    if len(normalized) != len(set(normalized)):
        raise RegistryValidationError(f"{context} contains duplicates")
    return normalized


def _parse_family(raw: Any, index: int) -> SearchFamilyRecord:
    context = f"families[{index}]"
    if not isinstance(raw, dict):
        raise RegistryValidationError(f"{context} must be a mapping")
    family_id = _required_string(raw, "family_id", context)
    if not _FAMILY_ID_RE.fullmatch(family_id):
        raise RegistryValidationError(f"{context}.family_id is not a stable slug: {family_id!r}")

    blocker = raw.get("blocking_dependency")
    if blocker is not None:
        if not isinstance(blocker, str):
            raise RegistryValidationError(f"{context}.blocking_dependency must be a string or null")
        blocker = blocker.strip() or None
    relation_note = raw.get("relation_note", "")
    if not isinstance(relation_note, str):
        raise RegistryValidationError(f"{context}.relation_note must be a string")

    record = SearchFamilyRecord(
        family_id=family_id,
        instrument_class=_enum_value(
            raw, "instrument_class", INSTRUMENT_CLASSES, context
        ),
        instrument_roots=_string_tuple(raw.get("instrument_roots"), f"{context}.instrument_roots"),
        signal_kernel=_enum_value(raw, "signal_kernel", SIGNAL_KERNELS, context),
        position_family=_enum_value(raw, "position_family", POSITION_FAMILIES, context),
        sizing_family=_enum_value(raw, "sizing_family", SIZING_FAMILIES, context),
        sizing_modes=_string_tuple(raw.get("sizing_modes"), f"{context}.sizing_modes"),
        data_tier=_enum_value(raw, "data_tier", DATA_TIERS, context),
        config_path=_required_string(raw, "config_path", context),
        config_binding=_enum_value(raw, "config_binding", CONFIG_BINDINGS, context),
        relation_group=_required_string(raw, "relation_group", context),
        implementation_status=_enum_value(
            raw, "implementation_status", IMPLEMENTATION_STATUSES, context
        ),
        evidence_status=_enum_value(raw, "evidence_status", EVIDENCE_STATUSES, context),
        readiness_status=_enum_value(raw, "readiness_status", READINESS_STATUSES, context),
        blocking_dependency=blocker,
        next_gate=_enum_value(raw, "next_gate", NEXT_GATES, context),
        priority=_enum_value(raw, "priority", PRIORITIES, context),
        estimated_config_budget=_positive_int(
            raw.get("estimated_config_budget"), f"{context}.estimated_config_budget"
        ),
        relation_note=relation_note.strip(),
    )

    expected_roots = INSTRUMENT_ROOTS_BY_CLASS.get(record.instrument_class, frozenset())
    if set(record.instrument_roots) != set(expected_roots):
        raise RegistryValidationError(
            f"{context}.instrument_roots must be exactly {sorted(expected_roots)} "
            f"for {record.instrument_class}"
        )
    unsupported_sizing_modes = sorted(set(record.sizing_modes) - set(SIZING_MODES))
    if unsupported_sizing_modes:
        raise RegistryValidationError(
            f"{context}.sizing_modes has unsupported values: {unsupported_sizing_modes}"
        )
    if record.sizing_family == "configurable_risk_weighting" and not record.sizing_modes:
        raise RegistryValidationError(
            f"{context}.sizing_modes is required for configurable_risk_weighting"
        )
    if record.sizing_family != "configurable_risk_weighting" and record.sizing_modes:
        raise RegistryValidationError(
            f"{context}.sizing_modes is only valid for configurable_risk_weighting"
        )

    if record.implementation_status == "planned":
        if record.config_binding != "registry_only":
            raise RegistryValidationError(
                f"planned family {family_id} must use config_binding=registry_only"
            )
        if record.evidence_status != "not_tested":
            raise RegistryValidationError(
                f"planned family {family_id} cannot claim tested evidence"
            )
        if record.readiness_status != "blocked":
            raise RegistryValidationError(f"planned family {family_id} must be blocked")
        if not record.blocking_dependency:
            raise RegistryValidationError(
                f"planned family {family_id} requires a non-empty blocking_dependency"
            )
    elif record.config_binding not in {"strategy_space", "derivative_grid"}:
        raise RegistryValidationError(
            f"implemented family {family_id} must use a code-backed config_binding"
        )

    if record.evidence_status.startswith("tested_") and record.implementation_status != "implemented":
        raise RegistryValidationError(
            f"tested family {family_id} must have implementation_status=implemented"
        )
    if record.readiness_status == "blocked" and not record.blocking_dependency:
        raise RegistryValidationError(
            f"blocked family {family_id} requires a non-empty blocking_dependency"
        )
    if record.readiness_status == "runnable" and record.blocking_dependency:
        raise RegistryValidationError(
            f"runnable family {family_id} must not declare a blocking_dependency"
        )
    return record


def _parse_relation_audits(raw: Any) -> dict[str, RelationGroupAudit]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RegistryValidationError("relation_group_audit must be a mapping")
    audits: dict[str, RelationGroupAudit] = {}
    for group, value in raw.items():
        context = f"relation_group_audit.{group}"
        if not isinstance(group, str) or not group.strip():
            raise RegistryValidationError("relation_group_audit keys must be non-empty strings")
        if not isinstance(value, dict):
            raise RegistryValidationError(f"{context} must be a mapping")
        relation_type = _enum_value(value, "relation_type", RELATION_TYPES, context)
        members = value.get("members")
        if not isinstance(members, list) or len(members) < 2:
            raise RegistryValidationError(f"{context}.members must contain at least two families")
        normalized = tuple(
            _required_string({"member": member}, "member", f"{context}.members")
            for member in members
        )
        if len(normalized) != len(set(normalized)):
            raise RegistryValidationError(f"{context}.members contains duplicates")
        audits[group.strip()] = RelationGroupAudit(
            relation_type=relation_type,
            members=normalized,
            rationale=_required_string(value, "rationale", context),
        )
    return audits


def _resolve_repo_file(repo_root: Path, raw_path: str, context: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise RegistryValidationError(f"{context} must be a repo-relative path: {raw_path}")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise RegistryValidationError(f"{context} escapes the repository: {raw_path}") from exc
    if not resolved.is_file():
        raise RegistryValidationError(f"{context} does not exist: {raw_path}")
    return resolved


def discover_phase3_search_families(
    repo_root: str | Path,
    search_config_glob: str = "config/phase3_*search.yaml",
) -> dict[str, SearchConfigFamily]:
    """Discover each strategy-space family represented by current Phase 3 configs."""

    root = Path(repo_root).resolve()
    pattern = Path(search_config_glob)
    if pattern.is_absolute() or ".." in pattern.parts:
        raise RegistryValidationError("search_config_glob must be repo-relative")
    config_paths = sorted(path.resolve() for path in root.glob(search_config_glob) if path.is_file())
    if not config_paths:
        raise RegistryValidationError(
            f"search_config_glob matched no files: {search_config_glob}"
        )

    discovered: dict[str, SearchConfigFamily] = {}
    for path in config_paths:
        relative_path = path.relative_to(root).as_posix()
        config = _load_yaml_mapping(path, "Phase 3 search config")
        spaces = config.get("strategy_spaces")
        if not isinstance(spaces, dict) or not spaces:
            raise RegistryValidationError(
                f"Phase 3 search config requires non-empty strategy_spaces: {relative_path}"
            )
        sampling = config.get("sampling", {})
        if not isinstance(sampling, dict):
            raise RegistryValidationError(f"sampling must be a mapping: {relative_path}")
        default_budget = sampling.get("budget_per_family", 0)
        for family_id, value in spaces.items():
            context = f"{relative_path}.strategy_spaces.{family_id}"
            if not isinstance(family_id, str) or not family_id.strip():
                raise RegistryValidationError(f"{context} has an invalid family id")
            if family_id in discovered:
                previous = discovered[family_id].config_path
                raise RegistryValidationError(
                    f"search family {family_id} occurs in multiple configs: {previous}, {relative_path}"
                )
            if not isinstance(value, dict):
                raise RegistryValidationError(f"{context} must be a mapping")
            kind = _required_string(value, "kind", context)
            fixed = value.get("fixed", {})
            if not isinstance(fixed, dict):
                raise RegistryValidationError(f"{context}.fixed must be a mapping")
            parameters = value.get("parameters", {})
            if not isinstance(parameters, dict):
                raise RegistryValidationError(f"{context}.parameters must be a mapping")
            data_tier = _enum_value(fixed, "data_tier", DATA_TIERS, f"{context}.fixed")
            budget = _positive_int(value.get("budget", default_budget), f"{context}.budget")
            sizing_modes = _string_tuple(
                parameters.get("weighting"), f"{context}.parameters.weighting"
            )
            unsupported_sizing_modes = sorted(set(sizing_modes) - set(SIZING_MODES))
            if unsupported_sizing_modes:
                raise RegistryValidationError(
                    f"{context}.parameters.weighting has unsupported values: "
                    f"{unsupported_sizing_modes}"
                )
            discovered[family_id] = SearchConfigFamily(
                family_id=family_id,
                config_path=relative_path,
                signal_kernel=kind.removeprefix("real_"),
                data_tier=data_tier,
                config_budget=budget,
                sizing_modes=sizing_modes,
            )
    return discovered


_DERIVATIVE_FAMILY_META = {
    "P32_cffex_whole_lot_futures_overlay": "cffex_index_overlay",
    "P33_cffex_long_option_convexity_budget": "cffex_long_convexity",
    "P34_cffex_combined_derivative_overlay": "cffex_combined_derivative_overlay",
    "P35_cffex_dynamic_direction_overlay": "cffex_dynamic_direction_overlay",
}


def discover_phase3_derivative_families(
    repo_root: str | Path,
    derivative_config_glob: str,
) -> dict[str, SearchConfigFamily]:
    """Expand derivative grids and count each implemented semantic family."""

    from .phase3_overlay_factory import load_overlay_search_space

    root = Path(repo_root).resolve()
    pattern = Path(derivative_config_glob)
    if pattern.is_absolute() or ".." in pattern.parts:
        raise RegistryValidationError("derivative_config_glob must be repo-relative")
    config_paths = sorted(
        path.resolve()
        for path in root.glob(derivative_config_glob)
        if path.is_file()
    )
    if not config_paths:
        raise RegistryValidationError(
            f"derivative_config_glob matched no files: {derivative_config_glob}"
        )

    discovered: dict[str, SearchConfigFamily] = {}
    for path in config_paths:
        relative_path = path.relative_to(root).as_posix()
        try:
            space = load_overlay_search_space(path)
        except (OSError, ValueError) as exc:
            raise RegistryValidationError(
                f"invalid Phase 3 derivative grid {relative_path}: {exc}"
            ) from exc
        counts: Counter[str] = Counter()
        for spec in space.specs:
            if spec.composition == "futures_only":
                family_id = (
                    "P35_cffex_dynamic_direction_overlay"
                    if spec.direction_rule is not None
                    else "P32_cffex_whole_lot_futures_overlay"
                )
            elif spec.composition == "long_option_only":
                family_id = "P33_cffex_long_option_convexity_budget"
            else:
                family_id = "P34_cffex_combined_derivative_overlay"
            counts[family_id] += 1
        for family_id, budget in counts.items():
            if family_id in discovered:
                previous = discovered[family_id].config_path
                raise RegistryValidationError(
                    f"derivative family {family_id} occurs in multiple grids: "
                    f"{previous}, {relative_path}"
                )
            discovered[family_id] = SearchConfigFamily(
                family_id=family_id,
                config_path=relative_path,
                signal_kernel=_DERIVATIVE_FAMILY_META[family_id],
                data_tier="official_exchange_daily",
                config_budget=int(budget),
                sizing_modes=(),
            )
    return discovered


def _validate_relation_groups(
    families: Iterable[SearchFamilyRecord],
    audits: Mapping[str, RelationGroupAudit],
) -> None:
    grouped: dict[str, list[SearchFamilyRecord]] = defaultdict(list)
    for family in families:
        grouped[family.relation_group].append(family)
    repeated = {group: rows for group, rows in grouped.items() if len(rows) > 1}
    missing_audits = sorted(set(repeated) - set(audits))
    unexpected_audits = sorted(set(audits) - set(repeated))
    if missing_audits:
        raise RegistryValidationError(
            f"repeated relation_group values require an audit entry: {missing_audits}"
        )
    if unexpected_audits:
        raise RegistryValidationError(
            f"relation_group_audit entries must describe repeated groups: {unexpected_audits}"
        )
    for group, rows in repeated.items():
        audit = audits[group]
        actual_members = {row.family_id for row in rows}
        if actual_members != set(audit.members):
            raise RegistryValidationError(
                f"relation group {group} members differ from audit: "
                f"actual={sorted(actual_members)}, audited={sorted(audit.members)}"
            )
        kernels = {row.signal_kernel for row in rows}
        instruments = {row.instrument_class for row in rows}
        if len(kernels) != 1 or len(instruments) != 1:
            raise RegistryValidationError(
                f"identical-kernel relation group {group} must share one signal kernel and instrument class"
            )


def validate_search_registry(
    registry_path: str | Path,
    repo_root: str | Path | None = None,
) -> SearchRegistry:
    """Validate registry schema, status semantics, config coverage, and budgets."""

    path = Path(registry_path).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else path.parent.parent.resolve()
    raw = _load_yaml_mapping(path, "search registry")
    version = raw.get("version")
    if version != 1:
        raise RegistryValidationError("search registry version must be 1")
    registry_id = _required_string(raw, "registry_id", "registry")
    search_config_glob = _required_string(raw, "search_config_glob", "registry")
    derivative_config_glob_value = raw.get("derivative_config_glob", "")
    if not isinstance(derivative_config_glob_value, str):
        raise RegistryValidationError(
            "registry.derivative_config_glob must be a string"
        )
    derivative_config_glob = derivative_config_glob_value.strip()
    raw_families = raw.get("families")
    if not isinstance(raw_families, list) or not raw_families:
        raise RegistryValidationError("registry.families must be a non-empty list")
    families = tuple(_parse_family(value, index) for index, value in enumerate(raw_families))
    duplicates = sorted(
        family_id
        for family_id, count in Counter(family.family_id for family in families).items()
        if count > 1
    )
    if duplicates:
        raise RegistryValidationError(f"duplicate registry family_id values: {duplicates}")

    registry_resolved = path.resolve()
    for family in families:
        config_resolved = _resolve_repo_file(
            root, family.config_path, f"family {family.family_id}.config_path"
        )
        if family.config_binding == "registry_only" and config_resolved != registry_resolved:
            raise RegistryValidationError(
                f"registry-only family {family.family_id} must point config_path to the registry"
            )

    has_derivative_grids = any(
        family.config_binding == "derivative_grid" for family in families
    )
    if has_derivative_grids and not derivative_config_glob:
        raise RegistryValidationError(
            "implemented derivative-grid families require derivative_config_glob"
        )

    discovered = discover_phase3_search_families(root, search_config_glob)
    config_backed = {
        family.family_id: family
        for family in families
        if family.config_binding == "strategy_space"
    }
    missing = sorted(set(discovered) - set(config_backed))
    extra = sorted(set(config_backed) - set(discovered))
    if missing:
        raise RegistryValidationError(
            f"Phase 3 strategy-space families missing from registry: {missing}"
        )
    if extra:
        raise RegistryValidationError(
            f"config-backed registry families absent from Phase 3 search configs: {extra}"
        )
    for family_id, config_family in discovered.items():
        registered = config_backed[family_id]
        if registered.config_path != config_family.config_path:
            raise RegistryValidationError(
                f"family {family_id} config_path mismatch: "
                f"registry={registered.config_path}, config={config_family.config_path}"
            )
        if registered.signal_kernel != config_family.signal_kernel:
            raise RegistryValidationError(
                f"family {family_id} signal_kernel mismatch: "
                f"registry={registered.signal_kernel}, config={config_family.signal_kernel}"
            )
        if registered.data_tier != config_family.data_tier:
            raise RegistryValidationError(
                f"family {family_id} data_tier mismatch: "
                f"registry={registered.data_tier}, config={config_family.data_tier}"
            )
        if registered.estimated_config_budget != config_family.config_budget:
            raise RegistryValidationError(
                f"family {family_id} budget mismatch: "
                f"registry={registered.estimated_config_budget}, config={config_family.config_budget}"
            )
        if set(registered.sizing_modes) != set(config_family.sizing_modes):
            raise RegistryValidationError(
                f"family {family_id} sizing_modes mismatch: "
                f"registry={sorted(registered.sizing_modes)}, "
                f"config={sorted(config_family.sizing_modes)}"
            )

    derivative_discovered = (
        discover_phase3_derivative_families(root, derivative_config_glob)
        if derivative_config_glob
        else {}
    )
    derivative_registered = {
        family.family_id: family
        for family in families
        if family.config_binding == "derivative_grid"
    }
    missing_derivatives = sorted(
        set(derivative_discovered) - set(derivative_registered)
    )
    extra_derivatives = sorted(
        set(derivative_registered) - set(derivative_discovered)
    )
    if missing_derivatives:
        raise RegistryValidationError(
            "Phase 3 derivative families missing from registry: "
            f"{missing_derivatives}"
        )
    if extra_derivatives:
        raise RegistryValidationError(
            "derivative-grid registry families absent from grids: "
            f"{extra_derivatives}"
        )
    for family_id, config_family in derivative_discovered.items():
        registered = derivative_registered[family_id]
        if registered.config_path != config_family.config_path:
            raise RegistryValidationError(
                f"family {family_id} config_path mismatch: "
                f"registry={registered.config_path}, config={config_family.config_path}"
            )
        if registered.signal_kernel != config_family.signal_kernel:
            raise RegistryValidationError(
                f"family {family_id} signal_kernel mismatch: "
                f"registry={registered.signal_kernel}, config={config_family.signal_kernel}"
            )
        if registered.data_tier != config_family.data_tier:
            raise RegistryValidationError(
                f"family {family_id} data_tier mismatch: "
                f"registry={registered.data_tier}, config={config_family.data_tier}"
            )
        if registered.estimated_config_budget != config_family.config_budget:
            raise RegistryValidationError(
                f"family {family_id} budget mismatch: "
                f"registry={registered.estimated_config_budget}, "
                f"config={config_family.config_budget}"
            )

    audits = _parse_relation_audits(raw.get("relation_group_audit"))
    _validate_relation_groups(families, audits)
    return SearchRegistry(
        version=version,
        registry_id=registry_id,
        search_config_glob=search_config_glob,
        families=families,
        relation_group_audit=audits,
        registry_path=path,
        repo_root=root,
        derivative_config_glob=derivative_config_glob,
    )


def budget_summary(
    registry_or_families: SearchRegistry | Iterable[SearchFamilyRecord],
) -> dict[str, Any]:
    """Aggregate estimated configuration budgets without executing a strategy."""

    families = (
        registry_or_families.families
        if isinstance(registry_or_families, SearchRegistry)
        else tuple(registry_or_families)
    )

    def aggregate(attribute: str) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for family in families:
            totals[str(getattr(family, attribute))] += family.estimated_config_budget
        return dict(sorted(totals.items()))

    return {
        "family_count": len(families),
        "total_estimated_config_budget": sum(
            family.estimated_config_budget for family in families
        ),
        "config_backed_budget": sum(
            family.estimated_config_budget
            for family in families
            if family.config_binding in {"strategy_space", "derivative_grid"}
        ),
        "strategy_space_budget": sum(
            family.estimated_config_budget
            for family in families
            if family.config_binding == "strategy_space"
        ),
        "derivative_grid_budget": sum(
            family.estimated_config_budget
            for family in families
            if family.config_binding == "derivative_grid"
        ),
        "registry_only_budget": sum(
            family.estimated_config_budget
            for family in families
            if family.config_binding == "registry_only"
        ),
        "by_implementation_status": aggregate("implementation_status"),
        "by_evidence_status": aggregate("evidence_status"),
        "by_readiness_status": aggregate("readiness_status"),
        "by_instrument_class": aggregate("instrument_class"),
        "by_priority": aggregate("priority"),
    }


def relation_group_summary(registry: SearchRegistry) -> list[dict[str, Any]]:
    grouped: dict[str, list[SearchFamilyRecord]] = defaultdict(list)
    for family in registry.families:
        grouped[family.relation_group].append(family)
    rows = []
    for group, families in sorted(grouped.items()):
        if len(families) < 2:
            continue
        audit = registry.relation_group_audit[group]
        rows.append(
            {
                "relation_group": group,
                "signal_kernel": families[0].signal_kernel,
                "family_ids": ", ".join(sorted(family.family_id for family in families)),
                "estimated_config_budget": sum(
                    family.estimated_config_budget for family in families
                ),
                "rationale": audit.rationale,
            }
        )
    return rows


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    rendered = [f"| {' | '.join(headers)} |", f"| {' | '.join('---' for _ in headers)} |"]
    rendered.extend(
        f"| {' | '.join(_markdown_cell(value) for value in row)} |" for row in rows
    )
    return rendered


def render_markdown_summary(registry: SearchRegistry) -> str:
    summary = budget_summary(registry)
    config_backed = sorted(registry.config_backed_families, key=lambda family: family.family_id)
    derivative_backed = sorted(
        registry.derivative_grid_families,
        key=lambda family: family.family_id,
    )
    planned = sorted(registry.planned_families, key=lambda family: family.family_id)
    relation_rows = relation_group_summary(registry)

    def instrument_label(family: SearchFamilyRecord) -> str:
        roots = "/".join(family.instrument_roots)
        return family.instrument_class if not roots else f"{family.instrument_class} ({roots})"

    def sizing_label(family: SearchFamilyRecord) -> str:
        modes = "/".join(family.sizing_modes)
        return family.sizing_family if not modes else f"{family.sizing_family} [{modes}]"

    lines = [
        "# Phase 3 Search Registry Summary",
        "",
        f"Registry: `{registry.registry_id}`",
        "",
        "This is a configuration inventory and validation report. It does not execute strategies or create evidence.",
        "",
        "## Budget",
        "",
    ]
    lines.extend(
        _markdown_table(
            ["measure", "value"],
            [
                ("families", summary["family_count"]),
                ("config-backed budget", summary["config_backed_budget"]),
                ("stock strategy-space budget", summary["strategy_space_budget"]),
                ("derivative-grid budget", summary["derivative_grid_budget"]),
                ("registry-only planned budget", summary["registry_only_budget"]),
                ("total estimated config budget", summary["total_estimated_config_budget"]),
            ],
        )
    )
    lines.extend(["", "### Budget By Implementation Status", ""])
    lines.extend(
        _markdown_table(
            ["implementation status", "estimated config budget"],
            summary["by_implementation_status"].items(),
        )
    )
    lines.extend(["", "## Config-Backed Families", ""])
    lines.extend(
        _markdown_table(
            [
                "family",
                "instrument",
                "signal kernel",
                "position / sizing",
                "data tier",
                "config",
                "relation group",
                "implementation",
                "evidence",
                "readiness",
                "priority",
                "budget",
                "next gate",
            ],
            (
                (
                    family.family_id,
                    instrument_label(family),
                    family.signal_kernel,
                    f"{family.position_family} / {sizing_label(family)}",
                    family.data_tier,
                    family.config_path,
                    family.relation_group,
                    family.implementation_status,
                    family.evidence_status,
                    family.readiness_status,
                    family.priority,
                    family.estimated_config_budget,
                    family.next_gate,
                )
                for family in config_backed
            ),
        )
    )
    lines.extend(["", "## Derivative-Grid Families", ""])
    lines.extend(
        _markdown_table(
            [
                "family",
                "instrument",
                "signal kernel",
                "position / sizing",
                "data tier",
                "config",
                "readiness",
                "next gate",
                "budget",
            ],
            (
                (
                    family.family_id,
                    instrument_label(family),
                    family.signal_kernel,
                    f"{family.position_family} / {sizing_label(family)}",
                    family.data_tier,
                    family.config_path,
                    family.readiness_status,
                    family.next_gate,
                    family.estimated_config_budget,
                )
                for family in derivative_backed
            ),
        )
    )
    lines.extend(["", "## Planned And Blocked Frontier", ""])
    lines.extend(
        _markdown_table(
            [
                "family",
                "instrument",
                "signal kernel",
                "position / sizing",
                "implementation",
                "evidence",
                "readiness",
                "blocking dependency",
                "priority",
                "budget",
                "next gate",
            ],
            (
                (
                    family.family_id,
                    instrument_label(family),
                    family.signal_kernel,
                    f"{family.position_family} / {sizing_label(family)}",
                    family.implementation_status,
                    family.evidence_status,
                    family.readiness_status,
                    family.blocking_dependency,
                    family.priority,
                    family.estimated_config_budget,
                    family.next_gate,
                )
                for family in planned
            ),
        )
    )
    lines.extend(["", "## Shared-Kernel Relation Groups", ""])
    lines.extend(
        _markdown_table(
            ["relation group", "signal kernel", "families", "budget", "audit rationale"],
            (
                (
                    row["relation_group"],
                    row["signal_kernel"],
                    row["family_ids"],
                    row["estimated_config_budget"],
                    row["rationale"],
                )
                for row in relation_rows
            ),
        )
    )
    return "\n".join(lines).rstrip() + "\n"
