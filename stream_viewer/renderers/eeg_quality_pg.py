#  Copyright (C) 2014-2021 Syntrogi Inc dba Intheon. All rights reserved.

"""Real-time EEG quality visualization as color-coded circles in stacked channel lanes."""

from typing import Any, List
import numpy as np
import pyqtgraph as pg
from stream_viewer.renderers.line_pg import LinePG


class EEGQualityPG(LinePG):
    """Render EEG quality values as color-coded circles in stacked channel lanes (live LSL)."""

    gui_kwargs = dict(LinePG.gui_kwargs, marker_size=float)

    def __init__(self, marker_size: float = 8.0, **kwargs):
        self._marker_size = marker_size
        self._unknown_brush = pg.mkBrush('#808080')
        self._quality_brushes = {
            'red': pg.mkBrush('#d32f2f'),
            'orange': pg.mkBrush('#f57c00'),
            'yellow': pg.mkBrush('#fdd835'),
            'green': pg.mkBrush('#43a047'),
        }
        self._symbol_pen = pg.mkPen('#202020', width=0.5)
        self._scatter_items: List[List[pg.ScatterPlotItem]] = []
        super().__init__(offset_channels=True, auto_scale='none', show_chan_labels=True, antialias=False, line_width=0.0, **kwargs)


    def _quality_value_to_brush(self, value: Any):
        if value is None:
            return self._unknown_brush
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return self._unknown_brush
        if not np.isfinite(numeric_value):
            return self._unknown_brush
        if numeric_value <= 1.0:
            return self._quality_brushes['red']
        if numeric_value <= 2.0:
            return self._quality_brushes['orange']
        if numeric_value <= 3.0:
            return self._quality_brushes['yellow']
        return self._quality_brushes['green']


    def reset_renderer(self, reset_channel_labels=True):
        super().reset_renderer(reset_channel_labels=reset_channel_labels)
        self._replace_curves_with_scatters()


    def _replace_curves_with_scatters(self):
        self._scatter_items = [[] for _ in range(len(self._data_sources))]
        if len(self.chan_states) == 0:
            return
        row_offset = -1
        for src_ix, src in enumerate(self._data_sources):
            ch_states = self.chan_states[self.chan_states['src'] == src.identifier]
            n_vis_src = ch_states['vis'].sum()
            if n_vis_src == 0:
                continue
            row_offset += 1
            pw = self._widget.getItem(row_offset, 0)
            if pw is None:
                continue
            for curve in list(pw.curves):
                pw.removeItem(curve)
            scatters = []
            for _, ch_state in ch_states.iterrows():
                if ch_state['vis']:
                    scatter_item = pg.ScatterPlotItem(size=self.marker_size, symbol='o', pen=self._symbol_pen, pxMode=True, name=ch_state['name'])
                    pw.addItem(scatter_item)
                    scatters.append(scatter_item)
            self._scatter_items[src_ix] = scatters


    def update_visualization(self, data: np.ndarray, timestamps: np.ndarray) -> None:
        if len(timestamps) == 0 or not any([np.any(_) for _ in timestamps[0]]):
            return
        row_offset = -1
        for src_ix in range(len(data)):
            if src_ix >= len(self._data_sources):
                break
            ch_states = self.chan_states[self.chan_states['src'] == self._data_sources[src_ix].identifier]
            if ch_states['vis'].sum() == 0:
                continue
            if src_ix >= len(self._scatter_items) or len(self._scatter_items[src_ix]) == 0:
                continue
            row_offset += 1
            pw = self._widget.getItem(row_offset, 0)
            if pw is None:
                return
            dat, _mrk = data[src_ix]
            ts, _mrk_ts = timestamps[src_ix]
            if not dat.size:
                continue
            if self._do_yaxis_sync:
                self.sync_y_axes()
            scatter_items = self._scatter_items[src_ix]
            for ch_ix, channel_values in enumerate(dat):
                if ch_ix >= len(scatter_items):
                    break
                x_vals = np.asarray(ts % self.duration, dtype=float)
                y_vals = np.asarray(channel_values, dtype=float)
                valid_mask = np.isfinite(x_vals) & np.isfinite(y_vals)
                x_vals = x_vals[valid_mask]
                y_vals = y_vals[valid_mask]
                if x_vals.size == 0:
                    scatter_items[ch_ix].setData([], [])
                    continue
                brushes = [self._quality_value_to_brush(v) for v in y_vals]
                scatter_items[ch_ix].setData(x=x_vals, y=np.full(x_vals.shape, float(ch_ix), dtype=float), brush=brushes)


    @property
    def marker_size(self):
        return self._marker_size


    @marker_size.setter
    def marker_size(self, value):
        self._marker_size = value
        self.reset_renderer(reset_channel_labels=False)
