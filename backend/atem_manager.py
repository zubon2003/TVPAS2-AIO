import threading
import time
import socket
import struct

class AtemManager:
    def __init__(self, config_manager, result_manager=None):
        self.config_manager = config_manager
        self.result_manager = result_manager
        self.log_callback = None
        self.connected = False
        self.running = False
        self.thread = None
        self._last_program_input = -1
        self.status = {"program": 0, "connected": False}
        
        # ATEM通信用
        self.sock = None
        self.remote_addr = None

    def log(self, category, message):
        if self.log_callback: self.log_callback(category, message)

    def start(self):
        if not self.config_manager.get("atem", "enabled"):
            self.log("ATEM", "ATEM Service is disabled in config.")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            if not self.connected:
                ip = self.config_manager.get("atem", "ip") or "192.168.1.25"
                try:
                    self.remote_addr = (ip, 8000)
                    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.sock.settimeout(2.0)
                    # 簡易的なATEMハンドシェイク
                    self.sock.sendto(b'\x08\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', self.remote_addr)
                    self.connected = True
                    self.status["connected"] = True
                    self.log("ATEM", f"ATEM Service started (Target: {ip})")
                except Exception as e:
                    self.log("ATEM", f"Connection failed: {e}")
                    time.sleep(5.0); continue
            time.sleep(1.0)

    def switch_to_input(self, input_number):
        if not self.config_manager.get("atem", "enabled") or not self.connected: 
            return False
        
        # 最後に送ったコマンドと同じならスキップ
        if input_number == self._last_program_input: 
            return True
        
        try:
            self.log("ATEM", f"Switching ATEM to Input {input_number}")
            
            # ATEM 'CPPr' (Change Program Input) コマンドパケットの構築
            cmd_name = b'CPPr'
            cmd_data = struct.pack('>B B H', 0, 0, input_number) 
            
            # パケット全体 (非常に簡易的なATEMプロトコル形式)
            # 0x0814 = 長さ20byte
            packet = b'\x08\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' + cmd_name + b'\x00\x0c\x00\x00' + cmd_data
            
            if self.sock and self.remote_addr:
                self.sock.sendto(packet, self.remote_addr)
            
            # 接続があるときのみ状態を更新
            self._last_program_input = input_number
            self.status["program"] = input_number
            return True
        except Exception as e:
            self.log("ATEM", f"Switch Error: {e}"); return False

    def get_status(self):
        return self.status

    def on_lap_event(self, seat, server_id, rank, is_finished):
        if not self.config_manager.get("atem", "enabled"): return
        mode = self.config_manager.get("atem", "mode") or "Auto"
        target_input = None
        if mode == "Auto":
            if rank == 1 and not is_finished: target_input = server_id
        elif mode.startswith("Pos"):
            try:
                target_rank = int(mode[3:])
                if rank == target_rank and not is_finished: target_input = server_id
            except: pass
        elif mode.startswith("Seat"):
            try:
                target_seat = int(mode[4:]) - 1
                if seat == target_seat and not is_finished: target_input = server_id
            except: pass
        if target_input: self.switch_to_input(target_input)

    def on_race_start(self):
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self.switch_to_input(def_cam)

    def on_race_end(self):
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self.switch_to_input(def_cam)

    def stop(self): self.running = False
