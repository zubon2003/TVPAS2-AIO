import os
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class DataProcessor(FileSystemEventHandler):
    def __init__(self, config_manager, web_server, sheets_manager):
        self.config_manager = config_manager
        self.web = web_server
        self.sheets = sheets_manager
        self.observer = None
        self._lock = threading.Lock()
        self.running = False
        
        self._last_event_time = 0 # 最後に変化が起きた時刻
        self._update_pending = False # 更新待ちフラグ
        self._last_finished_heat_count = -1

    def on_modified(self, event):
        if event.src_path.endswith(".json"):
            self._trigger_debounce()

    def _trigger_debounce(self):
        """更新予約を入れる。2秒ルールを開始する。"""
        # レース中は自動更新を避ける
        if self.web.result_manager.is_race_active:
            return
            
        with self._lock:
            self._last_event_time = time.time()
            if not self._update_pending:
                self._update_pending = True
                threading.Thread(target=self._debounce_worker, daemon=True).start()

    def _debounce_worker(self):
        """2秒間変化がないことを監視して実行する"""
        while True:
            time.sleep(0.5)
            now = time.time()
            with self._lock:
                if (now - self._last_event_time) >= 2.0:
                    self._update_pending = False
                    self._perform_full_update()
                    break

    def _trigger_update(self, immediate=False):
        if immediate:
            threading.Thread(target=self._perform_full_update, daemon=True).start()
        else:
            self._trigger_debounce()

    def _perform_full_update(self):
        print(f"[{time.strftime('%H:%M:%S')}] Executing Full Update (Overlays & Sheets)...")
        # 内部キャッシュの強制更新
        if not self.web.result_manager.update_all():
            return
            
        self.web.trigger_leaderboard_refresh()
        
        if self.config_manager.get("result_formatter", "enable_spreadsheet_writing"):
            try:
                data_dict = self.web.result_manager.get_all_sheets_data()
                if data_dict:
                    self.sheets.update_all_ranking_sheets(data_dict)
            except Exception as e:
                if self.sheets.log_callback: self.sheets.log_callback("Sheet", f"Update Error: {e}")

    def start(self):
        # WebServer に自分自身を登録 (逆参照用)
        self.web.processor = self
        
        try:
            base_dir = self.config_manager.get("result_formatter", "fpvtrackside_dir_path")
            if base_dir and os.path.exists(base_dir):
                events_dir = base_dir
                if os.path.basename(base_dir).lower() != "events":
                    p = os.path.join(base_dir, "Events")
                    if os.path.exists(p): events_dir = p
                
                self.observer = Observer()
                self.observer.schedule(self, events_dir, recursive=True)
                self.observer.start()
                print(f"[Processor] Activity monitoring started: {events_dir}")
        except Exception as e:
            print(f"[Processor] Failed to start observer: {e}")
        
        self.running = True
        threading.Thread(target=self._pb_monitor_loop, daemon=True).start()

    def _pb_monitor_loop(self):
        while self.running:
            try:
                # レース中でない時だけ PocketBase の変化を監視
                if not self.web.result_manager.is_race_active:
                    races = self.web.result_manager.fetch_pb("races", params={"perPage": 500})
                    finished_races = [r for r in races if self.web.result_manager.is_race_finished(r)]
                    current_count = len(finished_races)
                    if self._last_finished_heat_count == -1: self._last_finished_heat_count = current_count
                    if current_count != self._last_finished_heat_count:
                        self._last_finished_heat_count = current_count
                        self._trigger_debounce()
            except: pass
            time.sleep(2.0)

    def stop(self):
        self.running = False
        if self.observer: self.observer.stop(); self.observer.join()
