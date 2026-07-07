"""Main PyQt5 window and interaction logic."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PyQt5.QtCore import QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from sounddevice import PortAudioError

from audio.audio_utils import MAX_SAMPLES, normalize_prepared_mix
from audio.cinema_8d import apply_8d_stereo_pan
from audio.gigaport_output import GigaportOutput, build_vibration_layers_from_stems
from audio.synthetic_types import SYNTHETIC_TYPES
from audio.vibration_presets import (
    CUSTOM_FREQ_MAX_HZ,
    CUSTOM_FREQ_MIN_HZ,
    FREQUENCY_PROFILES,
    SEGMENTATION_PRESETS,
    SATORI_CHANNEL_SUFFIXES,
    get_frequency_profile,
)
from processors.quick_stems import load_quick_stems
from processors.stem_manager import StemSeparationError, separate_stems


@dataclass
class PreparedMix:
    """Container for separated stems and the source file metadata."""

    source_path: str
    sample_rate: int
    bass: np.ndarray
    drums: np.ndarray
    other: np.ndarray
    original: np.ndarray
    quick_mode: bool = False
    bass_mono: np.ndarray | None = None
    drums_mono: np.ndarray | None = None
    other_mono: np.ndarray | None = None
    drum_impacts: np.ndarray | None = None


class StemWorker(QThread):
    """Background worker: quick preview first, then full stem separation."""

    preview_ready = pyqtSignal(object)
    finished_ok = pyqtSignal(object)
    separation_skipped = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, source_path: str, work_dir: str) -> None:
        super().__init__()
        self.source_path = source_path
        self.work_dir = work_dir

    def _to_prepared(self, stems: dict, quick_mode: bool) -> PreparedMix:
        return normalize_prepared_mix(
            PreparedMix(
                source_path=self.source_path,
                sample_rate=stems["sample_rate"],
                bass=stems["bass"],
                drums=stems["drums"],
                other=stems["other"],
                original=stems["original"],
                quick_mode=quick_mode,
            )
        )

    def run(self) -> None:
        preview_emitted = False
        try:
            quick = load_quick_stems(self.source_path)
            self.preview_ready.emit(self._to_prepared(quick, quick_mode=True))
            preview_emitted = True

            stems = separate_stems(self.source_path, self.work_dir)
            self.finished_ok.emit(self._to_prepared(stems, quick_mode=False))
        except StemSeparationError as exc:
            if preview_emitted:
                self.separation_skipped.emit(str(exc))
            else:
                self.failed.emit(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            if preview_emitted:
                self.separation_skipped.emit(f"Stem separation failed: {exc}")
            else:
                self.failed.emit(f"Unexpected processing failure: {exc}")


@dataclass
class VibrationRebuildResult:
    """Pre-built vibration layers ready to swap into the playback engine."""

    token: int
    legs: np.ndarray
    mid: np.ndarray
    upper_mid: np.ndarray
    head: np.ndarray
    bass_energy: np.ndarray
    generated_freq: np.ndarray
    cinema_8d: bool
    speaker_program: np.ndarray | None
    preserve_playhead: bool
    reset_playhead: bool
    resume_playback: bool
    playhead_fraction: float


class VibrationRebuildWorker(QThread):
    """Background worker for CPU-heavy vibration preset rebuilds."""

    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        token: int,
        mix: PreparedMix,
        *,
        synthetic_vibro: bool,
        segmentation_id: str,
        frequency_profile_id: str,
        cinema_8d: bool,
        synthetic_type_id: str,
        custom_frequency_bands: dict[str, tuple[float, float]] | None,
        rebuild_speaker: bool,
        preserve_playhead: bool,
        reset_playhead: bool,
        resume_playback: bool,
        playhead_fraction: float,
    ) -> None:
        super().__init__()
        self.token = token
        self.mix = mix
        self.synthetic_vibro = synthetic_vibro
        self.segmentation_id = segmentation_id
        self.frequency_profile_id = frequency_profile_id
        self.cinema_8d = cinema_8d
        self.synthetic_type_id = synthetic_type_id
        self.custom_frequency_bands = custom_frequency_bands
        self.rebuild_speaker = rebuild_speaker
        self.preserve_playhead = preserve_playhead
        self.reset_playhead = reset_playhead
        self.resume_playback = resume_playback
        self.playhead_fraction = playhead_fraction

    def run(self) -> None:
        try:
            mix = normalize_prepared_mix(self.mix)
            legs, mid, upper_mid, head, bass_energy, generated_freq = build_vibration_layers_from_stems(
                mix.sample_rate,
                mix.bass,
                mix.drums,
                mix.other,
                synthetic_vibro=self.synthetic_vibro,
                segmentation_id=self.segmentation_id,
                frequency_profile_id=self.frequency_profile_id,
                cinema_8d=self.cinema_8d,
                synthetic_type_id=self.synthetic_type_id,
                custom_frequency_bands=self.custom_frequency_bands,
                bass_mono=mix.bass_mono,
                drums_mono=mix.drums_mono,
                other_mono=mix.other_mono,
                drum_impacts=mix.drum_impacts,
            )

            speaker_program = None
            if self.rebuild_speaker:
                if self.cinema_8d:
                    speaker_program = apply_8d_stereo_pan(mix.original, mix.sample_rate)
                else:
                    speaker_program = mix.original.copy()

            self.finished_ok.emit(
                VibrationRebuildResult(
                    token=self.token,
                    legs=legs,
                    mid=mid,
                    upper_mid=upper_mid,
                    head=head,
                    bass_energy=bass_energy,
                    generated_freq=generated_freq,
                    cinema_8d=self.cinema_8d,
                    speaker_program=speaker_program,
                    preserve_playhead=self.preserve_playhead,
                    reset_playhead=self.reset_playhead,
                    resume_playback=self.resume_playback,
                    playhead_fraction=self.playhead_fraction,
                )
            )
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(self.token, f"Vibration rebuild failed: {exc}")


class MainWindow(QMainWindow):
    """Desktop UI for loading, processing, and playing vibro mixes."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Satori Vibro Test")
        self.resize(950, 760)

        self.engine = GigaportOutput()
        GigaportOutput.warm_up_audio_backend()
        self.current_mix: Optional[PreparedMix] = None
        self.processing_worker: Optional[StemWorker] = None
        self.vibration_worker: Optional[VibrationRebuildWorker] = None
        self.temp_dir = tempfile.mkdtemp(prefix="satori_vibro_")
        self._slider_being_dragged = False
        self._rebuild_token = 0
        self._rebuild_needs_speaker = False
        self._rebuild_preserve_playhead = True
        self._rebuild_reset_playhead = False
        self._rebuild_resume_playback = False
        self._rebuild_playhead_fraction = 0.0
        self._loaded_file_label = "No file loaded"
        self._stem_busy = False
        self._vibration_rebuild_active = False
        self._last_applied_rebuild_token = 0
        self._preset_dirty = False
        self._applied_cinema_8d = False
        self._pending_fingerprint: tuple | None = None

        self._build_ui()
        self._refresh_devices()

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._refresh_runtime_ui)
        self.timer.start()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.engine.stop()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)

        self.file_label = QLabel("No file loaded")
        self.file_label.setWordWrap(True)
        main_layout.addWidget(self.file_label)

        busy_row = QHBoxLayout()
        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("color: #b8860b;")
        self.busy_progress = QProgressBar()
        self.busy_progress.setRange(0, 0)
        self.busy_progress.setFixedWidth(140)
        self.busy_progress.setVisible(False)
        self.busy_label.setVisible(False)
        busy_row.addWidget(self.busy_label, 1)
        busy_row.addWidget(self.busy_progress)
        main_layout.addLayout(busy_row)

        top_controls = QHBoxLayout()
        self.pick_button = QPushButton("Load Audio File")
        self.pick_button.clicked.connect(self._pick_file)
        top_controls.addWidget(self.pick_button)

        self.device_combo = QComboBox()
        top_controls.addWidget(QLabel("Output Device:"))
        top_controls.addWidget(self.device_combo, 1)

        self.refresh_devices_btn = QPushButton("Refresh Devices")
        self.refresh_devices_btn.clicked.connect(self._refresh_devices)
        top_controls.addWidget(self.refresh_devices_btn)

        main_layout.addLayout(top_controls)

        self.progress_slider = QSlider()
        self.progress_slider.setOrientation(1)  # Qt.Horizontal
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.sliderPressed.connect(self._on_slider_pressed)
        self.progress_slider.sliderReleased.connect(self._on_slider_released)
        main_layout.addWidget(self.progress_slider)

        transport = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._play)
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.engine.pause)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.engine.stop)
        transport.addWidget(self.play_button)
        transport.addWidget(self.pause_button)
        transport.addWidget(self.stop_button)

        transport.addWidget(QLabel("Volume"))
        self.volume_slider = QSlider()
        self.volume_slider.setOrientation(1)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(85)
        self.volume_slider.valueChanged.connect(self._set_volume)
        transport.addWidget(self.volume_slider)
        main_layout.addLayout(transport)

        self.synthetic_checkbox = QCheckBox("Generate Synthetic Vibro")
        self.synthetic_checkbox.toggled.connect(self._on_synthetic_toggled)
        main_layout.addWidget(self.synthetic_checkbox)

        synthetic_row = QHBoxLayout()
        synthetic_row.addWidget(QLabel("Synthetic Type:"))
        self.synthetic_type_combo = QComboBox()
        for synth in SYNTHETIC_TYPES:
            self.synthetic_type_combo.addItem(synth.label, synth.id)
        self.synthetic_type_combo.setEnabled(False)
        self.synthetic_type_combo.currentIndexChanged.connect(self._on_synthetic_type_changed)
        synthetic_row.addWidget(self.synthetic_type_combo, 1)
        self.synthetic_desc_label = QLabel(SYNTHETIC_TYPES[0].description)
        self.synthetic_desc_label.setWordWrap(True)
        main_layout.addLayout(synthetic_row)
        main_layout.addWidget(self.synthetic_desc_label)

        self.cinema_8d_checkbox = QCheckBox("8D Cinema Mode (spatial L/R pan + body motion)")
        self.cinema_8d_checkbox.toggled.connect(self._on_cinema_8d_toggled)
        main_layout.addWidget(self.cinema_8d_checkbox)

        vibro_box = QGroupBox("Vibration Presets")
        vibro_layout = QGridLayout(vibro_box)

        self.segmentation_combo = QComboBox()
        for preset in SEGMENTATION_PRESETS:
            self.segmentation_combo.addItem(preset.label, preset.id)
        self.segmentation_combo.currentIndexChanged.connect(self._on_preset_control_changed)
        vibro_layout.addWidget(QLabel("Segmentation:"), 0, 0)
        vibro_layout.addWidget(self.segmentation_combo, 0, 1)

        self.frequency_combo = QComboBox()
        for profile in FREQUENCY_PROFILES:
            self.frequency_combo.addItem(profile.label, profile.id)
        satori_index = next(i for i, p in enumerate(FREQUENCY_PROFILES) if p.id == "satori")
        self.frequency_combo.setCurrentIndex(satori_index)
        self.frequency_combo.currentIndexChanged.connect(self._on_frequency_profile_changed)
        vibro_layout.addWidget(QLabel("Frequency Bands:"), 1, 0)
        vibro_layout.addWidget(self.frequency_combo, 1, 1)

        self.custom_freq_widget = QWidget()
        custom_freq_layout = QGridLayout(self.custom_freq_widget)
        custom_freq_layout.setContentsMargins(0, 0, 0, 0)

        satori_profile = get_frequency_profile("satori")
        self.freq_low_sliders: dict[str, QSlider] = {}
        self.freq_high_sliders: dict[str, QSlider] = {}
        self.freq_low_labels: dict[str, QLabel] = {}
        self.freq_high_labels: dict[str, QLabel] = {}
        freq_zone_defs = (
            ("legs", "Legs Freq (Ls)", satori_profile.legs_band),
            ("mid", "Mid Freq (Rs)", satori_profile.mid_band),
            ("upper", "Upper Freq (LFE)", satori_profile.upper_mid_band),
            ("head", "Head Freq (C)", satori_profile.head_band),
        )
        for row, (zone_id, zone_name, (low_hz, high_hz)) in enumerate(freq_zone_defs):
            low_slider = QSlider()
            low_slider.setOrientation(1)
            low_slider.setRange(int(CUSTOM_FREQ_MIN_HZ), int(CUSTOM_FREQ_MAX_HZ))
            low_slider.setValue(int(low_hz))
            low_slider.valueChanged.connect(
                lambda value, z=zone_id: self._on_custom_freq_changed(z, "low", value)
            )

            high_slider = QSlider()
            high_slider.setOrientation(1)
            high_slider.setRange(int(CUSTOM_FREQ_MIN_HZ), int(CUSTOM_FREQ_MAX_HZ))
            high_slider.setValue(int(high_hz))
            high_slider.valueChanged.connect(
                lambda value, z=zone_id: self._on_custom_freq_changed(z, "high", value)
            )

            low_label = QLabel(f"{int(low_hz)} Hz")
            high_label = QLabel(f"{int(high_hz)} Hz")

            self.freq_low_sliders[zone_id] = low_slider
            self.freq_high_sliders[zone_id] = high_slider
            self.freq_low_labels[zone_id] = low_label
            self.freq_high_labels[zone_id] = high_label

            custom_freq_layout.addWidget(QLabel(f"{zone_name}:"), row, 0)
            band_row = QHBoxLayout()
            band_row.addWidget(QLabel("Low"))
            band_row.addWidget(low_slider, 1)
            band_row.addWidget(low_label)
            band_row.addWidget(QLabel("High"))
            band_row.addWidget(high_slider, 1)
            band_row.addWidget(high_label)
            custom_freq_layout.addLayout(band_row, row, 1)

        self.custom_freq_widget.setVisible(False)
        vibro_layout.addWidget(self.custom_freq_widget, 2, 0, 1, 2)

        channel_info = QLabel(
            "Bed wiring: C=Head | LFE=Upper | Ls=Legs | Rs=Mid | L/R=original audio"
        )
        channel_info.setWordWrap(True)
        vibro_layout.addWidget(channel_info, 3, 0, 1, 2)

        self.zone_sliders: dict[str, QSlider] = {}
        self.zone_labels: dict[str, QLabel] = {}
        zone_defs = (
            ("mid", "Mid (Rs)"),
            ("legs", "Legs (Ls)"),
            ("upper", "Upper (LFE)"),
            ("head", "Head (C)"),
        )
        for row, (zone_id, zone_name) in enumerate(zone_defs, start=4):
            slider = QSlider()
            slider.setOrientation(1)
            slider.setRange(0, 300)
            slider.setValue(140)
            slider.valueChanged.connect(lambda value, z=zone_id: self._set_zone_intensity(z, value))
            label = QLabel("140%")
            self.zone_sliders[zone_id] = slider
            self.zone_labels[zone_id] = label
            vibro_layout.addWidget(QLabel(f"{zone_name}:"), row, 0)
            zone_row = QHBoxLayout()
            zone_row.addWidget(slider, 1)
            zone_row.addWidget(label)
            vibro_layout.addLayout(zone_row, row, 1)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("Test zone:"))
        self.zone_test_buttons: dict[str, QPushButton] = {}
        test_defs = (
            ("mid", "Mid (Rs)"),
            ("legs", "Legs (Ls)"),
            ("upper", "Upper (LFE)"),
            ("head", "Head (C)"),
        )
        for zone_id, zone_name in test_defs:
            btn = QPushButton(zone_name)
            btn.setCheckable(False)
            btn.clicked.connect(lambda _checked=False, z=zone_id: self._test_zone(z))
            self.zone_test_buttons[zone_id] = btn
            test_row.addWidget(btn)
        vibro_layout.addLayout(test_row, 8, 0, 1, 2)

        self.apply_presets_btn = QPushButton("Apply Changes")
        self.apply_presets_btn.clicked.connect(self._apply_vibration_presets)
        self.apply_presets_btn.setEnabled(False)
        vibro_layout.addWidget(self.apply_presets_btn, 9, 0, 1, 2)

        self.preset_summary_label = QLabel("")
        self.preset_summary_label.setWordWrap(True)
        vibro_layout.addWidget(self.preset_summary_label, 10, 0, 1, 2)

        main_layout.addWidget(vibro_box)

        debug_box = QGroupBox("Debug View")
        debug_layout = QGridLayout(debug_box)
        self.legs_rms_label = QLabel("Legs RMS (Ls): 0.000")
        self.mid_rms_label = QLabel("Mid RMS (Rs): 0.000")
        self.upper_rms_label = QLabel("Upper Mid RMS (LFE): 0.000")
        self.head_rms_label = QLabel("Head RMS (C): 0.000")
        self.bass_energy_label = QLabel("Bass Energy: 0.000")
        self.vib_freq_label = QLabel("Generated Vib Freq: 0.0 Hz")
        self.cpu_label = QLabel("CPU Usage Estimate: 0.0%")

        labels = [
            self.legs_rms_label,
            self.mid_rms_label,
            self.upper_rms_label,
            self.head_rms_label,
            self.bass_energy_label,
            self.vib_freq_label,
            self.cpu_label,
        ]
        for i, label in enumerate(labels):
            debug_layout.addWidget(label, i // 2, i % 2)

        main_layout.addWidget(debug_box)
        self._sync_zone_intensities_to_engine()
        self._update_preset_summary()

    def _refresh_devices(self) -> None:
        self.device_combo.clear()
        for device in self.engine.list_output_devices(min_channels=6):
            self.device_combo.addItem(f"{device['name']} ({device['index']})", device["index"])

    def _get_vibration_preset_ids(self) -> tuple[str, str]:
        return (
            self.segmentation_combo.currentData(),
            self.frequency_combo.currentData(),
        )

    def _is_custom_frequency_mode(self) -> bool:
        return self.frequency_combo.currentData() == "custom"

    def _get_custom_frequency_bands(self) -> dict[str, tuple[float, float]] | None:
        if not self._is_custom_frequency_mode():
            return None
        return {
            "legs": (
                float(self.freq_low_sliders["legs"].value()),
                float(self.freq_high_sliders["legs"].value()),
            ),
            "mid": (
                float(self.freq_low_sliders["mid"].value()),
                float(self.freq_high_sliders["mid"].value()),
            ),
            "upper_mid": (
                float(self.freq_low_sliders["upper"].value()),
                float(self.freq_high_sliders["upper"].value()),
            ),
            "head": (
                float(self.freq_low_sliders["head"].value()),
                float(self.freq_high_sliders["head"].value()),
            ),
        }

    def _preset_fingerprint(self) -> tuple:
        segmentation_id, frequency_id = self._get_vibration_preset_ids()
        bands = self._get_custom_frequency_bands()
        return (
            segmentation_id,
            frequency_id,
            self.synthetic_checkbox.isChecked(),
            self.synthetic_type_combo.currentData(),
            self.cinema_8d_checkbox.isChecked(),
            bands,
        )

    def _on_preset_control_changed(self) -> None:
        self._mark_preset_dirty()

    def _on_frequency_profile_changed(self) -> None:
        self.custom_freq_widget.setVisible(self._is_custom_frequency_mode())
        self._mark_preset_dirty()

    def _on_custom_freq_changed(self, zone_id: str, edge: str, value: int) -> None:
        low_slider = self.freq_low_sliders[zone_id]
        high_slider = self.freq_high_sliders[zone_id]

        if edge == "low" and value >= high_slider.value():
            high_slider.blockSignals(True)
            high_slider.setValue(min(value + 1, int(CUSTOM_FREQ_MAX_HZ)))
            high_slider.blockSignals(False)
        elif edge == "high" and value <= low_slider.value():
            low_slider.blockSignals(True)
            low_slider.setValue(max(value - 1, int(CUSTOM_FREQ_MIN_HZ)))
            low_slider.blockSignals(False)

        self.freq_low_labels[zone_id].setText(f"{low_slider.value()} Hz")
        self.freq_high_labels[zone_id].setText(f"{high_slider.value()} Hz")
        self._mark_preset_dirty()

    def _mark_preset_dirty(self) -> None:
        if self.current_mix is None:
            self._update_preset_summary()
            return
        self._preset_dirty = True
        self._update_preset_summary()
        self._update_apply_button()
        self._update_busy_ui()

    def _update_apply_button(self) -> None:
        if self.current_mix is None:
            self.apply_presets_btn.setEnabled(False)
            self.apply_presets_btn.setText("Apply Changes")
            return
        if self._preset_dirty:
            self.apply_presets_btn.setEnabled(True)
            self.apply_presets_btn.setText("Apply Changes")
        else:
            self.apply_presets_btn.setEnabled(False)
            self.apply_presets_btn.setText("Changes Applied")

    def _update_preset_summary(self) -> None:
        seg = self.segmentation_combo.currentText()
        if self._is_custom_frequency_mode():
            bands = self._get_custom_frequency_bands() or {}
            freq = (
                f"Custom Legs {bands['legs'][0]:.0f}-{bands['legs'][1]:.0f} Hz | "
                f"Mid {bands['mid'][0]:.0f}-{bands['mid'][1]:.0f} Hz | "
                f"Upper {bands['upper_mid'][0]:.0f}-{bands['upper_mid'][1]:.0f} Hz | "
                f"Head {bands['head'][0]:.0f}-{bands['head'][1]:.0f} Hz"
            )
        else:
            freq = self.frequency_combo.currentText()
        ch = ", ".join(SATORI_CHANNEL_SUFFIXES)
        cinema = "ON" if self.cinema_8d_checkbox.isChecked() else "OFF"
        state = "Pending" if self._preset_dirty and self.current_mix is not None else "Active"
        self.preset_summary_label.setText(
            f"{state}: {seg} | {freq} | 8D Cinema: {cinema} | Channels: {ch}"
        )

    def _on_synthetic_toggled(self, enabled: bool) -> None:
        self.synthetic_type_combo.setEnabled(enabled)
        self._update_synthetic_description()
        self._mark_preset_dirty()

    def _update_synthetic_description(self) -> None:
        idx = self.synthetic_type_combo.currentIndex()
        if idx >= 0:
            self.synthetic_desc_label.setText(SYNTHETIC_TYPES[idx].description)

    def _on_synthetic_type_changed(self) -> None:
        self._update_synthetic_description()
        self._mark_preset_dirty()

    def _on_cinema_8d_toggled(self) -> None:
        self._mark_preset_dirty()

    def _collect_vibration_rebuild_options(self) -> dict:
        segmentation_id, frequency_id = self._get_vibration_preset_ids()
        return {
            "synthetic_vibro": self.synthetic_checkbox.isChecked(),
            "segmentation_id": segmentation_id,
            "frequency_profile_id": frequency_id,
            "cinema_8d": self.cinema_8d_checkbox.isChecked(),
            "synthetic_type_id": self.synthetic_type_combo.currentData(),
            "custom_frequency_bands": self._get_custom_frequency_bands(),
        }

    def _queue_vibration_rebuild(
        self,
        *,
        rebuild_speaker: bool = False,
        preserve_playhead: bool = True,
        reset_playhead: bool = False,
        resume_playback: bool = False,
        playhead_fraction: float | None = None,
    ) -> None:
        if self.current_mix is None:
            return

        self._rebuild_needs_speaker = self._rebuild_needs_speaker or rebuild_speaker
        if reset_playhead:
            self._rebuild_reset_playhead = True
            self._rebuild_preserve_playhead = False
        elif preserve_playhead:
            self._rebuild_preserve_playhead = True
        self._rebuild_resume_playback = self._rebuild_resume_playback or resume_playback
        if playhead_fraction is not None:
            self._rebuild_playhead_fraction = playhead_fraction
        elif preserve_playhead and not reset_playhead:
            self._rebuild_playhead_fraction = self.engine.get_runtime_stats()["progress_fraction"]

        self._pending_fingerprint = self._preset_fingerprint()
        self._rebuild_token += 1
        self._update_busy_ui()

    def _apply_vibration_presets(self) -> None:
        if self.current_mix is None or not self._preset_dirty:
            return

        was_playing = self.engine.is_playing
        playhead_frac = self.engine.get_runtime_stats()["progress_fraction"]
        cinema_8d = self.cinema_8d_checkbox.isChecked()
        self._queue_vibration_rebuild(
            rebuild_speaker=cinema_8d != self._applied_cinema_8d,
            preserve_playhead=True,
            resume_playback=was_playing,
            playhead_fraction=playhead_frac,
        )
        self._start_vibration_rebuild()

    def _update_busy_ui(self) -> None:
        messages: list[str] = []
        if self._stem_busy:
            messages.append("Separating stems…")
        if self._vibration_rebuild_active:
            messages.append("Updating vibration…")
        elif self._preset_dirty and self.current_mix is not None:
            messages.append("Unapplied preset changes")

        busy = bool(messages)
        self.busy_label.setText("  ".join(messages))
        self.busy_label.setVisible(busy)
        self.busy_progress.setVisible(self._stem_busy or self._vibration_rebuild_active)

    def _start_vibration_rebuild(self) -> None:
        if self.current_mix is None:
            return
        if self.vibration_worker is not None and self.vibration_worker.isRunning():
            return

        self._vibration_rebuild_active = True
        self._update_busy_ui()
        mix = self.current_mix
        token = self._rebuild_token
        options = self._collect_vibration_rebuild_options()
        rebuild_speaker = self._rebuild_needs_speaker
        preserve_playhead = self._rebuild_preserve_playhead and not self._rebuild_reset_playhead
        reset_playhead = self._rebuild_reset_playhead
        resume_playback = self._rebuild_resume_playback
        playhead_fraction = self._rebuild_playhead_fraction

        self._rebuild_needs_speaker = False
        self._rebuild_preserve_playhead = True
        self._rebuild_reset_playhead = False
        self._rebuild_resume_playback = False

        self.vibration_worker = VibrationRebuildWorker(
            token,
            mix,
            rebuild_speaker=rebuild_speaker,
            preserve_playhead=preserve_playhead,
            reset_playhead=reset_playhead,
            resume_playback=resume_playback,
            playhead_fraction=playhead_fraction,
            **options,
        )
        self.vibration_worker.finished_ok.connect(self._on_vibration_rebuild_done)
        self.vibration_worker.failed.connect(self._on_vibration_rebuild_failed)
        self.vibration_worker.start()

    def _on_vibration_rebuild_done(self, result: VibrationRebuildResult) -> None:
        if result.token < self._rebuild_token:
            self._start_vibration_rebuild()
            return

        self.engine.apply_vibration_update(
            result.legs,
            result.mid,
            result.upper_mid,
            result.head,
            result.bass_energy,
            result.generated_freq,
            cinema_8d=result.cinema_8d,
            speaker_program=result.speaker_program,
            preserve_playhead=result.preserve_playhead,
            reset_playhead=result.reset_playhead,
        )
        if result.resume_playback or self.engine.is_playing:
            self.engine.set_position_fraction(result.playhead_fraction)
            self._start_playback_safe(show_errors=False)

        self._last_applied_rebuild_token = max(self._last_applied_rebuild_token, result.token)

        if result.token < self._rebuild_token:
            self._start_vibration_rebuild()
            return

        self._vibration_rebuild_active = False
        self._applied_cinema_8d = result.cinema_8d
        if self._pending_fingerprint is not None and self._preset_fingerprint() == self._pending_fingerprint:
            self._preset_dirty = False
        self._update_preset_summary()
        self._update_apply_button()
        self._update_busy_ui()

    def _on_vibration_rebuild_failed(self, token: int, message: str) -> None:
        if token < self._rebuild_token:
            self._start_vibration_rebuild()
            return

        self._vibration_rebuild_active = False
        self._update_busy_ui()
        QMessageBox.critical(self, "Vibration Rebuild Failed", message)

    def _pick_file(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Select Audio File",
            "",
            "Audio Files (*.wav *.mp3)",
        )
        if not source:
            return

        self._set_controls_enabled(False)
        self.file_label.setText(f"Loading preview: {os.path.basename(source)}")

        self.processing_worker = StemWorker(source, self.temp_dir)
        self.processing_worker.preview_ready.connect(self._processing_preview)
        self.processing_worker.finished_ok.connect(self._processing_complete)
        self.processing_worker.separation_skipped.connect(self._processing_separation_skipped)
        self.processing_worker.failed.connect(self._processing_failed)
        self.processing_worker.start()

    def _apply_mix(
        self,
        mix: PreparedMix,
        status_suffix: str,
        *,
        reset_playhead: bool = True,
        resume_playback: bool = False,
        playhead_fraction: float | None = None,
    ) -> None:
        self.current_mix = normalize_prepared_mix(mix)
        duration_note = ""
        if len(mix.original) >= MAX_SAMPLES:
            duration_note = " (first 20 min — file trimmed for memory)"
        self._loaded_file_label = f"Loaded: {os.path.basename(mix.source_path)}{status_suffix}{duration_note}"
        self.file_label.setText(self._loaded_file_label)
        cinema_8d = self.cinema_8d_checkbox.isChecked()
        self.engine.load_program_base(
            sample_rate=mix.sample_rate,
            original=mix.original,
            cinema_8d=cinema_8d,
            reset_playhead=reset_playhead,
        )
        self.engine.set_volume(self.volume_slider.value() / 100.0)
        self._sync_zone_intensities_to_engine()
        self._preset_dirty = False
        self._applied_cinema_8d = cinema_8d
        self._queue_vibration_rebuild(
            rebuild_speaker=True,
            preserve_playhead=not reset_playhead,
            reset_playhead=reset_playhead,
            resume_playback=resume_playback,
            playhead_fraction=playhead_fraction,
        )
        self._start_vibration_rebuild()
        self._update_preset_summary()
        self._update_apply_button()

    def _processing_preview(self, mix: PreparedMix) -> None:
        try:
            self._apply_mix(
                mix,
                status_suffix=" — quick preview (separating stems...)",
                reset_playhead=True,
            )
            self._set_controls_enabled(True)
        except PortAudioError as exc:
            QMessageBox.critical(self, "Audio Device Error", str(exc))
            self._set_controls_enabled(True)

    def _processing_complete(self, mix: PreparedMix) -> None:
        try:
            was_playing = self.engine.is_playing
            playhead_frac = self.engine.get_runtime_stats()["progress_fraction"]
            self._apply_mix(
                mix,
                status_suffix="",
                reset_playhead=False,
                resume_playback=was_playing,
                playhead_fraction=playhead_frac,
            )
        except PortAudioError as exc:
            QMessageBox.critical(self, "Audio Device Error", str(exc))
        finally:
            self._set_controls_enabled(True)

    def _processing_separation_skipped(self, _message: str) -> None:
        """ML separation unavailable; keep playing the already-loaded quick preview."""
        self._set_controls_enabled(True)
        if self.current_mix is None:
            return
        name = os.path.basename(self.current_mix.source_path)
        self._loaded_file_label = f"Loaded: {name} — quick preview (ML stems unavailable)"
        self.file_label.setText(self._loaded_file_label)

    def _processing_failed(self, message: str) -> None:
        self._set_controls_enabled(True)
        if self.current_mix is not None:
            self._processing_separation_skipped(message)
            return
        QMessageBox.critical(self, "Stem Separation Failed", message)
        self.file_label.setText("No file loaded")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.pick_button.setEnabled(enabled)
        self._stem_busy = not enabled
        self._update_busy_ui()

    def _set_volume(self, value: int) -> None:
        self.engine.set_volume(value / 100.0)

    def _sync_zone_intensities_to_engine(self) -> None:
        self.engine.set_zone_intensities(
            mid=self.zone_sliders["mid"].value() / 100.0,
            legs=self.zone_sliders["legs"].value() / 100.0,
            upper=self.zone_sliders["upper"].value() / 100.0,
            head=self.zone_sliders["head"].value() / 100.0,
        )

    def _set_zone_intensity(self, zone_id: str, value: int) -> None:
        self.zone_labels[zone_id].setText(f"{value}%")
        self.engine.set_zone_intensities(
            mid=self.zone_sliders["mid"].value() / 100.0,
            legs=self.zone_sliders["legs"].value() / 100.0,
            upper=self.zone_sliders["upper"].value() / 100.0,
            head=self.zone_sliders["head"].value() / 100.0,
        )

    def _test_zone(self, zone_id: str) -> None:
        if self.device_combo.currentIndex() < 0:
            QMessageBox.warning(self, "No Device", "Select a 6-channel output device first.")
            return

        self.engine.set_device(self.device_combo.currentData())
        test_freq = None
        if self._is_custom_frequency_mode():
            bands = self._get_custom_frequency_bands()
            zone_band_keys = {
                "legs": "legs",
                "mid": "mid",
                "upper": "upper_mid",
                "head": "head",
            }
            if bands and zone_id in zone_band_keys:
                low_hz, high_hz = bands[zone_band_keys[zone_id]]
                test_freq = (low_hz + high_hz) / 2.0
        try:
            self.engine.trigger_zone_test(zone_id, frequency_hz=test_freq)
        except PortAudioError as exc:
            QMessageBox.critical(self, "Zone Test Error", str(exc))

    def _start_playback_safe(self, *, show_errors: bool = True) -> bool:
        """Open the output stream and start playback, retrying transient PortAudio glitches."""
        last_error: PortAudioError | None = None
        for attempt in range(3):
            try:
                self.engine.play()
                return True
            except PortAudioError as exc:
                last_error = exc
                message = str(exc).lower()
                transient = any(
                    token in message
                    for token in (
                        "library not found",
                        "unanticipated host error",
                        "error querying host api",
                        "auhal",
                    )
                )
                if attempt < 2 and transient:
                    time.sleep(0.08 * (attempt + 1))
                    continue
                break

        if show_errors and last_error is not None:
            QMessageBox.critical(self, "Playback Error", str(last_error))
        return False

    def _play(self) -> None:
        if self.current_mix is None:
            QMessageBox.information(self, "No File", "Load a file first.")
            return

        if self.device_combo.currentIndex() >= 0:
            device_index = self.device_combo.currentData()
            if device_index != self.engine.device_index:
                self.engine.set_device(device_index)

        self._start_playback_safe()

    def _reload_mix_in_engine(self) -> None:
        self._apply_vibration_presets()

    def _on_slider_pressed(self) -> None:
        self._slider_being_dragged = True

    def _on_slider_released(self) -> None:
        self._slider_being_dragged = False
        frac = self.progress_slider.value() / 1000.0
        self.engine.set_position_fraction(frac)

    def _refresh_runtime_ui(self) -> None:
        stats = self.engine.get_runtime_stats()
        if not self._slider_being_dragged:
            self.progress_slider.setValue(int(stats["progress_fraction"] * 1000.0))

        self.legs_rms_label.setText(f"Legs RMS (Ls): {stats['rms_legs']:.3f}")
        self.mid_rms_label.setText(f"Mid RMS (Rs): {stats['rms_mid']:.3f}")
        self.upper_rms_label.setText(f"Upper Mid RMS (LFE): {stats['rms_upper_mid']:.3f}")
        self.head_rms_label.setText(f"Head RMS (C): {stats['rms_head']:.3f}")
        self.bass_energy_label.setText(f"Bass Energy: {stats['bass_energy']:.3f}")
        self.vib_freq_label.setText(f"Generated Vib Freq: {stats['generated_freq_hz']:.1f} Hz")
        self.cpu_label.setText(f"CPU Usage Estimate: {stats['cpu_usage_estimate']:.1f}%")
        self._update_busy_ui()
