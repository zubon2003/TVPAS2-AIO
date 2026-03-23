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
        self.min_switch_interval = 0.5 
        # HTTP接続を維持するためのセッションを初期化
        self.session = requests.Session()

    def log(self, msg):
        if self.log_callback: self.log_callback("ATEM", msg)

    def get_status(self):
        return {"program": self.current_program, "connected": self.connected}

    def switch_to_input(self, input_num):
        if not self.config_manager.get("atem", "enabled"): return
        self.log(f"Manual Switch Request: Input {input_num}")
        self._send_switch_command(input_num)

    def _send_switch_command(self, input_num):
        """切り替え命令。メインスレッドをブロックしないよう別スレッドで実行する"""
        now = time.time()
        # 同じ入力への過剰な連続リクエストを防止
        if input_num == self.current_program:
            if (now - self.last_switch_time < self.min_switch_interval): return
        
        # 実際の送信処理を別スレッドで実行
        threading.Thread(target=self._execute_switch_post, args=(input_num, now), daemon=True).start()

    def _execute_switch_post(self, input_num, request_time):
        """別スレッドで実行されるHTTP POST処理"""
        ip = self.config_manager.get("atem", "ip") or "127.0.0.1"
        port = self.config_manager.get("atem", "port") or 8888
        url = f"http://{ip}:{port}/api/switch"
        
        try:
            # セッションを使用して接続を再利用し、タイムアウトを短く設定
            resp = self.session.post(url, json={"input": input_num}, timeout=0.2)
            if resp.status_code == 200:
                self.current_program, self.last_switch_time, self.connected = input_num, time.time(), True
                self.log(f"Switched to Input {input_num} (OK)")
            else:
                self.connected = False
                self.log(f"Switch Error: HTTP {resp.status_code}")
        except Exception as e:
            self.connected = False
            # タイムアウト等のエラーログ（頻出しすぎないよう注意）
            if time.time() - self.last_switch_time > 5.0: # 5秒に1回程度に制限
                self.log(f"Connection Error: {e}")

    def on_lap_event(self, seat, server_id, rank, is_finished):
        if not self.config_manager.get("atem", "enabled"): return
        mode = self.config_manager.get("atem", "mode") or "Auto"
        
        # 内部状態の可視化用ログ
        states_summary = []
        target_pilot = None
        
        if mode == "Auto":
            active_states = []
            for s_id, s_data in self.result_manager.realtime_race_state.items():
                p_name = s_data.get("pilot_name", "???")
                p_pos = s_data.get("position", 9)
                p_lap = s_data.get("lap_count", 0)
                p_sec = s_data.get("last_sector", 0)
                p_fin = s_data.get("is_finished", False)
                
                status_str = f"#{p_pos} {p_name}(L{p_lap} S{p_sec})"
                if p_fin: status_str += " [FIN]"
                states_summary.append(status_str)

                if s_data.get("is_active") and not p_fin:
                    active_states.append({"seat": s_id, "rank": p_pos, "name": p_name})
            
            # 詳細ログはデバッグモード時のみ出力
            if self.config_manager.get("global", "debug_mode"):
                self.log(f"Live Ranks: {', '.join(states_summary)}")

            if not active_states:
                if self.config_manager.get("global", "debug_mode"):
                    self.log("Ignore: All pilots finished or no active pilots.")
                return

            # 現在走行中のトップを特定
            top_active = min(active_states, key=lambda x: x["rank"])
            target_pilot = top_active
            
            if seat == top_active["seat"]:
                # 切り替え実行時のログは常に残す
                self.log(f">>> TARGET MATCH: {top_active['name']} passed Gate {server_id}")
                self._send_switch_command(server_id)
            else:
                pass # 静かに無視（ログが多すぎないようにするため）

        elif mode.startswith("Pos"):
            target_rank = int(mode.replace("Pos", ""))
            if rank == target_rank: self._send_switch_command(server_id)
        elif mode.startswith("Seat"):
            target_seat = int(mode.replace("Seat", ""))
            if (seat + 1) == target_seat: self._send_switch_command(server_id)

    def on_race_start(self):
        if not self.config_manager.get("atem", "enabled"): return
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self._send_switch_command(def_cam)

    def on_race_end(self):
        if not self.config_manager.get("atem", "enabled"): return
        def_cam = self.config_manager.get("atem", "default_camera") or 4
        self._send_switch_command(def_cam)

    def start(self):
        if self.running: return
        self.running = True; self.log("ATEM Manager started.")

    def stop(self):
        self.running = False; self.log("ATEM Manager stopped.")
