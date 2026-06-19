"""Vibration preset definitions — Satori 6-channel body zones."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

# Satori 5.1 channel order: L, R, C, LFE, Ls, Rs
SATORI_CHANNEL_SUFFIXES = ("L", "R", "C", "LFE", "Ls", "Rs")
SATORI_OUTPUT_CHANNELS = 6
# Vibration transducers on this Gigaport bed (physical wiring):
# Ch C -> Head/Top | Ch LFE -> Upper Mid | Ch Ls -> Legs | Ch Rs -> Torso Mid
PHYSICAL_ZONE_TO_CHANNEL = {
    "head": 2,       # C
    "upper_mid": 3,  # LFE
    "legs": 4,       # Ls
    "mid": 5,        # Rs
}


@dataclass(frozen=True)
class FrequencyProfile:
    """Band-pass ranges (Hz) for Satori body zones."""

    id: str
    label: str
    legs_band: tuple[float, float]
    mid_band: tuple[float, float]
    upper_mid_band: tuple[float, float]
    head_band: tuple[float, float]


@dataclass(frozen=True)
class SegmentationPreset:
    """How bass, drums, and other stems feed each body zone. Vocals are never used."""

    id: str
    label: str
    legs_weights: tuple[float, float, float]
    mid_weights: tuple[float, float, float]
    upper_mid_weights: tuple[float, float, float]
    head_weights: tuple[float, float, float]
    use_transient_on_head: bool = True
    gentle_dynamics: bool = False
    amplify_quiet: bool = False
    synthetic_freq_range: tuple[float, float] | None = None
    energy_window: int = 1024


FREQUENCY_PROFILES: list[FrequencyProfile] = [
    FrequencyProfile(
        "satori",
        "Satori (Legs 30-40 / Mid 50-68 / Upper 80-100 / Head 100-150)",
        (30, 40),
        (50, 68),
        (80, 100),
        (100, 150),
    ),
    FrequencyProfile(
        "standard",
        "Standard (20-60 / 60-120 / 120-200 / 150-250)",
        (20, 60),
        (60, 120),
        (120, 200),
        (150, 250),
    ),
    FrequencyProfile(
        "deep",
        "Deep (15-45 / 45-90 / 90-180 / 120-200)",
        (15, 45),
        (45, 90),
        (90, 180),
        (120, 200),
    ),
    FrequencyProfile(
        "healing",
        "Healing (18-50 / 50-95 / 95-130 / 130-170)",
        (18, 50),
        (50, 95),
        (95, 130),
        (130, 170),
    ),
    FrequencyProfile(
        "healing_soft",
        "Healing Soft (15-40 / 40-80 / 80-120 / 120-150)",
        (15, 40),
        (40, 80),
        (80, 120),
        (120, 150),
    ),
]

_SPECIAL_SEGMENTATIONS: list[SegmentationPreset] = [
    SegmentationPreset(
        "default",
        "Default (Bass / Bass+Drums / Drums+Other / Transient)",
        (1.0, 0.0, 0.0),
        (0.6, 0.4, 0.0),
        (0.2, 0.5, 0.3),
        (0.0, 0.7, 0.3),
        use_transient_on_head=True,
    ),
    SegmentationPreset(
        "8d_cinema",
        "8D Cinema (Bass+Drums / Drums / Drums+Other / Transient)",
        (0.7, 0.3, 0.0),
        (0.2, 0.8, 0.0),
        (0.1, 0.6, 0.3),
        (0.0, 0.75, 0.25),
        use_transient_on_head=True,
        energy_window=512,
    ),
    SegmentationPreset(
        "freq_only",
        "Frequency Split Only (Full Instrumental)",
        (0.34, 0.33, 0.33),
        (0.34, 0.33, 0.33),
        (0.34, 0.33, 0.33),
        (0.34, 0.33, 0.33),
        use_transient_on_head=False,
    ),
    SegmentationPreset(
        "bass_heavy",
        "Bass Heavy",
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.5, 0.5, 0.0),
        (0.3, 0.5, 0.2),
        use_transient_on_head=False,
    ),
    SegmentationPreset(
        "drums_heavy",
        "Drums Heavy",
        (0.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.7, 0.3),
        (0.0, 0.8, 0.2),
        use_transient_on_head=True,
    ),
    SegmentationPreset(
        "healing",
        "Healing / Ambient (Pads + Warm Low)",
        (0.25, 0.0, 0.75),
        (0.15, 0.0, 0.85),
        (0.10, 0.0, 0.90),
        (0.05, 0.0, 0.95),
        use_transient_on_head=False,
        gentle_dynamics=True,
        amplify_quiet=True,
        synthetic_freq_range=(32.0, 38.0),
        energy_window=4096,
    ),
    SegmentationPreset(
        "healing_drone",
        "Healing Drone (Bass+Other / Other)",
        (0.45, 0.0, 0.55),
        (0.10, 0.0, 0.90),
        (0.05, 0.0, 0.95),
        (0.05, 0.0, 0.95),
        use_transient_on_head=False,
        gentle_dynamics=True,
        amplify_quiet=True,
        synthetic_freq_range=(28.0, 38.0),
        energy_window=8192,
    ),
    SegmentationPreset(
        "healing_nature",
        "Healing Nature (Other Only)",
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        use_transient_on_head=False,
        gentle_dynamics=True,
        amplify_quiet=True,
        synthetic_freq_range=(32.0, 45.0),
        energy_window=4096,
    ),
    SegmentationPreset(
        "bass_only",
        "Bass Only (All Zones From Bass)",
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        use_transient_on_head=False,
    ),
    SegmentationPreset(
        "drums_only",
        "Drums Only (All Zones From Drums)",
        (0.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        use_transient_on_head=False,
    ),
    SegmentationPreset(
        "other_only",
        "Other Only (All Zones From Other)",
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        use_transient_on_head=False,
    ),
]

_STEM_NAMES = ("bass", "drums", "other")
_ZONE_NAMES = ("legs", "mid", "upper_mid", "head")


def _stem_weights(stem: str) -> tuple[float, float, float]:
    if stem == "bass":
        return (1.0, 0.0, 0.0)
    if stem == "drums":
        return (0.0, 1.0, 0.0)
    return (0.0, 0.0, 1.0)


_PERM_SEGMENTATIONS: list[SegmentationPreset] = []
for legs_stem, mid_stem, other_stem in permutations(_STEM_NAMES):
    _PERM_SEGMENTATIONS.append(
        SegmentationPreset(
            id=f"perm_{legs_stem}_{mid_stem}_{other_stem}",
            label=(
                f"Single Stem: Legs={legs_stem.title()}, Mid={mid_stem.title()}, "
                f"Upper={other_stem.title()}"
            ),
            legs_weights=_stem_weights(legs_stem),
            mid_weights=_stem_weights(mid_stem),
            upper_mid_weights=_stem_weights(other_stem),
            head_weights=_stem_weights(other_stem),
            use_transient_on_head=False,
        )
    )

SEGMENTATION_PRESETS: list[SegmentationPreset] = _SPECIAL_SEGMENTATIONS + _PERM_SEGMENTATIONS


def get_frequency_profile(profile_id: str) -> FrequencyProfile:
    for profile in FREQUENCY_PROFILES:
        if profile.id == profile_id:
            return profile
    return FREQUENCY_PROFILES[0]


def get_segmentation_preset(preset_id: str) -> SegmentationPreset:
    for preset in SEGMENTATION_PRESETS:
        if preset.id == preset_id:
            return preset
    return SEGMENTATION_PRESETS[0]
