#  Copyright (C) 2026 Pho Hale. All rights reserved.

"""EMOTIV-style circular yaw/roll/pitch orientation gauges for MotionOrientation3D."""

from typing import Literal
from qtpy import QtWidgets, QtCore, QtGui

GaugeKind = Literal['yaw', 'roll', 'pitch']

_GAUGE_LABELS = {'yaw': 'Rotation', 'roll': 'Roll', 'pitch': 'Pitch'}
_CIRCLE_COLOR = QtGui.QColor('#c0c0c0')
_LABEL_COLOR = QtGui.QColor('#9e9e9e')
_BLUE = QtGui.QColor('#1976d2')
_PINK = QtGui.QColor('#e91e63')


class AxisGaugeWidget(QtWidgets.QWidget):
    """Circular gauge with static crosshairs and a rotating axis indicator."""

    def __init__(self, gauge_kind: GaugeKind = 'yaw', parent=None):
        super().__init__(parent)
        self._gauge_kind: GaugeKind = gauge_kind
        self._angle_degrees: float = 0.0
        self.setMinimumSize(72, 88)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)


    def set_angle_degrees(self, angle_degrees: float) -> None:
        self._angle_degrees = float(angle_degrees)
        self.update()


    @staticmethod
    def _shortest_angle_delta_deg(target_deg: float, reference_deg: float) -> float:
        return (target_deg - reference_deg + 180.0) % 360.0 - 180.0


    def set_angle_degrees_unwrapped(self, angle_degrees: float) -> None:
        self._angle_degrees += self._shortest_angle_delta_deg(float(angle_degrees), self._angle_degrees)
        self.update()


    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        label_h = 16
        gauge_size = min(w - 8, h - label_h - 4)
        cx = w * 0.5
        cy = (h - label_h) * 0.5
        radius = gauge_size * 0.42
        circle_pen = QtGui.QPen(_CIRCLE_COLOR, 1.0)
        painter.setPen(circle_pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)
        cross_pen = QtGui.QPen(_CIRCLE_COLOR, 0.75)
        painter.setPen(cross_pen)
        painter.drawLine(QtCore.QPointF(cx - radius, cy), QtCore.QPointF(cx + radius, cy))
        painter.drawLine(QtCore.QPointF(cx, cy - radius), QtCore.QPointF(cx, cy + radius))
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(self._angle_degrees)
        if self._gauge_kind == 'yaw':
            self._paint_yaw_indicator(painter, radius)
        elif self._gauge_kind == 'roll':
            self._paint_roll_indicator(painter, radius)
        else:
            self._paint_pitch_indicator(painter, radius)
        painter.restore()
        label_font = QtGui.QFont()
        label_font.setPointSize(9)
        painter.setFont(label_font)
        painter.setPen(_LABEL_COLOR)
        label_rect = QtCore.QRectF(0, h - label_h, w, label_h)
        painter.drawText(label_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter, _GAUGE_LABELS[self._gauge_kind])


    def _paint_yaw_indicator(self, painter: QtGui.QPainter, radius: float) -> None:
        arm = radius * 0.72
        shaft_pen = QtGui.QPen(QtGui.QColor('#bdbdbd'), 1.25)
        painter.setPen(shaft_pen)
        painter.drawLine(QtCore.QPointF(0, -arm), QtCore.QPointF(0, arm))
        self._draw_arrow_head(painter, QtCore.QPointF(0, -arm), _BLUE, pointing_up=True)
        self._draw_arrow_head(painter, QtCore.QPointF(0, arm), _PINK, pointing_up=False)


    def _paint_roll_indicator(self, painter: QtGui.QPainter, radius: float) -> None:
        arm = radius * 0.72
        shaft_pen = QtGui.QPen(QtGui.QColor('#bdbdbd'), 1.25)
        painter.setPen(shaft_pen)
        painter.drawLine(QtCore.QPointF(-arm, 0), QtCore.QPointF(arm, 0))
        dot_r = 3.5
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(_PINK)
        painter.drawEllipse(QtCore.QPointF(-arm, 0), dot_r, dot_r)
        painter.setBrush(_BLUE)
        painter.drawEllipse(QtCore.QPointF(arm, 0), dot_r, dot_r)


    def _paint_pitch_indicator(self, painter: QtGui.QPainter, radius: float) -> None:
        painter.rotate(-45.0)
        arm = radius * 0.72
        shaft_pen = QtGui.QPen(QtGui.QColor('#bdbdbd'), 1.25)
        painter.setPen(shaft_pen)
        painter.drawLine(QtCore.QPointF(-arm, 0), QtCore.QPointF(arm, 0))
        self._draw_arrow_head(painter, QtCore.QPointF(-arm, 0), _BLUE, pointing_up=False, along_horizontal=True)
        self._draw_arrow_head(painter, QtCore.QPointF(arm, 0), _PINK, pointing_up=True, along_horizontal=True)


    @staticmethod
    def _draw_arrow_head(painter: QtGui.QPainter, tip: QtCore.QPointF, color: QtGui.QColor, pointing_up: bool, along_horizontal: bool = False) -> None:
        painter.setPen(QtGui.QPen(color, 1.5))
        painter.setBrush(color)
        head_len = 7.0
        head_half = 4.0
        if along_horizontal:
            if pointing_up:
                pts = [tip, QtCore.QPointF(tip.x() - head_half, tip.y() - head_len), QtCore.QPointF(tip.x() + head_half, tip.y() - head_len)]
            else:
                pts = [tip, QtCore.QPointF(tip.x() - head_half, tip.y() + head_len), QtCore.QPointF(tip.x() + head_half, tip.y() + head_len)]
        else:
            if pointing_up:
                pts = [tip, QtCore.QPointF(tip.x() - head_half, tip.y() + head_len), QtCore.QPointF(tip.x() + head_half, tip.y() + head_len)]
            else:
                pts = [tip, QtCore.QPointF(tip.x() - head_half, tip.y() - head_len), QtCore.QPointF(tip.x() + head_half, tip.y() - head_len)]
        painter.drawPolygon(QtGui.QPolygonF(pts))
