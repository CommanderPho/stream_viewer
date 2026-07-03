from qtpy import QtWidgets
from qtpy import QtCore
from stream_viewer.widgets.interface import IControlPanel


class GenericControlPanel(IControlPanel):
    pass


class NoChansControlPanel(IControlPanel):

    def reset_widgets(self, renderer):
        super().reset_widgets(renderer)
        # Disable a couple standard widgets that we can't use.
        _tree = self.findChild(QtWidgets.QTreeWidget, name="Chans_TreeWidget")
        _tree.setEnabled(False)
        _tree.setVisible(False)
        _checkbox = self.findChild(QtWidgets.QCheckBox, name="ShowNames_CheckBox")
        _checkbox.setEnabled(False)


class HidableCtrlWrapWidget(QtWidgets.QWidget):
    def __init__(self, control_panel, name="HidableCtrlWrapWidget", is_visible=False, **kwargs):
        super().__init__(**kwargs)
        self.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.MinimumExpanding)
        self.setObjectName(name)

        # push button to show/hide everything in control_panel.
        showhide_pb = QtWidgets.QPushButton()
        showhide_pb.setObjectName("ShowHide_PushButton")
        showhide_pb.clicked.connect(self.handle_showhide)
        showhide_pb.setIcon(self.style().standardIcon(
            QtWidgets.QStyle.SP_ArrowLeft if is_visible else QtWidgets.QStyle.SP_ArrowRight, None, self))

        self._vis_toggle = is_visible
        self._ctrl_panel = control_panel
        self._ctrl_panel.setVisible(self._vis_toggle)

        # Autoscale buttons
        self.btn_autoscale_channel = QtWidgets.QToolButton()
        self.btn_autoscale_channel.setObjectName("AutoScale_Channel_ToolButton")
        self.btn_autoscale_channel.setToolTip("Auto-scale Once By-Channel")
        self.btn_autoscale_channel.setText("Ch")
        self.btn_autoscale_channel.setVisible(False)
        self.btn_autoscale_channel.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Minimum)

        self.btn_autoscale_stream = QtWidgets.QToolButton()
        self.btn_autoscale_stream.setObjectName("AutoScale_Stream_ToolButton")
        self.btn_autoscale_stream.setToolTip("Auto-scale Once By-Stream")
        self.btn_autoscale_stream.setText("St")
        self.btn_autoscale_stream.setVisible(False)
        self.btn_autoscale_stream.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Minimum)

        try:
            import qtawesome as qta
            self.btn_autoscale_channel.setIcon(qta.icon('fa.align-justify', color='white'))
            self.btn_autoscale_stream.setIcon(qta.icon('fa.arrows-v', color='white'))
        except ImportError:
            pass

        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(showhide_pb)
        self.layout().addWidget(self.btn_autoscale_channel)
        self.layout().addWidget(self.btn_autoscale_stream)
        self.layout().addWidget(self._ctrl_panel)

    @QtCore.Slot(bool)
    def handle_showhide(self, checked):
        self._ctrl_panel.setVisible(not self._vis_toggle)
        # Update pushbutton icon
        _pb = self.findChild(QtWidgets.QPushButton, "ShowHide_PushButton")
        _pb.setIcon(self.style().standardIcon(
            QtWidgets.QStyle.SP_ArrowRight if self._vis_toggle else QtWidgets.QStyle.SP_ArrowLeft,
            None, self))
        # Save state
        self._vis_toggle = not self._vis_toggle

    @property
    def control_panel(self):
        return self._ctrl_panel
