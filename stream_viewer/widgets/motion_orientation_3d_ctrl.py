from qtpy import QtWidgets
from stream_viewer.widgets.interface import IControlPanel
from pathlib import Path


class MotionOrientation3DControlPanel(IControlPanel):
    """Configuration panel for MotionOrientation3D renderer."""

    def __init__(self, renderer, name="MotionOrientation3DControlPanelWidget", **kwargs):
        super().__init__(renderer, name=name, **kwargs)

    def _init_widgets(self):
        super()._init_widgets()
        for spin_name in ("LL_SpinBox", "UL_SpinBox", "HP_SpinBox"):
            spinbox = self.findChild(QtWidgets.QDoubleSpinBox, name=spin_name)
            if spinbox is not None:
                spinbox.setVisible(False)
                row, col, row_span, col_span = self.layout().getItemPosition(self.layout().indexOf(spinbox))
                label_item = self.layout().itemAtPosition(row, 0)
                if label_item is not None:
                    label_item.widget().setVisible(False)
        row_ix = self._last_row + 1
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        self.layout().addWidget(separator, row_ix, 0, 1, 2)
        row_ix += 1
        self.layout().addWidget(QtWidgets.QLabel("Head Mesh Path"), row_ix, 0, 1, 1)
        mesh_path_layout = QtWidgets.QHBoxLayout()
        _line_edit = QtWidgets.QLineEdit()
        _line_edit.setObjectName("HeadMeshPath_LineEdit")
        _line_edit.setReadOnly(True)
        mesh_path_layout.addWidget(_line_edit)
        _button = QtWidgets.QPushButton("Browse...")
        _button.setObjectName("HeadMeshPath_Button")
        mesh_path_layout.addWidget(_button)
        mesh_path_widget = QtWidgets.QWidget()
        mesh_path_widget.setLayout(mesh_path_layout)
        self.layout().addWidget(mesh_path_widget, row_ix, 1, 1, 1)
        row_ix += 1
        self.layout().addWidget(QtWidgets.QLabel("Madgwick beta"), row_ix, 0, 1, 1)
        _beta_spin = QtWidgets.QDoubleSpinBox()
        _beta_spin.setObjectName("MadgwickBeta_SpinBox")
        _beta_spin.setRange(0.01, 1.0)
        _beta_spin.setSingleStep(0.01)
        _beta_spin.setDecimals(2)
        self.layout().addWidget(_beta_spin, row_ix, 1, 1, 1)

    def reset_widgets(self, renderer):
        super().reset_widgets(renderer)
        _line_edit = self.findChild(QtWidgets.QLineEdit, name="HeadMeshPath_LineEdit")
        _button = self.findChild(QtWidgets.QPushButton, name="HeadMeshPath_Button")
        if _line_edit is not None:
            _line_edit.setText(renderer.head_mesh_path)
        if _button is not None:
            try:
                _button.clicked.disconnect()
            except Exception:
                pass
            _button.clicked.connect(lambda: self._on_browse_head_mesh(renderer))
        _beta_spin = self.findChild(QtWidgets.QDoubleSpinBox, name="MadgwickBeta_SpinBox")
        if _beta_spin is not None:
            _beta_spin.setValue(renderer.madgwick_beta)
            try:
                _beta_spin.valueChanged.disconnect()
            except Exception:
                pass
            _beta_spin.valueChanged.connect(lambda v, r=renderer: setattr(r, 'madgwick_beta', float(v)))

    def _on_browse_head_mesh(self, renderer):
        current_path = Path(renderer.head_mesh_path) if renderer.head_mesh_path else Path.home()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Head Mesh STL", str(current_path), "STL Files (*.stl);;All Files (*)")
        if path:
            renderer.head_mesh_path = path
            _line_edit = self.findChild(QtWidgets.QLineEdit, name="HeadMeshPath_LineEdit")
            if _line_edit is not None:
                _line_edit.setText(path)
