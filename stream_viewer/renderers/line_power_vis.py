#  Copyright (C) 2014-2021 Syntrogi Inc dba Intheon. All rights reserved.

"""
EEG Power Band Visualization with Global Field Power (GFP).

Displays real-time GFP for four standard EEG frequency bands (Theta, Alpha, Beta, Gamma)
as stacked subplots with linked time axes and bootstrap confidence intervals, inspired by MNE's
time_frequency_global_field_power example.

Reference: https://mne.tools/stable/auto_examples/time_frequency/time_frequency_global_field_power.html
"""

import json
import time
import numpy as np
from scipy import signal
from qtpy import QtGui
import pyqtgraph as pg
from stream_viewer.renderers.data.base import RendererDataTimeSeries
from stream_viewer.renderers.display.pyqtgraph import PGRenderer


# Standard EEG frequency bands
FREQUENCY_BANDS = [
    ('Theta', 4, 7),
    ('Alpha', 8, 12),
    ('Beta', 13, 25),
    ('Gamma', 30, 45),
]

# Colors inspired by matplotlib's winter_r colormap (reversed for visual clarity)
BAND_COLORS = [
    (0, 255, 127),    # Theta - spring green
    (0, 191, 191),    # Alpha - cyan-ish
    (0, 127, 255),    # Beta - dodger blue
    (0, 63, 255),     # Gamma - blue
]


class LinePowerVis(RendererDataTimeSeries, PGRenderer):
    """
    Pyqtgraph-based renderer for EEG power band visualization with Global Field Power (GFP).

    Displays GFP (sum of squares across channels) for four standard frequency bands
    as stacked subplots with synchronized/linked time axes:
    - Theta (4-7 Hz)
    - Alpha (8-12 Hz)
    - Beta (13-25 Hz)
    - Gamma (30-45 Hz)

    Each band is displayed in its own subplot with bootstrap confidence intervals
    shown as shaded regions around the GFP line.
    """
    COMPAT_ICONTROL = ['LinePowerControlPanel']
    plot_modes = ["Sweep", "Scroll"]
    gui_kwargs = dict(
        RendererDataTimeSeries.gui_kwargs,
        **PGRenderer.gui_kwargs,
        filter_order=int,
        n_bootstrap=int,
        baseline_start=float,
        baseline_end=float,
        show_confidence=bool,
        line_width=float,
        antialias=bool,
    )

    def __init__(
        self,
        # Override inherited
        auto_scale: str = 'none',
        show_chan_labels: bool = False,
        color_set: str = 'viridis',
        # New parameters
        filter_order: int = 4,
        n_bootstrap: int = 100,
        baseline_start: float = None,
        baseline_end: float = 0.0,
        show_confidence: bool = False,
        line_width: float = 0.5,
        antialias: bool = True,
        **kwargs
    ):
        """
        EEG Power Band Visualization with Global Field Power.

        Args:
            filter_order: Order of the Butterworth bandpass filter.
            n_bootstrap: Number of bootstrap iterations for confidence intervals.
            baseline_start: Start time (in seconds from buffer start) for baseline correction.
                           None means from the beginning of the buffer.
            baseline_end: End time (in seconds from buffer start) for baseline correction.
            show_confidence: Whether to display bootstrap confidence interval regions.
            line_width: Width of the GFP lines.
            antialias: Enable anti-aliasing for smoother lines.
            **kwargs: Additional arguments passed to parent classes.
        """
        self._filter_order = filter_order
        self._n_bootstrap = n_bootstrap
        self._baseline_start = baseline_start
        self._baseline_end = baseline_end
        self._show_confidence = show_confidence
        self._line_width = line_width
        self._antialias = antialias
        
        self._initial_auto_scale_duration = 5.0
        self._first_update_time = None

        # Set _auto_scale early to avoid race conditions during initialization
        self._auto_scale = auto_scale

        self._widget = pg.GraphicsLayoutWidget()
        self._plot_widgets = []  # One PlotWidget per band
        self._curves = []  # One PlotCurveItem per band
        self._fill_items = []  # One FillBetweenItem per band for confidence intervals
        self._ci_upper_curves = []  # Upper CI boundary curves
        self._ci_lower_curves = []  # Lower CI boundary curves

        # Cache for filter coefficients (avoid recomputing each frame)
        self._filter_cache = {}
        self._last_srate = None
        
        # Track y-axis ranges for each band to prevent continuous rescaling
        self._y_ranges = {}  # band_idx -> (min, max)
        self._range_update_counter = 0  # Counter to throttle range updates

        super().__init__(
            auto_scale=auto_scale,
            show_chan_labels=show_chan_labels,
            color_set=color_set,
            **kwargs
        )
        self.reset_renderer()

    def reset_renderer(self, reset_channel_labels=True):
        """Reset and rebuild the visualization."""
        self._widget.clear()
        self._widget.setBackground(self.parse_color_str(self.bg_color))
        self._plot_widgets = []
        self._curves = []
        self._fill_items = []
        self._ci_upper_curves = []
        self._ci_lower_curves = []
        self._filter_cache = {}
        self._last_srate = None
        self._y_ranges = {}  # Reset y-axis ranges
        self._range_update_counter = 0
        self._first_update_time = None

        if len(self.chan_states) == 0 or len(self._data_sources) == 0:
            return

        labelStyle = {'color': '#FFF', 'font-size': str(self.font_size) + 'pt'}

        font = QtGui.QFont("Arial", int(self.font_size - 2))

        # Get initial time vector
        if len(self._buffers) > 0 and self._buffers[0]._data.size > 0:
            n_samples = self._buffers[0]._data.shape[-1]
            srate = self._data_sources[0].data_stats.get('srate', 1.0) or 1.0
            t_vec = np.arange(n_samples, dtype=float) / srate
        else:
            t_vec = np.linspace(0, self.duration, 100)

        # Create stacked subplots - one per frequency band (reversed order so Gamma is at top)
        n_bands = len(FREQUENCY_BANDS)
        for band_idx, (band_name, fmin, fmax) in enumerate(FREQUENCY_BANDS):
            # Create subplot for this band
            pw = self._widget.addPlot(row=band_idx, col=0, antialias=self._antialias)
            pw.showGrid(x=True, y=True, alpha=0.3)
            pw.setXRange(0, self.duration)

            # Set y-range based on limits (disable auto-range to prevent jerky updates)
            if self.auto_scale != 'none':
                # Set initial range from limits, will be updated from data in update_visualization
                pw.setYRange(self.lower_limit, self.upper_limit)
                pw.disableAutoRange(axis='y')  # Disable continuous auto-scaling
                self._y_ranges[band_idx] = (self.lower_limit, self.upper_limit)
            else:
                pw.setYRange(self.lower_limit, self.upper_limit)
                pw.disableAutoRange(axis='y')
                self._y_ranges[band_idx] = (self.lower_limit, self.upper_limit)

            pw.getAxis("bottom").setTickFont(font)
            pw.getAxis("left").setTickFont(font)

            # Y-axis label with band name and frequency range
            pw.setLabel('left', f'{band_name}\n({fmin}-{fmax}Hz)', **labelStyle)

            # Hide x-axis labels for all but the bottom plot
            if band_idx < n_bands - 1:
                pw.getAxis("bottom").setStyle(showValues=False)
            else:
                pw.setLabel('bottom', 'Time', units='s', **labelStyle)

            self._plot_widgets.append(pw)

            # Create curves for this band
            color = BAND_COLORS[band_idx]
            pen = pg.mkPen(color=color, width=self._line_width)

            # Main GFP curve
            curve = pg.PlotCurveItem(
                t_vec, np.zeros_like(t_vec),
                pen=pen,
                connect='finite',
                name=f'{band_name} ({fmin}-{fmax}Hz)'
            )
            pw.addItem(curve)
            self._curves.append(curve)

            if self._show_confidence:
                # Create invisible curves for CI boundaries
                ci_color = (*color, 80)  # Semi-transparent
                ci_pen = pg.mkPen(color=ci_color, width=0)

                ci_upper = pg.PlotCurveItem(t_vec, np.zeros_like(t_vec), pen=ci_pen)
                ci_lower = pg.PlotCurveItem(t_vec, np.zeros_like(t_vec), pen=ci_pen)

                # Fill between CI curves
                fill_brush = pg.mkBrush(color=(*color, 50))
                fill_item = pg.FillBetweenItem(ci_lower, ci_upper, brush=fill_brush)

                pw.addItem(ci_upper)
                pw.addItem(ci_lower)
                pw.addItem(fill_item)

                self._ci_upper_curves.append(ci_upper)
                self._ci_lower_curves.append(ci_lower)
                self._fill_items.append(fill_item)

        # Link all x-axes to the bottom plot for synchronized panning/zooming
        if len(self._plot_widgets) > 1:
            bottom_pw = self._plot_widgets[-1]
            for pw in self._plot_widgets[:-1]:
                pw.setXLink(bottom_pw)

        # Reduce spacing between subplots
        self._widget.ci.setSpacing(2.)

    def _get_filter_sos(self, fmin, fmax, srate):
        """
        Get or create bandpass filter coefficients (second-order sections).

        Args:
            fmin: Lower cutoff frequency in Hz.
            fmax: Upper cutoff frequency in Hz.
            srate: Sampling rate in Hz.

        Returns:
            SOS filter coefficients.
        """
        cache_key = (fmin, fmax, srate)
        if cache_key not in self._filter_cache:
            nyq = srate / 2.0
            low = fmin / nyq
            high = min(fmax / nyq, 0.99)  # Ensure below Nyquist

            if low >= high or low <= 0:
                return None

            try:
                sos = signal.butter(self._filter_order, [low, high], btype='band', output='sos')
                self._filter_cache[cache_key] = sos
            except ValueError:
                return None

        return self._filter_cache.get(cache_key)

    def _bandpass_filter(self, data, fmin, fmax, srate):
        """
        Apply bandpass filter to data.

        Args:
            data: 2D array (n_channels, n_samples).
            fmin: Lower cutoff frequency in Hz.
            fmax: Upper cutoff frequency in Hz.
            srate: Sampling rate in Hz.

        Returns:
            Filtered data with same shape as input.
        """
        sos = self._get_filter_sos(fmin, fmax, srate)
        if sos is None:
            return data

        # Handle NaN values by interpolating
        filtered = np.zeros_like(data)
        for ch_idx in range(data.shape[0]):
            ch_data = data[ch_idx, :]
            valid_mask = np.isfinite(ch_data)

            if not np.any(valid_mask):
                filtered[ch_idx, :] = 0
                continue

            if np.all(valid_mask):
                # No NaNs, filter directly
                try:
                    filtered[ch_idx, :] = signal.sosfiltfilt(sos, ch_data)
                except ValueError:
                    filtered[ch_idx, :] = ch_data
            else:
                # Interpolate NaNs, filter, then restore NaNs
                interp_data = ch_data.copy()
                valid_indices = np.where(valid_mask)[0]
                invalid_indices = np.where(~valid_mask)[0]

                if len(valid_indices) >= 2:
                    interp_data[invalid_indices] = np.interp(
                        invalid_indices, valid_indices, ch_data[valid_indices]
                    )
                    try:
                        filtered[ch_idx, :] = signal.sosfiltfilt(sos, interp_data)
                    except ValueError:
                        filtered[ch_idx, :] = interp_data
                else:
                    filtered[ch_idx, :] = 0

        return filtered

    def _compute_gfp(self, data):
        """
        Compute Global Field Power (sum of squares across channels).

        Args:
            data: 2D array (n_channels, n_samples).

        Returns:
            1D array of GFP values (n_samples,).
        """
        return np.sum(data ** 2, axis=0)

    def _baseline_rescale(self, gfp, t_vec):
        """
        Apply baseline correction to GFP.

        Args:
            gfp: 1D array of GFP values.
            t_vec: Time vector corresponding to gfp.

        Returns:
            Baseline-corrected GFP.
        """
        if self._baseline_start is None and self._baseline_end is None:
            return gfp

        # Determine baseline indices
        start_t = self._baseline_start if self._baseline_start is not None else t_vec[0]
        end_t = self._baseline_end if self._baseline_end is not None else t_vec[-1]

        baseline_mask = (t_vec >= start_t) & (t_vec <= end_t)

        if not np.any(baseline_mask):
            return gfp

        baseline_mean = np.nanmean(gfp[baseline_mask])

        if baseline_mean != 0 and np.isfinite(baseline_mean):
            return gfp / baseline_mean - 1
        return gfp

    def _bootstrap_ci(self, data, stat_fun=None):
        """
        Compute bootstrap confidence intervals.

        Args:
            data: 2D array (n_channels, n_samples).
            stat_fun: Function to compute statistic. Default is sum of squares.

        Returns:
            Tuple of (ci_lower, ci_upper) arrays.
        """
        if stat_fun is None:
            stat_fun = lambda x: np.sum(x ** 2, axis=0)

        n_channels = data.shape[0]
        n_samples = data.shape[1]

        # Bootstrap resampling
        rng = np.random.default_rng()
        boot_stats = np.zeros((self._n_bootstrap, n_samples))

        for i in range(self._n_bootstrap):
            indices = rng.integers(0, n_channels, size=n_channels)
            boot_data = data[indices, :]
            boot_stats[i, :] = stat_fun(boot_data)

        # Compute percentiles for CI
        ci_lower = np.percentile(boot_stats, 2.5, axis=0)
        ci_upper = np.percentile(boot_stats, 97.5, axis=0)

        return ci_lower, ci_upper

    def update_visualization(self, data, timestamps) -> None:
        """Update the visualization with new data."""
        if not any([np.any(_) for _ in timestamps[0]]):
            return

        if len(self._plot_widgets) == 0:
            return

        # Track initial update time for auto-scaling
        if self._first_update_time is None:
            self._first_update_time = time.time()
            
        is_initial_period = (time.time() - self._first_update_time) <= self._initial_auto_scale_duration

        # Aggregate data from all sources
        all_data = []
        srate = None

        for src_ix in range(len(data)):
            dat, _ = data[src_ix]
            ts, _ = timestamps[src_ix]

            if dat.size == 0:
                continue

            all_data.append(dat)

            if srate is None:
                srate = self._data_sources[src_ix].data_stats.get('srate', 0.0) or 0.0

        if len(all_data) == 0 or srate <= 0:
            return

        # Combine all channel data
        combined_data = np.vstack(all_data) if len(all_data) > 1 else all_data[0]
        n_samples = combined_data.shape[1]

        # Create time vector
        t_vec = np.arange(n_samples, dtype=float) / srate

        # Check if sample rate changed (invalidate filter cache)
        if self._last_srate != srate:
            self._filter_cache = {}
            self._last_srate = srate

        # Process each frequency band
        for band_idx, (band_name, fmin, fmax) in enumerate(FREQUENCY_BANDS):
            # Skip bands that exceed Nyquist frequency
            if fmin >= srate / 2:
                continue

            # Bandpass filter
            filtered_data = self._bandpass_filter(combined_data, fmin, fmax, srate)

            # Compute GFP
            gfp = self._compute_gfp(filtered_data)

            # Apply baseline correction
            gfp = self._baseline_rescale(gfp, t_vec)

            # Handle sweep/scroll mode for time display
            if self.plot_mode == "Sweep":
                display_t = t_vec % self.duration
            else:
                display_t = t_vec

            # Update main curve
            if band_idx < len(self._curves):
                self._curves[band_idx].setData(display_t, gfp)

            # Update confidence intervals if enabled
            if self._show_confidence and band_idx < len(self._ci_upper_curves):
                ci_low, ci_up = self._bootstrap_ci(filtered_data)

                # Apply baseline correction to CI as well
                ci_low = self._baseline_rescale(ci_low, t_vec)
                ci_up = self._baseline_rescale(ci_up, t_vec)

                self._ci_lower_curves[band_idx].setData(display_t, gfp - ci_low)
                self._ci_upper_curves[band_idx].setData(display_t, gfp + ci_up)
            
            # Update y-axis range when auto_scale is enabled 
            # To prevent constant flickering, we only auto-fit during the initial few seconds
            if self.auto_scale != 'none' and band_idx < len(self._plot_widgets) and is_initial_period:
                # Compute current data range
                if np.isfinite(gfp).any():
                    data_min = float(np.nanmin(gfp))
                    data_max = float(np.nanmax(gfp))
                    
                    # Add small padding (5% on each side)
                    data_range = data_max - data_min
                    if data_range > 0:
                        padding = data_range * 0.05
                        new_min = data_min - padding
                        new_max = data_max + padding
                    else:
                        # Handle case where all values are the same
                        new_min = data_min - abs(data_min) * 0.1 if data_min != 0 else -1.0
                        new_max = data_max + abs(data_max) * 0.1 if data_max != 0 else 1.0
                    
                    # Continuously update during the initial period to catch the real signal
                    self._plot_widgets[band_idx].setYRange(new_min, new_max)
                    self._y_ranges[band_idx] = (new_min, new_max)

    # ------------------------------ #
    # Properties
    # ------------------------------ #

    @property
    def filter_order(self):
        return self._filter_order

    @filter_order.setter
    def filter_order(self, value):
        self._filter_order = max(1, int(value))
        self._filter_cache = {}  # Invalidate cache

    @property
    def n_bootstrap(self):
        return self._n_bootstrap

    @n_bootstrap.setter
    def n_bootstrap(self, value):
        self._n_bootstrap = max(10, int(value))

    @property
    def baseline_start(self):
        return self._baseline_start

    @baseline_start.setter
    def baseline_start(self, value):
        self._baseline_start = value

    @property
    def baseline_end(self):
        return self._baseline_end

    @baseline_end.setter
    def baseline_end(self, value):
        self._baseline_end = value

    @property
    def show_confidence(self):
        return self._show_confidence

    @show_confidence.setter
    def show_confidence(self, value):
        self._show_confidence = value
        self.reset_renderer(reset_channel_labels=False)

    @property
    def line_width(self):
        return self._line_width

    @line_width.setter
    def line_width(self, value):
        self._line_width = value
        self.reset_renderer(reset_channel_labels=False)

    @property
    def antialias(self):
        return self._antialias

    @antialias.setter
    def antialias(self, value):
        self._antialias = value
        self.reset_renderer(reset_channel_labels=False)

