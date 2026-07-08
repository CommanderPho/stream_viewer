from collections import deque, namedtuple
from dataclasses import dataclass
import json
import logging
import warnings
import numpy as np
from qtpy import QtGui, QtWidgets
import pyqtgraph as pg
from pyqtgraph.widgets.RemoteGraphicsView import RemoteGraphicsView
from scipy import signal
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from stream_viewer.buffers.stream_data_buffers import TimeSeriesBuffer

from stream_viewer.renderers.data.base import RendererDataTimeSeries
from stream_viewer.renderers.display.pyqtgraph import PGRenderer


logger = logging.getLogger(__name__)

# Constants
DEFAULT_DB_FLOOR = -120.0  # Default dB value for NaN/invalid data
DEFAULT_DB_CEILING = 0.0    # Default dB ceiling for auto-scale fallback
EPSILON_DB = 1e-12          # Small epsilon to prevent log(0) in dB conversion
MIN_NPERSEG = 8              # Minimum segment size for spectrogram
MIN_SRATE = 0.001            # Minimum valid sampling rate (Hz)

MarkerMap = namedtuple('MarkerMap', ['source_id', 'timestamp', 'item'])


@dataclass
class SourceState:
    """Per-source state for spectrogram rendering."""
    heatmap: Optional[np.ndarray] = None
    freq_mask: Optional[np.ndarray] = None
    n_time_cols: Optional[int] = None
    hop_size: Optional[int] = None
    write_index: int = 0
    locked_levels: Optional[Tuple[float, float]] = None
    last_write_index: Optional[float] = None  # Write index (int in Sweep mode) or timestamp (float in Scroll mode)
    sample_carry: int = 0
    last_processed_write_idx: Optional[float] = None  # Track last processed write index (int in Sweep mode) or timestamp (float in Scroll mode)
    session_start_time: Optional[float] = None  # Track when session began for this source
    session_time_range: Optional[Tuple[float, float]] = None  # Track full time span [start, end]
    preallocated_capacity: int = 0  # Track pre-allocated capacity for Scroll mode optimization
    
    def reset(self):
        """Reset all state to initial values."""
        self.heatmap = None
        self.freq_mask = None
        self.n_time_cols = None
        self.hop_size = None
        self.write_index = 0
        self.locked_levels = None
        self.last_write_index = None
        self.sample_carry = 0
        self.last_processed_write_idx = None
        self.session_start_time = None
        self.session_time_range = None
        self.preallocated_capacity = 0


class HeatmapPG(RendererDataTimeSeries, PGRenderer):
    """
    Production-quality PyQtGraph-based 2D spectrogram (heatmap) renderer for live EEG data.
    
    Compatible with HeatmapControlPanel which provides Apply/Revert buttons
    for responsive configuration changes.
    
    This renderer computes and displays spectrograms by averaging across visible channels
    for each data source. It supports both Sweep and Scroll visualization modes with
    efficient column-wise updates and robust error handling.
    
    Features:
    - Real-time spectrogram computation with configurable frequency ranges
    - Efficient column-wise updates to minimize computational overhead
    - Robust validation and error handling to prevent crashes
    - Automatic level locking for stable color scaling
    - Support for markers and time-based expiration
    
    Args:
        auto_scale: Auto-scaling mode ('none', 'by-channel', 'by-stream')
        show_chan_labels: Whether to show channel labels
        color_set: Colormap name for the heatmap
        ylabel_as_title: Display ylabel as title instead of axis label
        ylabel_width: Minimum width for y-axis label
        ylabel: Custom y-axis label text
        fmin_hz: Minimum frequency for spectrogram (Hz)
        fmax_hz: Maximum frequency for spectrogram (Hz)
        nperseg: Number of samples per segment for FFT
        noverlap: Number of overlapping samples between segments
    """
    COMPAT_ICONTROL = ['HeatmapControlPanel']
    plot_modes = ["Sweep", "Scroll"]
    gui_kwargs = dict(
        RendererDataTimeSeries.gui_kwargs,
        **PGRenderer.gui_kwargs,
        ylabel_as_title=bool,
        ylabel_width=int,
        ylabel=str,
        fmin_hz=float,
        fmax_hz=float,
        nperseg=int,
        noverlap=int,
        update_rate_hz=float,
        max_samples_per_spectrogram=int,
    )

    def __init__(self,
                 # inherited/overrides
                 auto_scale: str = 'none',
                 show_chan_labels: bool = True,
                 color_set: str = 'viridis',
                 # new
                 ylabel_as_title: bool = False,
                 ylabel_width: Optional[int] = None,
                 ylabel: Optional[str] = None,
                 fmin_hz: float = 1.0,
                 fmax_hz: float = 42.0,
                 nperseg: int = 256,
                 noverlap: int = 128,
                 update_rate_hz: float = 30.0,
                 max_samples_per_spectrogram: Optional[int] = None,
                 **kwargs):
        self._ylabel_as_title = ylabel_as_title
        self._ylabel_width = ylabel_width
        self._ylabel = ylabel
        self._requested_auto_scale = auto_scale.lower()

        # spectrogram parameters
        self._fmin_hz = float(fmin_hz)
        self._fmax_hz = float(fmax_hz)
        self._nperseg = int(nperseg)
        self._noverlap = int(noverlap)
        
        # Performance optimization parameters
        self._update_rate_hz = float(update_rate_hz)
        self._max_samples_per_spectrogram = max_samples_per_spectrogram

        # Container widget with vertical layout for RemoteGraphicsView instances
        self._widget = QtWidgets.QWidget()
        self._widget.setLayout(QtWidgets.QVBoxLayout())
        self._remote_views: list[Optional[RemoteGraphicsView]] = []  # Store RemoteGraphicsView instances (None for skipped sources)
        self._plot_items: list = []  # Store remote plot items for x-axis linking (None for skipped sources)
        self._do_yaxis_sync = False
        self._src_last_marker_time = []
        self._marker_texts_pool = deque()
        self._marker_info = deque()
        self._t_expired = -np.inf
        self._image_items = []
        # Per-source state using SourceState dataclass
        self._source_states: list[SourceState] = []
        # Cached LUT for performance
        self._cached_lut: Optional[np.ndarray] = None
        self._cached_color_set: Optional[str] = None
        # Global min/max tracking for normalization across all sources
        self._global_min: Optional[float] = None
        self._global_max: Optional[float] = None
        
        # Scroll-back and re-sync state tracking
        self._is_manually_scrolled: bool = False
        self._last_auto_xrange: Optional[Tuple[float, float]] = None
        self._suppress_range_signal: bool = False  # Flag to suppress signal during programmatic updates
        self._range_changed_connection = None  # Store signal connection for cleanup
        
        # Debounce timer for property changes
        self._reset_timer = pg.QtCore.QTimer()
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._do_pending_reset)
        self._pending_reset = {'reset_channel_labels': False}
        
        # Performance optimization: cached FFT window and dirty flag for batch updates
        self._cached_window: Optional[np.ndarray] = None
        self._cached_nperseg: Optional[int] = None
        self._display_dirty: dict[int, bool] = {}  # Track which sources need display update

        super().__init__(show_chan_labels=show_chan_labels, color_set=color_set, **kwargs)
        self.reset_renderer()

    # ------------------------------ #
    # Lifecycle / layout
    # ------------------------------ #
    def reset_renderer(self, reset_channel_labels: bool = True) -> None:
        """
        Reset the renderer to initial state.
        
        Args:
            reset_channel_labels: Whether to reset channel labels
        """
        # Cancel any pending debounced resets to avoid double resets
        self._reset_timer.stop()
        self._pending_reset = {'reset_channel_labels': False}
        
        # Reset global min/max tracking
        self._global_min = None
        self._global_max = None
        
        # Reset scroll-back and re-sync state
        self._is_manually_scrolled = False
        self._last_auto_xrange = None
        self._suppress_range_signal = False
        
        # Disconnect signal connection before deleting widgets to prevent RuntimeError
        if self._range_changed_connection is not None:
            try:
                self._range_changed_connection.disconnect()
            except Exception:
                pass  # Connection may already be disconnected or object deleted
            self._range_changed_connection = None
        
        # Clear container layout - remove all RemoteGraphicsView widgets
        layout = self._widget.layout()
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        
        # Clear storage - will be reinitialized below to match data_sources indices
        self._src_last_marker_time = [-np.inf for _ in range(len(self._data_sources))]
        n_sources = len(self._data_sources)
        self._image_items = [None] * n_sources
        # Initialize source states
        n_sources = len(self._data_sources)
        while len(self._source_states) < n_sources:
            self._source_states.append(SourceState())
        # Reset all source states
        for state in self._source_states:
            state.reset()
        # Trim if we have too many
        self._source_states = self._source_states[:n_sources]

        if len(self.chan_states) == 0:
            return

        labelStyle = {'color': '#FFF', 'font-size': str(self.font_size) + 'pt'}

        # Requested auto-scale maps to actual behavior; for heatmaps, pyqtgraph levels handle scaling
        if self._requested_auto_scale == 'all':
            self._auto_scale = 'none'
        else:
            self._auto_scale = self._requested_auto_scale

        row_offset = -1
        last_row = 0
        bottom_plot_item = None
        
        # Initialize plot_items list to match data_sources indices (None for skipped sources)
        n_sources = len(self._data_sources)
        self._plot_items = [None] * n_sources
        self._remote_views = [None] * n_sources
        
        for src_ix, src in enumerate(self._data_sources):
            ch_states = self.chan_states[self.chan_states['src'] == src.identifier]
            n_vis_src = ch_states['vis'].sum()
            if n_vis_src == 0:
                continue

            row_offset += 1
            last_row = row_offset
            
            # Create RemoteGraphicsView for this data source
            remote_view = RemoteGraphicsView(debug=False)
            # Enable antialiasing in remote process (doesn't affect main process performance)
            remote_view.pg.setConfigOptions(antialias=True)  # type: ignore
            
            # Create plot item in remote process
            pw = remote_view.pg.PlotItem()  # type: ignore
            # Performance: Apply deferGetattr to speed up access
            pw._setProxyOptions(deferGetattr=True)
            # Set background on the view widget using Qt palette (RemoteGraphicsView is in main process)
            bg_color_str = self.parse_color_str(self.bg_color)
            bg_qcolor = pg.mkColor(bg_color_str)
            palette = remote_view.palette()
            palette.setColor(remote_view.backgroundRole(), bg_qcolor)
            remote_view.setPalette(palette)
            remote_view.setAutoFillBackground(True)
            remote_view.setCentralItem(pw)
            
            # Store references at source index (not row_offset)
            self._remote_views[src_ix] = remote_view
            self._plot_items[src_ix] = pw
            
            # Add to container layout
            layout.addWidget(remote_view)
            
            pw.showGrid(x=True, y=True, alpha=0.3)

            # Create font in remote process to avoid QGuiApplication warnings
            font = remote_view.pg.QtGui.QFont()  # type: ignore
            font.setPointSize(self.font_size - 2)
            pw.setXRange(0, self.duration)
            pw.getAxis("bottom").setTickFont(font)
            pw.getAxis("bottom").setStyle(showValues=self.ylabel_as_title)

            yax = pw.getAxis('left')
            yax.setTickFont(font)
            stream_ylabel = json.loads(src.identifier)['name']
            if 'unit' in ch_states and ch_states['unit'].nunique() == 1:  # type: ignore
                stream_ylabel = stream_ylabel + ' (%s)' % ch_states['unit'].iloc[0]  # type: ignore
            pw.setLabel('top' if self.ylabel_as_title else 'left', self._ylabel or stream_ylabel, **labelStyle)
            if self.ylabel_as_title:
                pw.getAxis("top").setStyle(showValues=False)

            # Create the image item for spectrogram in remote process; attach color LUT later on first update
            img = remote_view.pg.ImageItem(axisOrder='row-major')  # type: ignore
            pw.addItem(img)
            self._image_items[src_ix] = img

            # Y axis range (frequency) - lock to frequency range
            pw.setYRange(self._fmin_hz, self._fmax_hz)
            pw.disableAutoRange(axis='y')  # Disable y-axis auto-scaling
            # Lock y-axis limits to prevent scrolling/zooming
            pw.setLimits(yMin=self._fmin_hz, yMax=self._fmax_hz, minYRange=self._fmax_hz - self._fmin_hz, maxYRange=self._fmax_hz - self._fmin_hz)
            # Disable y-axis mouse interactions (only allow x-axis scrolling/zooming)
            vb = pw.getViewBox()
            if vb is not None:
                vb.setMouseEnabled(x=True, y=False)  # Only allow x-axis mouse interactions

        # Bottom axis label and link x-axes manually
        # Find the last non-None plot item (bottom one)
        bottom_plot_item = None
        for plot_item in reversed(self._plot_items):
            if plot_item is not None:
                bottom_plot_item = plot_item
                break
        
        if bottom_plot_item is not None:
            bottom_plot_item.setLabel('bottom', 'Time', units='s', **labelStyle)
            bottom_plot_item.getAxis("bottom").setStyle(showValues=True)
            # Link all plots to bottom plot
            for plot_item in self._plot_items:
                if plot_item is not None and plot_item is not bottom_plot_item:
                    plot_item.setXLink(bottom_plot_item)
            
            # Connect signal to detect manual x-axis range changes (only in Scroll mode)
            # We connect to the bottom plot item since all others are linked to it
            # Note: Connecting bound methods across process boundaries can fail due to pickling issues
            # We use a lambda wrapper to avoid pickling the bound method directly
            try:
                # Use a lambda that captures self to avoid pickling bound method
                # The lambda itself can be pickled, and we'll check self inside the callback
                def range_changed_wrapper():
                    # Check if self still exists and method is callable
                    if hasattr(self, '_on_xrange_changed'):
                        try:
                            self._on_xrange_changed()
                        except Exception as e:
                            logger.debug(f"Error in xrange changed callback: {e}")
                
                self._range_changed_connection = bottom_plot_item.sigRangeChanged.connect(range_changed_wrapper)
            except Exception as e:
                # If signal connection fails (e.g., proxy/pickling issues), log and continue
                logger.debug(f"Could not connect sigRangeChanged signal for scroll detection: {e}")
                self._range_changed_connection = None

        # Set layout spacing
        layout.setSpacing(int(10. if self.ylabel_as_title else 0.))
        self._do_yaxis_sync = True

    def _schedule_reset(self, reset_channel_labels: bool = False) -> None:
        """
        Schedule a debounced reset_renderer call.
        
        This prevents expensive reset operations from blocking the UI during rapid
        property changes. The reset will execute after 300ms of inactivity.
        
        Args:
            reset_channel_labels: Whether to reset channel labels when reset executes
        """
        self._pending_reset['reset_channel_labels'] = (
            self._pending_reset.get('reset_channel_labels', False) or reset_channel_labels
        )
        self._reset_timer.stop()
        self._reset_timer.start(300)  # 300ms debounce

    def _do_pending_reset(self) -> None:
        """
        Execute the pending reset (called by debounce timer).
        
        Stops the update timer during reset to prevent conflicts, then restarts it.
        """
        if self._timer.isActive():
            self._timer.stop()
        try:
            self.reset_renderer(reset_channel_labels=self._pending_reset['reset_channel_labels'])
        finally:
            if not self._timer.isActive():
                self.restart_timer()
        self._pending_reset = {'reset_channel_labels': False}
        # Reset display dirty flags
        self._display_dirty.clear()
    
    def restart_timer(self) -> None:
        """
        Override restart_timer to use custom update rate for spectrograms.
        
        Uses update_rate_hz instead of default 60 Hz for better performance.
        """
        if self._timer.isActive():
            self._timer.stop()
        interval = int(1000.0 / self._update_rate_hz)
        self._timer.start(interval)

    def _ensure_source_state(self, src_ix: int, srate: float, f: np.ndarray, Pxx: np.ndarray) -> bool:
        """
        Ensure source state is initialized. Returns True if state is ready, False otherwise.
        
        Args:
            src_ix: Source index
            srate: Sampling rate
            f: Frequency array from spectrogram
            Pxx: Power spectral density array
            
        Returns:
            True if state is fully initialized and ready, False otherwise
        """
        if src_ix >= len(self._source_states):
            logger.warning(f"Source index {src_ix} out of range")
            return False
            
        state = self._source_states[src_ix]
        
        # Initialize frequency mask
        if state.freq_mask is None and f is not None and f.size > 0:
            f_mask = (f >= float(self._fmin_hz)) & (f <= float(self._fmax_hz))
            if not np.any(f_mask):
                logger.warning(f"Source {src_ix}: No frequencies in range [{self._fmin_hz}, {self._fmax_hz}] Hz, using all frequencies")
                f_mask = np.ones_like(f, dtype=bool)
            state.freq_mask = f_mask

        # Initialize hop size (used for column calculation)
        if state.hop_size is None and srate >= MIN_SRATE:
            hop = max(1, int(self._nperseg - self._noverlap))
            state.hop_size = hop

        # Initialize heatmap with initial size based on duration (will grow dynamically)
        if (state.heatmap is None and state.freq_mask is not None and 
            state.hop_size is not None):
            n_freq = int(np.sum(state.freq_mask))
            # Start with initial size based on duration for initial display window
            # This will grow as new columns are added
            if srate >= MIN_SRATE:
                N = max(0, int(srate * self.duration))
                initial_n_cols = 1 + max(0, (N - int(self._nperseg)) // state.hop_size) if N >= int(self._nperseg) else 1
            else:
                initial_n_cols = 1
            state.n_time_cols = initial_n_cols  # Track current number of columns
            
            if n_freq > 0 and initial_n_cols > 0:
                # Use float32 for memory and performance optimization
                state.heatmap = np.full((n_freq, initial_n_cols), np.nan, dtype=np.float32)
                state.write_index = 0
                state.locked_levels = None
                return True
            else:
                logger.warning(f"Source {src_ix}: Invalid heatmap dimensions ({n_freq}, {initial_n_cols})")
                return False
        
        return state.heatmap is not None

    def sync_y_axes(self) -> None:
        """Synchronize y-axis widths across all plots."""
        max_width = self._ylabel_width or 0.
        for pw in self._plot_items:
            if pw is None:
                continue
            yax = pw.getAxis('left')
            max_width = max(max_width, yax.minimumWidth())

        for pw in self._plot_items:
            if pw is None:
                continue
            pw.getAxis('left').setWidth(max_width)
        self._do_yaxis_sync = False

    def _on_xrange_changed(self) -> None:
        """
        Handle x-axis range changes to detect manual scrolling.
        
        This method is called when the user manually changes the x-axis range.
        It detects if the change is manual (not programmatic) and triggers de-sync.
        """
        # Only handle in Scroll mode
        if self.plot_mode != "Scroll":
            return
        
        # Suppress signal during programmatic updates
        if self._suppress_range_signal:
            return
        
        # Get current x-axis range from bottom plot item
        bottom_plot_item = None
        for plot_item in reversed(self._plot_items):
            if plot_item is not None:
                bottom_plot_item = plot_item
                break
        
        if bottom_plot_item is None:
            return
        
        try:
            current_range = bottom_plot_item.viewRange()[0]  # [x_min, x_max]
            current_xmin, current_xmax = float(current_range[0]), float(current_range[1])
        except Exception:
            return
        
        # Check if this is a manual change (different from last auto-update)
        if self._last_auto_xrange is not None:
            last_xmin, last_xmax = self._last_auto_xrange
            # Use small tolerance for floating point comparison
            tolerance = 0.01
            if (abs(current_xmin - last_xmin) > tolerance or 
                abs(current_xmax - last_xmax) > tolerance):
                # Manual change detected - de-sync
                if not self._is_manually_scrolled:
                    self._is_manually_scrolled = True
                    # Unlink x-axes
                    for plot_item in self._plot_items:
                        if plot_item is not None:
                            try:
                                plot_item.setXLink(None)
                            except Exception:
                                pass
                    # Notify control panel to enable re-sync button
                    self._notify_sync_state_changed()

    def _notify_sync_state_changed(self) -> None:
        """
        Notify control panel that sync state has changed.
        
        This allows the control panel to update the re-sync and disconnect button states.
        """
        # Try to find and update the control panel via parent widget
        # The control panel should have a method to update button state
        try:
            parent = self._widget.parent()
            while parent is not None:
                if hasattr(parent, '_update_sync_button_states'):
                    parent._update_sync_button_states()  # type: ignore
                    break
                # Also check children
                for child in parent.findChildren(QtWidgets.QWidget):
                    if (hasattr(child, '_update_sync_button_states') and 
                        hasattr(child, '_renderer') and 
                        child._renderer is self):
                        child._update_sync_button_states()  # type: ignore
                        break
                parent = parent.parent()
        except Exception:
            # If notification fails, it's not critical - button state will update on next access
            pass

    def disconnect_from_realtime(self) -> None:
        """
        Manually disconnect from realtime by unlinking x-axes.
        
        This method allows users to explicitly disconnect from realtime without
        needing to scroll first. It sets the manual scroll state and unlinks
        all x-axes to stop auto-updating.
        """
        if self.plot_mode != "Scroll":
            return
        
        # Set manual scroll state
        self._is_manually_scrolled = True
        
        # Unlink all x-axes
        for plot_item in self._plot_items:
            if plot_item is not None:
                try:
                    plot_item.setXLink(None)
                except Exception:
                    pass
        
        # Notify control panel to update button states
        self._notify_sync_state_changed()
    
    def sync_to_present(self) -> None:
        """
        Re-sync the view to the current time window.
        
        This method re-links all x-axes and updates the view to show the current
        time window, effectively catching up to the present time.
        """
        if self.plot_mode != "Scroll":
            return
        
        # Reset manual scroll state
        self._is_manually_scrolled = False
        self._suppress_range_signal = True
        
        try:
            # Find bottom plot item
            bottom_plot_item = None
            for plot_item in reversed(self._plot_items):
                if plot_item is not None:
                    bottom_plot_item = plot_item
                    break
            
            if bottom_plot_item is None:
                return
            
            # Re-link all x-axes to bottom plot
            for plot_item in self._plot_items:
                if plot_item is not None and plot_item is not bottom_plot_item:
                    try:
                        plot_item.setXLink(bottom_plot_item)
                    except Exception:
                        pass
            
            # Update x-axis range to current time window (most recent duration seconds)
            # Check if we have session tracking for full history
            if len(self._source_states) > 0:
                state = self._source_states[0]
                if state.session_time_range is not None and state.session_start_time is not None:
                    # Use session range: show most recent duration window
                    session_start, session_end = state.session_time_range
                    current_time = session_end
                    time_end = current_time
                    time_start = time_end - self.duration
                    time_width = self.duration
                elif len(self._buffers) > 0:
                    # Fallback to buffer range
                    buf = self._buffers[0]
                    if buf._tvec.size > 0:
                        t_max = float(np.nanmax(buf._tvec))
                        if np.isfinite(t_max):
                            time_end = t_max
                            time_start = time_end - self.duration
                            time_width = self.duration
                        else:
                            time_start = 0.0
                            time_width = float(self.duration)
                    else:
                        time_start = 0.0
                        time_width = float(self.duration)
                else:
                    time_start = 0.0
                    time_width = float(self.duration)
            else:
                time_start = 0.0
                time_width = float(self.duration)
            
            # Update x-axis range
            bottom_plot_item.setXRange(time_start, time_start + time_width)
            self._last_auto_xrange = (time_start, time_start + time_width)
        finally:
            self._suppress_range_signal = False
        
        # Notify control panel to update button states
        self._notify_sync_state_changed()

    # ------------------------------ #
    # Update loop
    # ------------------------------ #
    def _validate_buffer_data(self, buff_data: np.ndarray) -> bool:
        """
        Validate buffer data before processing.
        
        Args:
            buff_data: Buffer data array
            
        Returns:
            True if data is valid for processing, False otherwise
        """
        if buff_data.size == 0:
            return False
        if buff_data.ndim != 2:
            return False
        if buff_data.shape[0] == 0 or buff_data.shape[1] == 0:
            return False
        # Check if at least one channel has finite data
        if not np.isfinite(buff_data).any():
            return False
        return True
    
    def _compute_channel_average(self, buff_data: np.ndarray) -> Optional[np.ndarray]:
        """
        Compute mean across channels with proper validation.
        
        Args:
            buff_data: Buffer data array (channels x samples)
            
        Returns:
            Averaged signal or None if invalid
        """
        if not self._validate_buffer_data(buff_data):
            return None
        
        # Compute mean across channels while tolerating all-NaN columns
        # Suppress RuntimeWarning for "Mean of empty slice" when columns are all-NaN
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=RuntimeWarning, message='Mean of empty slice')
            with np.errstate(invalid='ignore', divide='ignore'):
                x = np.nanmean(buff_data, axis=0)
        
        # Validate result
        if not np.isfinite(x).any():
            return None
        
        # Replace NaN with zeros for spectrogram computation
        x = np.nan_to_num(x, nan=0.0)
        return x
    
    def _compute_spectrogram(self, x: np.ndarray, srate: float) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """
        Compute spectrogram with validation and error handling.
        
        Includes performance optimizations:
        - Data downsampling for high sample rates
        - Power-of-2 nperseg for faster FFT
        - Cached window function
        
        Args:
            x: Input signal
            srate: Sampling rate
            
        Returns:
            Tuple of (frequencies, times, power) or None if computation fails
        """
        if x.size == 0:
            return None
        if srate < MIN_SRATE:
            logger.debug(f"Invalid sampling rate: {srate} Hz")
            return None
        
        # Performance optimization: downsample if signal is too long
        original_srate = float(srate)
        if self._max_samples_per_spectrogram is not None and x.size > self._max_samples_per_spectrogram:
            # Downsample to reduce computation time
            downsample_factor = max(1, int(np.ceil(x.size / self._max_samples_per_spectrogram)))
            # Use decimation for better anti-aliasing (requires scipy.signal.decimate)
            # For simplicity, use every Nth sample (could be improved with proper decimation)
            x = x[::downsample_factor]
            srate = original_srate / downsample_factor
        
        # Optimize nperseg to be power of 2 for faster FFT
        nperseg = max(MIN_NPERSEG, min(self._nperseg, x.size))
        # Round to nearest power of 2 (but not less than MIN_NPERSEG)
        if nperseg > MIN_NPERSEG:
            nperseg_pow2 = int(2 ** np.ceil(np.log2(nperseg)))
            # Only use power of 2 if it's not too much larger (max 2x)
            if nperseg_pow2 <= 2 * nperseg:
                nperseg = min(nperseg_pow2, x.size)
        
        noverlap = max(0, min(self._noverlap, nperseg - 1))
        
        # Cache window function if nperseg hasn't changed (performance optimization)
        window = None
        nperseg_int = int(nperseg)
        if self._cached_nperseg == nperseg_int and self._cached_window is not None:
            window = self._cached_window
        else:
            # Create and cache hann window (default for spectrogram)
            # Use get_window for compatibility across scipy versions
            try:
                self._cached_window = signal.get_window('hann', nperseg_int)
            except AttributeError:
                # Fallback: use string name (scipy will create it)
                self._cached_window = None
                window = 'hann'
            self._cached_nperseg = nperseg_int
            if self._cached_window is not None:
                window = self._cached_window
        
        try:
            f, t, Sxx = signal.spectrogram(
                x, fs=srate, nperseg=int(nperseg), noverlap=int(noverlap),
                scaling='density', mode='psd', window=window
            )
        except Exception as e:
            logger.warning(f"Spectrogram computation failed: {e}", exc_info=True)
            return None
        
        if Sxx.size == 0:
            return None
        
        # Convert to dB with safe handling, use float32 for memory efficiency
        with np.errstate(invalid='ignore', divide='ignore'):
            Pxx = 10.0 * np.log10(Sxx + EPSILON_DB).astype(np.float32)
        
        return (f, t, Pxx)
    
    def _calculate_new_columns(self, src_ix: int, buf) -> int:
        """
        Calculate number of new columns to add based on buffer write index progression (Sweep) or timestamp progression (Scroll).
        
        Args:
            src_ix: Source index
            buf: Buffer object
            
        Returns:
            Number of new columns to add
        """
        state = self._source_states[src_ix]
        
        buf_len = int(buf._data.shape[1]) if buf._data.ndim == 2 else 0
        if buf_len == 0:
            return 0
        
        if self.plot_mode == "Scroll":
            # In Scroll mode, _write_idx doesn't advance, so use timestamp-based calculation
            if buf._tvec.size == 0:
                return 0
            
            curr_last_timestamp = float(buf._tvec[-1])
            # last_write_index stores the last processed timestamp in Scroll mode
            if state.last_write_index is None:
                state.last_write_index = curr_last_timestamp
                return 0
            
            prev_timestamp = float(state.last_write_index)
            # Calculate time delta and convert to samples
            time_delta = curr_last_timestamp - prev_timestamp
            if time_delta <= 0:
                return 0
            
            # Get sampling rate to convert time delta to samples
            srate = self._data_sources[src_ix].data_stats.get('srate', 0.0) or 0.0
            if srate < MIN_SRATE:
                return 0
            
            delta_samples = int(time_delta * srate)
            state.last_write_index = curr_last_timestamp
            state.sample_carry += delta_samples
            
            hop = int(state.hop_size or max(1, self._nperseg - self._noverlap))
            new = int(state.sample_carry // hop)
            state.sample_carry %= hop
            
            return new
        else:
            # Sweep mode: use write index comparison
            if not hasattr(buf, "_write_idx"):
                return 0
            
            curr_wi = int(buf._write_idx)
            prev_wi = state.last_write_index
            
            if prev_wi is None:
                state.last_write_index = float(curr_wi)  # Store as float for type consistency
                return 0
            
            delta = int((curr_wi - int(prev_wi)) % buf_len)
            state.last_write_index = float(curr_wi)  # Store as float for type consistency
            state.sample_carry += delta
            
            hop = int(state.hop_size or max(1, self._nperseg - self._noverlap))
            new = int(state.sample_carry // hop)
            state.sample_carry %= hop
            
            return new
    
    def _update_heatmap_columns(self, src_ix: int, P_use: np.ndarray, new_cols: int) -> bool:
        """
        Update heatmap with new columns for either Sweep or Scroll mode.
        
        Args:
            src_ix: Source index
            P_use: Power spectral density array (filtered by frequency mask)
            new_cols: Number of new columns to add
            
        Returns:
            True if update was successful, False otherwise
        """
        if new_cols <= 0:
            return False
        
        state = self._source_states[src_ix]
        if state.heatmap is None:
            return False
        
        heat = state.heatmap
        n_cols = heat.shape[1]
        # Limit new_cols to available columns in P_use
        new_cols = min(new_cols, P_use.shape[1])
        
        if self.plot_mode != "Sweep":
            # Scroll: append new columns with memory optimization (pre-allocate chunks)
            # Extract the columns to add from P_use (take the last new_cols columns)
            cols_to_add = P_use[:, -new_cols:].astype(np.float32)
            
            # Memory optimization: pre-allocate in chunks to reduce reallocation overhead
            current_cols = heat.shape[1]
            needed_cols = current_cols + new_cols
            
            # Pre-allocate in chunks of 100 columns to reduce memory fragmentation
            CHUNK_SIZE = 100
            if needed_cols > state.preallocated_capacity:
                # Calculate new capacity (round up to next chunk)
                new_capacity = ((needed_cols // CHUNK_SIZE) + 1) * CHUNK_SIZE
                # Pre-allocate larger array
                n_freq = heat.shape[0]
                new_heatmap = np.empty((n_freq, new_capacity), dtype=np.float32)
                new_heatmap[:, :current_cols] = heat
                new_heatmap[:, current_cols:current_cols + new_cols] = cols_to_add
                # Fill remaining with NaN
                new_heatmap[:, current_cols + new_cols:] = np.nan
                state.heatmap = new_heatmap
                state.preallocated_capacity = new_capacity
            else:
                # Use existing pre-allocated space
                if current_cols + new_cols <= state.preallocated_capacity:
                    state.heatmap[:, current_cols:current_cols + new_cols] = cols_to_add
                else:
                    # Fallback to concatenation if pre-allocation wasn't enough (shouldn't happen)
                    state.heatmap = np.concatenate([heat, cols_to_add], axis=1)
                    state.preallocated_capacity = state.heatmap.shape[1]
            
            state.n_time_cols = current_cols + new_cols  # Update column count
        else:
            # Sweep: circular write into columns (unchanged behavior)
            new_cols = min(new_cols, n_cols)  # Limit to current size in Sweep mode
            widx = state.write_index
            for k in range(new_cols):
                col = (widx + k) % n_cols
                heat[:, col] = P_use[:, -new_cols + k]
            state.write_index = (widx + new_cols) % n_cols
        
        return True
    
    def _prepare_display_heatmap(self, src_ix: int, pw=None, target_xrange: Optional[Tuple[float, float]] = None) -> Tuple[Optional[np.ndarray], Optional[Tuple[float, float]]]:
        """
        Prepare display heatmap, handling Sweep mode wrap-around or extracting visible time window in Scroll mode.
        
        Args:
            src_ix: Source index
            pw: Plot widget (optional, used to get x-axis range in Scroll mode)
            target_xrange: Optional target x-axis range (start, end). If provided, use this instead of reading current range.
            
        Returns:
            Tuple of (display array, time range tuple (start, end)) or (None, None) if not available
        """
        state = self._source_states[src_ix]
        if state.heatmap is None:
            return None, None
        
        if self.plot_mode == "Sweep":
            widx = int(state.write_index)
            heat = state.heatmap
            if heat.shape[1] > 1:
                display = np.hstack([heat[:, (widx+1):], heat[:, :(widx+1)]])
            else:
                display = heat
            # Sweep mode: time range is fixed 0 to duration
            time_range = (0.0, float(self.duration))
        else:
            # Scroll mode: extract columns corresponding to visible time window
            heat = state.heatmap
            n_cols = heat.shape[1]
            
            # Get visible time range from plot if available
            if pw is not None and state.session_start_time is not None and state.hop_size is not None:
                try:
                    # Use target_xrange if provided, otherwise read current x-axis range from plot
                    if target_xrange is not None:
                        visible_t_min, visible_t_max = target_xrange
                    else:
                        # Get current x-axis range from plot
                        view_range = pw.viewRange()[0]  # [x_min, x_max]
                        visible_t_min = float(view_range[0])
                        visible_t_max = float(view_range[1])
                    
                    # Get sampling rate to calculate time per column
                    srate = self._data_sources[src_ix].data_stats.get('srate', 0.0) or 0.0
                    if srate >= MIN_SRATE:
                        time_per_column = state.hop_size / srate
                        session_start = state.session_start_time
                        
                        # Calculate column indices for visible time range
                        # Column i corresponds to time: session_start + i * time_per_column
                        col_start = max(0, int((visible_t_min - session_start) / time_per_column))
                        col_end = min(n_cols, int(np.ceil((visible_t_max - session_start) / time_per_column)))
                        
                        # Ensure valid range
                        if col_start < col_end and col_start < n_cols:
                            display = heat[:, col_start:col_end].copy()
                            # Calculate actual time range of extracted columns
                            img_time_start = session_start + col_start * time_per_column
                            img_time_end = session_start + col_end * time_per_column
                            time_range = (img_time_start, img_time_end)
                        else:
                            # Fallback: show all columns
                            display = heat.copy()
                            if state.session_time_range is not None:
                                time_range = state.session_time_range
                            else:
                                time_range = (visible_t_min, visible_t_max)
                    else:
                        # Invalid srate: show all columns
                        display = heat.copy()
                        time_range = (visible_t_min, visible_t_max) if pw is not None else None
                except Exception:
                    # Fallback: show all columns if range extraction fails
                    display = heat.copy()
                    time_range = None
            else:
                # No plot or session info: show all columns
                display = heat.copy()
                if state.session_time_range is not None:
                    time_range = state.session_time_range
                else:
                    time_range = None
        
        # Replace NaN values with default for rendering
        # Ensure float32 for memory efficiency
        if display is not None:
            if not np.isfinite(display).all():
                display = np.nan_to_num(display, nan=DEFAULT_DB_FLOOR)
            # Ensure float32 dtype
            if display.dtype != np.float32:
                display = display.astype(np.float32)
        
        return display, time_range
    
    def _update_color_levels(self, src_ix: int, display: Optional[np.ndarray] = None) -> Tuple[float, float]:
        """
        Update and return color levels for display.
        
        Uses global min/max across all sources when auto_scale is enabled
        for consistent normalization. Global values persist and never
        decrease (max) or increase (min) to maintain stable scaling.
        
        Args:
            src_ix: Source index
            display: Optional display array (with NaNs replaced) to use for min/max calculation.
                    If not provided, will prepare display array from heatmap.
            
        Returns:
            Tuple of (min_level, max_level)
        """
        # Prepare display array if not provided
        if display is None:
            display, _ = self._prepare_display_heatmap(src_ix)
        
        # Always calculate/update global min/max from display data (in dB units)
        # This ensures we have valid dB-range values regardless of auto_scale setting
        if display is not None and np.isfinite(display).any():
            # Exclude DEFAULT_DB_FLOOR values from min/max calculation
            # These are placeholder values for NaN regions and shouldn't affect normalization
            valid_mask = display > (DEFAULT_DB_FLOOR + 1.0)  # Add small tolerance for floating point
            if np.any(valid_mask):
                hmin = float(np.min(display[valid_mask]))
                hmax = float(np.max(display[valid_mask]))
                if np.isfinite(hmin) and np.isfinite(hmax) and (hmax > hmin):
                    # Use exponential smoothing (leaky integrator) for stable but adaptable limits
                    alpha = 0.1  # Adaptation rate (0.0 to 1.0)
                    
                    if getattr(self, '_global_min', None) is None:
                        self._global_min = hmin
                    else:
                        # Allow min to adapt back up slowly, but track new lows quickly
                        if hmin < self._global_min:
                            self._global_min = 0.5 * self._global_min + 0.5 * hmin
                        else:
                            self._global_min = (1.0 - alpha) * self._global_min + alpha * hmin
                            
                    if getattr(self, '_global_max', None) is None:
                        self._global_max = hmax
                    else:
                        # Allow max to adapt back down slowly, but track new highs quickly
                        if hmax > self._global_max:
                            self._global_max = 0.5 * self._global_max + 0.5 * hmax
                        else:
                            self._global_max = (1.0 - alpha) * self._global_max + alpha * hmax
        
        # When auto_scale is 'none', check if limits are in dB range
        # If limits are outside reasonable dB range (likely in raw power units), use global min/max
        if self._auto_scale == 'none' and not getattr(self, 'is_statically_scaled', False):
            # Check if limits are in reasonable dB range (dB values are typically negative, < 0)
            # If limits are positive or very large, they're likely in raw power units, not dB
            limit_min = float(self.lower_limit)
            limit_max = float(self.upper_limit)
            
            # If limits look like they're in raw power units (positive, large values), use global min/max
            if (limit_min > 0 or limit_max > 100) and (self._global_min is not None and self._global_max is not None):
                return (self._global_min, self._global_max)
            # Otherwise, use the limits as-is (assuming they're in dB)
            return (limit_min, limit_max)
        
        # Auto-scale mode: always use global min/max
        # Return global values if available, otherwise fallback to defaults
        if self._global_min is not None and self._global_max is not None:
            return (self._global_min, self._global_max)
        else:
            return (DEFAULT_DB_FLOOR, DEFAULT_DB_CEILING)
    
    def _get_colormap_lut(self) -> np.ndarray:
        """
        Get colormap lookup table with caching.
        
        Returns:
            Lookup table array
        """
        if self._cached_lut is None or self._cached_color_set != self.color_set:
            self._cached_lut = self.get_colormap(self.color_set, 256)
            self._cached_color_set = self.color_set
        return self._cached_lut
    
    def _update_markers(self, src_ix: int, pw, mrk: np.ndarray, mrk_ts: np.ndarray) -> None:
        """
        Update markers on the plot.
        
        Args:
            src_ix: Source index
            pw: Plot widget (unused, kept for compatibility)
            mrk: Marker array
            mrk_ts: Marker timestamps
        """
        # Get plot item and remote view for this source
        if src_ix >= len(self._plot_items) or src_ix >= len(self._remote_views):
            return
        pw = self._plot_items[src_ix]
        remote_view = self._remote_views[src_ix]
        
        # Update expiry threshold
        if not self._buffers[src_ix]._tvec.size:
            return
        
        buf = self._buffers[src_ix]
        if hasattr(buf, "_write_idx"):
            lead_t = float(buf._tvec[buf._write_idx])  # type: ignore
            self._t_expired = max(lead_t - self._duration, self._t_expired)

        # Remove expired markers
        # Note: pw.items works via proxy, but isinstance check may need to be done differently
        # We'll check if there are items and remove expired ones
        try:
            items = pw.items
            if items and len(items) > 0:
                while (len(self._marker_info) > 0) and (self._marker_info[0].timestamp < self._t_expired):
                    pop_info = self._marker_info.popleft()
                    pw.removeItem(pop_info[2])
                    self._marker_texts_pool.append(pop_info[2])
        except Exception:
            # If items access fails, try to remove based on marker_info
            while (len(self._marker_info) > 0) and (self._marker_info[0].timestamp < self._t_expired):
                pop_info = self._marker_info.popleft()
                try:
                    pw.removeItem(pop_info[2])
                except Exception:
                    pass
                self._marker_texts_pool.append(pop_info[2])

        # Add new markers
        if mrk.size:
            b_new = mrk_ts > self._src_last_marker_time[src_ix]
            for _t, _m in zip(mrk_ts[b_new], mrk[b_new]):
                if len(self._marker_texts_pool) > 0:
                    text = self._marker_texts_pool.popleft()
                    text.setText(_m)
                else:
                    # Create TextItem in remote process
                    text = remote_view.pg.TextItem(text=_m, angle=90)  # type: ignore
                    # Create font in remote process to avoid QGuiApplication warnings
                    font = remote_view.pg.QtGui.QFont()  # type: ignore
                    font.setPointSize(int(self.font_size + 2.0))
                    text.setFont(font)

                # Place at bottom of frequency range
                text.setPos((_t % self.duration), float(self._fmin_hz))
                pw.addItem(text)
                self._marker_info.append(MarkerMap(src_ix, _t, text))

            if np.any(b_new):
                self._src_last_marker_time[src_ix] = mrk_ts[b_new][-1]

    def update_visualization(self, data: np.ndarray, timestamps: np.ndarray) -> None:
        """
        Update visualization with new data.
        
        Includes performance optimizations:
        - Early exit if no new data
        - Batch update tracking with dirty flags
        - Minimum time delta checks
        
        Args:
            data: List of (data, markers) tuples per source
            timestamps: List of (timestamps, marker_timestamps) tuples per source
        """
        # Early exit: check if we have any timestamps at all
        if not any([np.any(_) for _ in timestamps[0]]):
            return

        # Get cached LUT
        lut = self._get_colormap_lut()

        # Pre-calculate x-axis range once before processing sources (for Scroll mode)
        # This ensures consistent range calculation and prevents jerky updates
        target_xrange: Optional[Tuple[float, float]] = None
        if self.plot_mode == "Scroll" and not self._is_manually_scrolled:
            # Find first available source to calculate range
            for src_ix in range(len(data)):
                if src_ix >= len(self._plot_items) or src_ix >= len(self._buffers):
                    continue
                buf = self._buffers[src_ix]
                state = self._source_states[src_ix]
                
                if buf._data.size > 0:
                    # Calculate time range for x-axis using this source's state
                    if state.session_time_range is not None and state.session_start_time is not None:
                        # Full session available: use session range for x-axis limits
                        session_start, session_end = state.session_time_range
                        # Default to showing most recent duration window
                        if buf._tvec.size > 0:
                            current_time = float(np.nanmax(buf._tvec))
                            time_end = current_time
                            time_start = time_end - self.duration
                        else:
                            time_end = session_end
                            time_start = time_end - self.duration
                    else:
                        # No session range yet: use buffer range
                        if buf._tvec.size > 0:
                            t_max = float(np.nanmax(buf._tvec))
                            if np.isfinite(t_max):
                                time_end = t_max
                                time_start = time_end - self.duration
                            else:
                                time_start = 0.0
                                time_end = time_start + self.duration
                        else:
                            time_start = 0.0
                            time_end = time_start + self.duration
                    
                    target_xrange = (time_start, time_end)
                    break
        
        # Update x-axis range early (before preparing displays) to ensure consistency
        if target_xrange is not None:
            # Find bottom plot item (the one all others are linked to)
            bottom_plot_item = None
            for plot_item in reversed(self._plot_items):
                if plot_item is not None:
                    bottom_plot_item = plot_item
                    break
            
            if bottom_plot_item is not None:
                time_start, time_end = target_xrange
                # Suppress signal during programmatic update
                self._suppress_range_signal = True
                try:
                    bottom_plot_item.setXRange(time_start, time_end)
                    # Store the auto-update range for comparison
                    self._last_auto_xrange = (time_start, time_end)
                finally:
                    self._suppress_range_signal = False

        for src_ix in range(len(data)):
            # Get plot item from remote plot items list
            if src_ix >= len(self._plot_items):
                continue
            pw = self._plot_items[src_ix]
            if pw is None:
                continue

            dat, mrk = data[src_ix]
            ts, mrk_ts = timestamps[src_ix]

            # Align axis label widths if needed
            if self._do_yaxis_sync:
                self.sync_y_axes()

            # Process spectrogram if buffer has data
            buff_data = self._buffers[src_ix]._data
            has_new_data = False
            if buff_data.size > 0:
                buf = self._buffers[src_ix]
                state = self._source_states[src_ix]
                
                # Check if there's new data to process (avoid expensive spectrogram computation if no new data)
                # In Scroll mode, _write_idx doesn't advance (buffer shifts data instead), so we use timestamp-based detection
                # In Sweep mode, _write_idx advances, so we can use write index comparison
                if self.plot_mode == "Scroll":
                    # In Scroll mode, check if the last timestamp in buffer has changed
                    # Enhanced: add minimum time delta check to avoid processing tiny changes
                    MIN_TIME_DELTA = 0.01  # Minimum 10ms change required
                    if buf._tvec.size > 0:
                        curr_last_timestamp = float(buf._tvec[-1])
                        # Use a small threshold to handle floating point precision
                        if state.last_processed_write_idx is None:
                            has_new_data = True
                        else:
                            # last_processed_write_idx stores the last processed timestamp in Scroll mode
                            last_processed_timestamp = float(state.last_processed_write_idx)
                            time_delta = abs(curr_last_timestamp - last_processed_timestamp)
                            # Only process if time delta is significant (avoids processing tiny timestamp changes)
                            has_new_data = time_delta > MIN_TIME_DELTA
                    else:
                        has_new_data = False
                elif hasattr(buf, "_write_idx"):
                    # Sweep mode: use write index comparison
                    curr_write_idx = int(buf._write_idx)  # type: ignore
                    # Only compute spectrogram if we have new data or haven't processed this source yet
                    has_new_data = (state.last_processed_write_idx is None or 
                                   curr_write_idx != state.last_processed_write_idx)
                
                if has_new_data:
                    # Compute channel average with validation
                    x = self._compute_channel_average(buff_data)
                    if x is not None:
                        # Get sampling rate
                        srate = self._data_sources[src_ix].data_stats.get('srate', 0.0) or 0.0
                        if srate >= MIN_SRATE:
                            # Track session start time on first data update
                            if self.plot_mode == "Scroll" and buf._tvec.size > 0:
                                if state.session_start_time is None:
                                    # First data: record session start time
                                    state.session_start_time = float(np.nanmin(buf._tvec))
                                    state.session_time_range = (state.session_start_time, state.session_start_time)
                                # Update session time range (end time)
                                current_time = float(np.nanmax(buf._tvec))
                                if state.session_time_range is not None:
                                    state.session_time_range = (state.session_start_time, current_time)
                            
                            # Compute spectrogram (only when we have new data)
                            spec_result = self._compute_spectrogram(x, srate)
                            if spec_result is not None:
                                f, t, Pxx = spec_result

                                # Ensure source state is initialized
                                if self._ensure_source_state(src_ix, srate, f, Pxx):
                                    if state.freq_mask is not None and state.heatmap is not None:
                                        # Apply frequency mask
                                        P_use = Pxx[state.freq_mask, :]

                                        is_first_update = state.last_processed_write_idx is None

                                        # Calculate new columns to add
                                        new_cols = self._calculate_new_columns(src_ix, buf)
                                        
                                        if is_first_update:
                                            if self.plot_mode == "Sweep":
                                                n_cols = state.heatmap.shape[1]
                                                cols_to_copy = min(P_use.shape[1], n_cols)
                                                if cols_to_copy > 0:
                                                    state.heatmap[:, :cols_to_copy] = P_use[:, -cols_to_copy:]
                                                    state.write_index = cols_to_copy % n_cols
                                            else:
                                                state.heatmap = P_use.astype(np.float32).copy()
                                                state.preallocated_capacity = state.heatmap.shape[1]
                                                state.n_time_cols = state.heatmap.shape[1]
                                        elif new_cols > 0:
                                            self._update_heatmap_columns(src_ix, P_use, new_cols)
                                        
                                        # Mark this write index/timestamp as processed
                                        if self.plot_mode == "Scroll":
                                            # In Scroll mode, store the last processed timestamp
                                            if buf._tvec.size > 0:
                                                state.last_processed_write_idx = float(buf._tvec[-1])
                                        elif hasattr(buf, "_write_idx"):
                                            # In Sweep mode, store the write index
                                            state.last_processed_write_idx = int(buf._write_idx)  # type: ignore

                # Prepare display heatmap (always update display, even if no new data)
                # Pass target_xrange to ensure consistent column extraction
                display, display_time_range = self._prepare_display_heatmap(src_ix, pw=pw, target_xrange=target_xrange)
                if display is not None:
                    # Update color levels using display array (after NaN replacement)
                    # This ensures global min/max are calculated from the same data that will be displayed
                    levels = self._update_color_levels(src_ix, display=display)
                    
                    # Determine time range for image rect (use target_xrange if available, otherwise calculate)
                    if target_xrange is not None:
                        time_start, time_end = target_xrange
                        time_width = time_end - time_start
                    elif self.plot_mode == "Scroll":
                        # Fallback: calculate time range (shouldn't happen if target_xrange was calculated)
                        state = self._source_states[src_ix]
                        if state.session_time_range is not None and state.session_start_time is not None:
                            session_start, session_end = state.session_time_range
                            if buf._tvec.size > 0:
                                current_time = float(np.nanmax(buf._tvec))
                                time_end = current_time
                                time_start = time_end - self.duration
                                time_width = self.duration
                            else:
                                time_end = session_end
                                time_start = time_end - self.duration
                                time_width = self.duration
                        else:
                            if buf._tvec.size > 0:
                                t_max = float(np.nanmax(buf._tvec))
                                if np.isfinite(t_max):
                                    time_end = t_max
                                    time_start = time_end - self.duration
                                    time_width = self.duration
                                else:
                                    time_start = 0.0
                                    time_width = float(self.duration)
                            else:
                                time_start = 0.0
                                time_width = float(self.duration)
                    else:
                        # In sweep mode, use fixed 0 to duration
                        time_start = 0.0
                        time_width = float(self.duration)
                    
                    # Update image with performance optimization: use _callSync='off' for operations that don't need return values
                    # Batch update: only update if display is dirty or has changed significantly
                    should_update = self._display_dirty.get(src_ix, True) or has_new_data
                    if should_update and src_ix < len(self._image_items):
                        img = self._image_items[src_ix]
                        if img is not None:
                            img.setImage(display, levels=levels, autoLevels=False, _callSync='off')
                            img.setLookupTable(lut, _callSync='off')
                            # Use display_time_range if available, otherwise fallback to calculated time range
                            if display_time_range is not None:
                                img_time_start, img_time_end = display_time_range
                                img_time_width = img_time_end - img_time_start
                            else:
                                img_time_start = time_start
                                img_time_width = time_width
                            rect = pg.QtCore.QRectF(
                                img_time_start, float(self._fmin_hz),
                                img_time_width,
                                float(self._fmax_hz - self._fmin_hz)
                            )
                            img.setRect(rect, _callSync='off')
                            # Mark as clean after update
                            self._display_dirty[src_ix] = False
                    elif has_new_data:
                        # Mark as dirty for next update
                        self._display_dirty[src_ix] = True

            # Update markers
            self._update_markers(src_ix, pw, mrk, mrk_ts)

    # ------------------------------ #
    # Properties
    # ------------------------------ #
    @property
    def ylabel_as_title(self):
        return self._ylabel_as_title

    @ylabel_as_title.setter
    def ylabel_as_title(self, value):
        self._ylabel_as_title = value
        self._schedule_reset(reset_channel_labels=True)

    @property
    def ylabel(self):
        return self._ylabel

    @ylabel.setter
    def ylabel(self, value):
        self._ylabel = value
        self._schedule_reset(reset_channel_labels=True)

    @property
    def ylabel_width(self):
        return self._ylabel_width

    @ylabel_width.setter
    def ylabel_width(self, value):
        self._ylabel_width = value
        self._schedule_reset(reset_channel_labels=True)

    @property
    def fmin_hz(self):
        return self._fmin_hz

    @fmin_hz.setter
    def fmin_hz(self, value):
        self._fmin_hz = float(value)
        # Update Y-axis range and limits in-place if plots exist
        if self._plot_items:
            for pw in self._plot_items:
                if pw is not None:
                    pw.setYRange(self._fmin_hz, self._fmax_hz)
                    # Update y-axis limits to lock the range
                    pw.setLimits(yMin=self._fmin_hz, yMax=self._fmax_hz, minYRange=self._fmax_hz - self._fmin_hz, maxYRange=self._fmax_hz - self._fmin_hz)
            # Invalidate frequency mask - will recompute on next update
            for state in self._source_states:
                state.freq_mask = None
        else:
            # No plots yet, need full reset
            self._schedule_reset(reset_channel_labels=False)

    @property
    def fmax_hz(self):
        return self._fmax_hz

    @fmax_hz.setter
    def fmax_hz(self, value):
        self._fmax_hz = float(value)
        # Update Y-axis range and limits in-place if plots exist
        if self._plot_items:
            for pw in self._plot_items:
                if pw is not None:
                    pw.setYRange(self._fmin_hz, self._fmax_hz)
                    # Update y-axis limits to lock the range
                    pw.setLimits(yMin=self._fmin_hz, yMax=self._fmax_hz, minYRange=self._fmax_hz - self._fmin_hz, maxYRange=self._fmax_hz - self._fmin_hz)
            # Invalidate frequency mask - will recompute on next update
            for state in self._source_states:
                state.freq_mask = None
        else:
            # No plots yet, need full reset
            self._schedule_reset(reset_channel_labels=False)

    @property
    def nperseg(self):
        return self._nperseg

    @nperseg.setter
    def nperseg(self, value):
        self._nperseg = int(value)
        # Invalidate state - will recompute with new parameters on next update
        if self._image_items:
            for state in self._source_states:
                state.hop_size = None
                state.n_time_cols = None
                state.heatmap = None
                state.write_index = 0
                state.last_processed_write_idx = None
                state.sample_carry = 0
        else:
            # No plots yet, need full reset
            self._schedule_reset(reset_channel_labels=False)

    @property
    def noverlap(self):
        return self._noverlap

    @noverlap.setter
    def noverlap(self, value):
        self._noverlap = int(value)
        # Invalidate state - will recompute with new parameters on next update
        if self._image_items:
            for state in self._source_states:
                state.hop_size = None
                state.n_time_cols = None
                state.heatmap = None
                state.write_index = 0
                state.last_processed_write_idx = None
                state.sample_carry = 0
        else:
            # No plots yet, need full reset
            self._schedule_reset(reset_channel_labels=False)

    @RendererDataTimeSeries.auto_scale.setter
    def auto_scale(self, value):
        self._requested_auto_scale = value.lower()
        self._schedule_reset(reset_channel_labels=False)
    
    @property
    def is_manually_scrolled(self) -> bool:
        """Get whether the view is currently manually scrolled (de-synced)."""
        return self._is_manually_scrolled
    
    @property
    def plot_mode(self):
        """Get current plot mode."""
        return self._plot_mode
    
    @plot_mode.setter
    def plot_mode(self, value):
        """Override plot_mode setter to handle sync state on mode changes."""
        old_mode = self._plot_mode
        # Set the mode and call reset (same as parent behavior)
        self._plot_mode = value
        self.reset(reset_channel_labels=False)
        
        # Reset sync state when mode changes
        if old_mode != value:
            self._is_manually_scrolled = False
            self._last_auto_xrange = None
            self._suppress_range_signal = False
    
    @property
    def color_set(self):
        """Get current color set."""
        return super().color_set
    
    @color_set.setter
    def color_set(self, value):
        """Set color set and invalidate LUT cache."""
        super().color_set = value  # type: ignore
        self._cached_lut = None
        self._cached_color_set = None
        # Update image items with new colormap if they exist
        if self._image_items:
            lut = self._get_colormap_lut()
            for img in self._image_items:
                if img is not None:
                    img.setLookupTable(lut)
    
    @property
    def update_rate_hz(self):
        """Get current update rate in Hz."""
        return self._update_rate_hz
    
    @update_rate_hz.setter
    def update_rate_hz(self, value):
        """Set update rate and restart timer with new interval."""
        self._update_rate_hz = float(value)
        if self._timer.isActive():
            self.restart_timer()
    
    @property
    def max_samples_per_spectrogram(self):
        """Get maximum samples per spectrogram computation."""
        return self._max_samples_per_spectrogram
    
    @max_samples_per_spectrogram.setter
    def max_samples_per_spectrogram(self, value):
        """Set maximum samples per spectrogram (None for no limit)."""
        self._max_samples_per_spectrogram = int(value) if value is not None else None


