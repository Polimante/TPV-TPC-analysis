import os
import time
import logging
from datetime import datetime

import numpy as np

import pyqtgraph as pg
pg.setConfigOptions(antialias=True, background='w', foreground='k')

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QRadioButton, QLabel, QPushButton, QToolButton,
    QLineEdit, QFileDialog, QListWidget, QListWidgetItem,
    QMessageBox, QStatusBar, QCheckBox, QFrame, QSizePolicy,
    QComboBox, QSpinBox, QScrollArea, QProgressBar, QButtonGroup,
    QApplication, QProgressDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QUrl, QEvent, QTimer
from PyQt6.QtGui import QColor, QPalette, QAction, QDesktopServices, QKeySequence

from hardware import OsciloscopioController, ArduinoController
from core import salvar_csv
from version import __version__
import updater

logger = logging.getLogger(__name__)

TAB10 = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]

# ── Design tokens (kept in sync with styles.qss) ────────────────────────────────

C_ACCENT = '#1565c0'
C_MUTED  = '#6b7a8c'
C_GRID   = '#c9d3de'
C_EMPTY  = '#aab6c3'


# ── Small helpers ───────────────────────────────────────────────────────────────

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


_ENG = ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1.0, ''),
        (1e-3, 'm'), (1e-6, 'µ'), (1e-9, 'n'), (1e-12, 'p'))


def _fmt_eng(value: float, unit: str) -> str:
    """Format a number with an engineering prefix, e.g. 0.0124 V → '12.4 mV'."""
    a = abs(float(value))
    if a == 0.0 or not np.isfinite(a):
        return f"0 {unit}"
    for scale, prefix in _ENG:
        if a >= scale:
            return f"{value / scale:.3g} {prefix}{unit}"
    return f"{value:.3g} {unit}"


def _repolish(widget: QWidget):
    """Re-apply the stylesheet after changing a dynamic property."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


# ── Worker thread ───────────────────────────────────────────────────────────────

# (label, explanation) — mirrored by _ProgressCard
STAGES = [
    ("Configure oscilloscope", "averaging, memory depth and data format"),
    ("Stabilise the cell",     "beam dumper closed, V_oc settling"),
    ("Acquire",                "beam dumper open, averaging laser shots"),
    ("Save & plot",            "download waveforms, write CSV + XLSX"),
]


class _Worker(QThread):
    done           = pyqtSignal(dict)
    failed         = pyqtSignal(str)
    progress       = pyqtSignal(str)
    stage          = pyqtSignal(int)    # index into STAGES
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
            self.stage.emit(0)
            self.progress.emit("Configuring scope…")
            self._osc.configurar_scope(p['n_avg'], p['mlen'], p['dtform'])

            # Step 3 — Close beam dumper (safety before settling)
            self._ard.fechar()
            self.dumper_changed.emit(False)

            # Step 4 — Wait settling time (V_oc stabilisation)
            self.stage.emit(1)
            settling = int(p['settling_s'])
            for remaining in range(settling, 0, -1):
                self.progress.emit(f"Settling… {remaining} s remaining")
                time.sleep(1.0)

            # Steps 5-7 — Open beam dumper, arm scope, block until N averages done
            self.stage.emit(2)
            n_avg = p['n_avg']
            avg_label = f"{n_avg}× averaging" if n_avg > 1 else "single shot"
            self.progress.emit(f"Acquiring ({avg_label})…")

            self._ard.abrir()
            self.dumper_changed.emit(True)

            completed = self._osc.aguardar_aquisicao(p['timeout_s'])

            # Step 8 — Close beam dumper IMMEDIATELY
            self._ard.fechar()
            self.dumper_changed.emit(False)

            self.stage.emit(3)
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


class _UpdateCheckWorker(QThread):
    found  = pyqtSignal(object)   # updater.Release, or None if up to date
    failed = pyqtSignal(str)

    def run(self):
        try:
            rel = updater.fetch_latest()
            self.found.emit(rel if updater.is_newer(rel) else None)
        except Exception as exc:
            logger.info("Update check failed: %s", exc)
            self.failed.emit(str(exc))


class _UpdateDownloadWorker(QThread):
    progress = pyqtSignal(int, int)   # done bytes, total bytes
    done     = pyqtSignal(str)        # path of the downloaded exe
    failed   = pyqtSignal(str)

    def __init__(self, release):
        super().__init__()
        self._release = release

    def run(self):
        try:
            path = updater.download_exe(self._release, self.progress.emit)
            self.done.emit(path)
        except Exception as exc:
            logger.exception("Update download failed")
            self.failed.emit(str(exc))


# ── Card container ──────────────────────────────────────────────────────────────

class _Card(QFrame):
    """White rounded panel with an optional numbered header and hint line."""

    def __init__(self, step: str = '', title: str = '', hint: str = ''):
        super().__init__()
        self.setObjectName('card')

        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 13, 15, 15)
        outer.setSpacing(10)

        self.head = QHBoxLayout()
        self.head.setSpacing(9)

        if title:
            if step:
                badge = QLabel(step)
                badge.setObjectName('stepBadge')
                badge.setFixedSize(23, 23)
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.head.addWidget(badge)

            title_lbl = QLabel(title)
            title_lbl.setObjectName('cardTitle')
            self.head.addWidget(title_lbl)
            self.head.addStretch()
            outer.addLayout(self.head)

            if hint:
                hint_lbl = QLabel(hint)
                hint_lbl.setObjectName('cardHint')
                hint_lbl.setWordWrap(True)
                outer.addWidget(hint_lbl)

        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body)


# ── Connection chip (header bar) ────────────────────────────────────────────────

class _ConnChip(QFrame):
    reconnect = pyqtSignal()

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName('chip')
        self.setProperty('on', False)
        self._label = label

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 5, 6, 5)
        h.setSpacing(7)

        self._led = QLabel("●")
        self._led.setObjectName('chipLedOff')

        self._text = QLabel(f"{label} · offline")
        self._text.setObjectName('chipText')

        self._btn = QToolButton()
        self._btn.setText("↺")
        self._btn.setObjectName('chipBtn')
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setToolTip(f"Reconnect {label}")
        self._btn.clicked.connect(self.reconnect)

        h.addWidget(self._led)
        h.addWidget(self._text)
        h.addWidget(self._btn)

        self.set_disconnected()

    def _apply(self, on: bool, text: str, tip: str):
        self.setProperty('on', on)
        self._led.setObjectName('chipLedOn' if on else 'chipLedOff')
        self._text.setText(text)
        self.setToolTip(tip)
        _repolish(self._led)
        _repolish(self)

    def set_connected(self, port: str):
        short = port if len(port) <= 22 else port[:10] + "…" + port[-10:]
        self._apply(True, f"{self._label} · {short}",
                    f"{self._label} connected at {port}")

    def set_disconnected(self):
        self._apply(False, f"{self._label} · offline",
                    f"{self._label} is not connected — click ↺ to try again")


# ── Beam dumper button ──────────────────────────────────────────────────────────

class _BeamBtn(QPushButton):
    def __init__(self):
        super().__init__()
        self._open = False
        self.setObjectName('beamBtn')
        self.setMinimumHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._refresh()

    def _refresh(self):
        if self._open:
            action = "   —   click to block" if self.isEnabled() else ""
            self.setText("BEAM OPEN" + action)
            self.setProperty('state', 'open')
        else:
            action = "   —   click to open" if self.isEnabled() else ""
            self.setText("BEAM BLOCKED" + action)
            self.setProperty('state', 'closed')
        _repolish(self)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self._refresh()

    def is_open(self) -> bool:
        return self._open

    def set_state(self, value: bool):
        self._open = value
        self._refresh()


# ── Acquisition progress card ───────────────────────────────────────────────────

class _ProgressCard(_Card):
    """Vertical checklist showing where the acquisition currently is."""

    def __init__(self):
        super().__init__('', 'Progress')
        self._rows: list[tuple[QLabel, QLabel]] = []

        steps = QVBoxLayout()
        steps.setSpacing(3)
        for name, desc in STAGES:
            row = QHBoxLayout()
            row.setSpacing(9)

            glyph = QLabel("○")
            glyph.setObjectName('stagePending')
            glyph.setFixedWidth(16)
            glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)

            text = QLabel(name)
            text.setObjectName('stageText')
            text.setToolTip(desc)

            row.addWidget(glyph)
            row.addWidget(text, 1)
            steps.addLayout(row)
            self._rows.append((glyph, text))
        self.body.addLayout(steps)

        self._bar = QProgressBar()
        self._bar.setObjectName('busyBar')
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        self._bar.setRange(0, 1)
        self._bar.setValue(0)
        self._bar.hide()
        self.body.addSpacing(2)
        self.body.addWidget(self._bar)

        self._detail = QLabel("Idle — fill in the steps above and press Collect.")
        self._detail.setObjectName('progressDetail')
        self._detail.setWordWrap(True)
        self.body.addWidget(self._detail)

        self.reset()

    def _set_row(self, index: int, state: str):
        glyph, text = self._rows[index]
        glyph.setText({'done': "✓", 'active': "●", 'pending': "○"}[state])
        glyph.setObjectName({'done': 'stageDone', 'active': 'stageActive',
                             'pending': 'stagePending'}[state])
        text.setProperty('state', state)
        _repolish(glyph)
        _repolish(text)

    def reset(self):
        for i in range(len(self._rows)):
            self._set_row(i, 'pending')
        self._bar.hide()
        self._bar.setRange(0, 1)

    def start(self):
        self.reset()
        self._bar.setRange(0, 0)   # indeterminate / busy
        self._bar.show()

    def set_stage(self, index: int):
        for i in range(len(self._rows)):
            self._set_row(i, 'done' if i < index else
                             'active' if i == index else 'pending')

    def finish(self, ok: bool):
        if ok:
            for i in range(len(self._rows)):
                self._set_row(i, 'done')
        self._bar.setRange(0, 1)
        self._bar.hide()

    def set_detail(self, text: str):
        self._detail.setText(text)


# ── Plot card ───────────────────────────────────────────────────────────────────

class _PlotCard(QFrame):
    """A pyqtgraph plot wrapped in a card, with header, readout and crosshair."""

    def __init__(self, title: str, hint: str = ''):
        super().__init__()
        self.setObjectName('card')

        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 11, 13, 13)
        outer.setSpacing(8)

        self.head = QHBoxLayout()
        self.head.setSpacing(9)

        title_lbl = QLabel(title)
        title_lbl.setObjectName('cardTitle')
        self.head.addWidget(title_lbl)

        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setObjectName('cardHint')
            self.head.addWidget(hint_lbl)

        self.head.addStretch()

        self.readout = QLabel("hover the plot to read values")
        self.readout.setObjectName('readout')
        self.readout.setToolTip("Move the mouse over the plot to read the "
                                "value under the cursor")
        self.head.addWidget(self.readout)

        zoom_btn = QToolButton()
        zoom_btn.setText("Fit")
        zoom_btn.setObjectName('miniBtn')
        zoom_btn.setToolTip("Reset zoom to fit all data")
        zoom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        zoom_btn.clicked.connect(self.autoscale)
        self.head.addWidget(zoom_btn)

        outer.addLayout(self.head)

        self.plot = pg.PlotWidget(background='w')
        self.plot.showGrid(x=True, y=True, alpha=0.22)
        self.plot.hideButtons()          # the header "Fit" button replaces it
        for axis in ('left', 'bottom'):
            ax = self.plot.getAxis(axis)
            ax.setStyle(tickTextOffset=6)
            ax.setPen(pg.mkPen(C_GRID))
            ax.setTextPen(pg.mkPen(C_MUTED))
        outer.addWidget(self.plot, 1)

        self._fmt = None
        self._vline = None
        self._empty = None

    # -- crosshair ------------------------------------------------------------

    def enable_crosshair(self, fmt):
        """`fmt(x, y) -> str` renders the readout text."""
        self._fmt = fmt
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen('#b6c2ce', width=1, style=Qt.PenStyle.DashLine),
        )
        self._vline.setZValue(10)
        self.plot.addItem(self._vline, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

    def _mouse_moved(self, pos):
        if self._fmt is None or not self.plot.sceneBoundingRect().contains(pos):
            return
        point = self.plot.getPlotItem().vb.mapSceneToView(pos)
        if self._vline is not None:
            self._vline.setPos(point.x())
        self.readout.setText(self._fmt(point.x(), point.y()))

    # -- empty state ----------------------------------------------------------

    def reset_readout(self):
        self.readout.setText("hover the plot to read values")

    def show_empty(self, message: str):
        self.reset_readout()
        if self._empty is None:
            self._empty = pg.TextItem(message, color=C_EMPTY, anchor=(0.5, 0.5))
            self._empty.setZValue(5)
        self.plot.addItem(self._empty)
        self.plot.getPlotItem().vb.disableAutoRange()
        self.plot.setXRange(0, 1, padding=0)
        self.plot.setYRange(0, 1, padding=0)
        self._empty.setPos(0.5, 0.5)
        self._axis_values(False)

    def hide_empty(self):
        self.reset_readout()
        if self._empty is not None and self._empty.scene() is not None:
            self.plot.removeItem(self._empty)
        self._axis_values(True)
        self.plot.getPlotItem().vb.enableAutoRange()

    def _axis_values(self, show: bool):
        """Blank the tick numbers while the plot has nothing to show."""
        for axis in ('left', 'bottom'):
            self.plot.getAxis(axis).setStyle(showValues=show, tickTextOffset=6)

    def readd_overlays(self):
        """Re-attach crosshair after a plot.clear()."""
        if self._vline is not None and self._vline.scene() is None:
            self.plot.addItem(self._vline, ignoreBounds=True)

    def autoscale(self):
        self.plot.getPlotItem().vb.enableAutoRange()
        self.plot.getPlotItem().vb.autoRange()


# ── Main window ─────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TPV / TPC — Teledyne LeCroy WaveJet 334T  (v{__version__})")
        self.setMinimumSize(1320, 840)

        self._settings = QSettings('TeledyneLab', 'TPVTPC')
        self._osc  = OsciloscopioController()
        self._ard  = ArduinoController()
        self._history: list[dict] = []
        self._meas_curves: list = []
        self._worker: _Worker | None = None
        self._collect_tipo  = ''
        self._collect_nome  = ''
        self._collect_pasta = ''
        self._meas_unit = 's'
        self._update_check: _UpdateCheckWorker | None = None
        self._update_dl: _UpdateDownloadWorker | None = None

        self._setup_logging()
        updater.cleanup_previous_update()
        self._build_menu()
        self._build_ui()
        self._auto_connect()
        # Let the window show first; the check runs in the background.
        QTimer.singleShot(2000, lambda: self._check_updates(manual=False))

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

        meas_menu = mb.addMenu("&Measurement")

        self._act_collect = QAction("&Collect measurement", self)
        self._act_collect.setShortcut(QKeySequence("Ctrl+Return"))
        self._act_collect.setStatusTip("Run the full acquisition sequence")
        self._act_collect.triggered.connect(self._on_coletar)
        meas_menu.addAction(self._act_collect)

        act_reconnect = QAction("&Reconnect instruments", self)
        act_reconnect.setShortcut("F5")
        act_reconnect.setStatusTip("Search again for the oscilloscope and the Arduino")
        act_reconnect.triggered.connect(self._auto_connect)
        meas_menu.addAction(act_reconnect)

        meas_menu.addSeparator()

        act_clear = QAction("Clear &history", self)
        act_clear.setStatusTip("Remove all curves from the comparison plot")
        act_clear.triggered.connect(self._clear_history)
        meas_menu.addAction(act_clear)

        help_menu = mb.addMenu("&Help")

        doc_action = QAction("&Documentation", self)
        doc_action.setShortcut("F1")
        doc_action.setStatusTip("Open software documentation")
        doc_action.triggered.connect(self._open_documentation)
        help_menu.addAction(doc_action)

        update_action = QAction("Check for &updates…", self)
        update_action.setStatusTip("Look for a newer version on GitHub")
        update_action.triggered.connect(lambda: self._check_updates(manual=True))
        help_menu.addAction(update_action)

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
            f"<p>Version {__version__}</p>"
            "<p>Software for automated <b>Transient Photovoltage</b> and "
            "<b>Transient Photocurrent</b> measurements on solar cells.</p>"
            "<p>Processing pipeline:<br>"
            "  &bull; Baseline subtraction (5% pre-pulse)<br>"
            "  &bull; Time alignment using CH1 (Q-Switch trigger)<br>"
            "  &bull; Normalisation &Delta;V / &Delta;V<sub>max</sub></p>"
            "<p>Output: CSV + XLSX with raw and processed columns.</p>"
            "<p><i>For full details: Help &rarr; Documentation (F1)</i></p>"
        )

    # ── Updates ──────────────────────────────────────────────────────────────

    def _check_updates(self, manual: bool):
        if self._update_check and self._update_check.isRunning():
            return
        w = _UpdateCheckWorker()
        w.found.connect(lambda rel: self._on_update_checked(rel, manual))
        w.failed.connect(lambda err: manual and QMessageBox.warning(
            self, "Check for updates",
            f"Could not reach GitHub to check for updates:\n{err}"))
        self._update_check = w
        w.start()

    def _on_update_checked(self, rel, manual: bool):
        if rel is None:
            if manual:
                QMessageBox.information(
                    self, "Check for updates",
                    f"You are running the latest version (v{__version__}).")
            return
        # The automatic check stays quiet about a version the user skipped.
        if not manual and self._settings.value('update/skip', '') == rel.version:
            return

        notes = rel.notes.strip() or "(no release notes)"
        msg = QMessageBox(self)
        msg.setWindowTitle("Update available")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(f"<b>Version {rel.version} is available</b> "
                    f"(you have {__version__}).")
        msg.setDetailedText(notes)

        if updater.is_frozen() and rel.exe_url:
            msg.setInformativeText("Download and install it now? "
                                   "The program will restart.")
            btn_go = msg.addButton("Update now", QMessageBox.ButtonRole.AcceptRole)
        else:
            msg.setInformativeText(
                "This copy runs from source code — update it with "
                "<code>git pull</code>, or download the new version from GitHub.")
            btn_go = msg.addButton("Open GitHub", QMessageBox.ButtonRole.AcceptRole)
        btn_skip = msg.addButton("Skip this version", QMessageBox.ButtonRole.RejectRole)
        msg.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is btn_skip:
            self._settings.setValue('update/skip', rel.version)
        elif clicked is btn_go:
            if updater.is_frozen() and rel.exe_url:
                self._download_update(rel)
            else:
                QDesktopServices.openUrl(QUrl(rel.page_url))

    def _download_update(self, rel):
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(
                self, "Update",
                "A measurement is running. Update again when it has finished "
                "(Help → Check for updates).")
            return

        dlg = QProgressDialog(f"Downloading version {rel.version}…", None, 0, 100, self)
        dlg.setWindowTitle("Updating")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        def on_progress(done: int, total: int):
            dlg.setValue(int(100 * done / total) if total else 0)

        def on_done(path: str):
            dlg.close()
            try:
                updater.install_and_restart(path)
            except Exception as exc:
                logger.exception("Update install failed")
                QMessageBox.critical(
                    self, "Update failed",
                    f"The update could not be installed:\n{exc}\n\n"
                    "If the program is in a protected folder (e.g. Program Files), "
                    "move TPV-TPC.exe to a folder you can write to.")
                return
            QApplication.quit()

        def on_failed(err: str):
            dlg.close()
            QMessageBox.critical(self, "Update failed",
                                 f"The update could not be downloaded:\n{err}")

        w = _UpdateDownloadWorker(rel)
        w.progress.connect(on_progress)
        w.done.connect(on_done)
        w.failed.connect(on_failed)
        self._update_dl = w
        w.start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        root.setObjectName('root')
        root.setAutoFillBackground(True)
        pal = root.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor('#eef2f7'))
        root.setPalette(pal)
        self.setCentralWidget(root)

        page = QVBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        page.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(14)

        # Left column: scrollable setup steps + a footer that always stays visible
        left_col = QWidget()
        left_col.setAutoFillBackground(False)
        left_col.setMinimumWidth(438)
        left_col.setMaximumWidth(478)
        left_col.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_v = QVBoxLayout(left_col)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(12)

        left_scroll = QScrollArea()
        left_scroll.setObjectName('leftScroll')
        left_scroll.setWidget(self._build_left())
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_v.addWidget(left_scroll, 1)

        left_v.addWidget(self._coletar_btn_widget())
        self._progress_card = _ProgressCard()
        left_v.addWidget(self._progress_card)

        body.addWidget(left_col)
        body.addWidget(self._build_right(), 1)

        page.addLayout(body, 1)

        self._sb = QStatusBar()
        self.setStatusBar(self._sb)
        self._state_lbl = QLabel("Ready")
        self._state_lbl.setObjectName('statePill')
        self._sb.addPermanentWidget(self._state_lbl)
        self._sb.showMessage("Welcome — follow steps 1 to 4 on the left, then press Collect.")

    # ── Header bar ────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName('headerBar')

        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 11, 16, 11)
        h.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        t = QLabel("TPV / TPC Measurement")
        t.setObjectName('appTitle')
        s = QLabel("Transient photovoltage and photocurrent  ·  "
                   "Teledyne LeCroy WaveJet 334T")
        s.setObjectName('appSubtitle')
        titles.addWidget(t)
        titles.addWidget(s)

        h.addLayout(titles)
        h.addStretch()

        self._osc_row = _ConnChip("Oscilloscope")
        self._osc_row.reconnect.connect(lambda: self._reconnect('osc'))
        self._ard_row = _ConnChip("Arduino")
        self._ard_row.reconnect.connect(lambda: self._reconnect('ard'))

        h.addWidget(self._osc_row)
        h.addWidget(self._ard_row)
        return bar

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_left(self) -> QWidget:
        w = QWidget()
        w.setAutoFillBackground(False)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(12)

        v.addWidget(self._build_tipo())
        v.addWidget(self._build_arquivo())
        v.addWidget(self._build_acq_settings())
        v.addWidget(self._build_dumper())
        v.addStretch()
        return w

    def _build_tipo(self) -> _Card:
        card = _Card('1', "What are you measuring?")

        self._rb_tpv = QRadioButton("TPV — Transient Photovoltage")
        self._rb_tpc = QRadioButton("TPC — Transient Photocurrent")
        self._rb_tpv.setChecked(True)
        for rb in (self._rb_tpv, self._rb_tpc):
            rb.setObjectName('bigRadio')
            rb.setCursor(Qt.CursorShape.PointingHandCursor)

        grp = QButtonGroup(self)
        grp.addButton(self._rb_tpv)
        grp.addButton(self._rb_tpc)

        tpv_hint = QLabel("Voltage decay after the laser pulse · <b>CH3</b> · 1 MΩ input")
        tpc_hint = QLabel("Current decay after the laser pulse · <b>CH2</b> · 50 Ω input")
        for lbl in (tpv_hint, tpc_hint):
            lbl.setObjectName('radioHint')
            lbl.setWordWrap(True)
            lbl.setContentsMargins(26, 0, 0, 0)

        card.body.addWidget(self._rb_tpv)
        card.body.addWidget(tpv_hint)
        card.body.addSpacing(4)
        card.body.addWidget(self._rb_tpc)
        card.body.addWidget(tpc_hint)

        self._tipo_warn = QLabel()
        self._tipo_warn.setObjectName('warnBox')
        self._tipo_warn.setWordWrap(True)
        card.body.addSpacing(4)
        card.body.addWidget(self._tipo_warn)

        self._rb_tpv.toggled.connect(self._update_tipo_warn)
        self._update_tipo_warn()
        return card

    def _update_tipo_warn(self):
        ch = "CH3 (1 MΩ)" if self._rb_tpv.isChecked() else "CH2 (50 Ω)"
        self._tipo_warn.setText(
            f"⚠  Make sure the cell cable is plugged into <b>{ch}</b> before collecting."
        )

    def _build_arquivo(self) -> _Card:
        card = _Card('2', "Where do you want to save it?")

        lbl_nome = QLabel("Sample name")
        lbl_nome.setObjectName('fieldLabel')
        card.body.addWidget(lbl_nome)

        self._nome_edit = QLineEdit()
        self._nome_edit.setPlaceholderText("e.g. Perovskite_Sample1")
        self._nome_edit.setToolTip(
            "Used as the file name prefix.\n"
            "Files are saved as <name>_<TPV|TPC>_<date>_<time>.csv / .xlsx"
        )
        self._nome_edit.textChanged.connect(lambda: self._mark_error(self._nome_edit, False))
        card.body.addWidget(self._nome_edit)

        lbl_pasta = QLabel("Output folder")
        lbl_pasta.setObjectName('fieldLabel')
        card.body.addWidget(lbl_pasta)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._pasta_edit = QLineEdit()
        self._pasta_edit.setText(self._settings.value('last_pasta', ''))
        self._pasta_edit.setPlaceholderText("Choose a folder…")
        self._pasta_edit.textChanged.connect(lambda: self._mark_error(self._pasta_edit, False))
        pasta_btn = QPushButton("Browse…")
        pasta_btn.setToolTip("Choose the output folder")
        pasta_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pasta_btn.clicked.connect(self._choose_folder)
        row.addWidget(self._pasta_edit, 1)
        row.addWidget(pasta_btn)
        card.body.addLayout(row)

        note = QLabel("Each run writes a CSV and an XLSX with raw, baseline-subtracted "
                      "and normalised columns.")
        note.setObjectName('cardHint')
        note.setWordWrap(True)
        card.body.addWidget(note)
        return card

    def _build_acq_settings(self) -> _Card:
        card = _Card('3', "Acquisition settings")

        self._adv_btn = QToolButton()
        self._adv_btn.setObjectName('linkBtn')
        self._adv_btn.setText("Show ▾")
        self._adv_btn.setCheckable(True)
        self._adv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._adv_btn.setToolTip("Show or hide the detailed acquisition parameters")
        self._adv_btn.toggled.connect(self._toggle_advanced)
        card.head.addWidget(self._adv_btn)

        self._settings_summary = QLabel()
        self._settings_summary.setObjectName('summaryBox')
        self._settings_summary.setWordWrap(True)
        card.body.addWidget(self._settings_summary)

        adv = QWidget()
        adv.setAutoFillBackground(False)
        grid = QGridLayout(adv)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        grid.setColumnMinimumWidth(0, 128)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)

        def _lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setObjectName('fieldLabel')
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
            ("Laser shots averaged", self._avg_combo),
            ("Points per channel",   self._mlen_combo),
            ("Vertical resolution",  self._dtform_combo),
            ("Settling time",        self._settling_spin),
            ("Acquisition timeout",  self._timeout_spin),
        ]
        for row, (text, widget) in enumerate(rows):
            grid.addWidget(_lbl(text), row, 0)
            grid.addWidget(widget,     row, 1)

        for combo in (self._avg_combo, self._mlen_combo, self._dtform_combo):
            combo.currentIndexChanged.connect(self._update_settings_summary)
        for spin in (self._settling_spin, self._timeout_spin):
            spin.valueChanged.connect(self._update_settings_summary)

        adv.hide()
        self._adv_widget = adv
        card.body.addWidget(adv)

        self._update_settings_summary()
        return card

    def _toggle_advanced(self, checked: bool):
        self._adv_widget.setVisible(checked)
        self._adv_btn.setText("Hide ▴" if checked else "Show ▾")

    def _update_settings_summary(self):
        n_avg = self._n_avg()
        shots = "single shot (no averaging)" if n_avg == 1 else f"{n_avg} shots averaged"
        bits  = "8-bit" if self._dtform_combo.currentText() == 'BYTE' else "16-bit"
        self._settings_summary.setText(
            f"<b>{shots}</b> · {self._mlen_combo.currentText()} points/channel · {bits} · "
            f"{self._settling_spin.value()} s settling · "
            f"{self._timeout_spin.value()} s timeout"
        )

    def _n_avg(self) -> int:
        return 1 if self._avg_combo.currentIndex() == 0 else int(self._avg_combo.currentText())

    def _build_dumper(self) -> _Card:
        card = _Card('4', "Laser beam dumper",
                     "Blocks or releases the laser beam. The software closes it "
                     "automatically before and after every acquisition.")
        self._dumper_btn = _BeamBtn()
        self._dumper_btn.setEnabled(False)
        self._dumper_btn.clicked.connect(self._on_dumper)
        card.body.addWidget(self._dumper_btn)
        return card

    def _coletar_btn_widget(self) -> QPushButton:
        self._coletar_btn = QPushButton("▶   Collect measurement")
        self._coletar_btn.setObjectName('primaryBtn')
        self._coletar_btn.setMinimumHeight(56)
        self._coletar_btn.setEnabled(False)
        self._coletar_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._coletar_btn.setToolTip("Run the full acquisition sequence  (Ctrl+Enter)")
        self._coletar_btn.clicked.connect(self._on_coletar)
        return self._coletar_btn

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right(self) -> QWidget:
        w = QWidget()
        w.setAutoFillBackground(False)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # -- CH1 trigger plot --------------------------------------------------
        self._card_ch1 = _PlotCard(
            "Laser trigger (CH1)",
            "used to find t = 0",
        )
        self._plot_ch1 = self._card_ch1.plot
        self._plot_ch1.setLabel('left', 'Voltage (V)')
        self._plot_ch1.setLabel('bottom', 'Time (s)')
        self._ch1_curve = self._plot_ch1.plot(pen=pg.mkPen('#1f77b4', width=2))
        self._t0_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen('#d62728', width=1, style=Qt.PenStyle.DashLine),
            label='t₀', labelOpts={'position': 0.92, 'color': '#d62728',
                                   'fill': (255, 255, 255, 200)},
        )
        self._card_ch1.enable_crosshair(
            lambda x, y: f"t = {x:.4g} {self._ch1_unit}   ·   {y:.4g} V"
        )
        self._ch1_unit = 's'
        self._card_ch1.show_empty("Waiting for the first measurement")

        # -- Measurement plot --------------------------------------------------
        self._card_meas = _PlotCard(
            "Measurement",
            "aligned at the laser pulse (t = 0)",
        )
        self._plot_meas = self._card_meas.plot

        self._view_combo = QComboBox()
        self._view_combo.setObjectName('miniCombo')
        self._view_combo.addItems(["Normalised", "Raw Δ signal"])
        self._view_combo.setToolTip(
            "Normalised: every curve scaled to its own peak (easy to compare shapes).\n"
            "Raw Δ signal: baseline-subtracted volts (TPV) or amps (TPC)."
        )
        self._view_combo.currentIndexChanged.connect(self._rebuild_meas_plot)
        self._card_meas.head.insertWidget(self._card_meas.head.count() - 2,
                                          self._view_combo)

        self._plot_meas.setLabel('left', 'Normalised  (Δ / Δ_max)')
        self._plot_meas.setLabel('bottom', 'Time from pulse')
        self._meas_legend = self._plot_meas.addLegend(offset=(10, 10), labelTextColor='k')
        self._card_meas.enable_crosshair(
            lambda x, y: f"t = {x:.4g} {self._meas_unit}   ·   {y:.4g}"
        )
        self._card_meas.show_empty("Collected curves will be compared here")

        v.addWidget(self._card_ch1, 2)
        v.addWidget(self._card_meas, 3)
        v.addWidget(self._build_history())
        return w

    def _build_history(self) -> _Card:
        card = _Card('', "Collected measurements")

        self._hist_count = QLabel("none yet")
        self._hist_count.setObjectName('cardHint')
        card.head.addWidget(self._hist_count)
        card.head.addStretch()

        clr = QToolButton()
        clr.setText("Clear all")
        clr.setObjectName('miniBtn')
        clr.setCursor(Qt.CursorShape.PointingHandCursor)
        clr.setToolTip("Remove every curve from the comparison plot")
        clr.clicked.connect(self._clear_history)
        card.head.addWidget(clr)

        self._hist_list = QListWidget()
        self._hist_list.setObjectName('histList')
        self._hist_list.setMinimumHeight(112)
        self._hist_list.setMaximumHeight(150)
        self._hist_list.setAlternatingRowColors(True)
        card.body.addWidget(self._hist_list)

        self._hist_empty = QLabel(
            "No measurement collected yet. Curves appear here so you can show, "
            "hide and compare them."
        )
        self._hist_empty.setObjectName('emptyHint')
        self._hist_empty.setWordWrap(True)
        self._hist_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.body.addWidget(self._hist_empty)
        self._hist_list.hide()
        return card

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
        running = bool(self._worker and self._worker.isRunning())

        can_collect = osc_ok and ard_ok and not running
        self._coletar_btn.setEnabled(can_collect)
        self._act_collect.setEnabled(can_collect)
        self._dumper_btn.setEnabled(ard_ok and not running)

        if running:
            self._set_state("Acquiring", 'busy')
            return
        if osc_ok and ard_ok:
            self._set_state("Ready", 'ok')
            self._coletar_btn.setToolTip(
                "Run the full acquisition sequence  (Ctrl+Enter)")
        else:
            missing = [n for n, k in (("oscilloscope", osc_ok), ("Arduino", ard_ok)) if not k]
            self._set_state("Instrument offline", 'off')
            self._coletar_btn.setToolTip(
                "Cannot collect: " + " and ".join(missing) + " not connected. "
                "Use the ↺ buttons in the top-right corner."
            )

    def _set_state(self, text: str, kind: str):
        self._state_lbl.setText(text)
        self._state_lbl.setProperty('kind', kind)
        _repolish(self._state_lbl)

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self._pasta_edit.text()
        )
        if d:
            self._pasta_edit.setText(d)
            self._settings.setValue('last_pasta', d)

    def _mark_error(self, widget: QWidget, is_error: bool):
        if widget.property('error') != is_error:
            widget.setProperty('error', is_error)
            _repolish(widget)

    # ── Beam dumper (manual control) ─────────────────────────────────────────

    def _on_dumper(self):
        new = not self._dumper_btn.is_open()
        self._dumper_btn.set_state(new)
        if new:
            self._ard.abrir()
            self._sb.showMessage("Beam dumper OPEN — the laser reaches the sample")
        else:
            self._ard.fechar()
            self._sb.showMessage("Beam dumper CLOSED — the laser is blocked")

    # ── Data collection ───────────────────────────────────────────────────────

    def _on_coletar(self):
        if not self._coletar_btn.isEnabled():
            return

        nome  = self._nome_edit.text().strip()
        pasta = self._pasta_edit.text().strip()

        if not nome:
            self._mark_error(self._nome_edit, True)
            self._nome_edit.setFocus()
            QMessageBox.warning(self, "Missing sample name",
                                "Type a sample name in step 2 — it becomes the file name.")
            return
        if not pasta:
            self._mark_error(self._pasta_edit, True)
            QMessageBox.warning(self, "Missing output folder",
                                "Choose the folder where the files will be saved (step 2).")
            return
        if not os.path.isdir(pasta):
            self._mark_error(self._pasta_edit, True)
            QMessageBox.warning(self, "Folder not found",
                                f"This folder does not exist:\n{pasta}")
            return

        self._collect_tipo  = 'TPV' if self._rb_tpv.isChecked() else 'TPC'
        self._collect_nome  = nome
        self._collect_pasta = pasta

        ch_msg = "CH3 (1 MΩ input)" if self._collect_tipo == 'TPV' else "CH2 (50 Ω input)"
        n_avg  = self._n_avg()
        shots  = "1 laser shot" if n_avg == 1 else f"{n_avg} laser shots (averaged)"

        msg = QMessageBox(self)
        msg.setWindowTitle("Ready to start")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"<b>Quick check before the laser fires</b>"
            f"<ol>"
            f"<li>Sample cable plugged into <b>{ch_msg}</b>.</li>"
            f"<li>Solar simulator lamp shutter open.</li>"
            f"</ol>"
            f"<p style='color:#5b6b7c;'>This run: <b>{self._collect_tipo}</b> · {shots} · "
            f"{self._settling_spin.value()} s settling · saving to <b>{nome}</b>.</p>"
            f"Press <b>OK</b> to start, or <b>Cancel</b> to go back."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Ok)
        if msg.exec() != QMessageBox.StandardButton.Ok:
            return

        params = {
            'n_avg':      n_avg,
            'mlen':       self._mlen_combo.currentText(),
            'dtform':     self._dtform_combo.currentText(),
            'settling_s': self._settling_spin.value(),
            'timeout_s':  self._timeout_spin.value(),
        }

        self._coletar_btn.setText("⏳   Acquiring…")
        self._progress_card.start()
        self._progress_card.set_detail("Starting acquisition sequence…")
        self._sb.showMessage("Starting acquisition sequence…")

        self._worker = _Worker(self._osc, self._ard, params)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_fail)
        self._worker.progress.connect(self._on_progress)
        self._worker.stage.connect(self._progress_card.set_stage)
        self._worker.dumper_changed.connect(self._dumper_btn.set_state)
        self._worker.start()
        self._refresh_buttons()

    def _on_progress(self, text: str):
        self._progress_card.set_detail(text)
        self._sb.showMessage(text)

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
            self._progress_card.finish(False)
            self._progress_card.set_detail("Could not save the files.")
            QMessageBox.critical(self, "Error", f"Failed to save:\n{exc}")
            self._finalize()
            return

        fp = res['filepath']
        t  = res['t']
        c1 = res['c1']

        # -- CH1 trigger plot --------------------------------------------------
        t_sc, unit = _auto_time(t)
        self._ch1_unit = unit
        self._card_ch1.hide_empty()
        self._plot_ch1.setLabel('bottom', f'Time ({unit})')
        self._ch1_curve.setData(t_sc, c1)

        idx = int(res['idx_t0'])
        if 0 <= idx < len(t_sc):
            if self._t0_line.scene() is None:
                self._plot_ch1.addItem(self._t0_line, ignoreBounds=True)
            self._t0_line.setPos(float(t_sc[idx]))
        self._card_ch1.readd_overlays()

        # -- History entry -----------------------------------------------------
        delta = np.asarray(res['delta_active'], dtype=float)
        peak = float(delta[int(np.argmax(np.abs(delta)))]) if delta.size else 0.0
        unit_sig = 'V' if self._collect_tipo == 'TPV' else 'A'

        color = TAB10[len(self._history) % len(TAB10)]
        entry = dict(
            name=os.path.basename(fp),
            path=fp,
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
            peak=peak,
            unit=unit_sig,
            color=color,
            tipo=self._collect_tipo,
            visible=True,
        )
        self._history.append(entry)
        self._refresh_history_list()
        self._rebuild_meas_plot()

        # Safety: ensure beam dumper is closed after collection
        self._dumper_btn.set_state(False)
        self._ard.fechar()

        self._progress_card.finish(True)
        self._progress_card.set_detail(
            f"Saved <b>{os.path.basename(fp)}</b> · peak "
            f"{_fmt_eng(peak, unit_sig)} · baseline "
            f"{_fmt_eng(res['baseline_active'], unit_sig)}"
        )
        self._sb.showMessage(f"Saved: {fp}")
        self._finalize()

    def _on_fail(self, msg: str):
        self._progress_card.finish(False)
        self._progress_card.set_detail(f"Acquisition failed: {msg}")
        QMessageBox.critical(self, "Hardware Error", f"Acquisition failed:\n{msg}")
        self._finalize()

    def _finalize(self):
        self._coletar_btn.setText("▶   Collect measurement")
        self._worker = None
        self._refresh_buttons()

    # ── Plots and history ─────────────────────────────────────────────────────

    def _rebuild_meas_plot(self):
        raw = self._view_combo.currentIndex() == 1

        for curve in self._meas_curves:
            self._plot_meas.removeItem(curve)
        self._meas_curves.clear()
        try:
            self._meas_legend.clear()
        except Exception:
            logger.debug("Legend clear() unavailable — rebuilding legend")
            try:
                self._plot_meas.removeItem(self._meas_legend)
            except Exception:
                pass
            self._meas_legend = self._plot_meas.addLegend(offset=(10, 10),
                                                          labelTextColor='k')

        shown = 0
        last_unit = 's'
        for entry in self._history:
            if not entry['visible']:
                continue
            t_sc, unit = _auto_time(entry['t_aligned'])
            last_unit = unit
            y = entry['delta_active'] if raw else entry['norm']
            curve = self._plot_meas.plot(
                t_sc, y,
                pen=pg.mkPen(entry['color'], width=2),
                name=entry['name'],
            )
            self._meas_curves.append(curve)
            shown += 1

        self._meas_unit = last_unit
        self._plot_meas.setLabel('bottom', f'Time from pulse ({last_unit})')
        self._plot_meas.setLabel(
            'left', 'Δ signal (V or A)' if raw else 'Normalised  (Δ / Δ_max)')

        if shown == 0:
            self._card_meas.show_empty(
                "Collected curves will be compared here"
                if not self._history else "All curves are hidden — tick one below"
            )
        else:
            self._card_meas.hide_empty()
        self._card_meas.readd_overlays()

    def _refresh_history_list(self):
        self._hist_list.clear()
        for entry in self._history:
            self._add_hist_item(entry)

        has = bool(self._history)
        self._hist_list.setVisible(has)
        self._hist_empty.setVisible(not has)
        visible = sum(1 for e in self._history if e['visible'])
        self._hist_count.setText(
            "none yet" if not has else f"{len(self._history)} saved · {visible} shown"
        )

    def _add_hist_item(self, entry: dict):
        item_w = QWidget()
        item_w.setAutoFillBackground(False)
        row = QHBoxLayout(item_w)
        row.setContentsMargins(8, 4, 6, 4)
        row.setSpacing(8)

        cb = QCheckBox()
        cb.setChecked(entry['visible'])
        cb.setToolTip("Show or hide this curve in the comparison plot")
        cb.stateChanged.connect(lambda s, e=entry: self._toggle_entry(e, s))

        dot = QLabel("●")
        dot.setStyleSheet(
            f"color: {entry['color']}; font-size: 17px; background: transparent;"
        )
        dot.setFixedWidth(18)

        name_lbl = QLabel(entry['name'])
        name_lbl.setToolTip(entry['path'])
        name_lbl.setObjectName('histName')

        tipo_lbl = QLabel(entry['tipo'])
        tipo_lbl.setObjectName('badgeTPV' if entry['tipo'] == 'TPV' else 'badgeTPC')

        peak_lbl = QLabel(_fmt_eng(entry['peak'], entry['unit']))
        peak_lbl.setObjectName('histMeta')
        peak_lbl.setToolTip("Peak of the baseline-subtracted signal")

        ts_lbl = QLabel(entry['ts'])
        ts_lbl.setObjectName('histMeta')

        open_btn = QToolButton()
        open_btn.setText("📂")
        open_btn.setObjectName('rowBtn')
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setToolTip("Open the folder containing this file")
        open_btn.clicked.connect(lambda _=False, e=entry: self._open_entry_folder(e))

        del_btn = QToolButton()
        del_btn.setText("✕")
        del_btn.setObjectName('rowBtn')
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("Remove this curve from the plot (the files are kept)")
        del_btn.clicked.connect(lambda _=False, e=entry: self._remove_entry(e))

        row.addWidget(cb)
        row.addWidget(dot)
        row.addWidget(name_lbl, 1)
        row.addWidget(tipo_lbl)
        row.addWidget(peak_lbl)
        row.addWidget(ts_lbl)
        row.addWidget(open_btn)
        row.addWidget(del_btn)

        li = QListWidgetItem()
        li.setSizeHint(item_w.sizeHint())
        self._hist_list.addItem(li)
        self._hist_list.setItemWidget(li, item_w)

    def _open_entry_folder(self, entry: dict):
        folder = os.path.dirname(entry['path'])
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.warning(self, "Folder not found", f"Cannot open:\n{folder}")

    def _remove_entry(self, entry: dict):
        if entry in self._history:
            self._history.remove(entry)
        self._refresh_history_list()
        self._rebuild_meas_plot()

    def _toggle_entry(self, entry: dict, state):
        entry['visible'] = bool(state)
        visible = sum(1 for e in self._history if e['visible'])
        self._hist_count.setText(f"{len(self._history)} saved · {visible} shown")
        self._rebuild_meas_plot()

    def _clear_history(self):
        if not self._history:
            return
        if QMessageBox.question(
            self, "Clear history",
            f"Remove all {len(self._history)} curves from the plot?\n"
            "The saved CSV/XLSX files are not deleted.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._history.clear()
        self._refresh_history_list()
        self._rebuild_meas_plot()

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
