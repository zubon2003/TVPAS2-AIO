import os
import time
import threading
import requests
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class DataProcessor(FileSystemEventHandler):
    def __init__(self, config_manager, web_server, sheets_manager):
        self.config_manager = config_manager
        self.web = web_server
        self.sheets = sheets_manager
        self.observer = None
        self.running = False
        self._lock = threading.Lock()
        self._last_event_time = 0
        self._pending_update = False

    def start(self):
        self.running = True
        path = self.config_manager.get("result_formatter", "fpvtrackside_dir_path")
        if path and os.path.exists(path):
            self.observer = Observer()
            self.observer.schedule(self, path, recursive=True)
            self.observer.start()
        
        # リアルタイム公式ラップ監視 (SSE方式)
        threading.Thread(target=self._official_lap_realtime_listener, daemon=True).start()
        # スプレッドシート更新用スレッド
        threading.Thread(target=self._debounce_worker, daemon=True).start()

    def _official_lap_realtime_listener(self):
        """PocketBase の公式ラップを SSE でリアルタイム検知する"""
        base_url = self.web.result_manager.pb_base_url.replace("/api", "")
        realtime_url = f"{base_url}/api/realtime"
        
        while self.running:
            try:
                self.web.log("System", "Connecting to PocketBase Realtime...")
                resp = requests.get(realtime_url, stream=True, timeout=120)
                client_id = None
                
                for line in resp.iter_lines():
                    if not self.running: break
                    if not line: continue
                    line_str = line.decode('utf-8').strip()
                    
                    if line_str.startswith('data:'):
                        raw_data = line_str.replace('data:', '').strip()
                        if not raw_data: continue
                        
                        try:
                            data = json.loads(raw_data)
                        except: continue

                        # 1. 接続完了時の clientId 取得と購読
                        if not client_id and "clientId" in data:
                            client_id = data["clientId"]
                            sub_resp = requests.post(realtime_url, json={"clientId": client_id, "subscriptions": ["laps/*"]}, timeout=5)
                            if sub_resp.status_code == 204 or sub_resp.status_code == 200:
                                self.web.log("System", f"PocketBase Realtime Connected. (ID: {client_id})")
                            else:
                                self.web.log("System", f"PocketBase Subscription Failed: {sub_resp.status_code}")
                        
                        # 2. ラップ生成・更新イベントの処理
                        elif data.get("action") in ["create", "update"] and data.get("record"):
                            # 公式ラップ確定・修正時のアクション実行
                            official_update = self.web.result_manager.process_official_lap(data.get("record", {}))
                            # update の場合は既に処理済み(processed_official_laps)であっても、
                            # タイムが変わっている可能性があるため、スプレッドシートの更新をトリガーする
                            if official_update:
                                self.web.log("System", f"Official Lap {data.get('action')}: {official_update.get('pilot_name')} Lap {official_update.get('lap_number')}")
                                self.web.process_final_emit(official_update)
                            else:
                                # process_official_lap が None を返した場合（既にIDが登録済みの場合など）でも、
                                # action が 'update' ならスプレッドシートの再集計を行う
                                if data.get("action") == "update":
                                    self.web.log("System", "Lap updated in PocketBase. Triggering spreadsheet refresh.")
                                    self._trigger_debounce()
            except Exception as e:
                self.web.log("System", f"PocketBase Realtime Error: {e}")
                time.sleep(2.0)

    def _debounce_worker(self):
        """スプレッドシートの遅延更新処理"""
        while self.running:
            with self._lock:
                # レース中でない場合のみ、保留中の更新を実行
                if self._pending_update and not self.web.result_manager.is_race_active:
                    if time.time() - self._last_event_time >= 2.0:
                        self._pending_update = False
                        threading.Thread(target=self._perform_full_update, daemon=True).start()
            time.sleep(0.5)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.json'):
            self._trigger_debounce()

    def _trigger_debounce(self, immediate=False):
        # レース中は自動更新を予約しない
        if self.web.result_manager.is_race_active and not immediate: return
        if immediate:
            threading.Thread(target=self._perform_full_update, daemon=True).start()
            return
        with self._lock:
            self._last_event_time = time.time()
            self._pending_update = True

    def _perform_full_update(self):
        """実際に PocketBase と Sheets を更新する"""
        # 二重チェック: レースが始まっていたら中止
        if self.web.result_manager.is_race_active: return
        
        if not self.web.result_manager.update_all(): return
        if self.config_manager.get("result_formatter", "enable_spreadsheet_writing"):
            data = self.web.result_manager.get_all_sheets_data()
            if data: self.sheets.update_all_ranking_sheets(data)

    def stop(self):
        self.running = False
        if self.observer: self.observer.stop(); self.observer.join()
