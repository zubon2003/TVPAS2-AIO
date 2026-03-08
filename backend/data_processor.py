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
        self._debounce_timer = None
        self._pending_timer = None # レート制限による待機用
        self._lock = threading.Lock()
        self._last_sheet_update_time = 0
        self._rate_limit_sec = 30.0
        
        # 新規: スプレッドシート監視用
        self._last_data_change_time = 0
        self._last_finished_heat_count = -1
        self._sheet_update_pending = False
        self.running = False

    def on_modified(self, event):
        # Activity detection only. No heavy processing here.
        filename = os.path.basename(event.src_path)
        if filename.endswith(".json"):
            self._last_data_change_time = time.time()

    def _trigger_update(self, immediate=False):
        """Manual or immediate trigger."""
        if immediate:
            threading.Thread(target=self._perform_full_update, daemon=True).start()

    def _perform_full_update(self):
        """一括更新：全オーバーレイとスプレッドシートを最新状態にする"""
        print(f"[{time.strftime('%H:%M:%S')}] Executing Full Update (Overlays & Sheets)...")
        
        # 1. Web Overlays を更新
        self.web.result_manager.update_all() # キャッシュとリアルタイム状態を公式データで更新
        self.web.trigger_leaderboard_refresh()
        
        # リアルタイム状態の公式同期を全クライアントへ通知 (Race Feed同期用)
        if self.web.loop:
            full_state = self.web.result_manager.get_realtime_state_all()
            for pilot_data in full_state:
                pilot_data["server_now"] = time.monotonic()
                async def do_sync(pd=pilot_data):
                    for p, info in self.web.sio_servers.items():
                        if info["role"] == "web_ui":
                            await info["sio"].emit('race_lap_update', pd)
                import asyncio
                asyncio.run_coroutine_threadsafe(do_sync(), self.web.loop)
        
        # 2. スプレッドシートを更新 (チェックが入っている場合のみ)
        if self.config_manager.get("result_formatter", "enable_spreadsheet_writing"):
            try:
                # ResultManager の PocketBase ベースの抽出ロジックを使用
                data_dict = self.web.result_manager.get_all_sheets_data()
                if data_dict:
                    self.sheets.update_all_ranking_sheets(data_dict)
                    self._last_sheet_update_time = time.time()
            except Exception as e:
                if self.sheets.log_callback: self.sheets.log_callback("Sheet", f"Update Error: {e}")

    def start(self):
        # 1. File monitoring (Watchdog) - used for 'silence' detection
        base_dir = self.config_manager.get("result_formatter", "fpvtrackside_dir_path")
        if base_dir and os.path.exists(base_dir):
            events_dir = base_dir
            if os.path.basename(base_dir).lower() != "events":
                p = os.path.join(base_dir, "Events")
                if os.path.exists(p): events_dir = p
            self.observer = Observer(); self.observer.schedule(self, events_dir, recursive=True); self.observer.start()
            print(f"Activity monitoring started: {events_dir}")
        
        # 2. PocketBase monitoring loop
        self.running = True
        threading.Thread(target=self._spreadsheet_monitor_loop, daemon=True).start()
        
        # Initial sync (with a reliable retry mechanism)
        if self.config_manager.get("result_formatter", "enable_spreadsheet_writing"):
            def startup_sync_worker():
                retries = 3
                for i in range(retries):
                    time.sleep(5.0 + (i * 5.0)) # 5s, 15s, 30s wait
                    print(f"[Sheet] Startup sync attempt {i+1}/{retries}...")
                    try:
                        # データの存在チェックを兼ねて取得
                        data = self.web.result_manager.get_all_sheets_data()
                        if data:
                            self._perform_full_update()
                            print("[Sheet] Startup synchronization successful.")
                            break
                        else:
                            print(f"[Sheet] Startup sync: No data available yet (attempt {i+1}).")
                    except Exception as e:
                        print(f"[Sheet] Startup sync failed (attempt {i+1}): {e}")
                
            threading.Thread(target=startup_sync_worker, daemon=True).start()

    def _spreadsheet_monitor_loop(self):
        """PocketBaseの状態を監視し、ヒート終了後の静止を検知する"""
        while self.running:
            try:
                # PocketBaseから完了ヒート数を取得
                races = self.web.result_manager.fetch_pb("races", params={"perPage": 500})
                finished_races = [r for r in races if self.web.result_manager.is_race_finished(r)]
                current_count = len(finished_races)
                
                if self._last_finished_heat_count == -1:
                    self._last_finished_heat_count = current_count
                
                # 新しいヒートが完了したかチェック
                if current_count > self._last_finished_heat_count:
                    self._last_finished_heat_count = current_count
                    self._sheet_update_pending = True
                    print(f"[System] Heat completion detected in PocketBase. Waiting for silence...")

                # 2秒間の静止（ファイル更新なし）を待ってから実行
                if self._sheet_update_pending:
                    now = time.time()
                    if (now - self._last_data_change_time) >= 2.0:
                        print(f"[System] 2s silence confirmed. Triggering full update.")
                        self._sheet_update_pending = False
                        self._perform_full_update()
                
            except Exception as e:
                print(f"[PB Monitor Error] {e}")
            
            time.sleep(1.0) # 1秒間隔でポーリング

    def stop(self):
        self.running = False
        if self._debounce_timer: self._debounce_timer.cancel()
        if self.observer: self.observer.stop(); self.observer.join()
