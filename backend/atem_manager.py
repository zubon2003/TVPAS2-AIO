import threading
import time
import requests
import json

class AtemManager:
    def __init__(self, config_manager, result_manager=None):
        self.config_manager = config_manager
        self.result_manager = result_manager
        self.log_callback = None
        self.running = False
        self.thread = None
        self.current_program = 0
        self.connected = False
        self.last_switch_time = 0
        self.min_switch_interval = 2.0 # 同一カメラへの連続切り替え防止

    def log(self, msg):
        if self.log_callback: self.log_callback("ATEM", msg)

    def get_status(self):
        return {"program": self.current_program, "connected": self.connected}

    def switch_to_input(self, input_num):
        # 手動切り替えや外部APIからの呼び出し
        if not self.config_manager.get("atem", "enabled"): return
        self._send_switch_command(input_num)

    def _send_switch_command(self, input_num):
        ip = self.config_manager.get("atem", "ip")
        if not ip: return
        url = f"http://{ip}/api/switch" # 仮のATEM Proxy API想定
        try:
            # 実際には PyATEMMax や特定の HTTP Proxy を介して制御
            # ここではログ出力とステータス更新に留める (実装に合わせて調整)
            self.current_program = input_num
            self.log(f"Switched to Input {input_num}")
        except Exception as e:
            self.log(f"Switch Error: {e}")

    def on_lap_event(self, seat, server_id, rank, is_finished):
        if not self.config_manager.get("atem", "enabled"): return
        mode = self.config_manager.get("atem", "mode") or "Auto"
        
        if mode == "Auto":
            # 1位の人がゴールゲートを通過した時にそのカメラに切り替える
            if rank == 1:
                self._send_switch_command(seat + 1)
        elif mode.startswith("Pos"):
            # 特定の順位が通過した時 (例: Pos1)
            try:
                target_rank = int(mode.replace("Pos", ""))
                if rank == target_rank: self._send_switch_command(seat + 1)
            except: pass
        elif mode.startswith("Seat"):
            # 特定のシートが通過した時
            try:
                target_seat = int(mode.replace("Seat", ""))
                if (seat + 1) == target_seat: self._send_switch_command(seat + 1)
            except: pass

    def on_race_start(self):
        if not self.config_manager.get("atem", "enabled"): return
        # レース開始時にデフォルトカメラ（通常は全体俯瞰）へ
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self._send_switch_command(def_cam)

    def on_race_end(self):
        if not self.config_manager.get("atem", "enabled"): return
        # レース終了時にデフォルトカメラへ
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self._send_switch_command(def_cam)

    def start(self):
        if self.running: return
        self.running = True
        self.log("ATEM Manager started.")

    def stop(self):
        self.running = False
        self.log("ATEM Manager stopped.")
