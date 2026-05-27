import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QPalette, QColor

from gui import MainWindow


def _light_palette() -> QPalette:
    p = QPalette()
    W = '#f4f6f8'
    roles = {
        QPalette.ColorRole.Window:          W,
        QPalette.ColorRole.WindowText:      '#212121',
        QPalette.ColorRole.Base:            '#ffffff',
        QPalette.ColorRole.AlternateBase:   '#f0f4f8',
        QPalette.ColorRole.ToolTipBase:     '#37474f',
        QPalette.ColorRole.ToolTipText:     '#ffffff',
        QPalette.ColorRole.Text:            '#212121',
        QPalette.ColorRole.Button:          '#eceff1',
        QPalette.ColorRole.ButtonText:      '#263238',
        QPalette.ColorRole.BrightText:      '#ffffff',
        QPalette.ColorRole.Link:            '#1565c0',
        QPalette.ColorRole.Highlight:       '#1565c0',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Mid:             '#b0bec5',
        QPalette.ColorRole.Dark:            '#78909c',
        QPalette.ColorRole.Shadow:          '#546e7a',
        QPalette.ColorRole.Light:           '#ffffff',
        QPalette.ColorRole.Midlight:        '#eceff1',
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
