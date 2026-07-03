from qtpy import QtWidgets, QtCore
from stream_viewer.widgets.control_panel import HidableCtrlWrapWidget


class ConfigAndRenderWidget(QtWidgets.QWidget):
    """
    This widget encapsulates 2 widgets:
    On the left is a control panel; on the right is a renderer.
    Specifically, the control panel widget is a HidableControlPanel
    which must receive as an argument a control panel widget
    that has already been tuned to the specific renderer.
    """

    def __init__(self, renderer, control_panel, *args, make_hidable=True, **kwargs):
        super().__init__(*args, **kwargs)

        # TODO: Assert renderer and control_panel are compatible.

        self.setLayout(QtWidgets.QHBoxLayout())

        if control_panel is not None:
            settings_widget = HidableCtrlWrapWidget(control_panel) if make_hidable else control_panel
            self.layout().addWidget(settings_widget)

            if isinstance(settings_widget, HidableCtrlWrapWidget) and hasattr(renderer, 'auto_scale_once'):
                settings_widget.btn_autoscale_channel.setVisible(True)
                settings_widget.btn_autoscale_channel.clicked.connect(lambda _=False: renderer.auto_scale_once(mode='by-channel'))
                settings_widget.btn_autoscale_stream.setVisible(True)
                settings_widget.btn_autoscale_stream.clicked.connect(lambda _=False: renderer.auto_scale_once(mode='by-stream'))

        self.renderer = renderer

        # Right panel with stacked layout: placeholder + renderer widget
        right_panel = QtWidgets.QWidget(self)
        right_panel.setLayout(QtWidgets.QStackedLayout())
        self._stack = right_panel.layout()

        # Placeholder shown until sources are connected
        self._placeholder = QtWidgets.QLabel(parent=right_panel)
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setText("Waiting for stream...")
        self._stack.addWidget(self._placeholder)

        # Actual renderer widget
        renderer.native_widget.setParent(right_panel)
        self._stack.addWidget(renderer.native_widget)

        # Add right panel to main layout
        self.layout().addWidget(right_panel)

        # React to renderer/source changes
        if hasattr(self.renderer, 'chan_states_changed'):
            self.renderer.chan_states_changed.connect(self._update_source_status)
        # Initial state
        self._update_source_status(self.renderer)

    @property
    def control_panel(self):
        return self.children()[1].control_panel

    @QtCore.Slot(QtCore.QObject)
    def _update_source_status(self, *_):
        # Determine connection status and placeholder text
        sources = getattr(self.renderer, "_data_sources", [])
        waiting_names = []
        all_connected = True
        for src in sources:
            is_conn = getattr(src, "is_connected", True)
            if not is_conn:
                all_connected = False
                # Try to extract user-friendly name from identifier JSON
                name = None
                try:
                    import json
                    ident = json.loads(src.identifier) if hasattr(src, "identifier") else {}
                    name = ident.get("name", None)
                except Exception:
                    name = None
                waiting_names.append(name or "unknown")

        if all_connected or len(sources) == 0:
            # Show renderer
            self._stack.setCurrentIndex(1)
        else:
            # Show placeholder
            if waiting_names:
                self._placeholder.setText("Waiting for stream: " + ", ".join(waiting_names))
            else:
                self._placeholder.setText("Waiting for stream...")
            self._stack.setCurrentIndex(0)
