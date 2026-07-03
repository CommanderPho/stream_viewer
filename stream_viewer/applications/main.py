#  Copyright (C) 2014-2021 Syntrogi Inc dba Intheon. All rights reserved.

import json
import sys
import functools
import argparse
import logging
from pathlib import Path
from qtpy import QtWidgets, QtCore
from qtpy.QtGui import QIcon, QKeySequence
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
import stream_viewer
from stream_viewer.data import LSLDataSource
from stream_viewer.data import LSLStreamInfoTableModel
from stream_viewer.widgets import load_widget
from stream_viewer.widgets import ConfigAndRenderWidget
from stream_viewer.widgets import StreamStatusQMLWidget
from phopyqthelper.widgets import ConsoleOutputWidget
from stream_viewer.renderers import load_renderer, list_renderers, get_kwargs_from_settings

# Suppress console window on Windows
if sys.platform == 'win32':
    try:
        import ctypes
        # Try to hide the console window
        kernel32 = ctypes.windll.kernel32
        # GetConsoleWindow returns a handle to the console window, or None if no console
        console_window = kernel32.GetConsoleWindow()
        if console_window:
            # Hide the console window
            user32 = ctypes.windll.user32
            user32.ShowWindow(console_window, 0)  # SW_HIDE = 0
    except Exception:
        # If ctypes fails, continue without hiding console
        pass


logger = logging.getLogger(__name__)


class LSLViewer(QtWidgets.QMainWindow):
    RENDERER = 'LineVis'

    def __init__(self, settings_path: str = None):
        """
        This can be run at the terminal either with `python -m stream_viewer.applications.main` or the executable
        `lsl_viewer`.

        Additional command-line arguments are available. See `lsl_viewer --help`.

        The LSL Viewer Main application provides an interface to connect LSL data sources to a variety of different
        renderers. The window has 2 main areas: the dock area on the left and the dock area on the right.

        A list of streams appears in the dock area on the left. Please see the [LSL Status documentation](lsl_status.md)
        for a description of this panel. The stream list can be removed from the main window and float as its own dock,
        but there is rarely a good reason for doing so.

        Double-clicking on a stream will launch a modal window with a dropdown box giving a list of identified
        renderers. This includes renderers that come with the stream_viewer package as well as any renderers that
        appear in plugins folders. Searched plugins folders include ~/.stream_viewer/plugins/renderers ,
        and any folders that are specified in the settings file.

        Choosing a renderer and clicking OK will spawn a new renderer dock. The renderer will be initialized with
        settings in the ini file.

        The settings parsed from the ini file will determine whether the dock is docked or floating, and its position
        and size if floating. This can always be modified after the fact by dragging it out and resizing it. The
        floating status and location information will be overwritten in the ini file when the application is closed.

        The settings parsed from the ini file will also be used to provide initial configuration options to the
        renderer. Most of these options can be updated thereafter using the widgets in the control panel.

        Args:
            settings_path: path to the ini file storing application settings. If not provided then the default
                ~/.stream_viewer/lsl_viewer.ini is used.
        """
        super().__init__()

        self._open_renderers = []  # List of renderer keys (rend_cls :: strm_name :: int)
        self._plugin_dirs = {'renderers': [], 'widgets': []}  # Dict of lists of directories to search for plugins
                                                              #  in addition to default search dir of
                                                              #  ~/.stream_viewer/plugins/{renderers|widgets}
        self._monitor_sources = {}

        self.setWindowTitle("Stream Viewer")
        home_dir = Path(QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.HomeLocation))
        self._settings_path = home_dir / '.stream_viewer' / 'lsl_viewer.ini'
        if settings_path is not None:
            settings_path = Path(settings_path)
            if not settings_path.exists():
                # Try only the filename in the default folder.
                settings_path = home_dir / '.stream_viewer' / settings_path.name
            if settings_path.exists():
                self._settings_path = settings_path

        # Set the data model for the stream status view. This handles its own list of streams.
        self.stream_status_model = LSLStreamInfoTableModel(refresh_interval=5.0)
        # Create the stream status panel.
        self.stream_status_widget = StreamStatusQMLWidget(self.stream_status_model)
        # self.stream_status_widget.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
        #                                         QtWidgets.QSizePolicy.MinimumExpanding)
        self.stream_status_widget.stream_activated.connect(self.on_stream_activated)
        self.stream_status_widget.stream_added.connect(self.on_stream_added)
        self.setup_status_panel()

        # Setup console output panel
        self.setup_console_panel()

        # Setup menubar
        self.setup_menus()

        # Read settings and restore geometry.
        self.restoreOnStartup()

    def setup_menus(self):
        refresh_act = QtWidgets.QAction("&Refresh", self)
        refresh_act.triggered.connect(self.stream_status_model.refresh)

        prefs_act = QtWidgets.QAction("&Preferences...", self)
        prefs_act.triggered.connect(self.launch_modal_prefs)
        prefs_act.setEnabled(False)

        # Action to show all stream settings - disabled because it's hard to disconnect from closed docks.
        # stream_settings_act = QtWidgets.QAction("&Stream Settings", self)
        # stream_settings_act.setObjectName("stream_settings_action")  # For easier lookup

        # File menu actions for visualization layout
        file_menu = self.menuBar().addMenu("&File")
        save_layout_act = QtWidgets.QAction("Save Visualization Layout...", self)
        save_layout_act.triggered.connect(self._on_save_visualization_layout)
        load_layout_act = QtWidgets.QAction("Load Visualization Layout...", self)
        load_layout_act.triggered.connect(self._on_load_visualization_layout)
        file_menu.addAction(save_layout_act)
        file_menu.addAction(load_layout_act)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(refresh_act)
        view_menu.addAction(prefs_act)
        # view_menu.addAction(stream_settings_act)
        
        # Console output toggle action
        self._console_act = QtWidgets.QAction("Show &Console Output", self)
        self._console_act.setCheckable(True)
        self._console_act.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self._console_act.triggered.connect(self._toggle_console_output)
        view_menu.addSeparator()
        view_menu.addAction(self._console_act)

    def setup_status_panel(self):
        dock = QtWidgets.QDockWidget("LSL Streams", self)
        dock.setObjectName("StatusPanel")
        dock.setAllowedAreas(QtCore.Qt.AllDockWidgetAreas)
        dock.setMinimumWidth(300)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetClosable | QtWidgets.QDockWidget.DockWidgetFloatable)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)
        dock.setWidget(self.stream_status_widget)

    def setup_console_panel(self):
        """Set up the console output dock widget."""
        self._console_widget = ConsoleOutputWidget(self)
        dock = QtWidgets.QDockWidget("Console Output", self)
        dock.setObjectName("ConsoleOutput")
        dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.TopDockWidgetArea)
        dock.setMinimumHeight(150)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetClosable | QtWidgets.QDockWidget.DockWidgetFloatable)
        dock.setWidget(self._console_widget)
        self._console_dock = dock
        # Initially hidden, can be shown via menu action
        dock.setVisible(False)
        # Sync menu action when dock visibility changes (e.g., when closed via X button)
        dock.visibilityChanged.connect(self._on_console_dock_visibility_changed)

    def restoreOnStartup(self):
        # The start counterpart to closeEvent
        settings = QtCore.QSettings(str(self._settings_path), QtCore.QSettings.IniFormat)

        # ---- Main window geometry ----
        try:
            settings.beginGroup("MainWindow")
            self.resize(settings.value("size", QtCore.QSize(800, 600)))
            self.move(settings.value("pos", QtCore.QPoint(200, 200)))
            if settings.value("fullScreen", 'false') == 'true':
                self.showFullScreen()
            elif settings.value("maximized", 'false') == 'true':
                self.showMaximized()
        except Exception as exc:
            logger.warning("Failed to restore main window geometry: %s", exc)
        finally:
            settings.endGroup()

        # ---- PluginFolders: extra plugin search dirs ----
        try:
            settings.beginGroup("PluginFolders")
            for plugin_group in settings.childGroups():
                try:
                    settings.beginGroup(plugin_group)
                    if plugin_group not in self._plugin_dirs:
                        self._plugin_dirs[plugin_group] = []
                    for str_ix in settings.childKeys():
                        val = settings.value(str_ix)
                        if val is not None and val not in self._plugin_dirs[plugin_group]:
                            self._plugin_dirs[plugin_group].append(val)
                except Exception as exc:
                    logger.warning("Failed to restore plugin folder group '%s': %s", plugin_group, exc)
                finally:
                    settings.endGroup()  # renderers, widgets, etc
        except Exception as exc:
            logger.warning("Failed to restore PluginFolders: %s", exc)
        finally:
            settings.endGroup()  # PluginFolders

        # ---- StreamStatus panel geometry ----
        try:
            settings.beginGroup("StreamStatus")
            status_dock = self.findChild(QtWidgets.QDockWidget, name="StatusPanel")
            if status_dock is not None:
                # Restore dock area
                dock_area = settings.value("dockWidgetArea")
                if dock_area is not None:
                    dock_area = int(dock_area)
                else:
                    dock_area = QtCore.Qt.LeftDockWidgetArea
                
                # Add dock if not already in the correct area
                if self.dockWidgetArea(status_dock) != dock_area:
                    self.addDockWidget(dock_area, status_dock)

                # Restore floating state and geometry
                is_floating = settings.value("floating", 'false') == 'true'
                if is_floating:
                    status_dock.setFloating(True)
                    saved_size = settings.value("size")
                    saved_pos = settings.value("pos")
                    if saved_size is not None:
                        status_dock.resize(saved_size)
                    if saved_pos is not None:
                        status_dock.move(saved_pos)
                else:
                    status_dock.setFloating(False)
                    saved_size = settings.value("size")
                    if saved_size is not None:
                        status_dock.resize(saved_size)
        except Exception as exc:
            logger.warning("Failed to restore StreamStatus dock: %s", exc)
        finally:
            settings.endGroup()

        # ---- ConsoleOutput panel geometry ----
        try:
            settings.beginGroup("ConsoleOutput")
            console_dock = self.findChild(QtWidgets.QDockWidget, name="ConsoleOutput")
            if console_dock is not None:
                # Restore visibility
                is_visible = settings.value("visible", 'false') == 'true'
                if is_visible:
                    # Restore dock area
                    dock_area = settings.value("dockWidgetArea")
                    if dock_area is not None:
                        dock_area = int(dock_area)
                    else:
                        dock_area = QtCore.Qt.BottomDockWidgetArea
                    
                    # Add dock if not already added
                    if self.dockWidgetArea(console_dock) == QtCore.Qt.NoDockWidgetArea:
                        self.addDockWidget(dock_area, console_dock)
                    
                    # Restore floating state and geometry
                    is_floating = settings.value("floating", 'false') == 'true'
                    if is_floating:
                        console_dock.setFloating(True)
                        saved_size = settings.value("size")
                        saved_pos = settings.value("pos")
                        if saved_size is not None:
                            console_dock.resize(saved_size)
                        if saved_pos is not None:
                            console_dock.move(saved_pos)
                    else:
                        console_dock.setFloating(False)
                        saved_size = settings.value("size")
                        if saved_size is not None:
                            console_dock.resize(saved_size)
                    
                    console_dock.setVisible(is_visible)
                    # Sync menu action state
                    if hasattr(self, '_console_act'):
                        self._console_act.setChecked(is_visible)
                else:
                    console_dock.setVisible(False)
                    if hasattr(self, '_console_act'):
                        self._console_act.setChecked(False)
        except Exception as exc:
            logger.warning("Failed to restore ConsoleOutput dock: %s", exc)
        finally:
            settings.endGroup()

        # ---- Restore renderer docks / layouts ----
        try:
            settings.beginGroup("RendererDocksMain")
            dock_groups = settings.childGroups()
        except Exception as exc:
            logger.warning("Failed to read RendererDocksMain from settings: %s", exc)
            dock_groups = []
        finally:
            settings.endGroup()

        for dock_name in dock_groups:
            try:
                settings.beginGroup(dock_name)

                # Build data sources
                data_sources = []
                try:
                    settings.beginGroup("data_sources")
                    for ds_id in settings.childGroups():
                        try:
                            settings.beginGroup(ds_id)
                            cls_name = settings.value("class")
                            src_key = settings.value("identifier")
                            if not cls_name or not src_key:
                                continue

                            src_cls = getattr(stream_viewer.data, cls_name, None)
                            if src_cls is None:
                                logger.warning(
                                    "Unknown data source class '%s' for dock '%s'; skipping source '%s'",
                                    cls_name, dock_name, ds_id,
                                )
                                continue

                            if issubclass(src_cls, LSLDataSource):
                                try:
                                    ident = json.loads(src_key)
                                except Exception as exc:
                                    logger.warning(
                                        "Failed to decode identifier for source '%s' in dock '%s': %s",
                                        ds_id, dock_name, exc,
                                    )
                                    continue
                                try:
                                    data_sources.append(src_cls(ident))
                                except Exception as exc:
                                    logger.warning(
                                        "Failed to construct LSLDataSource for '%s' in dock '%s': %s",
                                        ds_id, dock_name, exc,
                                    )
                            # TODO: handle other src_cls types if/when added
                        except Exception as exc:
                            logger.warning(
                                "Error while restoring data source '%s' for dock '%s': %s",
                                ds_id, dock_name, exc,
                            )
                        finally:
                            settings.endGroup()  # ds_id
                except Exception as exc:
                    logger.warning(
                        "Failed to restore data_sources for dock '%s': %s",
                        dock_name, exc,
                    )
                finally:
                    settings.endGroup()  # data_sources

                # If there are no valid sources, skip this dock
                if not data_sources:
                    logger.warning(
                        "No valid data_sources restored for dock '%s'; skipping renderer activation",
                        dock_name,
                    )
                    settings.endGroup()  # dock_name
                    continue

                # Renderer name / class
                rend_name = dock_name.split("|")[0]
                try:
                    rend_cls = load_renderer(
                        rend_name,
                        extra_search_dirs=self._plugin_dirs.get('renderers', []),
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to load renderer '%s' for dock '%s': %s",
                        rend_name, dock_name, exc,
                    )
                    settings.endGroup()
                    continue

                # Renderer kwargs from settings
                try:
                    rend_kwargs = get_kwargs_from_settings(settings, rend_cls)
                except Exception as exc:
                    logger.warning(
                        "Failed to read renderer settings for '%s' in dock '%s': %s",
                        rend_name, dock_name, exc,
                    )
                    rend_kwargs = {}

                settings.endGroup()  # dock_name

                # Activate renderer dock; failures here should not abort startup
                try:
                    self.on_stream_activated(
                        data_sources,
                        renderer_name=rend_name,
                        renderer_kwargs=rend_kwargs,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to activate renderer '%s' from dock '%s': %s",
                        rend_name, dock_name, exc,
                    )

            except Exception as exc:
                # Catch any unexpected error for this dock and move on
                logger.warning("Unexpected error while restoring dock '%s': %s", dock_name, exc)
                try:
                    settings.endGroup()
                except Exception:
                    pass

    def closeEvent(self, event):
        # Restore stdout/stderr before closing
        if hasattr(self, '_console_widget'):
            self._console_widget.restore_streams()
        self.saveSettings()
        QtWidgets.QMainWindow.closeEvent(self, event)  # super?

    def saveSettings(self):
        settings = QtCore.QSettings(str(self._settings_path), QtCore.QSettings.IniFormat)

        # Prune stale renderer groups at root (renderer configs keyed by rend_key)
        reserved_groups = set(["MainWindow", "PluginFolders", "StreamStatus", "ConsoleOutput", "RendererDocksMain"])
        for grp in settings.childGroups():
            if grp not in reserved_groups and grp not in set(self._open_renderers):
                settings.remove(grp)

        # Save MainWindow geometry.
        settings.beginGroup("MainWindow")
        settings.setValue("fullScreen", self.isFullScreen())
        settings.setValue("maximized", self.isMaximized())
        if not self.isFullScreen() and not self.isMaximized():
            settings.setValue("size", self.size())
            settings.setValue("pos", self.pos())
        settings.endGroup()

        # Save list of search directories
        settings.beginGroup("PluginFolders")
        for k, v in self._plugin_dirs.items():
            settings.beginGroup(k)
            for ix, _dir in enumerate(v):
                settings.setValue(str(ix), _dir)
            settings.endGroup()  # plugin group: renderers, widgets, etc.
        settings.endGroup()  # PluginFolders

        # Save StatusPanel geometry.
        status_dock = self.findChild(QtWidgets.QDockWidget, name="StatusPanel")
        if status_dock:
            settings.beginGroup("StreamStatus")
            settings.setValue("dockWidgetArea", self.dockWidgetArea(status_dock))
            settings.setValue("size", status_dock.size())
            settings.setValue("pos", status_dock.pos())
            settings.setValue("floating", status_dock.isFloating())
            settings.endGroup()

        # Save ConsoleOutput panel geometry.
        console_dock = self.findChild(QtWidgets.QDockWidget, name="ConsoleOutput")
        if console_dock:
            settings.beginGroup("ConsoleOutput")
            settings.setValue("visible", console_dock.isVisible())
            if console_dock.isVisible():
                settings.setValue("dockWidgetArea", self.dockWidgetArea(console_dock))
                settings.setValue("size", console_dock.size())
                settings.setValue("pos", console_dock.pos())
                settings.setValue("floating", console_dock.isFloating())
            settings.endGroup()

        # Save all of the docks' geometry. They are keyed by the dock object name,
        # which is probably equivalent to ";".join([renderer_name, first_src.identifier])
        # Clear old geometry first to avoid resurrecting closed docks
        settings.remove("RendererDocksMain")
        settings.beginGroup("RendererDocksMain")
        dws = [_ for _ in self.findChildren(QtWidgets.QDockWidget) if _ is not status_dock]
        for rend_dw in dws:
            settings.beginGroup(rend_dw.objectName())  # Same as rend_key
            settings.setValue("dockWidgetArea", self.dockWidgetArea(rend_dw))
            settings.setValue("size", rend_dw.size())
            settings.setValue("pos", rend_dw.pos())
            settings.setValue("floating", rend_dw.isFloating())
            settings.endGroup()
        settings.endGroup()

        # Independently save each renderer's configuration (color, scale, etc.).
        # These are keyed the same as the docks.
        for rend_key in self._open_renderers:
            dw = self.findChild(QtWidgets.QDockWidget, rend_key)
            stream_widget = dw.widget()  # instance of ConfigAndRenderWidget
            renderer = stream_widget.renderer
            settings = renderer.save_settings(settings=settings)

        settings.sync()

    @QtCore.Slot()
    def launch_modal_prefs(self):
        print("TODO! launch_modal_prefs")

    @QtCore.Slot(bool)
    def _toggle_console_output(self, checked):
        """Toggle console output dock visibility."""
        if checked:
            if not self._console_dock.isVisible():
                # Add dock if not already added
                if self.dockWidgetArea(self._console_dock) == QtCore.Qt.NoDockWidgetArea:
                    self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self._console_dock)
                self._console_dock.setVisible(True)
                self._console_act.setChecked(True)
        else:
            self._console_dock.setVisible(False)
            self._console_act.setChecked(False)

    @QtCore.Slot(bool)
    def _on_console_dock_visibility_changed(self, visible):
        """Handle console dock visibility changes (e.g., when closed via X button)."""
        if hasattr(self, '_console_act'):
            self._console_act.setChecked(visible)

    @QtCore.Slot(dict)
    def on_stream_added(self, strm):
        self._monitor_sources[strm['uid']] = LSLDataSource(strm, auto_start=True, timer_interval=1000,
                                                           monitor_only=True)
        self._monitor_sources[strm['uid']].rate_updated.connect(
            functools.partial(self.stream_status_widget.model.handleRateUpdated, stream_data=strm)
        )

    @QtCore.Slot(dict)
    def on_stream_activated(self, sources, renderer_name=None, renderer_kwargs={}):
        return self.on_stream_activated(sources, renderer_name=renderer_name, renderer_kwargs=renderer_kwargs, forced_rend_key=None)

    def on_stream_activated(self, sources, renderer_name=None, renderer_kwargs={}, forced_rend_key=None):
        # Normalize renderer_name: if not provided then use a popup combo box.
        if renderer_name is None:
            item, ok = QtWidgets.QInputDialog.getItem(self, "Select Renderer", "Found Renderers",
                                                      list_renderers(extra_search_dirs=self._plugin_dirs['renderers'])
                                                      + self._open_renderers)
            renderer_name = item if ok else None

        if renderer_name is None:
            return

        # Normalize sources. str -> [strs] -> [dicts] -> [LSLDataSources]
        if not isinstance(sources, list):
            sources = [sources]
        # If there are no sources, nothing to activate; return gracefully
        if len(sources) == 0:
            return
        for src_ix, src in enumerate(sources):
            if isinstance(src, str):
                src = json.loads(src)
            if isinstance(src, dict):
                src = LSLDataSource(src)
            if not isinstance(src, LSLDataSource):
                raise ValueError("Only LSLDataSource type currently supported.")
            sources[src_ix] = src

        # If the renderer is already open then we just use that one and add the source(s).
        if renderer_name in self._open_renderers:
            found = self.findChild(QtWidgets.QDockWidget, renderer_name)
            if found is not None:  # Should never be None
                stream_widget = found.widget()  # instance of ConfigAndRenderWidget
                renderer = stream_widget.renderer
                for src in sources:
                    renderer.add_source(src)
                stream_widget.control_panel.reset_widgets(renderer)
                return

        # Renderer not already open. We need a new dock, a control panel, and a renderer with sources added.
        # We keep track of these with a key derived from the renderer_name and the source identifier
        if forced_rend_key is not None:
            rend_key = forced_rend_key
        else:
            src_id = json.loads(sources[0].identifier)
            rend_key = "|".join([renderer_name, src_id['name']])
            n_match = len([_ for _ in self._open_renderers if _.startswith(rend_key)])
            rend_key = rend_key + "|" + str(n_match)

        # New dock
        dock = QtWidgets.QDockWidget(rend_key, self)
        dock.setAllowedAreas(QtCore.Qt.RightDockWidgetArea)
        dock.setObjectName(rend_key)
        dock.setAttribute(QtCore.Qt.WA_DeleteOnClose, on=True)
        dock.setMinimumHeight(300)

        # New renderer
        renderer_kwargs['key'] = rend_key
        renderer_cls = load_renderer(renderer_name, extra_search_dirs=self._plugin_dirs['renderers'])
        renderer = renderer_cls(**renderer_kwargs)
        for src in sources:
            renderer.add_source(src)

        # New control panel
        if hasattr(renderer, 'COMPAT_ICONTROL') and len(renderer.COMPAT_ICONTROL) > 0:
            # Infer the control panel class from a string
            control_panel_cls = load_widget(renderer.COMPAT_ICONTROL[0], extra_search_dirs=self._plugin_dirs['widgets'])
            ctrl_panel = control_panel_cls(renderer)
        else:
            ctrl_panel = None

        # Load the renderer and control panel into a common widget, parented by dock.
        stream_widget = ConfigAndRenderWidget(renderer, ctrl_panel, parent=dock)
        dock.setWidget(stream_widget)

        # Store a map from the renderer friendly name (for popup list) to the dock name
        self._open_renderers.append(rend_key)

        dock.destroyed.connect(functools.partial(
            self.onDockDestroyed, skey=sources[0].identifier, rkey=rend_key))
        dock.visibilityChanged.connect(functools.partial(self.onDockVisChanged, rkey=rend_key))

        # Attach the dock to the mainwindow
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)

        # Restore Dock geometry
        # self.restoreDockWidget(dock)  # Doesn't seem to do anything. Use custom settings instead.
        settings = QtCore.QSettings(str(self._settings_path), QtCore.QSettings.IniFormat)
        settings.beginGroup("RendererDocksMain")
        settings.beginGroup(rend_key)
        saved_size = settings.value("size")
        is_floating = settings.value("floating", 'false') == 'true'
        
        if is_floating:
            dock.setFloating(True)
            if saved_size is not None:
                dock.resize(saved_size)
                dock.move(settings.value("pos"))
        elif saved_size is None:
            # New dock with no saved geometry: apply default size
            # Use QTimer to defer resize until dock is properly laid out
            default_size = self._get_default_dock_size(QtCore.Qt.RightDockWidgetArea)
            QtCore.QTimer.singleShot(0, lambda: self.resizeDocks([dock], [default_size.width()], QtCore.Qt.Horizontal))
        
        settings.endGroup()
        settings.endGroup()

    def _get_default_dock_size(self, dock_area: QtCore.Qt.DockWidgetArea) -> QtCore.QSize:
        """
        Calculate a reasonable default size for a new dock widget based on the main window size
        and existing docks in the same area.

        Args:
            dock_area: The dock widget area where the dock will be placed

        Returns:
            A QSize with reasonable default dimensions
        """
        main_size = self.size()
        main_width = main_size.width()
        main_height = main_size.height()

        # Check for existing docks in the same area
        status_dock = self.findChild(QtWidgets.QDockWidget, name="StatusPanel")
        existing_docks = [
            dw for dw in self.findChildren(QtWidgets.QDockWidget)
            if dw is not status_dock and self.dockWidgetArea(dw) == dock_area and not dw.isFloating()
        ]

        if existing_docks:
            # Use average width of existing docks in the same area
            avg_width = sum(dw.size().width() for dw in existing_docks) // len(existing_docks)
            # Use the height of the main window (docks typically span full height)
            default_width = max(avg_width, 400)  # Ensure minimum 400px width
            default_height = main_height
        else:
            # No existing docks: use a percentage of main window size
            # For right dock area (horizontal docks), use 30-40% of width
            if dock_area == QtCore.Qt.RightDockWidgetArea:
                default_width = max(int(main_width * 0.35), 500)  # 35% of width, minimum 500px
                default_height = main_height  # Full height
            else:
                # For other areas, use similar logic but adjust as needed
                default_width = max(int(main_width * 0.35), 500)
                default_height = main_height

        return QtCore.QSize(default_width, default_height)

    def update(self):
        pass

    @QtCore.Slot()
    def _on_save_visualization_layout(self):
        # Prompt for file
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Visualization Layout", str(self._settings_path.with_suffix('.vis_config')),
            "Visualization Layout (*.vis_config)"
        )
        if not path:
            return
        self.save_visualization_layout(path)

    @QtCore.Slot()
    def _on_load_visualization_layout(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Visualization Layout", str(self._settings_path.with_suffix('.vis_config')),
            "Visualization Layout (*.vis_config)"
        )
        if not path:
            return
        self.load_visualization_layout(path)

    def save_visualization_layout(self, filepath: str):
        settings = QtCore.QSettings(filepath, QtCore.QSettings.IniFormat)

        # Clear previous content for a clean snapshot
        reserved_groups = set(["RendererDocksMain"])  # In vis_config we only store renderer stuff
        for grp in settings.childGroups():
            if grp not in reserved_groups and grp not in set(self._open_renderers):
                settings.remove(grp)
        settings.remove("RendererDocksMain")

        # Save only renderer docks geometry
        status_dock = self.findChild(QtWidgets.QDockWidget, name="StatusPanel")
        settings.beginGroup("RendererDocksMain")
        dws = [_ for _ in self.findChildren(QtWidgets.QDockWidget) if _ is not status_dock]
        for rend_dw in dws:
            settings.beginGroup(rend_dw.objectName())  # Same as rend_key
            settings.setValue("dockWidgetArea", self.dockWidgetArea(rend_dw))
            settings.setValue("size", rend_dw.size())
            settings.setValue("pos", rend_dw.pos())
            settings.setValue("floating", rend_dw.isFloating())
            settings.endGroup()
        settings.endGroup()

        # Save each renderer's settings into top-level groups keyed by rend_key
        for rend_key in self._open_renderers:
            dw = self.findChild(QtWidgets.QDockWidget, rend_key)
            if dw is None:
                continue
            stream_widget = dw.widget()  # instance of ConfigAndRenderWidget
            renderer = stream_widget.renderer
            settings = renderer.save_settings(settings=settings)

        settings.sync()

    def load_visualization_layout(self, filepath: str):
        # Replace current layout: close existing renderer docks (keep StatusPanel)
        status_dock = self.findChild(QtWidgets.QDockWidget, name="StatusPanel")
        for rend_dw in [_ for _ in self.findChildren(QtWidgets.QDockWidget) if _ is not status_dock]:
            rend_dw.close()
        self._open_renderers = []

        settings = QtCore.QSettings(filepath, QtCore.QSettings.IniFormat)

        # Read renderer keys from geometry group
        settings.beginGroup("RendererDocksMain")
        dock_groups = settings.childGroups()
        settings.endGroup()

        for dock_name in dock_groups:
            # Build data sources and renderer kwargs from the renderer group
            settings.beginGroup(dock_name)
            settings.beginGroup("data_sources")
            data_sources = []
            for ds_id in settings.childGroups():
                settings.beginGroup(ds_id)
                src_cls = getattr(stream_viewer.data, settings.value("class"))
                src_key = settings.value("identifier")
                if issubclass(src_cls, LSLDataSource):
                    data_sources.append(src_cls(json.loads(src_key)))
                settings.endGroup()
            settings.endGroup()
            rend_name = dock_name.split("|")[0]
            rend_cls = load_renderer(rend_name, extra_search_dirs=self._plugin_dirs['renderers'])
            rend_kwargs = get_kwargs_from_settings(settings, rend_cls)
            settings.endGroup()

            # Create the dock/renderer with forced key
            self.on_stream_activated(data_sources, renderer_name=rend_name, renderer_kwargs=rend_kwargs, forced_rend_key=dock_name)

            # Apply geometry from the vis_config file
            settings.beginGroup("RendererDocksMain")
            settings.beginGroup(dock_name)
            dock = self.findChild(QtWidgets.QDockWidget, dock_name)
            if dock is not None and settings.value("floating", 'false') == 'true':
                dock.setFloating(True)
                dock.resize(settings.value("size"))
                dock.move(settings.value("pos"))
            settings.endGroup()
            settings.endGroup()

    @QtCore.Slot(bool)
    def onDockVisChanged(self, visible, rkey: str=''):
        # Using this as a bit of a hack to stop LineVis, otherwise it continues to run after the dock has closed.
        found = self.findChild(QtWidgets.QDockWidget, rkey)
        if found is not None:
            if not visible:
                found.widget().renderer.freeze()
            else:
                found.widget().renderer.unfreeze()

    @QtCore.Slot(QtWidgets.QDockWidget)
    def onDockDestroyed(self, obj: QtWidgets.QDockWidget, skey: str='', rkey: str=''):
        if rkey in self._open_renderers:
            self._open_renderers = [_ for _ in self._open_renderers if _ != rkey]


def main():
    parser = argparse.ArgumentParser(prog="lsl_viewer",
                                     description="Interactive application for visualizing LSL streams.")
    parser.add_argument('-s', '--settings_path', nargs='?', help="Path to config file.")
    args = parser.parse_args()

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("LabStreamingLayer")
    app.setOrganizationDomain("labstreaminglayer.org")
    app.setApplicationName("LSLViewer")

    # Attempt to set a Windows .ico application icon (no-op if unavailable)
    try:
        repo_root = Path(__file__).resolve().parents[2]
        ico_path = repo_root / "icons" / "stream_viewer icon_no_bg2.ico"
        if ico_path.exists():
            app.setWindowIcon(QIcon(str(ico_path)))
    except Exception:
        pass

    window = LSLViewer(**args.__dict__)
    # Ensure the main window adopts the application icon
    try:
        window.setWindowIcon(app.windowIcon())
    except Exception:
        pass
    window.show()

    if False:
        timer = QtCore.QTimer(app)
        timer.timeout.connect(window.update)
        timer.start(0)

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
