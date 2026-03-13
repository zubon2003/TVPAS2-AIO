import cv2
import numpy as np
import time
import threading
import sys
import os
import json
import webbrowser

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QComboBox, QPushButton, QCheckBox, 
                             QTabWidget, QGroupBox, QSpinBox, QDoubleSpinBox, 
                             QPlainTextEdit, QFileDialog, QMessageBox, QRadioButton, QButtonGroup)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QSize
from PyQt6.QtGui import QImage, QPixmap, QFont, QTextCursor, QColor

from backend.config_manager import ConfigManager
from backend.web_server import WebServer
from backend.relay_manager import RelayManager
from backend.timer_manager import TimerManager
from backend.sheets_manager import SheetsManager
from backend.atem_manager import AtemManager
from backend.voice_manager import VoiceManager
from backend import data_processor

if sys.platform == 'win32':
    try:
        from pygrabber.dshow_graph import FilterGraph
        HAS_DSHOW = True
    except ImportError:
        HAS_DSHOW = False
else:
    HAS_DSHOW = False

class LogSignaler(QObject):
    log_signal = pyqtSignal(str, str)

class VideoWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Timer Preview")
        self.resize(1280, 720)
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget); self.layout.setContentsMargins(0, 0, 0, 0)
        self.video_label = QLabel("Waiting for video..."); self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: black; color: white;"); self.layout.addWidget(self.video_label)
        self.is_fullscreen = False
        self.target_ratio = 16/9

    def update_frame(self, frame, aspect_str):
        if frame is None: return
        h, w, ch = frame.shape; bytes_per_line = ch * w
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qt_image = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        tw, th = self.video_label.width(), self.video_label.height()
        if tw > 10 and th > 10:
            pixmap = QPixmap.fromImage(qt_image).scaled(QSize(tw, th), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.video_label.setPixmap(pixmap)

    def resizeEvent(self, event):
        new_width = event.size().width()
        new_height = int(new_width / self.target_ratio)
        if abs(self.height() - new_height) > 2:
            self.setFixedHeight(new_height)
        super().resizeEvent(event)

    def closeEvent(self, event): self.hide(); event.ignore()
    def mouseDoubleClickEvent(self, event): self.toggle_fullscreen()
    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        if self.is_fullscreen: self.showFullScreen()
        else: self.showNormal(); self.setFixedHeight(int(self.width() / self.target_ratio))

class IntegratedLapTimerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TVPAS2-AIO Lap Timer")
        self.resize(500, 800)

        self.config = ConfigManager()
        self.web = WebServer(self.config)
        self.relay = RelayManager(self.config, on_lap_callback=self.web.emit_lap, on_heartbeat_callback=self.on_relay_heartbeat)
        self.web.relay_manager = self.relay
        self.timer_mgr = TimerManager(self.config, on_lap_callback=self.relay.on_lap_detected, on_heartbeat_callback=self.on_timer_heartbeat)
        self.relay.set_timer_manager(self.timer_mgr)
        self.atem = AtemManager(self.config, result_manager=self.web.result_manager)
        self.atem.log_callback = self.log_message
        self.web.atem_manager = self.atem
        self.voice = VoiceManager(self.config)
        self.voice.log_callback = self.log_message
        self.web.voice_manager = self.voice
        self.sheets = SheetsManager(self.config)
        self.processor = data_processor.DataProcessor(self.config, self.web, self.sheets)
        
        # 明示的に WebServer に processor を渡す
        self.web.processor = self.processor
        
        self.log_signaler = LogSignaler()
        self.log_signaler.log_signal.connect(self._append_log)
        self.web.log_callback = self.log_message; self.relay.log_callback = self.log_message; self.timer_mgr.log_callback = self.log_message; self.sheets.log_callback = self.log_message
        self.last_gui_update_time = time.monotonic(); self.last_cycle_count = 0
        
        self.init_ui()
        self.web.start()
        self.processor.start()
        
        if not self.timer_mgr.running: self.timer_mgr.start()
        QTimer.singleShot(1000, lambda: self.on_vcam_toggled(self.chk_vcam.isChecked()))
        self.ui_timer = QTimer(); self.ui_timer.timeout.connect(self.update_gui_loop); self.ui_timer.start(30)

    def log_message(self, cat, msg): self.log_signaler.log_signal.emit(cat, msg)
    def _append_log(self, cat, msg):
        ui_ready = hasattr(self, 'log_area') and hasattr(self, 'log_filters')
        if ui_ready and cat in self.log_filters:
            if not self.log_filters[cat].isChecked(): return
        ts = time.strftime("%H:%M:%S")
        if not ui_ready: return
        colors = {"System": "#ffffff", "RX": "#5bc0de", "TX": "#f0ad4e", "Timer": "#00ff00", "Relay": "#ff00ff", "ATEM": "#df691a", "Voice": "#ffcc00", "Heartbeat": "#666666"}
        color = colors.get(cat, "#ffffff")
        self.log_area.appendHtml(f'<span style="color:gray">[{ts}]</span> <span style="color:{color}">[{cat}]</span> {msg}')
        self.log_area.moveCursor(QTextCursor.MoveOperation.End)

    def on_relay_heartbeat(self, status): pass
    def on_timer_heartbeat(self, status): pass

    def init_ui(self):
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget); self.main_layout = QVBoxLayout(self.central_widget)
        header = QHBoxLayout(); title = QLabel("TVPAS2-AIO"); title.setFont(QFont("Orbitron", 18, QFont.Weight.Bold)); title.setStyleSheet("color: #df691a;"); header.addWidget(title); header.addStretch()
        self.fps_label = QLabel("FPS: 0.0"); self.fps_label.setFont(QFont("Consolas", 12, QFont.Weight.Bold)); self.fps_label.setStyleSheet("color: #00ff00;"); header.addWidget(self.fps_label)
        self.btn_show_cam = QPushButton("Show Camera"); self.btn_show_cam.setFixedWidth(120); self.btn_show_cam.clicked.connect(self.toggle_camera_window); header.addWidget(self.btn_show_cam); self.main_layout.addLayout(header)
        self.tabs = QTabWidget(); self.main_layout.addWidget(self.tabs)
        self.create_timer_tab(); self.create_relay_tab(); self.create_atem_tab(); self.create_result_tab(); self.create_voice_tab(); self.create_sheets_tab()
        log_group = QGroupBox(" System Logs "); log_layout = QVBoxLayout(log_group)
        filter_layout = QHBoxLayout(); self.log_filters = {}
        for cat in ["System", "RX", "TX", "Timer", "Relay", "Voice", "Heartbeat"]:
            cb = QCheckBox(cat); cb.setChecked(cat != "Heartbeat"); self.log_filters[cat] = cb; filter_layout.addWidget(cb)
        filter_layout.addStretch(); log_layout.addLayout(filter_layout)
        self.log_area = QPlainTextEdit(); self.log_area.setReadOnly(True); self.log_area.setFont(QFont("Consolas", 9)); self.log_area.setStyleSheet("background-color: #1e1e1e; color: #ebebeb;"); log_layout.addWidget(self.log_area); self.main_layout.addWidget(log_group, 1)
        self.video_win = VideoWindow()
        self.start_go_backend()

    def start_go_backend(self):
        import subprocess
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            exe_path = os.path.join(base_dir, "stag-test", "drone-dashboard.exe")
            if not os.path.exists(exe_path): exe_path = os.path.join(base_dir, "drone-dashboard.exe")
            if os.path.exists(exe_path):
                pb_port = self.config.get("global", "drone_dashboard_port") or 8089
                env = os.environ.copy(); env["SUPERUSER_EMAIL"] = "admin@example.com"; env["SUPERUSER_PASSWORD"] = "admin123456"
                subprocess.Popen([exe_path, "--ingest-enabled=true", f"--port={pb_port}"], cwd=os.path.dirname(exe_path), env=env, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        except: pass

    def update_mode_states(self):
        res, aspect = self.combo_res.currentText(), self.combo_aspect.currentText()
        is_supported = (aspect == "4:3") and (res in ["1280x720", "1920x1080"])
        self.combo_detect.setEnabled(is_supported)
        for btn in self.view_grp.buttons(): btn.setEnabled(is_supported)
        if not is_supported:
            self.combo_detect.setCurrentText("Original")
            for btn in self.view_grp.buttons():
                if btn.text() == "Original": btn.setChecked(True); break

    def create_timer_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        cam_row = QHBoxLayout(); cam_row.addWidget(QLabel("Target Camera:")); self.combo_cam = QComboBox(); self.combo_cam.addItems(self.timer_mgr.get_input_devices())
        target_cam = self.config.get("timer", "target_camera_name"); self.combo_cam.blockSignals(True); self.combo_cam.setCurrentText(target_cam); self.combo_cam.blockSignals(False)
        self.combo_cam.currentTextChanged.connect(lambda t: (self.config.set("timer", "target_camera_name", t), self.update_camera_formats(), self.timer_mgr.restart()))
        cam_row.addWidget(self.combo_cam, 1); layout.addLayout(cam_row)
        fmt_row = QHBoxLayout(); fmt_row.addWidget(QLabel("Format:")); self.combo_res = QComboBox(); fmt_row.addWidget(self.combo_res); fmt_row.addWidget(QLabel("FPS:")); self.combo_fps = QComboBox(); fmt_row.addWidget(self.combo_fps); fmt_row.addWidget(QLabel("Aspect:")); self.combo_aspect = QComboBox(); self.combo_aspect.addItems(["16:9", "4:3"]); fmt_row.addWidget(self.combo_aspect); layout.addLayout(fmt_row)
        self.combo_aspect.blockSignals(True); self.combo_aspect.setCurrentText(self.config.get("timer", "aspect_ratio") or "16:9"); self.combo_aspect.blockSignals(False)
        self.combo_res.currentTextChanged.connect(lambda t: (self.config.set("timer", "resolution", t), self.update_camera_formats(), self.timer_mgr.restart(), self.update_mode_states()) if t else None)
        self.combo_fps.currentTextChanged.connect(lambda t: (self.config.set("timer", "target_fps", int(t)), self.timer_mgr.restart()) if t else None)
        self.combo_aspect.currentTextChanged.connect(lambda t: (self.config.set("timer", "aspect_ratio", t), self.timer_mgr.restart(), self.update_mode_states()))
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Detect Mode:")); self.combo_detect = QComboBox(); self.combo_detect.addItems(["Original", "Corrected", "Hybrid"]); row2.addWidget(self.combo_detect); row2.addWidget(QLabel("Threads:")); self.combo_threads = QComboBox(); self.combo_threads.addItems(["2", "4"]); row2.addWidget(self.combo_threads); layout.addLayout(row2)
        self.combo_detect.setCurrentText(self.config.get("timer", "detect_mode") or "Hybrid"); self.combo_threads.setCurrentText(str(self.config.get("timer", "thread_count") or "2"))
        self.combo_detect.currentTextChanged.connect(lambda t: self.config.set("timer", "detect_mode", t))
        self.combo_threads.currentTextChanged.connect(lambda t: self.config.set("timer", "thread_count", int(t)))
        m_row = QHBoxLayout(); m_row.addWidget(QLabel("Marker System:")); self.combo_m_sys = QComboBox(); self.combo_m_sys.addItems(["Stag", "ArUco", "Hybrid"]); m_row.addWidget(self.combo_m_sys); m_row.addWidget(QLabel("Stag Lib:")); self.combo_s_lib = QComboBox(); self.combo_s_lib.addItems(["11", "13", "15", "17", "19", "21"]); m_row.addWidget(self.combo_s_lib); layout.addLayout(m_row)
        self.combo_m_sys.setCurrentText(self.config.get("timer", "marker_system") or "Stag"); self.combo_s_lib.setCurrentText(str(self.config.get("timer", "stag_library") or "21"))
        self.combo_m_sys.currentTextChanged.connect(lambda t: (self.config.set("timer", "marker_system", t), self.timer_mgr.restart())); self.combo_s_lib.currentTextChanged.connect(lambda t: (self.config.set("timer", "stag_library", int(t)), self.timer_mgr.restart()))
        out_g = QGroupBox(" Output & Display "); out_l = QVBoxLayout(out_g); self.chk_vcam = QCheckBox("Enable Virtual Camera"); self.chk_vcam.setChecked(self.config.get("timer", "virtual_camera_enabled") or False); self.chk_vcam.toggled.connect(self.on_vcam_toggled); out_l.addWidget(self.chk_vcam)
        vrow = QHBoxLayout(); vrow.addWidget(QLabel("View Mode:")); self.view_grp = QButtonGroup(self)
        for m in ["Original", "Corrected", "Source"]:
            rb = QRadioButton(m); self.view_grp.addButton(rb); vrow.addWidget(rb)
            if m == (self.config.get("timer", "view_mode") or "Original"): rb.setChecked(True)
            rb.toggled.connect(lambda checked, mode=m: self.config.set("timer", "view_mode", mode) if checked else None)
        out_l.addLayout(vrow); ov_r = QHBoxLayout(); ov_r.addWidget(QLabel("Overlays:"))
        self.chk_det = QCheckBox("Det Info"); self.chk_det.setChecked(self.config.get("timer", "show_detection_info") is not False); self.chk_det.toggled.connect(lambda v: self.config.set("timer", "show_detection_info", v))
        self.chk_fps = QCheckBox("FPS"); self.chk_fps.setChecked(self.config.get("timer", "show_fps") is not False); self.chk_fps.toggled.connect(lambda v: self.config.set("timer", "show_fps", v))
        self.chk_mkr = QCheckBox("Marker"); self.chk_mkr.setChecked(self.config.get("timer", "show_marker_rectangle") is not False); self.chk_mkr.toggled.connect(lambda v: self.config.set("timer", "show_marker_rectangle", v))
        for c in [self.chk_det, self.chk_fps, self.chk_mkr]: ov_r.addWidget(c)
        out_l.addLayout(ov_r); layout.addWidget(out_g)
        r4 = QHBoxLayout(); r4.addWidget(QLabel("Maker Num Threshold:")); self.spin_thr = QSpinBox(); self.spin_thr.setRange(1, 10); self.spin_thr.setValue(self.config.get("timer", "marker_threshold") or 2); r4.addWidget(self.spin_thr)
        r4.addWidget(QLabel("Size(%):")); self.spin_size = QDoubleSpinBox(); self.spin_size.setRange(0.0, 10.0); self.spin_size.setSingleStep(0.01); self.spin_size.setValue(self.config.get("timer", "min_marker_percent") or 0.1); r4.addWidget(self.spin_size)
        r4.addWidget(QLabel("Flicker(ms):")); self.spin_flk = QSpinBox(); self.spin_flk.setRange(0, 2000); self.spin_flk.setValue(self.config.get("timer", "flicker_length") or 150); r4.addWidget(self.spin_flk)
        r4.addWidget(QLabel("Hybrid Dist:")); self.spin_h_dist = QSpinBox(); self.spin_h_dist.setRange(1, 100); self.spin_h_dist.setValue(self.config.get("timer", "hybrid_dist_threshold") or 20); self.spin_h_dist.valueChanged.connect(lambda v: self.config.set("timer", "hybrid_dist_threshold", v)); r4.addWidget(self.spin_h_dist)
        r4.addWidget(QLabel("ArUco Error Corr:")); self.spin_ecr = QDoubleSpinBox(); self.spin_ecr.setRange(0.0, 1.0); self.spin_ecr.setSingleStep(0.05); self.spin_ecr.setValue(self.config.get("timer", "error_correction_rate") or 0.6); self.spin_ecr.valueChanged.connect(lambda v: self.config.set("timer", "error_correction_rate", v)); r4.addWidget(self.spin_ecr)
        layout.addLayout(r4); layout.addStretch(); self.tabs.addTab(tab, "TIMER"); self.update_camera_formats()

    def create_relay_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        cur_f = self.config.get("timer", "camera_frequencies") or [5705, 5740, 5800, 5820]
        f_g = QGroupBox(" Camera Frequencies (MHz) "); f_l = QHBoxLayout(f_g); self.freq_edits = []
        for i in range(4):
            vbox = QVBoxLayout(); vbox.addWidget(QLabel(f"Camera {i}", alignment=Qt.AlignmentFlag.AlignCenter))
            edit = QLineEdit(str(cur_f[i])); edit.setFixedWidth(60); edit.editingFinished.connect(self.save_relay_config); self.freq_edits.append(edit); vbox.addWidget(edit); f_l.addLayout(vbox)
        layout.addWidget(f_g); s_g = QGroupBox(" Server Assignments "); s_l = QVBoxLayout(s_g); self.srv_edits = []
        for i in range(1, 5):
            k = f"server{i}"; row = QHBoxLayout(); row.addWidget(QLabel(f"Server {i}:"))
            p_e = QLineEdit(str(self.config.get("relay", f"{k}_port"))); p_e.setFixedWidth(60); p_e.editingFinished.connect(self.save_relay_config); row.addWidget(p_e)
            id_e = QLineEdit(",".join(map(str, self.config.get("relay", f"{k}_id") or []))); id_e.editingFinished.connect(self.save_relay_config); row.addWidget(id_e); self.srv_edits.append((p_e, id_e)); s_l.addLayout(row)
        layout.addWidget(s_g); l_g = QGroupBox(" LED Comport "); l_l = QHBoxLayout(l_g)
        self.chk_com = QCheckBox("Use Comport"); self.chk_com.setChecked(self.config.get("relay", "useComPort") or False); self.chk_com.toggled.connect(lambda v: self.config.set("relay", "useComPort", v)); l_l.addWidget(self.chk_com)
        self.edit_com = QLineEdit(self.config.get("relay", "comPort") or "COM1"); self.edit_com.editingFinished.connect(lambda: self.config.set("relay", "comPort", self.edit_com.text())); l_l.addWidget(self.edit_com)
        layout.addWidget(l_g); c_g = QGroupBox(" Timing Compensation "); c_l = QHBoxLayout(c_g); c_l.addWidget(QLabel("ESPNow Compensation (ms):"))
        self.spin_comp = QSpinBox(); self.spin_comp.setRange(0, 1000); self.spin_comp.setFixedWidth(80); self.spin_comp.setValue(self.config.get("relay", "espnow_compensation") or 200); self.spin_comp.valueChanged.connect(lambda v: self.config.set("relay", "espnow_compensation", v))
        c_l.addWidget(self.spin_comp); c_l.addStretch(); layout.addWidget(c_g); layout.addStretch(); self.tabs.addTab(tab, "RELAY")

    def create_atem_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab); g = QGroupBox(" ATEM Switcher Settings "); l = QVBoxLayout(g)
        self.chk_atem = QCheckBox("Enable ATEM Control"); self.chk_atem.setChecked(self.config.get("atem", "enabled") or False); l.addWidget(self.chk_atem)
        ip_row = QHBoxLayout(); ip_row.addWidget(QLabel("ATEM IP Address:")); self.edit_atem_ip = QLineEdit(self.config.get("atem", "ip") or "192.168.1.25"); self.edit_atem_ip.editingFinished.connect(lambda: self.config.set("atem", "ip", self.edit_atem_ip.text())); ip_row.addWidget(self.edit_atem_ip); l.addLayout(ip_row)
        mode_row = QHBoxLayout(); mode_row.addWidget(QLabel("Switching Mode:")); self.combo_atem_mode = QComboBox(); self.combo_atem_mode.addItems(["Manual", "Auto", "Pos1", "Pos2", "Pos3", "Pos4", "Seat1", "Seat2", "Seat3", "Seat4"]); self.combo_atem_mode.setCurrentText(self.config.get("atem", "mode") or "Auto"); self.combo_atem_mode.currentTextChanged.connect(lambda t: self.config.set("atem", "mode", t)); mode_row.addWidget(self.combo_atem_mode); l.addLayout(mode_row)
        def_row = QHBoxLayout(); def_row.addWidget(QLabel("Default Camera Input:")); self.spin_atem_def = QSpinBox(); self.spin_atem_def.setRange(1, 8); self.spin_atem_def.setValue(self.config.get("atem", "default_camera") or 4); self.spin_atem_def.valueChanged.connect(lambda v: self.config.set("atem", "default_camera", v)); def_row.addWidget(self.spin_atem_def); l.addLayout(def_row)
        self.chk_atem.toggled.connect(lambda v: (self.config.set("atem", "enabled", v), self.atem.start() if v else self.atem.stop())); layout.addWidget(g)
        btn_open_atem = QPushButton("Open ATEM Web UI"); btn_open_atem.setFixedHeight(40); btn_open_atem.clicked.connect(lambda: self.open_url("atem")); layout.addWidget(btn_open_atem); layout.addStretch(); self.tabs.addTab(tab, "ATEM")
        if self.chk_atem.isChecked(): self.atem.start()

    def create_result_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        port_row = QHBoxLayout(); port_row.addWidget(QLabel("Drone Dashboard Port:")); self.spin_pb_port = QSpinBox(); self.spin_pb_port.setRange(1024, 65535); self.spin_pb_port.setValue(self.config.get("global", "drone_dashboard_port") or 8089); self.spin_pb_port.valueChanged.connect(lambda v: self.config.set("global", "drone_dashboard_port", v)); port_row.addWidget(self.spin_pb_port); port_row.addStretch(); layout.addLayout(port_row)
        path_row = QHBoxLayout(); path_row.addWidget(QLabel("FPVTrackside Path:")); self.edit_path = QLineEdit(self.config.get("result_formatter", "fpvtrackside_dir_path") or ""); path_row.addWidget(self.edit_path); btn_browse = QPushButton("..."); btn_browse.setFixedWidth(40); btn_browse.clicked.connect(self.browse_path); path_row.addWidget(btn_browse); layout.addLayout(path_row)
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Round:")); self.combo_round = QComboBox(); self.combo_round.addItem("All", "all"); self.combo_round.currentTextChanged.connect(self.on_round_changed); row2.addWidget(self.combo_round, 1); layout.addLayout(row2)
        row3 = QHBoxLayout(); row3.addWidget(QLabel("Sort By:")); self.combo_sort = QComboBox(); sort_options = [("Best Lap", "bestLap"), ("Consecutive (N)", "consecutive"), ("First Lap (N)", "firstLap"), ("Total Race (HS+N)", "totalRace"), ("Total Laps", "totalLaps")]
        for label, val in sort_options: self.combo_sort.addItem(label, val)
        idx = self.combo_sort.findData(self.config.get("result_formatter", "sorted_by") or "consecutive"); self.combo_sort.setCurrentIndex(idx if idx >= 0 else 1); self.combo_sort.currentTextChanged.connect(lambda t: self.config.set("result_formatter", "sorted_by", self.combo_sort.currentData())); row3.addWidget(self.combo_sort, 1)
        row3.addWidget(QLabel("N:")); self.spin_consec = QSpinBox(); self.spin_consec.setRange(2, 10); self.spin_consec.setValue(self.config.get("result_formatter", "consecutive_n") or 3); self.spin_consec.valueChanged.connect(lambda v: self.config.set("result_formatter", "consecutive_n", v)); row3.addWidget(self.spin_consec); layout.addLayout(row3)
        ov_g = QGroupBox(" Overlay Links "); ov_l = QVBoxLayout(ov_g); chk_r = QHBoxLayout(); self.ov_chks = {}; k, l = ("Countdown Voice", "countdown_voice"); cb = QCheckBox(k); cb.setChecked(self.config.get("result_formatter", l) or False); cb.toggled.connect(lambda v, key=l: self.config.set("result_formatter", key, v)); self.ov_chks[l] = cb; chk_r.addWidget(cb); ov_l.addLayout(chk_r); btn_r = QHBoxLayout()
        for l, p in [("Leaderboard", "leaderboard"), ("Result", "heatresult"), ("Next", "nextheat"), ("Start", "overlay"), ("Race Feed", "race_feed")]:
            b = QPushButton(l); b.clicked.connect(lambda chk, path=p: self.open_url(path)); btn_r.addWidget(b)
        ov_l.addLayout(btn_r); layout.addWidget(ov_g); layout.addStretch(); self.tabs.addTab(tab, "RESULT"); QTimer.singleShot(2000, self.update_result_ui_choices)

    def on_round_changed(self, text): self.config.set("result_formatter", "leaderboard_round", self.combo_round.currentData())
    def update_result_ui_choices(self):
        try:
            rounds = self.web.result_manager.get_round_list(); self.combo_round.blockSignals(True); self.combo_round.clear(); curr = self.config.get("result_formatter", "leaderboard_round") or "all"
            for r in rounds: self.combo_round.addItem(r["name"], r["id"]); 
            if r["id"] == curr: self.combo_round.setCurrentIndex(self.combo_round.count()-1)
            self.combo_round.blockSignals(False)
        except: pass

    def create_voice_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab); g = QGroupBox(" TTS Settings "); l = QVBoxLayout(g)
        self.chk_voice = QCheckBox("Enable Voice Announcement"); self.chk_voice.setChecked(self.config.get("voice", "enabled") or False); self.chk_voice.toggled.connect(lambda v: self.config.set("voice", "enabled", v)); l.addWidget(self.chk_voice)
        eng_row = QHBoxLayout(); eng_row.addWidget(QLabel("Voice Engine:")); self.combo_engine = QComboBox(); self.combo_engine.addItems(["pyttsx3", "voicevox"]); self.combo_engine.setCurrentText(self.config.get("voice", "engine") or "pyttsx3"); self.combo_engine.currentTextChanged.connect(lambda t: self.config.set("voice", "engine", t)); eng_row.addWidget(self.combo_engine); l.addLayout(eng_row)
        spd_row = QHBoxLayout(); spd_row.addWidget(QLabel("Speed:")); self.spin_v_spd = QDoubleSpinBox(); self.spin_v_spd.setRange(0.5, 2.0); self.spin_v_spd.setValue(self.config.get("voice", "speed") or 1.0); self.spin_v_spd.valueChanged.connect(lambda v: self.config.set("voice", "speed", v)); spd_row.addWidget(self.spin_v_spd); spd_row.addWidget(QLabel("Volume:")); self.spin_v_vol = QDoubleSpinBox(); self.spin_v_vol.setRange(0.0, 1.0); self.spin_v_vol.setValue(self.config.get("voice", "volume") or 1.0); self.spin_v_vol.valueChanged.connect(lambda v: self.config.set("voice", "volume", v)); spd_row.addWidget(self.spin_v_vol); l.addLayout(spd_row)
        vv_g = QGroupBox(" VOICEVOX Options "); vv_l = QVBoxLayout(vv_g); url_row = QHBoxLayout(); url_row.addWidget(QLabel("URL:")); self.edit_vv_url = QLineEdit(self.config.get("voicevox", "url") or "http://localhost:50021"); self.edit_vv_url.editingFinished.connect(lambda: self.config.set("voicevox", "url", self.edit_vv_url.text())); url_row.addWidget(self.edit_vv_url); vv_l.addLayout(url_row)
        spk_row = QHBoxLayout(); spk_row.addWidget(QLabel("Speaker ID:")); self.spin_vv_spk = QSpinBox(); self.spin_vv_spk.setRange(0, 100); self.spin_vv_spk.setValue(self.config.get("voicevox", "speaker") or 1); self.spin_vv_spk.valueChanged.connect(lambda v: self.config.set("voicevox", "speaker", v)); spk_row.addWidget(self.spin_vv_spk); vv_l.addLayout(spk_row); l.addWidget(vv_g)
        btn_test = QPushButton("Test Voice"); btn_test.clicked.connect(lambda: self.voice.speak("テストのアナウンスです。")); l.addWidget(btn_test); layout.addWidget(g); layout.addStretch(); self.tabs.addTab(tab, "VOICE")

    def create_sheets_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab); g = QGroupBox(" Google Spreadsheet Integration "); l = QVBoxLayout(g)
        self.chk_sheet = QCheckBox("Enable Spreadsheet Writing"); self.chk_sheet.setChecked(self.config.get("result_formatter", "enable_spreadsheet_writing") or False); l.addWidget(self.chk_sheet)
        id_row = QHBoxLayout(); id_row.addWidget(QLabel("Spreadsheet ID:")); self.edit_sheet_id = QLineEdit(self.config.get("result_formatter", "google_spreadsheet_id") or ""); self.edit_sheet_id.editingFinished.connect(lambda: self.config.set("result_formatter", "google_spreadsheet_id", self.edit_sheet_id.text())); id_row.addWidget(self.edit_sheet_id); l.addLayout(id_row)
        self.btn_sheet_update = QPushButton("Update Sheets Now"); self.btn_sheet_update.setFixedHeight(40); self.btn_sheet_update.setStyleSheet("background-color: #df691a; color: white; font-weight: bold;"); self.btn_sheet_update.clicked.connect(lambda: self.processor._trigger_update(immediate=True)); l.addWidget(self.btn_sheet_update)
        self.btn_sheet_open = QPushButton("Open Spreadsheet"); self.btn_sheet_open.setFixedHeight(40); self.btn_sheet_open.clicked.connect(self.open_spreadsheet); l.addWidget(self.btn_sheet_open)
        self.chk_sheet.toggled.connect(lambda v: (self.config.set("result_formatter", "enable_spreadsheet_writing", v), self.btn_sheet_update.setEnabled(v), self.btn_sheet_open.setEnabled(v))); layout.addWidget(g); layout.addStretch(); self.tabs.addTab(tab, "SHEETS")

    def on_vcam_toggled(self, on):
        self.config.set("timer", "virtual_camera_enabled", on); self.timer_mgr.restart()
        for w in [self.combo_cam, self.combo_res, self.combo_fps, self.combo_aspect, self.combo_detect, self.combo_threads, self.combo_m_sys, self.combo_s_lib]: w.setEnabled(not on)

    def update_camera_formats(self):
        self.combo_res.blockSignals(True); self.combo_fps.blockSignals(True); formats = self.timer_mgr.get_camera_formats(self.combo_cam.currentText()); self.combo_res.clear(); self.combo_res.addItems(formats.keys())
        curr_res = self.config.get("timer", "resolution"); 
        if curr_res in formats: self.combo_res.setCurrentText(curr_res)
        self.combo_fps.clear(); self.combo_fps.addItems([str(f) for f in formats.get(self.combo_res.currentText(), [])])
        curr_fps = str(self.config.get("timer", "target_fps"))
        if curr_fps in [self.combo_fps.itemText(i) for i in range(self.combo_fps.count())]: self.combo_fps.setCurrentText(curr_fps)
        self.combo_res.blockSignals(False); self.combo_fps.blockSignals(False)

    def save_relay_config(self):
        try:
            self.config.set("timer", "camera_frequencies", [int(e.text()) for e in self.freq_edits])
            for i, (pe, ie) in enumerate(self.srv_edits):
                k = f"server{i+1}"; self.config.set("relay", f"{k}_port", int(pe.text())); self.config.set("relay", f"{k}_id", [int(x.strip()) for x in ie.text().split(",") if x.strip().isdigit()])
        except: pass

    def browse_path(self):
        p = QFileDialog.getExistingDirectory(self, "Select FPVTrackside Directory")
        if p: p = os.path.normpath(p).replace("\\", "/"); self.edit_path.setText(p); self.config.set("result_formatter", "fpvtrackside_dir_path", p); self.update_result_ui_choices()

    def open_spreadsheet(self):
        sid = self.config.get("result_formatter", "google_spreadsheet_id")
        if sid: webbrowser.open(f"https://docs.google.com/spreadsheets/d/{sid}")

    def open_url(self, p): webbrowser.open(f"http://localhost:{self.config.get('global', 'web_ui_port') or 5050}/{p}")
    def toggle_camera_window(self):
        if self.video_win.isVisible(): self.video_win.hide(); self.btn_show_cam.setText("Show Camera")
        else: self.video_win.show(); self.btn_show_cam.setText("Hide Camera")

    def closeEvent(self, event): self.video_win.close(); self.timer_mgr.stop(); self.web.running = False; event.accept()
    def update_gui_loop(self):
        now = time.monotonic(); elap = now - self.last_gui_update_time
        if elap >= 1.0:
            cc = self.timer_mgr.cycle_count; self.fps_label.setText(f"FPS: {(cc - self.last_cycle_count)/elap:.1f}"); self.last_cycle_count = cc; self.last_gui_update_time = now
        if self.video_win.isVisible() and self.timer_mgr.display_frame is not None: self.video_win.update_frame(self.timer_mgr.display_frame, self.combo_aspect.currentText())

if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion"); win = IntegratedLapTimerApp(); win.show(); sys.exit(app.exec())
