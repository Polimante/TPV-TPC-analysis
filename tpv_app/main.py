import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QPalette, QColor

from gui import MainWindow


def _light_palette() -> QPalette:
    p = QPalette()
    W = '#eef2f7'
    roles = {
        QPalette.ColorRole.Window:          W,
        QPalette.ColorRole.WindowText:      '#1f2933',
        QPalette.ColorRole.Base:            '#ffffff',
        QPalette.ColorRole.AlternateBase:   '#fafcfe',
        QPalette.ColorRole.ToolTipBase:     '#263238',
        QPalette.ColorRole.ToolTipText:     '#ffffff',
        QPalette.ColorRole.Text:            '#1f2933',
        QPalette.ColorRole.Button:          '#f2f5f8',
        QPalette.ColorRole.ButtonText:      '#33424f',
        QPalette.ColorRole.BrightText:      '#ffffff',
        QPalette.ColorRole.Link:            '#1565c0',
        QPalette.ColorRole.Highlight:       '#1565c0',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Mid:             '#c6d1dc',
        QPalette.ColorRole.Dark:            '#8b98a5',
        QPalette.ColorRole.Shadow:          '#55636f',
        QPalette.ColorRole.Light:           '#ffffff',
        QPalette.ColorRole.Midlight:        '#f2f5f8',
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setPalette(_light_palette())
    app.setFont(QFont('Segoe UI', 10))

    qss = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'styles.qss')
    try:
        with open(qss, encoding='utf-8') as f:
            app.setStyleSheet(f.read())
    except FileNotFoundError:
        pass

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
