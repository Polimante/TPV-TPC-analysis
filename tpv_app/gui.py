import os
import time
import logging
from datetime import datetime

import numpy as np

import pyqtgraph as pg
pg.setConfigOptions(antialias=True, background='w', foreground='k')

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QGroupBox, QRadioButton, QLabel, QPushButton,
    QLineEdit, QFileDialog, QListWidget, QListWidgetItem,
    QMessageBox, QStatusBar, QCheckBox, QFrame, QSizePolicy,
    QComboBox, QSpinBox, QScrollArea,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl
from PyQt6.QtGui import QColor, QPalette, QAction, QDesktopServices

from hardware import OsciloscopioController, ArduinoController
from core import salvar_csv

logger = logging.getLogger(__name__)

TAB10 = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]

# ── Button stylesheets ──────────────────────────────────────────────────────────

_CSS_COLETAR = """
QPushButton {
    background-color: #1565c0;
    color: #ffffff;
    font-size: 12pt;
    font-weight: bold;
    border: none;
    border-radius: 7px;
    padding: 12px;
    letter-spacing: 0.5px;
}
QPushButton:hover   { background-color: #1976d2; }
QPushButton:pressed { background-color: #0d47a1; }
QPushButton:disabled {
    background-color: #90a4ae;
    color: #eceff1;
}
"""

_CSS_DUMPER_ABERTO = """
QPushButton {
    background-color: #2e7d32;
    color: #ffffff;
    font-size: 12pt;
    font-weight: bold;
    border: none;
    border-radius: 7px;
    padding: 12px;
}
QPushButton:hover   { background-color: #388e3c; }
QPushButton:pressed { background-color: #1b5e20; }
QPushButton:disabled { background-color: #a5d6a7; color: #e8f5e9; }
"""

_CSS_DUMPER_FECHADO = """
QPushButton {
    background-color: #b71c1c;
    color: #ffffff;
    font-size: 12pt;
    font-weight: bold;
    border: none;
    border-radius: 7px;
    padding: 12px;
}
QPushButton:hover   { background-color: #c62828; }
QPushButton:pressed { background-color: #7f0000; }
QPushButton:disabled { background-color: #ef9a9a; color: #ffebee; }
"""

_CSS_RECONECTAR = """
QPushButton {
    background-color: transparent;
    border: 1px solid #b0bec5;
    border-radius: 14px;
    color: #546e7a;
    font-size: 15px;
    font-weight: bold;
    padding: 0;
}
QPushButton:hover {
    background-color: #e3f2fd;
    border-color: #1565c0;
    color: #1565c0;
}
QPushButton:pressed { background-color: #bbdefb; }
"""


def _auto_time(t: np.ndarray) -> tuple[np.ndarray, str]:
    if t.size == 0:
        return t, 's'
    span = float(np.max(np.abs(t)))
    if span < 1e-6:
        return t * 1e9, 'ns'
    if span < 1e-3:
        return t * 1e6, 'µs'
    if span < 1.0:
        return t * 1e3, 'ms'
    return t, 's'


# ── Worker thread ───────────────────────────────────────────────────────────────

class _Worker(QThread):
    done           = pyqtSignal(dict)
    failed         = pyqtSignal(str)
    progress       = pyqtSignal(str)
    dumper_changed = pyqtSignal(bool)   # True = open, False = closed

    def __init__(self, osc: OsciloscopioController,
                 ard: ArduinoController, params: dict):
        super().__init__()
        self._osc = osc
        self._ard = ard
        self._p   = params

    def run(self):
        p = self._p
        try:
            # Step 2 — Configure scope
            self.progress.emit("Configuring scope…")
            self._osc.configurar_scope(p['n_avg'], p['mlen'], p['dtform'])

            # Step 3 — Close beam dumper (safety before settling)
            self._ard.fechar()
            self.dumper_changed.emit(False)

            # Step 4 — Wait settling time (V_oc stabilisation)
            settling = int(p['settling_s'])
            for remaining in range(settling, 0, -1):
                self.progress.emit(f"Settling… {remaining} s remaining")
                time.sleep(1.0)

            # Steps 5-7 — Open beam dumper, arm scope, block until N averages done
            n_avg = p['n_avg']
            avg_label = f"{n_avg}× avg" if n_avg > 1 else "single"
            self.progress.emit(f"Acquiring ({avg_label})…")

            self._ard.abrir()
            self.dumper_changed.emit(True)

            completed = self._osc.aguardar_aquisicao(p['timeout_s'])

            # Step 8 — Close beam dumper IMMEDIATELY
            self._ard.fechar()
            self.dumper_changed.emit(False)

            if not completed:
                self.progress.emit("Timeout reached — downloading available data…")
            else:
                self.progress.emit("Acquisition complete. Downloading waveforms…")

            # Steps 9-10 — Download
            data = self._osc.baixar_waveforms()
            self.done.emit(data)

        except Exception as exc:
            # Safety: always close beam dumper on any error
            try:
                self._ard.fechar()
                self.dumper_changed.emit(False)
            except Exception:
                pass
            logger.exception("Error in acquisition worker")
            self.failed.emit(str(exc))


# ── Beam dumper button ──────────────────────────────────────────────────────────

class _BeamBtn(QPushButton):
    def __init__(self):
        super().__init__()
        self._open = False
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._refresh()

    def _refresh(self):
        if self._open:
            self.setText("  OPEN")
            self.setStyleSheet(_CSS_DUMPER_ABERTO)
        else:
            self.setText("  CLOSED")
            self.setStyleSheet(_CSS_DUMPER_FECHADO)

    def is_open(self) -> bool:
        return self._open

    def set_state(self, value: bool):
        self._open = value
        self._refresh()


# ── Connection status row ───────────────────────────────────────────────────────

class _ConnRow(QWidget):
    reconnect = pyqtSignal()

    _LED_ON   = "color: #2e7d32; font-size: 20px; background: transparent;"
    _LED_OFF  = "color: #c62828; font-size: 20px; background: transparent;"
    _PORT_ON  = "color: #2e7d32; font-size: 9pt; font-weight: bold; background: transparent;"
    _PORT_OFF = "color: #78909c; font-size: 9pt; background: transparent;"

    def __init__(self, label: str):
        super().__init__()
        self.setAutoFillBackground(False)

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(10)

        self._led = QLabel("●")
        self._led.setFixedSize(22, 22)
        self._led.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._led.setStyleSheet(self._LED_OFF)

        info = QWidget()
        info.setAutoFillBackground(False)
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(1)

        self._name_lbl = QLabel(f"<b>{label}</b>")
        self._name_lbl.setStyleSheet("background: transparent;")

        self._port_lbl = QLabel("Not connected")
        self._port_lbl.setStyleSheet(self._PORT_OFF)

        iv.addWidget(self._name_lbl)
        iv.addWidget(self._port_lbl)

        self._btn = QPushButton("↺")
        self._btn.setFixedSize(30, 30)
        self._btn.setToolTip(f"Reconnect {label}")
        self._btn.setStyleSheet(_CSS_RECONECTAR)
        self._btn.clicked.connect(self.reconnect)

        h.addWidget(self._led)
        h.addWidget(info, 1)
        h.addWidget(self._btn)

    def set_connected(self, port: str):
        self._led.setStyleSheet(self._LED_ON)
        self._port_lbl.setText(port)
        self._port_lbl.setStyleSheet(self._PORT_ON)

    def set_disconnected(self):
        self._led.setStyleSheet(self._LED_OFF)
        self._port_lbl.setText("Not connected")
        self._port_lbl.setStyleSheet(self._PORT_OFF)


# ── Main window ─────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TPV/TPC — Teledyne LeCroy WaveJet 334T")
        self.setMinimumSize(1280, 800)

        self._settings = QSettings('TeledyneLab', 'TPVTPC')
        self._osc  = OsciloscopioController()
        self._ard  = ArduinoController()
        self._history: list[dict] = []
        self._worker: _Worker | None = None
        self._collect_tipo  = ''
        self._collect_nome  = ''
        self._collect_pasta = ''

        self._setup_logging()
        self._build_menu()
        self._build_ui()
        self._auto_connect()

    # ── Logging ──────────────────────────────────────────────────────────────

    def _setup_logging(self):
        logging.basicConfig(
            filename='tpv_app.log',
            level=logging.DEBUG,
            format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        )

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        help_menu = mb.addMenu("&Help")

        doc_action = QAction("&Documentation", self)
        doc_action.setShortcut("F1")
        doc_action.setStatusTip("Open software documentation")
        doc_action.triggered.connect(self._open_documentation)
        help_menu.addAction(doc_action)

        help_menu.addSeparator()

        about_action = QAction("&About", self)
        about_action.setStatusTip("About this software")
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _open_documentation(self):
        doc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'DOCUMENTACAO.md')
        if not os.path.exists(doc_path):
            QMessageBox.warning(
                self, "Documentation",
                f"Documentation file not found:\n{doc_path}"
            )
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(doc_path))
        if not opened:
            QMessageBox.information(
                self, "Documentation",
                f"Open manually:\n{doc_path}"
            )

    def _show_about(self):
        QMessageBox.about(
            self,
            "About — TPV/TPC",
            "<h3>TPV / TPC — Teledyne LeCroy WaveJet 334</h3>"
            "<p>Software for automated <b>Transient Photovoltage</b> and "
            "<b>Transient Photocurrent</b> measurements on solar cells.</p>"
            "<p>Processing pipeline:<br>"
            "  &bull; Baseline subtraction (5% pre-pulse)<br>"
            "  &bull; Time alignment using CH1 (Q-Switch trigger)<br>"
            "  &bull; Normalisation &Delta;V / &Delta;V<sub>max</sub></p>"
            "<p>Output: CSV + XLSX with raw and processed columns.</p>"
            "<p><i>For full details: Help &rarr; Documentation (F1)</i></p>"
        )

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        root.setAutoFillBackground(True)
        pal = root.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor('#f4f6f8'))
        root.setPalette(pal)
        self.setCentralWidget(root)

        main = QHBoxLayout(root)
        main.setContentsMargins(14, 14, 14, 14)
        main.setSpacing(0)

        left_inner = self._build_left()
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_inner)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(440)
        left_scroll.setMaximumWidth(480)
        left_scroll.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        main.addWidget(left_scroll)

        sep = QFrame()
        sep.setObjectName('panel_sep')
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet("background:#c2ccd6; border:none; max-width:1px;")
        sep.setFixedWidth(1)
        main.addSpacing(14)
        main.addWidget(sep)
        main.addSpacing(14)

        main.addWidget(self._build_right(), 1)

        self._sb = QStatusBar()
        self.setStatusBar(self._sb)
        self._sb.showMessage("Ready")

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_left(self) -> QWidget:
        w = QWidget()
        w.setAutoFillBackground(False)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        v.addWidget(self._build_connections())
        v.addWidget(self._build_tipo())
        v.addWidget(self._build_acq_settings())
        v.addWidget(self._build_arquivo())
        v.addWidget(self._build_dumper())
        v.addSpacing(6)
        v.addWidget(self._coletar_btn_widget())
        v.addStretch()
        return w

    def _build_connections(self) -> QGroupBox:
        grp = QGroupBox("Connections")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(0)

        self._osc_row = _ConnRow("Oscilloscope")
        self._osc_row.reconnect.connect(lambda: self._reconnect('osc'))

        sep = QFrame()
        sep.setObjectName('conn_sep')
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet("background: #e8edf2; border: none; max-height: 1px; margin: 0 10px;")
        sep.setFixedHeight(1)

        self._ard_row = _ConnRow("Arduino")
        self._ard_row.reconnect.connect(lambda: self._reconnect('ard'))

        lay.addWidget(self._osc_row)
        lay.addWidget(sep)
        lay.addWidget(self._ard_row)
        return grp

    def _build_tipo(self) -> QGroupBox:
        grp = QGroupBox("Measurement Type")
        lay = QVBoxLayout(grp)
        lay.setSpacing(8)

        self._rb_tpv = QRadioButton("TPV  —  CH3,  1 MΩ")
        self._rb_tpc = QRadioButton("TPC  —  CH2,  50 Ω")
        self._rb_tpv.setChecked(True)
        lay.addWidget(self._rb_tpv)
        lay.addWidget(self._rb_tpc)

        warn = QLabel("⚠  Connect cell cable to the selected channel")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background: #fff8e1;"
            "color: #e65100;"
            "border: 1px solid #ffca28;"
            "border-radius: 5px;"
            "padding: 7px 10px;"
            "font-size: 9pt;"
        )
        lay.addWidget(warn)
        return grp

    def _build_acq_settings(self) -> QGroupBox:
        grp = QGroupBox("Acquisition Settings")
        grid = QGridLayout(grp)
        grid.setContentsMargins(12, 10, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(0, 130)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)

        lbl_style = "font-size: 9.5pt; color: #546e7a;"

        def _lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setStyleSheet(lbl_style)
            l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return l

        self._avg_combo = QComboBox()
        self._avg_combo.addItems(['1 (off)', '2', '4', '8', '16', '32', '64', '128', '256'])
        self._avg_combo.setCurrentIndex(0)
        self._avg_combo.setToolTip(
            "Number of laser shots to average.\n"
            "More averages → less noise, longer exposure.\n"
            "Recommended: 32–64 for perovskite (< 20 s total)."
        )

        self._mlen_combo = QComboBox()
        self._mlen_combo.addItems(['500', '1K', '10K', '100K', '500K'])
        self._mlen_combo.setCurrentText('10K')
        self._mlen_combo.setToolTip(
            "Number of samples stored per channel.\n"
            "More points → better time resolution, slower download."
        )

        self._dtform_combo = QComboBox()
        self._dtform_combo.addItems(['BYTE', 'WORD'])
        self._dtform_combo.setCurrentText('BYTE')
        self._dtform_combo.setToolTip(
            "BYTE: 8-bit, 25 codes/div — fast, visible quantisation steps.\n"
            "WORD: 16-bit, 6400 codes/div — eliminates steps when averaging.\n"
            "Use WORD with N averages ≥ 2 for best vertical resolution."
        )

        self._settling_spin = QSpinBox()
        self._settling_spin.setRange(0, 60)
        self._settling_spin.setValue(5)
        self._settling_spin.setSuffix(' s')
        self._settling_spin.setToolTip(
            "Wait after closing beam dumper before arming.\n"
            "Allows V_oc to stabilise under bias light.\n"
            "Recommended: 5 s for perovskite."
        )

        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(5, 120)
        self._timeout_spin.setValue(30)
        self._timeout_spin.setSuffix(' s')
        self._timeout_spin.setToolTip(
            "Maximum time the beam dumper stays open.\n"
            "Must be > N_avg / laser_frequency.\n"
            "If exceeded, beam dumper closes and available data is saved."
        )

        rows = [
            ("N averages:",    self._avg_combo),
            ("Memory depth:",  self._mlen_combo),
            ("Data format:",   self._dtform_combo),
            ("Settling time:", self._settling_spin),
            ("Acq. timeout:",  self._timeout_spin),
        ]
        for row, (text, widget) in enumerate(rows):
            grid.addWidget(_lbl(text), row, 0)
            grid.addWidget(widget,     row, 1)

        return grp

    def _build_arquivo(self) -> QGroupBox:
        grp = QGroupBox("File")
        lay = QVBoxLayout(grp)
        lay.setSpacing(6)

        lbl_nome = QLabel("Base file name:")
        lbl_nome.setStyleSheet("font-size: 9.5pt; color: #546e7a;")
        lay.addWidget(lbl_nome)

        self._nome_edit = QLineEdit()
        self._nome_edit.setPlaceholderText("e.g. Perovskite_Sample1")
        lay.addWidget(self._nome_edit)

        lbl_pasta = QLabel("Output folder:")
        lbl_pasta.setStyleSheet("font-size: 9.5pt; color: #546e7a; margin-top: 4px;")
        lay.addWidget(lbl_pasta)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._pasta_edit = QLineEdit()
        self._pasta_edit.setText(self._settings.value('last_pasta', ''))
        self._pasta_edit.setPlaceholderText("Select folder…")
        pasta_btn = QPushButton("...")
        pasta_btn.setFixedSize(34, 34)
        pasta_btn.setToolTip("Choose output folder")
        pasta_btn.clicked.connect(self._choose_folder)
        row.addWidget(self._pasta_edit)
        row.addWidget(pasta_btn)
        lay.addLayout(row)
        return grp

    def _build_dumper(self) -> QGroupBox:
        grp = QGroupBox("Beam Dumper")
        lay = QVBoxLayout(grp)
        self._dumper_btn = _BeamBtn()
        self._dumper_btn.setEnabled(False)
        self._dumper_btn.clicked.connect(self._on_dumper)
        lay.addWidget(self._dumper_btn)
        return grp

    def _coletar_btn_widget(self) -> QPushButton:
        self._coletar_btn = QPushButton("▶   Collect Data")
        self._coletar_btn.setMinimumHeight(54)
        self._coletar_btn.setEnabled(False)
        self._coletar_btn.setStyleSheet(_CSS_COLETAR)
        self._coletar_btn.clicked.connect(self._on_coletar)
        return self._coletar_btn

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        w.setAutoFillBackground(False)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        self._plot_ch1 = pg.PlotWidget(background='w')
        self._plot_ch1.setTitle(
            '<span style="color:#455a64; font-size:10pt; font-weight:bold;">'
            'CH1 — Q-Switch Trigger</span>'
        )
        self._plot_ch1.setLabel('left', 'Voltage (V)')
        self._plot_ch1.setLabel('bottom', 'Time (s)')
        self._plot_ch1.showGrid(x=True, y=True, alpha=0.22)
        self._plot_ch1.getAxis('left').setStyle(tickTextOffset=6)
        self._plot_ch1.getAxis('bottom').setStyle(tickTextOffset=6)
        self._ch1_curve = self._plot_ch1.plot(pen=pg.mkPen('#1f77b4', width=2))

        self._plot_meas = pg.PlotWidget(background='w')
        self._plot_meas.setTitle(
            '<span style="color:#455a64; font-size:10pt; font-weight:bold;">'
            'Measurement Channel — normalised, aligned at t=0</span>'
        )
        self._plot_meas.setLabel('left', 'Normalised  (ΔV / ΔV_max)')
        self._plot_meas.setLabel('bottom', 'Time from pulse')
        self._plot_meas.showGrid(x=True, y=True, alpha=0.22)
        self._plot_meas.getAxis('left').setStyle(tickTextOffset=6)
        self._plot_meas.getAxis('bottom').setStyle(tickTextOffset=6)
        self._meas_legend = self._plot_meas.addLegend(
            offset=(10, 10),
            labelTextColor='k',
        )

        v.addWidget(self._plot_ch1, 2)
        v.addWidget(self._plot_meas, 3)
        v.addWidget(self._build_history())
        return w

    def _build_history(self) -> QGroupBox:
        grp = QGroupBox("Measurement History")
        lay = QVBoxLayout(grp)
        lay.setSpacing(8)

        self._hist_list = QListWidget()
        self._hist_list.setMaximumHeight(140)
        self._hist_list.setAlternatingRowColors(True)
        lay.addWidget(self._hist_list)

        clr = QPushButton("Clear history")
        clr.setMaximumWidth(170)
        clr.clicked.connect(self._clear_history)
        lay.addWidget(clr, alignment=Qt.AlignmentFlag.AlignRight)
        return grp

    # ── Connection logic ──────────────────────────────────────────────────────

    def _auto_connect(self):
        self._reconnect('osc')
        self._reconnect('ard')

    def _reconnect(self, key: str):
        if key == 'osc':
            ok, info = self._osc.conectar()
            row, name = self._osc_row, "Oscilloscope"
        else:
            ok, info = self._ard.conectar()
            row, name = self._ard_row, "Arduino"

        if ok:
            row.set_connected(info)
            self._sb.showMessage(f"{name} connected at {info}")
        else:
            row.set_disconnected()
            self._sb.showMessage(f"{name}: {info}")

        self._refresh_buttons()

    def _refresh_buttons(self):
        osc_ok = self._osc.is_connected
        ard_ok = bool(self._ard.ser and self._ard.ser.is_open)
        self._coletar_btn.setEnabled(osc_ok and ard_ok)
        self._dumper_btn.setEnabled(ard_ok)

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self._pasta_edit.text()
        )
        if d:
            self._pasta_edit.setText(d)
            self._settings.setValue('last_pasta', d)

    # ── Beam dumper (manual control) ─────────────────────────────────────────

    def _on_dumper(self):
        new = not self._dumper_btn.is_open()
        self._dumper_btn.set_state(new)
        if new:
            self._ard.abrir()
            self._sb.showMessage("Beam dumper: OPEN")
        else:
            self._ard.fechar()
            self._sb.showMessage("Beam dumper: CLOSED")

    # ── Data collection ───────────────────────────────────────────────────────

    def _on_coletar(self):
        nome  = self._nome_edit.text().strip()
        pasta = self._pasta_edit.text().strip()

        if not nome:
            QMessageBox.warning(self, "Warning", "Enter the base file name.")
            return
        if not pasta:
            QMessageBox.warning(self, "Warning", "Select the output folder.")
            return
        if not os.path.isdir(pasta):
            QMessageBox.warning(self, "Warning", f"Invalid folder:\n{pasta}")
            return

        self._collect_tipo  = 'TPV' if self._rb_tpv.isChecked() else 'TPC'
        self._collect_nome  = nome
        self._collect_pasta = pasta

        # Pre-measurement confirmation
        if self._collect_tipo == 'TPV':
            ch_msg = "CH3 (1 MΩ input)"
        else:
            ch_msg = "CH2 (50 Ω input)"

        msg = QMessageBox(self)
        msg.setWindowTitle("Before you start")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"<b>Measurement checklist</b>"
            f"<ol>"
            f"<li>Connect the sample cable to <b>{ch_msg}</b>.</li>"
            f"<li>Open the solar simulator lamp shutter.</li>"
            f"</ol>"
            f"Click <b>OK</b> to begin acquisition, or <b>Cancel</b> to abort."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Ok)
        if msg.exec() != QMessageBox.StandardButton.Ok:
            return

        # Read acquisition settings
        avg_text = self._avg_combo.currentText()
        n_avg = 1 if self._avg_combo.currentIndex() == 0 else int(avg_text)

        params = {
            'n_avg':      n_avg,
            'mlen':       self._mlen_combo.currentText(),
            'dtform':     self._dtform_combo.currentText(),
            'settling_s': self._settling_spin.value(),
            'timeout_s':  self._timeout_spin.value(),
        }

        self._coletar_btn.setEnabled(False)
        self._coletar_btn.setText("⏳   Acquiring…")
        self._sb.showMessage("Starting acquisition sequence…")

        self._worker = _Worker(self._osc, self._ard, params)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.progress.connect(self._sb.showMessage)
        self._worker.dumper_changed.connect(self._dumper_btn.set_state)
        self._worker.start()

    def _on_done(self, data: dict):
        try:
            res = salvar_csv(
                self._collect_pasta,
                self._collect_nome,
                self._collect_tipo,
                data,
                data['TDIV'],
                amostra=self._collect_nome,
            )
        except Exception as exc:
            logger.exception("Error saving CSV")
            QMessageBox.critical(self, "Error", f"Failed to save:\n{exc}")
            self._finalize()
            return

        fp = res['filepath']
        t  = res['t']
        c1 = res['c1']

        t_sc, unit = _auto_time(t)
        self._plot_ch1.setLabel('bottom', f'Time ({unit})')
        self._ch1_curve.setData(t_sc, c1)

        color = TAB10[len(self._history) % len(TAB10)]
        entry = dict(
            name=os.path.basename(fp),
            ts=datetime.now().strftime('%H:%M:%S'),
            t=t,
            t_aligned=res['t_aligned'],
            c1=c1,
            c2=res['c2'], c3=res['c3'],
            delta_active=res['delta_active'],
            baseline_active=res['baseline_active'],
            norm=res['norm'],
            idx_t0=res['idx_t0'],
            t0_s=res['t0_s'],
            color=color,
            tipo=self._collect_tipo,
            visible=True,
        )
        self._history.append(entry)
        self._add_hist_item(entry)
        self._rebuild_meas_plot()

        # Safety: ensure beam dumper is closed after collection
        self._dumper_btn.set_state(False)
        self._ard.fechar()

        self._sb.showMessage(f"Saved: {fp}")
        self._finalize()

    def _on_fail(self, msg: str):
        QMessageBox.critical(self, "Hardware Error", f"Acquisition failed:\n{msg}")
        self._finalize()

    def _finalize(self):
        self._coletar_btn.setText("▶   Collect Data")
        self._refresh_buttons()

    # ── Plots and history ─────────────────────────────────────────────────────

    def _rebuild_meas_plot(self):
        self._plot_meas.clear()
        self._meas_legend = self._plot_meas.addLegend(
            offset=(10, 10),
            labelTextColor='k',
        )

        last_unit = 's'
        for entry in self._history:
            if not entry['visible']:
                continue
            t_sc, unit = _auto_time(entry['t_aligned'])
            last_unit = unit
            self._plot_meas.plot(
                t_sc, entry['norm'],
                pen=pg.mkPen(entry['color'], width=2),
                name=entry['name'],
            )

        self._plot_meas.setLabel('bottom', f'Time from pulse ({last_unit})')

    def _add_hist_item(self, entry: dict):
        item_w = QWidget()
        item_w.setAutoFillBackground(False)
        row = QHBoxLayout(item_w)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)

        cb = QCheckBox()
        cb.setChecked(True)
        cb.stateChanged.connect(lambda s, e=entry: self._toggle_entry(e, s))

        dot = QLabel("●")
        dot.setStyleSheet(
            f"color: {entry['color']}; font-size: 17px; background: transparent;"
        )
        dot.setFixedWidth(20)

        name_lbl = QLabel(entry['name'])
        name_lbl.setToolTip(entry['name'])
        name_lbl.setStyleSheet("background: transparent;")

        tipo_lbl = QLabel(entry['tipo'])
        tipo_lbl.setStyleSheet(
            "color: #78909c; font-size: 8.5pt; background: transparent;"
        )

        ts_lbl = QLabel(entry['ts'])
        ts_lbl.setStyleSheet(
            "color: #90a4ae; font-size: 8.5pt; background: transparent;"
        )

        row.addWidget(cb)
        row.addWidget(dot)
        row.addWidget(name_lbl, 1)
        row.addWidget(tipo_lbl)
        row.addWidget(ts_lbl)

        li = QListWidgetItem()
        li.setSizeHint(item_w.sizeHint())
        self._hist_list.addItem(li)
        self._hist_list.setItemWidget(li, item_w)

    def _toggle_entry(self, entry: dict, state):
        entry['visible'] = bool(state)
        self._rebuild_meas_plot()

    def _clear_history(self):
        self._history.clear()
        self._hist_list.clear()
        self._plot_meas.clear()
        self._meas_legend = self._plot_meas.addLegend(
            offset=(10, 10),
            labelTextColor='k',
        )

    # ── Close event ───────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(3000)
        try:
            self._ard.desconectar()
        except Exception:
            pass
        try:
            self._osc.desconectar()
        except Exception:
            pass
        event.accept()
