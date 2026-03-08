import os
import time
import asyncio
import socketio
import uvicorn
import json
import threading
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend.result_manager import ResultManager

class WebServer:
    def __init__(self, config_manager, relay_manager=None):
        self.config_manager = config_manager
        self.relay_manager = relay_manager
        self.atem_manager = None # main.pyからセットされる
        self.voice_manager = None # main.pyからセットされる
        self.result_manager = ResultManager(config_manager)
        self.log_callback = None
        self.result_manager.log_callback = self.log
        
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.static_dir = os.path.join(self.base_dir, "static")
        self.public_dir = os.path.join(self.static_dir, "public")
        self.overlay_dir = os.path.join(self.base_dir, "html")
        
        self.app = FastAPI()
        self.setup_fastapi_routes()
        
        self.sio_servers = {} 
        self.loop = None
        self.running = True
        self.last_broadcast_time = 0

    def log(self, category, message):
        if self.log_callback: self.log_callback(category, message)

    def get_unix_offset(self): return time.time() - time.monotonic()
    def get_system_frequencies(self): return self.config_manager.get("timer", "camera_frequencies") or [5705, 5800, 5845, 5885]

    def setup_fastapi_routes(self):
        @self.app.get('/')
        async def index(): return {"status": "online", "server": "TVPAS2-AIO"}
        @self.app.get('/api/leaderboard')
        async def get_lb(): return JSONResponse(self.result_manager.get_leaderboard_data())
        @self.app.get('/api/pilot_image')
        async def get_pilot_image(path: str = None, id: str = None):
            fpv_root = self.config_manager.get("result_formatter", "fpvtrackside_dir_path")
            if path and path.strip():
                p = os.path.normpath(path)
                if os.path.exists(p) and os.path.isfile(p): return FileResponse(p)
                if fpv_root:
                    clean_rel = path.replace("..\\", "").replace("../", "").strip("\\/")
                    search_paths = [os.path.normpath(os.path.join(fpv_root, path)), os.path.normpath(os.path.join(fpv_root, clean_rel)), os.path.normpath(os.path.join(fpv_root, "Pilots", os.path.basename(path)))]
                    for sp in search_paths:
                        if os.path.exists(sp) and os.path.isfile(sp): return FileResponse(sp)
            fallback = os.path.join(self.overlay_dir, "image.png")
            if not os.path.exists(fallback): fallback = os.path.join(self.public_dir, "image.png")
            return FileResponse(fallback)
        @self.app.get('/leaderboard')
        async def lb_page(): return FileResponse(os.path.join(self.overlay_dir, "leaderboard.html"))
        @self.app.get('/heatresult')
        async def hr_page(): return FileResponse(os.path.join(self.overlay_dir, "heatresult.html"))
        @self.app.get('/nextheat')
        async def nh_page(): return FileResponse(os.path.join(self.overlay_dir, "nextHeat.html"))
        @self.app.get('/overlay')
        async def ov_page(): return FileResponse(os.path.join(self.overlay_dir, "obs_overlay.html"))
        @self.app.get('/race_feed')
        async def rf_page(): return FileResponse(os.path.join(self.overlay_dir, "race_feed.html"))
        @self.app.get('/atem')
        async def atem_page(): return FileResponse(os.path.join(self.overlay_dir, "atem.html"))
        
        @self.app.get('/api/atem/config')
        async def get_atem_config():
            return self.config_manager.get("atem")
        
        @self.app.post('/api/atem/config')
        async def set_atem_config(req: Request):
            data = await req.json()
            for k, v in data.items():
                self.config_manager.set("atem", k, v)
            if self.atem_manager:
                if data.get("enabled"): self.atem_manager.start()
                else: self.atem_manager.stop()
            return {"status": "success"}

        @self.app.get('/api/atem/status')
        async def get_atem_status():
            if self.atem_manager: return self.atem_manager.get_status()
            return {"program": 0, "connected": False}

        @self.app.post('/api/atem/switch')
        async def manual_atem_switch(req: Request):
            data = await req.json()
            input_num = data.get("input")
            if self.atem_manager and input_num:
                self.atem_manager.switch_to_input(input_num)
            return {"status": "success"}

        @self.app.get('/image.png')
        async def img1(): return FileResponse(os.path.join(self.overlay_dir, "image.png"))
        @self.app.get('/image2.png')
        async def img2(): return FileResponse(os.path.join(self.overlay_dir, "image2.png"))
        if os.path.exists(self.public_dir): self.app.mount("/public", StaticFiles(directory=self.public_dir), name="public")
        
    def create_async_sio(self, port, role_name):
        sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*', ping_timeout=20, ping_interval=10)
        @sio.on('connect')
        async def connect(sid, environ):
            self.log("System", f"[{role_name}] Client connected: {sid}")
            if role_name != "web_ui":
                await sio.emit('heartbeat', self.get_heartbeat_data(role_name), room=sid)
            else:
                # 接続時に現在の全ステータスを送って同期
                full_state = self.result_manager.get_realtime_state_all()
                for pilot_data in full_state:
                    pilot_data["server_now"] = time.monotonic()
                    await sio.emit('race_lap_update', pilot_data, room=sid)

        @sio.on('ts_server_info')
        async def server_info(sid, data=None):
            return {"release_version": "4.0.0-TVPAS2-AIO", "node_fw_versions": ["1.1.4"] * 4, "prog_start_epoch": str(int(time.time() * 1000)), "prog_start_time": time.strftime("%Y-%m-%d %H:%M:%S")}
        @sio.on('ts_server_time')
        async def server_time(sid, data=None): return float(time.monotonic())
        @sio.on('ts_frequency_setup')
        async def frequency_setup(sid, data):
            if self.relay_manager and role_name == "server1": self.relay_manager.on_frequency_setup(data)
            if isinstance(data, dict) and 'f' in data and len(data['f']) > 0:
                await sio.emit('frequency_set', {"node": 0, "frequency": data['f'][0]}, room=sid)
            return True
        @sio.on('ts_race_stage')
        async def race_stage(sid, data):
            self.log("RX", f"[{role_name}] ts_race_stage")
            
            # スタート時刻の算出 (Monotonic -> UNIX)
            try:
                if isinstance(data, list) and len(data) > 0: d = data[0]
                else: d = data
                st_mono = float(d.get("start_time_s", time.monotonic() + 5)) if isinstance(d, dict) else (time.monotonic() + 5)
            except: st_mono = time.monotonic() + 5
            
            # HTMLオーバーレイ向けの UNIX 開始時刻
            st_unix = time.time() + (st_mono - time.monotonic())

            if role_name == "server1":
                # 1. 最初にリセット信号を送り画面をクリア (startTimeを追加し、timeも維持)
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui": await s["sio"].emit("race_start", {"startTime": st_unix, "time": time.time()})
                
                # 2. 状態をリセットし、パケットから名前を解決
                self.result_manager.reset_realtime_state()
                if isinstance(data, list) and len(data) > 1:
                    self.result_manager.resolve_names_from_packet(data[1:])
                
                # 3. 名前入りの全状態をプッシュ
                full_state = self.result_manager.get_realtime_state_all()
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui":
                        for pilot_data in full_state:
                            pilot_data["server_now"] = time.monotonic()
                            await s["sio"].emit('race_lap_update', pilot_data)

            # 以降、スタート時刻処理
            try:
                if isinstance(data, list) and len(data) > 0: d = data[0]
                else: d = data
                st = float(d.get("start_time_s", time.monotonic() + 5)) if isinstance(d, dict) else (time.monotonic() + 5)
                if role_name == "server1": self.result_manager.race_start_mono = st
            except: st = time.monotonic() + 5
            await sio.emit("stage_ready", {"pi_starts_at_s": st}, room=sid)
            if role_name == "server1":
                if time.monotonic() - self.last_broadcast_time > 2.0:
                    self.last_broadcast_time = time.monotonic()
                    if self.relay_manager:
                        if self.relay_manager.timer_manager: self.relay_manager.timer_manager.set_race_start_time(st)
                        self.relay_manager.on_race_stage(st)
                
                # ATEM レース開始時切り替え
                if self.atem_manager:
                    try: self.atem_manager.on_race_start()
                    except: pass

            return True
        @sio.on('ts_race_stop')
        async def race_stop(sid, data=None):
            if role_name == "server1":
                self.result_manager.reset_realtime_state()
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui": await s["sio"].emit("race_reset", {})
                
                # ATEM レース終了時切り替え
                if self.atem_manager:
                    try: self.atem_manager.on_race_end()
                    except: pass

            if self.relay_manager: self.relay_manager.on_race_stop()
            return True
        @sio.on('ts_race_abort')
        async def race_abort(sid, data=None):
            if role_name == "server1":
                self.result_manager.reset_realtime_state()
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui": await s["sio"].emit("race_reset", {})
            if self.relay_manager: self.relay_manager.on_race_stop()
            return True
        @sio.on('*')
        async def catch_all(event, sid, *args): pass
        return sio

    def get_heartbeat_data(self, role_name="server1"):
        all_freqs = self.get_system_frequencies()
        count = len(all_freqs)
        return {"current_rssi": [50] * count, "frequency": all_freqs, "loop_time": [1000] * count, "crossing_flag": [False] * count}

    def start_port(self, port, role_name):
        sio = self.create_async_sio(port, role_name)
        sio_app = socketio.ASGIApp(sio, self.app if role_name == "web_ui" else None)
        self.sio_servers[port] = {"sio": sio, "role": role_name}
        def run_uvicorn():
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            if role_name == "web_ui": self.loop = loop
            config = uvicorn.Config(sio_app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
            server = uvicorn.Server(config); loop.run_until_complete(server.serve())
        threading.Thread(target=run_uvicorn, daemon=True).start()

    def emit_lap(self, port, data):
        internal_data = data.copy(); socket_data = data.copy()
        
        # Dashboard 向けに相対時間へ変換
        st = self.result_manager.race_start_mono
        if st > 0: socket_data["lap_time"] = float(socket_data["lap_time"]) - st
        
        if "marker_id" in socket_data: socket_data.pop("marker_id")
        if port in self.sio_servers and self.loop:
            sio = self.sio_servers[port]["sio"]
            async def do_emit(): await sio.emit('ts_lap_data', socket_data)
            asyncio.run_coroutine_threadsafe(do_emit(), self.loop)
        
        rt_update = self.result_manager.process_realtime_lap(port, internal_data)
        
        # ATEM切り替えのトリガー (RelayManagerは介さずここで行う)
        if self.atem_manager and rt_update:
            # port番号(5000-5003)からサーバーID(1-4)を算出
            try:
                # サーバーのリストからインデックスを取得
                ports = sorted([p for p, info in self.sio_servers.items() if info["role"].startswith("server")])
                server_idx = ports.index(port) if port in ports else 0
                self.atem_manager.on_lap_event(
                    seat=server_idx, 
                    server_id=server_idx + 1, 
                    rank=rt_update.get("position", 4), 
                    is_finished=rt_update.get("is_finished", False)
                )
            except: pass

        # 音声読み上げのトリガー
        # event が "lap_update" (ゴールゲート通過) の時のみ読み上げる
        if self.voice_manager and rt_update and rt_update.get("event") == "lap_update":
            pilot_name = rt_update.get("pilot_name") or f"Player {server_idx + 1}"
            laps = rt_update.get("lap_number", 0)
            lap_time = rt_update.get("lap_time", 0.0)
            total_time = sum(rt_update.get("history", []))
            pos = rt_update.get("position", 0)
            is_finished = rt_update.get("is_finished", False)

            if self.config_manager.get("voice", "read_pilot_name"):
                speech_text = ""
                if laps == 0:
                    # Holeshot
                    speech_text = f"{pilot_name}、スタート"
                elif is_finished:
                    # レース完走
                    speech_text = f"{pilot_name}、ゴール！ トータル {total_time:.3f}秒、 {pos}位"
                else:
                    # 周回通過
                    speech_text = f"{pilot_name}、ラップ {laps}、 {lap_time:.3f}秒"
                
                if speech_text:
                    self.voice_manager.speak(speech_text)

        if rt_update and self.loop:
            for p, info in self.sio_servers.items():
                if info["role"] == "web_ui":
                    sio_web = info["sio"]
                    async def do_web_emit(): await sio_web.emit('race_lap_update', rt_update)
                    asyncio.run_coroutine_threadsafe(do_web_emit(), self.loop)

    def trigger_leaderboard_refresh(self):
        if self.loop:
            for port, info in self.sio_servers.items():
                if info["role"] == "web_ui":
                    sio = info["sio"]
                    async def do_emit(): await sio.emit('leaderboard_refresh', {"time": time.time()})
                    asyncio.run_coroutine_threadsafe(do_emit(), self.loop)

    async def _heartbeat_worker(self):
        while self.running:
            try:
                for port, info in self.sio_servers.items():
                    if info["role"] != "web_ui": await info["sio"].emit('heartbeat', self.get_heartbeat_data(info["role"]))
            except: pass
            await asyncio.sleep(2.0)

    def start(self):
        web_port = self.config_manager.get("global", "drone_dashboard_port_web") or 5050
        self.start_port(web_port, "web_ui")
        for i in range(1, 5):
            p = self.config_manager.get("relay", f"server{i}_port")
            if p and p > 0: self.start_port(p, f"server{i}")
        def run_hb():
            while self.loop is None: time.sleep(0.1)
            asyncio.run_coroutine_threadsafe(self._heartbeat_worker(), self.loop)
        threading.Thread(target=run_hb, daemon=True).start()
