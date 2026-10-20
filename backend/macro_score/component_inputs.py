from collections.abc import Callable
from types import MappingProxyType
from typing import Final, TypeAlias

from macro_score.derived_components import build_derived_snapshot_series
from macro_score.inputs import MacroScoreCutoff
from macro_score.methodology import COMPONENTS, ComponentSpec, MacroComponentKey
from macro_score.models import ComponentSeries
from macro_score.raw_components import build_change_series, build_level_series
from macro_score.sources import MacroScoreSources
from macro_score.target_components import build_target_distance_series

ComponentBuilder: TypeAlias = Callable[
    [ComponentSpec, MacroScoreSources, MacroScoreCutoff],
    ComponentSeries,
]

_BUILDERS: Final[MappingProxyType[MacroComponentKey, ComponentBuilder]] = MappingProxyType({
    MacroComponentKey("ibc_br_sa_yoy"): build_change_series,
    MacroComponentKey("rendimento_real_yoy"): build_change_series,
    MacroComponentKey("dbgg_yoy_pp"): build_change_series,
    MacroComponentKey("abs_ipca_12m_target"): build_target_distance_series,
    MacroComponentKey("abs_focus_12m_target"): build_target_distance_series,
})

_DERIVED_KEYS: Final = frozenset({
    MacroComponentKey("juro_real_ex_ante_1y"),
    MacroComponentKey("inclinacao_di_5y_1y"),
    MacroComponentKey("spread_credito_pf_selic"),
    MacroComponentKey("cobertura_externa"),
})


def build_component_series(
    sources: MacroScoreSources,
    cutoff: MacroScoreCutoff,
    calculation_cycle_id: str | None = None,
) -> tuple[ComponentSeries, ...]:
    def build(spec: ComponentSpec) -> ComponentSeries:
        if spec.key in _DERIVED_KEYS:
            return build_derived_snapshot_series(spec, sources, cutoff, calculation_cycle_id)
        return _BUILDERS.get(spec.key, build_level_series)(spec, sources, cutoff)

    return tuple(build(spec) for spec in COMPONENTS)
