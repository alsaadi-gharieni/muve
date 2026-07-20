"""Main PyQt5 window and interaction logic."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional

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
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from sounddevice import PortAudioError

from audio.gigaport_output import GigaportOutput
from audio.live_input import (
    DEFAULT_VIBE_SYNC_MS,
    LiveAudioEngine,
    find_gigaport_loopback_device,
    list_capture_devices,
    pick_default_capture_device,
)
from audio.synthetic_types import SYNTHETIC_TYPES
from audio.vibration_presets import (
    FREQUENCY_PROFILES,
    SEGMENTATION_PRESETS,
    SATORI_CHANNEL_SUFFIXES,
)
from processors.quick_stems import load_quick_stems
from processors.stem_manager import StemSeparationError, separate_stems
from audio.audio_utils import MAX_SAMPLES


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


class StemWorker(QThread):
    """Background worker: quick preview first, then full stem separation."""

    preview_ready = pyqtSignal(object)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_path: str, work_dir: str) -> None:
        super().__init__()
        self.source_path = source_path
        self.work_dir = work_dir

    def _to_prepared(self, stems: dict, quick_mode: bool) -> PreparedMix:
        return PreparedMix(
            source_path=self.source_path,
            sample_rate=stems["sample_rate"],
            bass=stems["bass"],
            drums=stems["drums"],
            other=stems["other"],
            original=stems["original"],
            quick_mode=quick_mode,
        )

    def run(self) -> None:
        try:
            quick = load_quick_stems(self.source_path)
            self.preview_ready.emit(self._to_prepared(quick, quick_mode=True))

            stems = separate_stems(self.source_path, self.work_dir)
            self.finished_ok.emit(self._to_prepared(stems, quick_mode=False))
        except StemSeparationError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(f"Unexpected processing failure: {exc}")


class MainWindow(QMainWindow):
    """Desktop UI for loading, processing, and playing vibro mixes."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Satori Vibro Test")
        self.resize(950, 760)

        self.engine = GigaportOutput()
        self.live_engine = LiveAudioEngine()
        self.live_active = False
        self._live_silent_ticks = 0
        self.current_mix: Optional[PreparedMix] = None
        self.processing_worker: Optional[StemWorker] = None
        self.temp_dir = tempfile.mkdtemp(prefix="satori_vibro_")
        self._slider_being_dragged = False

        self._build_ui()
        self._refresh_devices()

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._refresh_runtime_ui)
        self.timer.start()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_live_audio()
        self.engine.stop()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)

        self.file_label = QLabel("No file loaded")
        self.file_label.setWordWrap(True)
        main_layout.addWidget(self.file_label)

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

        live_box = QGroupBox("Bluetooth / Live Audio")
        live_layout = QVBoxLayout(live_box)

        live_device_row = QHBoxLayout()
        self.capture_combo = QComboBox()
        live_device_row.addWidget(QLabel("Phone / Input:"))
        live_device_row.addWidget(self.capture_combo, 1)
        self.refresh_capture_btn = QPushButton("Refresh")
        self.refresh_capture_btn.clicked.connect(self._refresh_capture_devices)
        live_device_row.addWidget(self.refresh_capture_btn)
        live_layout.addLayout(live_device_row)

        live_hint = QLabel(
            "With AudioPlaybackConnector (APC): check 'APC plays L/R on Gigaport' below.\n"
            "APC sends phone music to Gigaport speakers (ch 1–2). This app adds vibration (ch 3–6).\n"
            "Pick ★ Gigaport [Speaker Loopback] as input. Start Live = vibration ON. Stop Live = vibration OFF.\n"
            "(APC music keeps playing — only vibration stops.)"
        )
        live_hint.setWordWrap(True)
        live_layout.addWidget(live_hint)

        self.apc_overlay_checkbox = QCheckBox(
            "APC / external router plays L+R on Gigaport (this app adds vibration only)"
        )
        self.apc_overlay_checkbox.toggled.connect(self._on_apc_overlay_toggled)
        live_layout.addWidget(self.apc_overlay_checkbox)

        self.live_signal_label = QLabel("")
        self.live_signal_label.setWordWrap(True)
        live_layout.addWidget(self.live_signal_label)

        sync_row = QHBoxLayout()
        sync_row.addWidget(QLabel("Vibration sync:"))
        self.vibe_sync_slider = QSlider()
        self.vibe_sync_slider.setOrientation(1)
        self.vibe_sync_slider.setRange(-4000, 4000)
        self.vibe_sync_slider.setValue(int(DEFAULT_VIBE_SYNC_MS))
        self.vibe_sync_slider.setToolTip(
            "Slide right (+) to delay vibration (vibration later).\n"
            "Slide left (-) to reduce delay (vibration earlier)."
        )
        self.vibe_sync_slider.valueChanged.connect(self._set_vibe_sync)
        sync_row.addWidget(self.vibe_sync_slider, 1)
        self.vibe_sync_label = QLabel(f"{int(DEFAULT_VIBE_SYNC_MS)} ms")
        sync_row.addWidget(self.vibe_sync_label)
        live_layout.addLayout(sync_row)

        sync_hint = QLabel(
            "Listen on Gigaport ch1-2. Slide right (+) = vibration later; slide left (-) = vibration earlier "
            "(the app delays the audio instead). Watch the audio/vibe delays in the status line. "
            "In APC mode only the vibration can be delayed (audio is not app-controlled)."
        )
        sync_hint.setWordWrap(True)
        live_layout.addWidget(sync_hint)

        live_transport = QHBoxLayout()
        self.live_start_btn = QPushButton("Start Live")
        self.live_start_btn.clicked.connect(self._start_live_audio)
        self.live_stop_btn = QPushButton("Stop Live")
        self.live_stop_btn.clicked.connect(self._stop_live_audio)
        self.live_stop_btn.setEnabled(False)
        self.live_status_label = QLabel("Live audio: stopped")
        live_transport.addWidget(self.live_start_btn)
        live_transport.addWidget(self.live_stop_btn)
        live_transport.addWidget(self.live_status_label, 1)
        live_layout.addLayout(live_transport)

        main_layout.addWidget(live_box)
        self._refresh_capture_devices()

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
        self.segmentation_combo.currentIndexChanged.connect(self._reload_mix_in_engine)
        vibro_layout.addWidget(QLabel("Segmentation:"), 0, 0)
        vibro_layout.addWidget(self.segmentation_combo, 0, 1)

        self.frequency_combo = QComboBox()
        for profile in FREQUENCY_PROFILES:
            self.frequency_combo.addItem(profile.label, profile.id)
        satori_index = next(i for i, p in enumerate(FREQUENCY_PROFILES) if p.id == "satori")
        self.frequency_combo.setCurrentIndex(satori_index)
        self.frequency_combo.currentIndexChanged.connect(self._reload_mix_in_engine)
        vibro_layout.addWidget(QLabel("Frequency Bands:"), 1, 0)
        vibro_layout.addWidget(self.frequency_combo, 1, 1)

        channel_info = QLabel(
            "Bed wiring: C=Head | LFE=Upper | Ls=Legs | Rs=Mid | L/R=original audio"
        )
        channel_info.setWordWrap(True)
        vibro_layout.addWidget(channel_info, 2, 0, 1, 2)

        self.zone_sliders: dict[str, QSlider] = {}
        self.zone_labels: dict[str, QLabel] = {}
        zone_defs = (
            ("mid", "Mid (Rs)"),
            ("legs", "Legs (Ls)"),
            ("upper", "Upper (LFE)"),
            ("head", "Head (C)"),
        )
        for row, (zone_id, zone_name) in enumerate(zone_defs, start=3):
            slider = QSlider()
            slider.setOrientation(1)
            slider.setRange(0, 300)
            slider.setValue(100)
            slider.valueChanged.connect(lambda value, z=zone_id: self._set_zone_intensity(z, value))
            label = QLabel("100%")
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
        vibro_layout.addLayout(test_row, 7, 0, 1, 2)

        self.preset_summary_label = QLabel("")
        self.preset_summary_label.setWordWrap(True)
        vibro_layout.addWidget(self.preset_summary_label, 8, 0, 1, 2)

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
        self._update_preset_summary()

    def _refresh_devices(self) -> None:
        self.device_combo.clear()
        devices = self.engine.list_output_devices(min_channels=6)
        if not devices:
            self.device_combo.addItem(
                "No 6-channel device found — install Gigaport ASIO driver, reconnect USB, restart app",
                None,
            )
            return
        for device in devices:
            tag = " ★ Gigaport" if device.get("is_gigaport") else ""
            label = (
                f"{device['name']}{tag} "
                f"({device['channels']} ch, {device['hostapi']}, #{device['index']})"
            )
            self.device_combo.addItem(label, device["index"])

    def _on_apc_overlay_toggled(self, enabled: bool) -> None:
        if enabled:
            gigaport_loopback = find_gigaport_loopback_device()
            if gigaport_loopback is not None:
                for i in range(self.capture_combo.count()):
                    if self.capture_combo.itemData(i) == gigaport_loopback:
                        self.capture_combo.setCurrentIndex(i)
                        break
            self.live_signal_label.setText(
                "APC mode: capture Gigaport loopback, output vibration to ch 3–6 only."
            )
        else:
            self.live_signal_label.setText("")

    def _refresh_capture_devices(self) -> None:
        self.capture_combo.clear()
        for device in list_capture_devices():
            if device.get("is_default_playback"):
                prefix = "★ DEFAULT "
            elif device.get("is_bluetooth") and device.get("backend") == "soundcard":
                prefix = "★ Phone/BT "
            elif device.get("backend") == "soundcard":
                prefix = "★ "
            elif device.get("is_stereo_bluetooth"):
                prefix = "BT Stereo "
            elif device.get("loopback"):
                prefix = "Loopback "
            elif device.get("is_bluetooth"):
                prefix = "BT "
            else:
                prefix = ""
            label = f"{prefix}{device['name']} ({device.get('index', 'loopback')})"
            self.capture_combo.addItem(label, device)

        default_dev = pick_default_capture_device()
        if default_dev is not None:
            for i in range(self.capture_combo.count()):
                if self.capture_combo.itemData(i) == default_dev:
                    self.capture_combo.setCurrentIndex(i)
                    break

    def _get_vibration_preset_ids(self) -> tuple[str, str]:
        return (
            self.segmentation_combo.currentData(),
            self.frequency_combo.currentData(),
        )

    def _update_preset_summary(self) -> None:
        seg = self.segmentation_combo.currentText()
        freq = self.frequency_combo.currentText()
        ch = ", ".join(SATORI_CHANNEL_SUFFIXES)
        cinema = "ON" if self.cinema_8d_checkbox.isChecked() else "OFF"
        self.preset_summary_label.setText(f"Active: {seg} | {freq} | 8D Cinema: {cinema} | Channels: {ch}")

    def _on_synthetic_toggled(self, enabled: bool) -> None:
        self.synthetic_type_combo.setEnabled(enabled)
        self._update_synthetic_description()
        self._reload_mix_in_engine()

    def _update_synthetic_description(self) -> None:
        idx = self.synthetic_type_combo.currentIndex()
        if idx >= 0:
            self.synthetic_desc_label.setText(SYNTHETIC_TYPES[idx].description)

    def _on_synthetic_type_changed(self) -> None:
        self._update_synthetic_description()
        self._reload_mix_in_engine()

    def _on_cinema_8d_toggled(self) -> None:
        if self.current_mix is None:
            self._update_preset_summary()
            return
        was_playing = self.engine.is_playing
        playhead_frac = self.engine.get_runtime_stats()["progress_fraction"]
        self._prepare_engine_mix(self.current_mix, initial_load=True, reset_playhead=False)
        if was_playing:
            self.engine.set_position_fraction(playhead_frac)
            self.engine.play()

    def _prepare_engine_mix(
        self,
        mix: PreparedMix,
        initial_load: bool = False,
        reset_playhead: bool = True,
    ) -> None:
        segmentation_id, frequency_id = self._get_vibration_preset_ids()
        cinema_8d = self.cinema_8d_checkbox.isChecked()
        synthetic_type_id = self.synthetic_type_combo.currentData()
        if initial_load:
            self.engine.prepare_program(
                sample_rate=mix.sample_rate,
                bass=mix.bass,
                drums=mix.drums,
                other=mix.other,
                original=mix.original,
                synthetic_vibro=self.synthetic_checkbox.isChecked(),
                segmentation_id=segmentation_id,
                frequency_profile_id=frequency_id,
                cinema_8d=cinema_8d,
                synthetic_type_id=synthetic_type_id,
                reset_playhead=reset_playhead,
            )
        else:
            self.engine.update_vibration_only(
                bass=mix.bass,
                drums=mix.drums,
                other=mix.other,
                synthetic_vibro=self.synthetic_checkbox.isChecked(),
                segmentation_id=segmentation_id,
                frequency_profile_id=frequency_id,
                cinema_8d=cinema_8d,
                synthetic_type_id=synthetic_type_id,
            )
        self._update_preset_summary()

    def _pick_file(self) -> None:
        self._stop_live_audio()
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
        self.processing_worker.failed.connect(self._processing_failed)
        self.processing_worker.start()

    def _apply_mix(self, mix: PreparedMix, initial_load: bool, status_suffix: str, reset_playhead: bool = True) -> None:
        self.current_mix = mix
        duration_note = ""
        if len(mix.original) >= MAX_SAMPLES:
            duration_note = " (first 20 min — file trimmed for memory)"
        self.file_label.setText(f"Loaded: {os.path.basename(mix.source_path)}{status_suffix}{duration_note}")
        self._prepare_engine_mix(mix, initial_load=initial_load, reset_playhead=reset_playhead)
        self.engine.set_volume(self.volume_slider.value() / 100.0)
        self._sync_zone_intensities_to_engine()

    def _processing_preview(self, mix: PreparedMix) -> None:
        try:
            self._apply_mix(mix, initial_load=True, status_suffix=" — quick preview (separating stems...)", reset_playhead=True)
            self._set_controls_enabled(True)
        except PortAudioError as exc:
            QMessageBox.critical(self, "Audio Device Error", str(exc))
            self._set_controls_enabled(True)

    def _processing_complete(self, mix: PreparedMix) -> None:
        try:
            was_playing = self.engine.is_playing
            playhead_frac = self.engine.get_runtime_stats()["progress_fraction"]
            self._apply_mix(mix, initial_load=True, status_suffix="", reset_playhead=False)
            if was_playing:
                self.engine.set_position_fraction(playhead_frac)
                self.engine.play()
        except PortAudioError as exc:
            QMessageBox.critical(self, "Audio Device Error", str(exc))
        finally:
            self._set_controls_enabled(True)

    def _processing_failed(self, message: str) -> None:
        self._set_controls_enabled(True)
        QMessageBox.critical(self, "Stem Separation Failed", message)
        self.file_label.setText("No file loaded")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.pick_button.setEnabled(enabled)
        self.play_button.setEnabled(enabled)
        self.pause_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)

    def _set_volume(self, value: int) -> None:
        volume = value / 100.0
        self.engine.set_volume(volume)
        self.live_engine.set_volume(volume)

    def _set_vibe_sync(self, value: int) -> None:
        if value > 0:
            self.vibe_sync_label.setText(f"+{self._format_sync_ms(value)} (later)")
        elif value < 0:
            self.vibe_sync_label.setText(f"-{self._format_sync_ms(abs(value))} (earlier)")
        else:
            self.vibe_sync_label.setText("0 ms")
        self.live_engine.set_vibe_sync_ms(float(value))

    @staticmethod
    def _format_sync_ms(ms: int) -> str:
        if abs(ms) >= 1000:
            return f"{abs(ms) / 1000:.1f} s"
        return f"{ms} ms"

    def _zone_intensity_values(self) -> dict[str, float]:
        return {
            "mid": self.zone_sliders["mid"].value() / 100.0,
            "legs": self.zone_sliders["legs"].value() / 100.0,
            "upper": self.zone_sliders["upper"].value() / 100.0,
            "head": self.zone_sliders["head"].value() / 100.0,
        }

    def _apply_zone_intensities(self) -> None:
        values = self._zone_intensity_values()
        self.engine.set_zone_intensities(**values)
        self.live_engine.set_zone_intensities(**values)

    def _sync_zone_intensities_to_engine(self) -> None:
        self._apply_zone_intensities()

    def _set_zone_intensity(self, zone_id: str, value: int) -> None:
        self.zone_labels[zone_id].setText(f"{value}%")
        self._apply_zone_intensities()

    def _test_zone(self, zone_id: str) -> None:
        if self.device_combo.currentIndex() < 0:
            QMessageBox.warning(self, "No Device", "Select a 6-channel output device first.")
            return

        self.engine.set_device(self.device_combo.currentData())
        try:
            self.engine.trigger_zone_test(zone_id)
        except PortAudioError as exc:
            QMessageBox.critical(self, "Zone Test Error", str(exc))

    def _play(self) -> None:
        if self.current_mix is None:
            QMessageBox.information(self, "No File", "Load a file first.")
            return

        if self.device_combo.currentIndex() >= 0:
            self.engine.set_device(self.device_combo.currentData())

        try:
            self.engine.play()
        except PortAudioError as exc:
            QMessageBox.critical(self, "Playback Error", str(exc))

    def _reload_mix_in_engine(self) -> None:
        if self.live_active:
            segmentation_id, frequency_id = self._get_vibration_preset_ids()
            self.live_engine.update_presets(
                segmentation_id,
                frequency_id,
                synthetic_vibro=self.synthetic_checkbox.isChecked(),
                synthetic_type_id=self.synthetic_type_combo.currentData(),
            )
        if self.current_mix is None:
            self._update_preset_summary()
            return

        self._prepare_engine_mix(self.current_mix, initial_load=False)

    def _start_live_audio(self) -> None:
        if self.capture_combo.currentIndex() < 0:
            QMessageBox.warning(self, "No Input", "Select a phone / Bluetooth input device first.")
            return
        if self.device_combo.currentIndex() < 0:
            QMessageBox.warning(self, "No Output", "Select a 6-channel output device first.")
            return
        output_index = self.device_combo.currentData()
        if output_index is None:
            QMessageBox.warning(
                self,
                "Gigaport Not Found",
                "No 6-channel output device is available.\n\n"
                "On Windows:\n"
                "1. Connect Gigaport eX via USB\n"
                "2. Install the Gigaport ASIO driver from ESI\n"
                "3. Close and restart this app (ASIO is enabled at startup)\n"
                "4. Click Refresh Devices — look for 'Gigaport' with ASIO and 6+ channels",
            )
            return

        capture = self.capture_combo.currentData()
        segmentation_id, frequency_id = self._get_vibration_preset_ids()

        self.engine.stop()
        self.engine.release_output_device()
        self.live_engine.set_volume(self.volume_slider.value() / 100.0)
        self.live_engine.set_vibe_sync_ms(float(self.vibe_sync_slider.value()))
        self._apply_zone_intensities()

        vibration_overlay = self.apc_overlay_checkbox.isChecked()
        if vibration_overlay:
            gigaport_lb = find_gigaport_loopback_device()
            if gigaport_lb is not None:
                capture = gigaport_lb

        try:
            self.live_engine.start(
                capture_device=capture,
                output_device_index=output_index,
                loopback=bool(capture.get("loopback")),
                capture_channels=int(capture.get("channels", 2)),
                segmentation_id=segmentation_id,
                frequency_profile_id=frequency_id,
                synthetic_vibro=self.synthetic_checkbox.isChecked(),
                synthetic_type_id=self.synthetic_type_combo.currentData(),
                vibration_overlay=vibration_overlay,
            )
        except (PortAudioError, RuntimeError, OSError) as exc:
            extra = ""
            if vibration_overlay:
                extra = (
                    "\n\nAPC mode: ensure AudioPlaybackConnector is playing to Gigaport, "
                    "and Gigaport ASIO driver is installed. If this fails, APC may have "
                    "exclusive control — try closing APC and use full app mode instead."
                )
            QMessageBox.critical(
                self,
                "Live Audio Error",
                f"{exc}{extra}",
            )
            return

        self.live_active = True
        self._live_silent_ticks = 0
        self.live_signal_label.setText("Checking for audio signal...")
        self.live_start_btn.setEnabled(False)
        self.live_stop_btn.setEnabled(True)
        self.pick_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.live_status_label.setText(
            "Live vibration: ON (Gigaport ch3-6 tactile only)"
            if vibration_overlay
            else "Live: ON (audio Gigaport ch1-2, tactile ch3-6)"
        )
        self.file_label.setText(
            "Live vibration active — Stop Live stops vibration only"
            if vibration_overlay
            else "Live active — audio + tactile both play through Gigaport (listen on ch1-2). "
            "Use the sync slider to shift vibration earlier/later."
        )

    def _stop_live_audio(self) -> None:
        if not self.live_active and not self.live_engine.is_active:
            return
        self.live_engine.stop()
        self.live_active = False
        self._live_silent_ticks = 0
        self.live_start_btn.setEnabled(True)
        self.live_stop_btn.setEnabled(False)
        self.live_status_label.setText("Live vibration: OFF")
        self.live_signal_label.setText("")
        self._set_controls_enabled(True)
        if self.current_mix is None:
            self.file_label.setText("No file loaded")

    def _on_slider_pressed(self) -> None:
        self._slider_being_dragged = True

    def _on_slider_released(self) -> None:
        self._slider_being_dragged = False
        frac = self.progress_slider.value() / 1000.0
        self.engine.set_position_fraction(frac)

    def _refresh_runtime_ui(self) -> None:
        stats = self.live_engine.get_runtime_stats() if self.live_active else self.engine.get_runtime_stats()
        if not self._slider_being_dragged and not self.live_active:
            self.progress_slider.setValue(int(stats["progress_fraction"] * 1000.0))

        if self.live_active:
            input_rms = stats.get("input_rms", 0.0)
            vibe_lag = stats.get("vibe_lag_ms", 0.0)
            audio_lag = stats.get("audio_lag_ms", 0.0)
            sync_ms = stats.get("vibe_sync_ms", 0.0)
            if input_rms > 0.002:
                self._live_silent_ticks = 0
                self.live_signal_label.setText(
                    f"✓ Receiving audio (input {input_rms:.3f}) — "
                    f"sync {sync_ms:+.0f} ms (audio +{audio_lag:.0f} ms / vibe +{vibe_lag:.0f} ms)"
                )
            else:
                self._live_silent_ticks += 1
                if self._live_silent_ticks > 15:
                    if self.apc_overlay_checkbox.isChecked():
                        self.live_signal_label.setText(
                            "✗ No signal on Gigaport loopback. Check APC is playing to Gigaport, "
                            "then pick ★ Gigaport [Speaker Loopback] and click Refresh."
                        )
                    else:
                        self.live_signal_label.setText(
                            "✗ NO AUDIO on this speaker. Music may be routed elsewhere.\n"
                            "With APC: enable 'APC plays L+R on Gigaport' checkbox above."
                        )

        self.legs_rms_label.setText(f"Legs RMS (Ls): {stats['rms_legs']:.3f}")
        self.mid_rms_label.setText(f"Mid RMS (Rs): {stats['rms_mid']:.3f}")
        self.upper_rms_label.setText(f"Upper Mid RMS (LFE): {stats['rms_upper_mid']:.3f}")
        self.head_rms_label.setText(f"Head RMS (C): {stats['rms_head']:.3f}")
        self.bass_energy_label.setText(f"Bass Energy: {stats['bass_energy']:.3f}")
        self.vib_freq_label.setText(f"Generated Vib Freq: {stats['generated_freq_hz']:.1f} Hz")
        self.cpu_label.setText(f"CPU Usage Estimate: {stats['cpu_usage_estimate']:.1f}%")
