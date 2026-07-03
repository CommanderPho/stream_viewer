#  Copyright (C) 2014-2021 Syntrogi Inc dba Intheon. All rights reserved.

from collections import deque, namedtuple
import json
import numpy as np
from qtpy import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from stream_viewer.renderers.data.base import RendererDataTimeSeries
from stream_viewer.renderers.display.pyqtgraph import PGRenderer


MarkerMap = namedtuple('MarkerMap', ['source_id', 'timestamp', 'item'])


class LinePG(RendererDataTimeSeries, PGRenderer):

    plot_modes = ["Sweep", "Scroll"]
    gui_kwargs = dict(RendererDataTimeSeries.gui_kwargs, **PGRenderer.gui_kwargs,
                      offset_channels=bool, reset_colormap=bool,
                      line_width=float, antialias=bool, ylabel_as_title=bool,
                      ylabel_width=int, show_value_trace=bool)

    def __init__(self,
                 # Override inherited
                 auto_scale: str = 'none',
                 show_chan_labels: bool = True,
                 color_set: str = 'viridis',
                 # New
                 offset_channels: bool = True,
                 reset_colormap: bool = False,
                 line_width: float = 0.5,
                 antialias: bool = True,
                 ylabel_as_title: bool = False,
                 ylabel_width: int = None,
                 ylabel: str = None,
                 show_value_trace: bool = True,
                 **kwargs):
        """
        Multi-channel timeseries visualization using pyqtgraph widgets. Channels originating from the same data source
         will be grouped together in a single axes.
        This is a slower but more flexible alternative to `LineVis`.

        Args:
            offset_channels: Set True to force channels into separate y-ranges within the same plot.
                Otherwise the channels are free to overlap.
            reset_colormap: Set True to reset the colormap iterator between data sources. i.e. channel i will have the
                same color for all data sources.
            line_width: Width of all lines.
            antialias: Set True to enable anti-aliasing. I haven't noticed much effect.
            ylabel_as_title: The ylabel is removed and instead a title is created with typical ylabel contents.
            ylabel_width: Force a minimum amount of real-estate to be allocated to the ylabel. This overcomes a problem
                with the required y-width not being calculated until after all the labels and ticks have been
                created.
            ylabel: The ylabel for the plot. If unspecified, the name of the LSL stream is automatically used.
            show_value_trace: Set True to show hovered channel values in a row along the bottom edge.
            **kwargs:
        """
        self._offset_channels = offset_channels
        self._reset_colormap = reset_colormap
        self._line_width = line_width
        self._antialias = antialias
        self._ylabel_as_title = ylabel_as_title
        self._ylabel_width = ylabel_width
        self._ylabel = ylabel
        self._show_value_trace = show_value_trace
        self._requested_auto_scale = auto_scale.lower()  # Actual auto-scale is different depending on n streams.
        self._widget = pg.GraphicsLayoutWidget()
        self._container = QtWidgets.QWidget()
        self._container_layout = QtWidgets.QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)
        self._container_layout.addWidget(self._widget, stretch=1)
        self._trace_label = QtWidgets.QLabel('')
        self._trace_label.setTextFormat(QtCore.Qt.RichText)
        self._trace_label.setWordWrap(False)
        self._trace_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self._trace_label.setVisible(False)
        self._trace_label.setStyleSheet('QLabel { background-color: rgba(20, 22, 28, 200); color: #EEE; padding: 2px 6px; }')
        self._container_layout.addWidget(self._trace_label)
        self._value_trace_proxy = None
        self._trace_vlines = {}
        self._trace_channel_meta = {}
        self._plot_rows = {}
        self._trace_last_key = None
        self._do_yaxis_sync = False
        self._src_last_marker_time = []
        self.marker_texts_pool = deque()
        self._marker_info = deque()
        self._t_expired = -np.inf  # Anything items (e.g. marker strings) older than this can be removed safely.
        super().__init__(show_chan_labels=show_chan_labels, color_set=color_set, **kwargs)
        self._apply_trace_label_font()
        self.reset_renderer()


    @property
    def native_widget(self):
        return self._container

    def reset_renderer(self, reset_channel_labels=True):
        # Clear existing elements
        self._teardown_value_trace_overlay()
        self._widget.clear()
        self._widget.setBackground(self.parse_color_str(self.bg_color))
        self._src_last_marker_time = [-np.inf for _ in range(len(self._data_sources))]
        self._plot_rows = {}
        self._trace_channel_meta = {}
        self._plot_rows = {}

        if len(self.chan_states) == 0:
            return

        chans_per_src = self.chan_states.loc[self.chan_states['vis']].groupby('src')['name'].nunique().values
        n_chans_colormap = np.max(chans_per_src) if self.reset_colormap else np.sum(chans_per_src)
        color_map = self.get_colormap(self.color_set, n_chans_colormap)

        labelStyle = {'color': '#FFF', 'font-size': str(self.font_size) + 'pt'}

        if self._requested_auto_scale == 'all' and (not self.offset_channels or np.all(chans_per_src <= 1)):
            # When doing a global auto scale, we don't actually need it if we are not offsetting channels or each
            #  source has at most 1 channel, because then we can rely on pg's auto scaling.
            self._auto_scale = 'none'
        else:
            self._auto_scale = self._requested_auto_scale

        row_offset = -1
        ch_offset_color = -1
        last_row = 0
        for src_ix, src in enumerate(self._data_sources):
            ch_states = self.chan_states[self.chan_states['src'] == src.identifier]
            n_vis_src = ch_states['vis'].sum()
            if n_vis_src == 0:
                continue

            offset_chans = self.offset_channels and n_vis_src > 1

            buff = self._buffers[src_ix]
            n_samples = buff._data.shape[-1]  # int(np.ceil(stats['srate'] * self.duration))
            t_vec = np.arange(n_samples, dtype=float)
            if src.data_stats['srate'] > 0:
                t_vec /= src.data_stats['srate']

            row_offset += 1
            pw = self._widget.addPlot(row=row_offset, col=0, antialias=self._antialias)
            last_row = row_offset
            self._plot_rows[src_ix] = row_offset
            self._trace_channel_meta[src_ix] = []

            if self.show_chan_labels and not offset_chans:
                legend_bg = QtGui.QColor(self.bg_color)
                legend_bg.setAlphaF(0.5)
                legend = pg.LegendItem(offset=(0, 1), brush=legend_bg)
                legend.setParentItem(pw)
            else:
                legend = None

            pw.showGrid(x=True, y=True, alpha=0.3)
            font = QtGui.QFont("Arial", int(self.font_size - 2))
            pw.setXRange(0, self.duration)
            pw.getAxis("bottom").setTickFont(font)
            pw.getAxis("bottom").setStyle(showValues=self.ylabel_as_title)
            yax = pw.getAxis('left')
            yax.setTickFont(font)
            stream_ylabel = json.loads(src.identifier)['name']
            if 'unit' in ch_states and ch_states['unit'].nunique() == 1:
                # I don't use the `units=` kwarg here because it prepends a magnitude prefix (u, m, k, M),
                # which we don't always want.
                stream_ylabel = stream_ylabel + ' (%s)' % ch_states['unit'].iloc[0]
            pw.setLabel('top' if self.ylabel_as_title else 'left', self._ylabel or stream_ylabel, **labelStyle)
            if self.ylabel_as_title:
                pw.getAxis("top").setStyle(showValues=False)

            # Set the y-range
            if (n_vis_src <= 1 and self._requested_auto_scale != 'none') \
                    or (self._requested_auto_scale == 'all' and not offset_chans):
                # We rely on pg auto range
                pw.enableAutoRange(axis='y')
            elif self._auto_scale == 'none' and not offset_chans:
                # Set the y range manually to a constant.
                pw.setYRange(self.lower_limit, self.upper_limit)
            else:
                # using -0.5 to 0.5, per channel
                major_ticks = []
                minor_ticks = []
                if offset_chans:
                    pw.setYRange(-0.5, n_vis_src - 0.5)
                    chan_ticks = list(zip(range(n_vis_src), ch_states['name']))
                    if self._auto_scale == 'none':
                        data_ticks = [(-0.5, str(self.lower_limit)), (0.5, str(self.upper_limit))]
                        data_ticks += [(_ + 0.5, '') for _ in range(1, n_vis_src)]
                    else:
                        data_ticks = []
                    if self.show_chan_labels:
                        major_ticks = chan_ticks
                        minor_ticks = data_ticks
                    else:
                        major_ticks = data_ticks
                else:
                    # Channels are overlapping within (-0.5, 0.5), but the data are auto-scaled. ticks don't make sense.
                    pw.setYRange(-0.5, 0.5)
                yax.setTicks([major_ticks, minor_ticks])

            ch_offset_row = -1
            for ch_ix, ch_state in ch_states.iterrows():
                if ch_state['vis']:
                    ch_offset_row += 1
                    ch_offset_color = ch_offset_row if self.reset_colormap else (ch_offset_color + 1)
                    pen = pg.mkPen(color_map[ch_offset_color], width=self.line_width)
                    curve = pg.PlotCurveItem(t_vec, buff._data[0], connect='finite', pen=pen, name=ch_state['name'])
                    curve.setPos(0, ch_offset_row if offset_chans else 0)
                    pw.addItem(curve)
                    self._trace_channel_meta[src_ix].append((ch_state['name'], pg.mkColor(color_map[ch_offset_color]).name()))
                    # pdi = pg.PlotDataItem(t_vec, buff._data[0],
                    #                       antialias=self.antialias,
                    #                       pen=pen, name=ch_state['name'])
                    # pw.addItem(pdi)
                    if self.show_chan_labels and not offset_chans:
                        legend.addItem(curve, name=ch_state['name'])

        # Unhide bottom x-axis. Link all axes to bottom axis.
        bottom_pw = self._widget.getItem(last_row, 0)
        bottom_pw.setLabel('bottom', 'Time', units='s', **labelStyle)
        bottom_pw.getAxis("bottom").setStyle(showValues=True)

        # Link for zooming
        for row_ix in range(last_row):
            pw = self._widget.getItem(row_ix, 0)
            pw.setXLink(bottom_pw)

        self._widget.ci.setSpacing(10. if self.ylabel_as_title else 0.)
        # self._widget.setContentsMargins(0., 0., 0., 0.)

        self._do_yaxis_sync = True
        self._apply_trace_label_font()
        self._setup_value_trace_overlay()

    def _apply_trace_label_font(self):
        font = QtGui.QFont("Arial", int(self.font_size - 2))
        self._trace_label.setFont(font)

    def _teardown_value_trace_overlay(self):
        if self._value_trace_proxy is not None:
            try:
                self._value_trace_proxy.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._value_trace_proxy = None
        scene = self._widget.scene()
        if scene is not None and hasattr(scene, 'sigMouseExited'):
            try:
                scene.sigMouseExited.disconnect(self._on_value_trace_mouse_exited)
            except (TypeError, RuntimeError):
                pass
        self._trace_vlines = {}
        self._trace_last_key = None
        self._trace_label.hide()
        self._trace_label.clear()

    def _setup_value_trace_overlay(self):
        if not self.show_value_trace or not self._plot_rows:
            return
        trace_pen = pg.mkPen('#888888', width=1, style=QtCore.Qt.DashLine)
        for src_ix, row_ix in self._plot_rows.items():
            pw = self._widget.getItem(row_ix, 0)
            if pw is None:
                continue
            vline = pg.InfiniteLine(angle=90, movable=False, pen=trace_pen)
            vline.setZValue(1000)
            pw.addItem(vline, ignoreBounds=True)
            vline.setVisible(False)
            self._trace_vlines[src_ix] = vline
        scene = self._widget.scene()
        if scene is not None:
            self._value_trace_proxy = pg.SignalProxy(scene.sigMouseMoved, rateLimit=60, slot=lambda evt: self._on_value_trace_mouse_moved(evt[0]))
            if hasattr(scene, 'sigMouseExited'):
                scene.sigMouseExited.connect(self._on_value_trace_mouse_exited)

    def _hide_value_trace(self):
        self._trace_last_key = None
        self._trace_label.hide()
        for vline in self._trace_vlines.values():
            vline.setVisible(False)

    def _on_value_trace_mouse_exited(self):
        self._hide_value_trace()

    def _plot_under_cursor(self, pos):
        for src_ix, row_ix in self._plot_rows.items():
            pw = self._widget.getItem(row_ix, 0)
            if pw is not None and pw.sceneBoundingRect().contains(pos):
                return src_ix, pw
        return None, None

    def _values_at_display_x(self, src_ix, hover_x):
        if src_ix >= len(self._buffers):
            return None
        buf = self._buffers[src_ix]
        if buf._tvec.size == 0 or buf._data.size == 0:
            return None
        x_mod = buf._tvec % self.duration
        idx = int(np.searchsorted(x_mod, hover_x))
        if idx >= x_mod.size:
            idx = x_mod.size - 1
        elif idx > 0 and abs(x_mod[idx - 1] - hover_x) < abs(x_mod[idx] - hover_x):
            idx = idx - 1
        return buf._data[:, idx]

    def _format_value_trace_html(self, src_ix, hover_x, values):
        parts = [f"<span style='color:#CCC'>t={hover_x:.3f} s</span>"]
        channel_meta = self._trace_channel_meta.get(src_ix, [])
        for ch_ix, (name, color_hex) in enumerate(channel_meta):
            if ch_ix >= values.size:
                break
            val = values[ch_ix]
            val_str = '—' if not np.isfinite(val) else f'{val:.3f}'
            parts.append(f"<span style='color:{color_hex}'>{name}: {val_str}</span>")
        return '&nbsp;&nbsp;'.join(parts)

    def _on_value_trace_mouse_moved(self, pos):
        if not self.show_value_trace:
            return
        src_ix, pw = self._plot_under_cursor(pos)
        if pw is None:
            self._hide_value_trace()
            return
        hover_x = float(np.clip(pw.vb.mapSceneToView(pos).x(), 0.0, self.duration))
        for trace_src_ix, vline in self._trace_vlines.items():
            if trace_src_ix == src_ix:
                vline.setPos(hover_x)
                vline.setVisible(True)
            else:
                vline.setVisible(False)
        values = self._values_at_display_x(src_ix, hover_x)
        if values is None:
            self._trace_label.hide()
            return
        trace_key = (src_ix, round(hover_x, 4))
        if trace_key != self._trace_last_key:
            self._trace_last_key = trace_key
            self._trace_label.setText(self._format_value_trace_html(src_ix, hover_x, values))
        self._trace_label.show()

    def sync_y_axes(self):
        # Get max width
        max_width = self._ylabel_width or 0.
        for src_ix in range(len(self._data_sources)):
            pw = self._widget.getItem(src_ix, 0)
            if pw is None:
                break
            yax = pw.getAxis('left')
            max_width = max(max_width, yax.minimumWidth())

        # Apply widths
        for src_ix in range(len(self._data_sources)):
            pw = self._widget.getItem(src_ix, 0)
            if pw is None:
                break
            pw.getAxis('left').setWidth(max_width)

        self._do_yaxis_sync = False

    def update_visualization(self, data: np.ndarray, timestamps: np.ndarray) -> None:
        if not any([np.any(_) for _ in timestamps[0]]):
            return

        for src_ix in range(len(data)):
            pw = self._widget.getItem(src_ix, 0)
            if pw is None:
                # Can happen if reset_renderer is slow.
                return
            dat, mrk = data[src_ix]
            ts, mrk_ts = timestamps[src_ix]

            if dat.size:
                if self._do_yaxis_sync:
                    self.sync_y_axes()

                # Time series data or a continuous-ified version of irregular data

                per_chan_range = (-0.5, 0.5)  # channel range if data are auto-scaled.

                offset_chans = self.offset_channels and dat.shape[0] > 1
                if self.auto_scale != 'none' or getattr(self, 'is_statically_scaled', False):
                    # dat auto-scaled between (0, 1). Each channel is allocated (-0.5, 0.5)
                    dat -= 0.5
                elif offset_chans:
                    # Data are not auto-scaled, but they need to be scaled here because of the channel offsets.
                    coef = (per_chan_range[1] - per_chan_range[0]) / (self.upper_limit - self.lower_limit)
                    dat = dat - self.lower_limit
                    np.multiply(dat, coef, out=dat)
                    np.add(dat, per_chan_range[0], out=dat)

                for ch_ix, _d in enumerate(dat):
                    curve = pw.curves[ch_ix + (1 if self._antialias else 0)]
                    curve.setData(ts % self.duration, _d)

                    # curve.setData(ts % self.duration, _d)

            if not self._buffers[src_ix]._tvec.size:
                continue
            lead_t = self._buffers[src_ix]._tvec[self._buffers[src_ix]._write_idx]
            self._t_expired = max(lead_t - self._duration, self._t_expired)

            # Remove any markers that are more than 1 sweep older than any time series.
            if isinstance(pw.items[-1], pg.TextItem):
                while (len(self._marker_info) > 0) and (self._marker_info[0].timestamp < self._t_expired):
                    pop_info = self._marker_info.popleft()
                    pw.removeItem(pop_info[2])
                    self.marker_texts_pool.append(pop_info[2])
                    # And update the pw.curves with zeros at pop_info[0]
                    for pw_ix in range(1 if self._antialias else 0, len(pw.curves)):
                        old_x = pw.curves[pw_ix].xData
                        old_y = pw.curves[pw_ix].yData
                        rem_ix = np.searchsorted(old_x, pop_info[1] % self._duration)
                        if old_y[rem_ix] == np.min(old_y) and old_y[rem_ix + 1] == np.nanmax(old_y):
                            rem_ix = rem_ix + 1
                        old_y[rem_ix] = np.nan
                        pw.curves[pw_ix].setData(old_x, old_y)

            if mrk.size:
                b_new = mrk_ts > self._src_last_marker_time[src_ix]
                # y_offset = pw.viewRange()[1][0]
                for _t, _m in zip(mrk_ts[b_new], mrk[b_new]):
                    if len(self.marker_texts_pool) > 0:
                        text = self.marker_texts_pool.popleft()
                        text.setText(_m)
                    else:
                        text = pg.TextItem(text=_m, angle=90)
                        font = QtGui.QFont("Arial", int(self.font_size + 2.0))
                        text.setFont(font)
                    text.setPos(_t % self.duration, -1)  # y_offset)
                    pw.addItem(text)
                    self._marker_info.append(MarkerMap(src_ix, _t, text))

                if np.any(b_new):
                    self._src_last_marker_time[src_ix] = mrk_ts[b_new][-1]

    @property
    def line_width(self):
        return self._line_width

    @line_width.setter
    def line_width(self, value):
        self._line_width = value
        self.reset_renderer(reset_channel_labels=False)

    @property
    def offset_channels(self):
        return self._offset_channels

    @offset_channels.setter
    def offset_channels(self, value):
        self._offset_channels = value
        self.reset_renderer(reset_channel_labels=True)

    @property
    def reset_colormap(self):
        return self._reset_colormap

    @reset_colormap.setter
    def reset_colormap(self, value):
        self._reset_colormap = value
        self.reset_renderer(reset_channel_labels=False)

    @property
    def antialias(self):
        return self._antialias

    @antialias.setter
    def antialias(self, value):
        self._antialias = value
        self.reset_renderer(reset_channel_labels=False)

    @property
    def ylabel_as_title(self):
        return self._ylabel_as_title

    @ylabel_as_title.setter
    def ylabel_as_title(self, value):
        self._ylabel_as_title = value
        self.reset_renderer(reset_channel_labels=True)

    @property
    def ylabel_width(self):
        return self._ylabel_width

    @ylabel_width.setter
    def ylabel_width(self, value):
        self._ylabel_width = value
        self.reset_renderer(reset_channel_labels=True)

    @property
    def show_value_trace(self):
        return self._show_value_trace

    @show_value_trace.setter
    def show_value_trace(self, value):
        self._show_value_trace = bool(value)
        self.reset_renderer(reset_channel_labels=False)

    @RendererDataTimeSeries.auto_scale.setter
    def auto_scale(self, value):
        self._requested_auto_scale = value.lower()
        self.reset_renderer(reset_channel_labels=False)
