"""Artefacts FabricOps generates from a recipe: variable libraries, parameter overlays."""

from .variable_library import ITEM_TYPE, VariableLibraryPlan, render_variable_library
from .writer import GENERATED_ROOT, declarations, reference_resolver, render_all, target_layers, write

__all__ = [
    "GENERATED_ROOT",
    "ITEM_TYPE",
    "VariableLibraryPlan",
    "declarations",
    "reference_resolver",
    "render_all",
    "render_variable_library",
    "target_layers",
    "write",
]
