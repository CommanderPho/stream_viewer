# Note: Cannot use relative imports here because these modules may be searched and loaded with importlib
from stream_viewer.renderers.bar_pg import BarPG
from stream_viewer.renderers.line_pg import LinePG
from stream_viewer.renderers.line_vis import LineVis
from stream_viewer.renderers.line_power_vis import LinePowerVis
from stream_viewer.renderers.eeg_quality_pg import EEGQualityPG
from stream_viewer.renderers.resolver import load_renderer, list_renderers, get_kwargs_from_settings
from stream_viewer.renderers.topo_vb import TopoVB
from stream_viewer.renderers.heatmap_pg import HeatmapPG
from stream_viewer.renderers.sonify_audio import SonifyAudio
from stream_viewer.renderers.motion_orientation_3d import MotionOrientation3D

# # Try to import HeatmapGPU (requires torch for GPU acceleration, falls back to CPU)
# try:
#     from stream_viewer.renderers.heatmap_gpu import HeatmapGPU
# except ImportError:
#     # torch not available, HeatmapGPU will not be available
#     pass