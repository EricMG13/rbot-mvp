"""Pathway module lists, copied from FW src/deploy_vii/contracts.py _PRESETS (fda70c29)."""

from pathlib import Path

PATHWAYS: dict[str, tuple[str, ...]] = {
    "credit_snapshot": (
        *("CP-0", "CP-1", "CP-MODEL", "CP-1A", "CP-2", "CP-4", "CP-3", "CP-5", "CP-6"),
    ),
    "earnings_update": ("CP-0", "CP-1", "CP-MODEL", "CP-1B", "CP-1D", "CP-5"),
    "relative_value": (
        *("CP-0", "CP-1", "CP-MODEL", "CP-1C", "CP-2", "CP-2D", "CP-3", "CP-3C", "CP-3D", "CP-5"),
    ),
    "covenant_refinancing": (
        "CP-0",
        "CP-1",
        "CP-MODEL",
        "CP-1A",
        "CP-2",
        "CP-2D",
        "CP-4",
        "CP-3C",
        "CP-5",
    ),
    "distressed_restructuring": (
        *("CP-0", "CP-1", "CP-1A", "CP-1C", "CP-2", "CP-2A", "CP-2G", "CP-2H", "CP-4", "CP-4C"),
        "CP-5",
    ),
    "portfolio_decision": ("CP-0", "CP-1", "CP-MODEL", "CP-2", "CP-3", "CP-5", "CP-6", "CP-8"),
}
IMPLEMENTED: frozenset[str] = frozenset({"CP-0", "CP-1"})
PROMPTS_DIR = Path(__file__).parent / "prompts"
