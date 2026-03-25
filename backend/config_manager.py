import json
import os
import threading


class ConfigManager:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path

        # FPVTracksideのパスを自動検出
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        default_fpv_path = ""
        if local_app_data:
            detected_path = os.path.join(local_app_data, "FPVTrackside").replace("\\", "/")
            if os.path.exists(detected_path):
                default_fpv_path = detected_path

        self.config = {
            "global": {"web_ui_port": 5050, "drone_dashboard_port": 8089},
            "timer": {
                "target_camera_name": "USB Camera",
                "resolution": "1280x720",
                "target_fps": 60,
                "aspect_ratio": "4:3",
                "detect_mode": "Hybrid",
                "view_mode": "Original",
                "marker_system": "ArUco",
                "stag_library": 21,
                "marker_threshold": 2,
                "flicker_length": 150,
                "min_marker_percent": 0.1,
                "error_correction_rate": 0.6,
                "show_detection_info": True,
                "show_marker_rectangle": True,
                "thread_count": 4,
                "virtual_camera_enabled": False,
                "camera_frequencies": [5705, 5740, 5800, 5820]
            },
            "relay": {
                "server1_port": 5000, "server1_id": [0, 1, 2, 3],
                "server2_port": 0, "server2_id": [],
                "server3_port": 0, "server3_id": [],
                "server4_port": 0, "server4_id": [],
                "espnow_compensation": 200,
                "useComPort": False,
                "comPort": "COM1"
            },
            "result_formatter": {
                "fpvtrackside_dir_path": default_fpv_path,
                "enable_spreadsheet_writing": False,
                "google_spreadsheet_id": ""
            },
            "atem": {
                "enabled": False,
                "ip": "127.0.0.1",
                "port": 8888,
                "mode": "Auto",
                "default_camera": 4
            },
            "voice": {
                "enabled": False,
                "engine": "pyttsx3",
                "speed": 1.0,
                "volume": 1.0
            },
            "voicevox": {
                "url": "http://localhost:50021",
                "speaker": 1
            }
        }
        self.lock = threading.Lock()
        self.load()

    def load(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                self._deep_update(self.config, user_config)
            except:
                pass
        else:
            self.save()

    def _deep_update(self, base, update):
        for k, v in update.items():
            if isinstance(v, dict) and k in base:
                self._deep_update(base[k], v)
            else:
                base[k] = v

    def save(self):
        with self.lock:
            try:
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
            except:
                pass

    def get(self, *keys):
        val = self.config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return None
        return val

    def set(self, *args):
        if len(args) < 2:
            return
        keys, value = args[:-1], args[-1]
        with self.lock:
            val = self.config
            for k in keys[:-1]:
                if k not in val or not isinstance(val[k], dict):
                    val[k] = {}
                val = val[k]
            val[keys[-1]] = value
        self.save()
