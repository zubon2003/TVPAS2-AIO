import os
import time
import threading
import pygame
from queue import Queue

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
        self.log_callback = None
        self.queue = Queue()
        self.running = True
        self.timer_manager = None
        self.ser = None
        
        # レース状態管理
        self.is_race_running = False
        self._start_time = 0.0 # レース開始時刻 (monotonic)
        self._stop_time = 0.0  # 終了信号受信時刻 (monotonic)
        
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.audio_dir = os.path.join(base_dir, "audio")
        if not pygame.mixer.get_init(): pygame.mixer.init()
        self.sounds = {}
        self._load_sounds()
        self._init_serial()
        threading.Thread(target=self._process_queue, daemon=True).start()

    def _init_serial(self):
        if self.ser:
            try: self.ser.close()
            except: pass
            self.ser = None

        if not HAS_SERIAL:
            self.log("System", "Serial library (pyserial) not installed.")
            return
        
        if self.config_manager.get("relay", "useComPort"):
            port = self.config_manager.get("relay", "comPort")
            try:
                self.ser = serial.Serial(port, 115200, timeout=1)
                self.log("System", f"Successfully opened COM port: {port}")
            except Exception as e:
                self.log("System", f"Failed to open COM port {port}: {e}")

    def _write_serial(self, data):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(data.encode())
                # self.log("TX", f"Serial SENT: {data}")
            except Exception as e:
                self.log("System", f"Serial Write Error: {e}")

    def set_timer_manager(self, timer_manager): self.timer_manager = timer_manager
    def _load_sounds(self):
        files = {'get_ready': 'get_ready.wav', '5': '5.wav', '4': '4.wav', '3': '3.wav', '2': '2.wav', '1': '1.wav', 'go': 'go.wav'}
        for k, n in files.items():
            p = os.path.join(self.audio_dir, n)
            if os.path.exists(p): self.sounds[k] = pygame.mixer.Sound(p)

    def log(self, category, message):
        if self.log_callback: self.log_callback(category, message)

    def on_lap_detected(self, area_idx, lap_time, marker_id):
        # 常にキューには入れる（判定は送信時に行う）
        self.queue.put({"type": "lap", "area_idx": area_idx, "lap_time": lap_time, "marker_id": marker_id})

    def on_race_stage(self, start_time_mono):
        self.is_race_running = True
        self._start_time = start_time_mono
        self._stop_time = 0.0
        self.log("System", f"Race Mid-Relay: READY (Start at {start_time_mono:.1f})")
        threading.Thread(target=self._countdown_executor, args=(start_time_mono,), daemon=True).start()

    def on_race_stop(self):
        # 終了信号を受信
        self._stop_time = time.monotonic()
        self.log("System", "Race Mid-Relay: STOP SIGNAL RECEIVED")
        self._write_serial('E')

    def _countdown_executor(self, target_time):
        # 音声再生のスケジュール
        voice_schedule = [(6.0, 'get_ready'), (5.0, '5'), (4.0, '4'), (3.0, '3'), (2.0, '2'), (1.0, '1'), (0.0, 'go')]
        # シリアル送信のスケジュール (対応する文字)
        serial_schedule = [(6.0, 'G'), (5.0, 'Z'), (4.0, 'Y'), (3.0, 'X'), (2.0, 'W'), (1.0, 'V'), (0.0, 'S')]
        
        played = set()
        serial_sent = set()
        
        while self.running:
            now = time.monotonic()
            diff = target_time - now
            if diff < -1.5: break

            # --- Serial Timing with Compensation ---
            comp_ms = self.config_manager.get("relay", "espnow_compensation") or 0
            comp_sec = comp_ms / 1000.0

            for t_before, char in serial_schedule:
                if char not in serial_sent and diff <= (t_before + comp_sec):
                    self._write_serial(char)
                    serial_sent.add(char)

            # --- Voice Audio Timing ---
            if self.config_manager.get("result_formatter", "generate_overlay") is not False:
                for t_before, key in voice_schedule:
                    if key not in played and diff <= t_before:
                        if key in self.sounds: self.sounds[key].play()
                        played.add(key)
            time.sleep(0.005)

    def _process_queue(self):
        while self.running:
            try:
                task = self.queue.get(timeout=1)
                if task["type"] == "lap":
                    now = time.monotonic()
                    flicker_sec = (self.config_manager.get("timer", "flicker_length") or 150) / 1000.0
                    
                    # --- 中継ガード判定 ---
                    allowed = False
                    if self.is_race_running:
                        # 1. 開始時刻に達しているか
                        if now >= self._start_time:
                            # 2. 終了信号がまだか、あるいは猶予期間内か
                            if self._stop_time == 0.0:
                                allowed = True # レース真っ最中
                            elif now <= (self._stop_time + flicker_sec):
                                allowed = True # 終了直後の猶予期間内
                            else:
                                self.is_race_running = False # 猶予期間終了
                                self.log("System", "Race Mid-Relay: IDLE (Post-Race Guard)")
                        else:
                            # 開始前（カウントダウン中など）は無視
                            pass
                    
                    if allowed:
                        marker_id = int(task["marker_id"])
                        area_idx = int(task["area_idx"])
                        target_server_port, target_server_name = None, None
                        for i in range(1, 5):
                            srv_key = f"server{i}"
                            allowed_ids = self.config_manager.get("relay", f"{srv_key}_id") or []
                            if marker_id in allowed_ids:
                                target_server_port = self.config_manager.get("relay", f"{srv_key}_port")
                                target_server_name = srv_key; break
                        
                        if target_server_port:
                            freqs = self.config_manager.get("timer", "camera_frequencies")
                            freq = freqs[area_idx] if area_idx < len(freqs) else 5800
                            lap_payload = {
                                "seat": area_idx, 
                                "frequency": int(freq), 
                                "lap_time": float(task["lap_time"]), 
                                "peak_rssi": 200,
                                "marker_id": marker_id # 内部処理用に追加
                            }
                            
                            # シリアル送信 (server1 宛の時のみ)
                            if target_server_name == 'server1':
                                self._write_serial(str(area_idx))

                            if self.on_lap_callback:
                                self.log("TX", f"LAP: CAMERA:{area_idx} ID:{marker_id} -> {target_server_name} Time:{task['lap_time']:.3f}s")
                                self.on_lap_callback(target_server_port, lap_payload)
                    else:
                        pass
                
                self.queue.task_done()
            except: continue

    def on_frequency_setup(self, data):
        if 'f' in data: self.config_manager.set("timer", "camera_frequencies", [int(x) for x in data['f']])
    def stop(self): 
        self.running = False
        if self.ser:
            try: self.ser.close()
            except: pass

