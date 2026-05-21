#  Copyright (C) 2026 Pho Hale. All rights reserved.

"""Live 3D head orientation and yaw/roll/pitch gauges from LSL motion (Acc/Gyro) streams."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from qtpy import QtWidgets

from stream_viewer.renderers.data.base import RendererBufferData
from stream_viewer.renderers.display.pyvista import PyVistaRenderer
from stream_viewer.widgets.axis_gauge_widget import AxisGaugeWidget

logger = logging.getLogger(__name__)

MOTION_CHANNEL_NAMES = ('AccX', 'AccY', 'AccZ', 'GyroX', 'GyroY', 'GyroZ')

try:
    import pyvista as pv
    import pyvistaqt as pvqt
    PYVISTA_AVAILABLE = True
except ImportError:
    PYVISTA_AVAILABLE = False
    pv = None
    pvqt = None

try:
    from phopymnehelper.motion_data import MotionData
    from phopymnehelper.resources import get_simplified_head_mesh_path
except Exception:  # pragma: no cover
    MotionData = None  # type: ignore[assignment,misc]
    get_simplified_head_mesh_path = None  # type: ignore[assignment,misc]


class MotionOrientation3D(RendererBufferData, PyVistaRenderer):
    """Fuse live accelerometer/gyroscope data into orientation; show head mesh and EMOTIV-style gauges."""

    COMPAT_ICONTROL = ['MotionOrientation3DControlPanel']
    gui_kwargs = dict(PyVistaRenderer.gui_kwargs, **RendererBufferData.gui_kwargs, head_mesh_path=str, mesh_opacity=float, madgwick_beta=float, euler_sequence=str)

    def __init__(self, head_mesh_path: Optional[str] = None, mesh_opacity: float = 0.92, madgwick_beta: float = 0.1, euler_sequence: str = 'ZYX', bg_color: str = 'white', duration: float = 1.0, **kwargs):
        if not PYVISTA_AVAILABLE:
            raise RuntimeError("pyvista and pyvistaqt are required for MotionOrientation3D")
        if MotionData is None:
            raise RuntimeError("phopymnehelper is required for MotionOrientation3D")
        kwargs.setdefault('auto_scale', 'none')
        kwargs.setdefault('plot_mode', 'Scrolling')
        self._head_mesh_path = Path(head_mesh_path).expanduser().resolve() if head_mesh_path else get_simplified_head_mesh_path()
        self._mesh_opacity = float(mesh_opacity)
        self._madgwick_beta = float(madgwick_beta)
        self._euler_sequence = str(euler_sequence)
        self._q = np.array([1.0, 0.0, 0.0, 0.0], dtype=float)
        self._last_ts: Optional[float] = None
        self._channel_row_index: Dict[str, int] = {}
        self._motion_src_ix: Optional[int] = None
        self._head_mesh: Optional[pv.PolyData] = None
        self._mesh_actor = None
        self._gauge_yaw: Optional[AxisGaugeWidget] = None
        self._gauge_roll: Optional[AxisGaugeWidget] = None
        self._gauge_pitch: Optional[AxisGaugeWidget] = None
        self._ui_built = False
        super().__init__(duration=duration, bg_color=bg_color, **kwargs)
        self._build_ui_shell()


    def _load_and_prepare_head_mesh(self) -> Optional[pv.PolyData]:
        try:
            if not self._head_mesh_path.exists():
                logger.warning(f"Head mesh path does not exist: {self._head_mesh_path}")
                return None
            mesh = pv.read(str(self._head_mesh_path))
            if np.max(np.abs(mesh.points)) > 0.1:
                mesh.points *= 1e-3
            mesh.translate(-np.array(mesh.center), inplace=True)
            return mesh.compute_normals(point_normals=True, cell_normals=False, consistent_normals=True)
        except Exception as e:
            logger.error(f"Failed to load head mesh: {e}")
            return None


    def _clear_container_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)


    def _pyvista_embed_widget(self):
        if self._plotter is None:
            return None
        frame = getattr(self._plotter, 'frame', None)
        if frame is not None:
            return frame
        return getattr(self._plotter, 'interactor', None)


    def _build_ui_shell(self) -> None:
        if self._ui_built:
            return
        self._clear_container_layout()
        if self._plotter is None:
            self._plotter = pvqt.BackgroundPlotter(show=False, auto_update=False)
            try:
                self._plotter.app_window.hide()
            except Exception:
                pass
        plotter_widget = self._pyvista_embed_widget()
        if plotter_widget is not None:
            plotter_widget.setParent(self._container)
            self._layout.addWidget(plotter_widget, stretch=1)
        gauge_row = QtWidgets.QWidget()
        gauge_layout = QtWidgets.QHBoxLayout(gauge_row)
        gauge_layout.setContentsMargins(8, 0, 8, 4)
        gauge_layout.setSpacing(12)
        self._gauge_yaw = AxisGaugeWidget(gauge_kind='yaw')
        self._gauge_roll = AxisGaugeWidget(gauge_kind='roll')
        self._gauge_pitch = AxisGaugeWidget(gauge_kind='pitch')
        for gauge in (self._gauge_yaw, self._gauge_roll, self._gauge_pitch):
            gauge_layout.addWidget(gauge, stretch=1)
        self._layout.addWidget(gauge_row, stretch=0)
        self._ui_built = True


    def _resolve_motion_channel_rows(self) -> bool:
        self._channel_row_index = {}
        self._motion_src_ix = None
        for src_ix, src in enumerate(self._data_sources):
            ch_states = self._sep_chan_states[src_ix] if src_ix < len(self._sep_chan_states) else self.chan_states.loc[self.chan_states['src'] == src.identifier]
            if 'name' not in ch_states.columns:
                continue
            name_to_row = {}
            if 'vis' in ch_states.columns:
                vis_chans = ch_states.loc[ch_states['vis'].astype(bool)]
            else:
                vis_chans = ch_states
            for row_ix, (_, ch_state) in enumerate(vis_chans.iterrows()):
                name_to_row[ch_state['name']] = row_ix
            if all(name in name_to_row for name in MOTION_CHANNEL_NAMES):
                self._motion_src_ix = src_ix
                self._channel_row_index = {name: name_to_row[name] for name in MOTION_CHANNEL_NAMES}
                return True
        return False


    def _rotation_matrix_to_vtk_user_matrix(self, rot_matrix: np.ndarray) -> np.ndarray:
        transform = np.eye(4, dtype=float)
        transform[:3, :3] = rot_matrix
        return transform


    def _apply_mesh_orientation(self, rot_matrix: np.ndarray) -> None:
        if self._mesh_actor is None:
            return
        user_matrix = self._rotation_matrix_to_vtk_user_matrix(rot_matrix)
        try:
            self._mesh_actor.user_matrix = user_matrix
        except Exception:
            try:
                self._mesh_actor.SetUserMatrix(user_matrix)
            except Exception as e:
                logger.warning(f"Could not set mesh user matrix: {e}")
        if self._plotter is not None:
            self._plotter.render()


    def reset(self, reset_channel_labels: bool = True):
        self.reset_buffers()
        self._sep_chan_states = []
        if len(self.chan_states) > 0:
            for src_ix, src in enumerate(self._data_sources):
                self._sep_chan_states.append(self.chan_states.loc[self.chan_states['src'] == src.identifier])
        self.reset_renderer(reset_channel_labels=reset_channel_labels)
        if len(self.chan_states) > 0 and not self.frozen:
            self.restart_timer()


    def reset_renderer(self, reset_channel_labels: bool = True):
        self._q = np.array([1.0, 0.0, 0.0, 0.0], dtype=float)
        self._last_ts = None
        self._build_ui_shell()
        has_motion = self._resolve_motion_channel_rows() if len(self.chan_states) > 0 else False
        self._head_mesh = self._load_and_prepare_head_mesh()
        if self._plotter is None:
            return
        self._plotter.clear()
        self._mesh_actor = None
        bg_rgb = self._pyvista_color_from_str(self.bg_color)
        self._plotter.set_background(bg_rgb)
        try:
            self._plotter.show_axes = False
        except Exception:
            pass
        if len(self.chan_states) == 0:
            self._plotter.add_text("Connect a motion stream (AccX–GyroZ)", font_size=12, color='black')
            self._plotter.render()
            return
        if not has_motion:
            self._plotter.add_text("Motion stream requires AccX–GyroZ channels", font_size=12, color='black')
            self._plotter.render()
            return
        if self._head_mesh is not None:
            self._mesh_actor = self._plotter.add_mesh(self._head_mesh, color='#e0e0e0', opacity=self._mesh_opacity, show_edges=False, smooth_shading=True)
            self._apply_mesh_orientation(np.eye(3))
            try:
                self._plotter.reset_camera()
            except Exception:
                pass
        else:
            self._plotter.add_text("Head mesh not available", font_size=12, color='black')
        self._plotter.render()


    def _latest_motion_sample(self, collect_data: List, collect_timestamps: List) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[float]]:
        if self._motion_src_ix is None or self._motion_src_ix >= len(collect_data):
            return None, None, None
        data_tuple, ts_tuple = collect_data[self._motion_src_ix], collect_timestamps[self._motion_src_ix]
        if data_tuple is None or len(data_tuple) < 1:
            return None, None, None
        data = data_tuple[0]
        timestamps = ts_tuple[0] if ts_tuple is not None and len(ts_tuple) > 0 else np.array([])
        if data is None or data.size == 0 or data.shape[1] == 0:
            return None, None, None
        sample = data[:, -1]
        ts_last = float(timestamps[-1]) if timestamps is not None and len(timestamps) > 0 else None
        return sample, timestamps, ts_last


    def _sample_to_acc_gyro(self, sample: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        acc_g = np.array([sample[self._channel_row_index['AccX']], sample[self._channel_row_index['AccY']], sample[self._channel_row_index['AccZ']]], dtype=float)
        gyro_deg_s = np.array([sample[self._channel_row_index['GyroX']], sample[self._channel_row_index['GyroY']], sample[self._channel_row_index['GyroZ']]], dtype=float)
        return acc_g, gyro_deg_s


    def update_visualization(self, collect_data, collect_timestamps) -> None:
        if self._motion_src_ix is None or self._mesh_actor is None:
            return
        if not isinstance(collect_data, list):
            collect_data, collect_timestamps = [collect_data], [collect_timestamps]
        sample, _, ts_last = self._latest_motion_sample(collect_data, collect_timestamps)
        if sample is None or ts_last is None:
            return
        if not np.all(np.isfinite(sample)):
            return
        acc_g, gyro_deg_s = self._sample_to_acc_gyro(sample)
        src = self._data_sources[self._motion_src_ix]
        srate = float(src.data_stats.get('srate', 16.0) or 16.0)
        if srate <= 0:
            srate = 16.0
        if self._last_ts is None:
            dt = 1.0 / srate
        else:
            dt = max(float(ts_last - self._last_ts), 1.0 / srate)
        self._last_ts = ts_last
        self._q = MotionData.update_quaternion(self._q, gyro_deg_s=gyro_deg_s, acc_g=acc_g, dt=dt, beta=self._madgwick_beta)
        rot_matrix = MotionData.quaternion_to_rot_matrix(self._q)
        self._apply_mesh_orientation(rot_matrix)
        yaw, pitch, roll = MotionData.quaternion_to_euler_deg(self._q, sequence=self._euler_sequence)
        if self._gauge_yaw is not None:
            self._gauge_yaw.set_angle_degrees(yaw)
        if self._gauge_roll is not None:
            self._gauge_roll.set_angle_degrees(roll)
        if self._gauge_pitch is not None:
            self._gauge_pitch.set_angle_degrees(pitch)


    @property
    def head_mesh_path(self) -> str:
        return str(self._head_mesh_path)


    @head_mesh_path.setter
    def head_mesh_path(self, value: str):
        self._head_mesh_path = Path(value).expanduser().resolve()
        self.reset_renderer(reset_channel_labels=True)


    @property
    def madgwick_beta(self) -> float:
        return self._madgwick_beta


    @madgwick_beta.setter
    def madgwick_beta(self, value: float):
        self._madgwick_beta = float(value)
