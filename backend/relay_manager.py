import os
import time
import threading
import pygame

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

class RelayManager:
    def __init__(self, config_manager, on_lap_callback=None, on_heartbeat_callback=None):
        self.config_manager = config_manager
        self.on_lap_callback = on_lap_callback
        self.on_heartbeat_callback = on_heartbeat_callback
        self.timer_manager = None
        self.log_callback = None
        self.ser = None
        
        pygame.mixer.init()
        self.sounds = {
            "ready": pygame.mixer.Sound(os.path.join("audio", "get_ready.wav")),
            "go": pygame.mixer.Sound(os.path.join("audio", "go.wav")),
            "1": pygame.mixer.Sound(os.path.join("audio", "1.wav")),
            "2": pygame.mixer.Sound(os.path.join("audio", "2.wav")),
            "3": pygame.mixer.Sound(os.path.join("audio", "3.wav")),
            "4": pygame.mixer.Sound(os.path.join("audio", "4.wav")),
            "5": pygame.mixer.Sound(os.path.join("audio", "5.wav"))
        }

    def log(self, cat, msg):
        if self.log_callback: self.log_callback(cat, msg)

    def set_timer_manager(self, tm): self.timer_manager = tm

    def on_lap_detected(self, seat, capture_time, marker_id):
        # TimerManager からの検知イベント
        active_ports = []
        for i in range(1, 5):
            # server1_id, server2_id ... に設定されたマーカーIDリストを取得
            ids = self.config_manager.get("relay", f"server{i}_id") or []
            # 検知された marker_id が、このサーバーに割り当てられたIDリストに含まれているか確認
            if marker_id in ids:
                port = self.config_manager.get("relay", f"server{i}_port")
                if port and port > 0: active_ports.append(port)
        
        freqs = self.config_manager.get("timer", "camera_frequencies") or [5705, 5740, 5800, 5820]
        freq = freqs[seat] if seat < len(freqs) else 0
        
        for port in active_ports:
            data = {"seat": seat, "lap_time": capture_time, "frequency": freq, "marker_id": marker_id}
            if self.on_lap_callback: self.on_lap_callback(port, data)
            self.log("TX", f"Lap to Port {port}: Marker {marker_id} in Seat {seat} @ {capture_time:.3f}")

    def on_race_stage(self, start_time):
        self.log("System", f"Race Stage Received. Start at: {start_time}")
        def countdown():
            voice_enabled = self.config_manager.get("result_formatter", "countdown_voice") or False

            now = time.monotonic()
            delay = start_time - now
            if delay > 8:
                time.sleep(delay - 8)
            if voice_enabled:
                self.sounds["ready"].play()

            for i in range(5, 0, -1):
                wait = start_time - i - time.monotonic()
                if wait > 0: time.sleep(wait)
                if voice_enabled: self.sounds[str(i)].play()
                self._write_serial(f"C{i}\n")
                self.log("System", f"Countdown: {i}")
            
            wait_go = start_time - time.monotonic()
            if wait_go > 0: time.sleep(wait_go)
            if voice_enabled: self.sounds["go"].play()
            self._write_serial("GO\n")
            self.log("System", "GO!")
            
        threading.Thread(target=countdown, daemon=True).start()

    def on_race_stop(self):
        self._write_serial("STOP\n")
        self.log("System", "Race Stopped.")

    def _write_serial(self, msg):
        if not self.config_manager.get("relay", "useComPort"): return
        if not self.ser or not self.ser.is_open:
            try:
                port = self.config_manager.get("relay", "comPort")
                self.ser = serial.Serial(port, 115200, timeout=0.1)
            except: return
        try: self.ser.write(msg.encode())
        except: self.ser = None

    def on_frequency_setup(self, data):
        self.log("System", "External frequency setup ignored. Current config preserved.")

    def stop(self):
        if self.ser:
            try:
                self.ser.close()
            except:
                pass
