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
        self.atem_manager = None 
        self.voice_manager = None 
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
        sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*', ping_timeout=60, ping_interval=25, allow_eio3=True)
        @sio.on('connect')
        async def connect(sid, environ):
            self.log("System", f"[{role_name}] Client connected: {sid}")
            if role_name != "web_ui":
                await sio.emit('heartbeat', self.get_heartbeat_data(role_name), room=sid)
            else:
                full_state = self.result_manager.get_realtime_state_all()
                for pilot_data in full_state:
                    pilot_data["server_now"] = time.monotonic()
                    await sio.emit('race_lap_update', pilot_data, room=sid)

        @sio.on('ts_server_info')
        async def server_info(sid, data=None):
            self.log("System", f"[{role_name}] ts_server_info requested")
            return {"release_version": "4.0.0-TVPAS2-AIO", "node_fw_versions": ["1.1.4"] * 4, "prog_start_epoch": str(int(time.time() * 1000)), "prog_start_time": time.strftime("%Y-%m-%d %H:%M:%S")}
        
        @sio.on('ts_server_time')
        async def server_time(sid, data=None):
            return float(time.monotonic())
            
        @sio.on('ts_frequency_setup')
        async def frequency_setup(sid, data):
            self.log("System", f"[{role_name}] ts_frequency_setup received (Ignored)")
            pass

        @sio.on('ts_race_stage')
        async def race_stage(sid, data):
            try:
                if isinstance(data, list) and len(data) > 0: d = data[0]
                else: d = data
                st_mono = float(d.get("start_time_s", time.monotonic() + 5)) if isinstance(d, dict) else (time.monotonic() + 5)
            except: st_mono = time.monotonic() + 5
            
            self.log("System", f"[{role_name}] ts_race_stage received. Start at: {st_mono:.3f}")
            self.result_manager.race_start_mono = st_mono
            self.result_manager.is_race_active = True
            
            st_unix = time.time() + (st_mono - time.monotonic())
            if role_name == "server1":
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui": await s["sio"].emit("race_start", {"startTime": st_unix, "time": time.time()})
                self.result_manager.reset_realtime_state()
                self.result_manager.is_race_active = True
                self.result_manager.race_start_mono = st_mono
                if isinstance(data, list) and len(data) > 1:
                    self.result_manager.resolve_names_from_packet(data[1:])
                full_state = self.result_manager.get_realtime_state_all()
                for p, s in self.sio_servers.items():
                    if s["role"] == "web_ui":
                        for pilot_data in full_state:
                            pilot_data["server_now"] = time.monotonic()
                            await s["sio"].emit('race_lap_update', pilot_data)
            
            await sio.emit("stage_ready", {"pi_starts_at_s": st_mono}, room=sid)
            if role_name == "server1":
                if time.monotonic() - self.last_broadcast_time > 2.0:
                    self.last_broadcast_time = time.monotonic()
                    if self.relay_manager:
                        if self.relay_manager.timer_manager: self.relay_manager.timer_manager.set_race_start_time(st_mono)
                        self.relay_manager.on_race_stage(st_mono)
                if self.atem_manager:
                    try: self.atem_manager.on_race_start()
                    except: pass
            return True

        @sio.on('ts_race_stop')
        async def race_stop(sid, data=None):
            self.log("System", f"[{role_name}] ts_race_stop received")
            if role_name == "server1":
                self.result_manager.reset_realtime_state()
                if self.atem_manager:
                    try: self.atem_manager.on_race_end()
                    except: pass
                
                # 猶予期間後に自動更新を予約
                def trigger_post_race_debounce():
                    flk = (self.config_manager.get("timer", "flicker_length") or 150) / 1000.0
                    time.sleep(flk + 0.1)
                    if hasattr(self, 'processor') and self.processor:
                        self.processor._trigger_update(immediate=False) # デバウンス経由で更新
                threading.Thread(target=trigger_post_race_debounce, daemon=True).start()
                
            if self.relay_manager: self.relay_manager.on_race_stop()
            return True

        @sio.on('ts_race_abort')
        async def race_abort(sid, data=None):
            self.log("System", f"[{role_name}] ts_race_abort received")
            if role_name == "server1":
                self.result_manager.reset_realtime_state()
                # 中止時も同様
                def trigger_post_race_debounce():
                    flk = (self.config_manager.get("timer", "flicker_length") or 150) / 1000.0
                    time.sleep(flk + 0.1)
                    if hasattr(self, 'processor') and self.processor:
                        self.processor._trigger_update(immediate=False)
                threading.Thread(target=trigger_post_race_debounce, daemon=True).start()
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
        self.sio_servers[port] = {"sio": sio, "role": role_name, "loop": None}
        def run_uvicorn():
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            self.sio_servers[port]["loop"] = loop
            if role_name == "web_ui": self.loop = loop
            config = uvicorn.Config(sio_app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
            server = uvicorn.Server(config)
            loop.run_until_complete(server.serve())
        threading.Thread(target=run_uvicorn, daemon=True).start()

    def emit_lap(self, port, data):
        now_abs = float(data.get("lap_time", time.monotonic()))
        flk_len_s = (self.config_manager.get("timer", "flicker_length") or 150) / 1000.0
        seat = data.get("seat", 0)
        
        # 1. レース中（開始後、かつ停止後+flicker経過前）のみ処理
        is_started = now_abs >= self.result_manager.race_start_mono
        is_within_grace = self.result_manager.is_race_active or (now_abs - self.result_manager.race_stop_time <= flk_len_s)
        
        if not is_started:
            self.log("System", f"[Ignored] Seat {seat} detected BEFORE race start. (Now:{now_abs:.3f} < Start:{self.result_manager.race_start_mono:.3f})")
            return
            
        if not is_within_grace:
            self.log("System", f"[Ignored] Seat {seat} detected AFTER race grace period. (Elapsed:{now_abs - self.result_manager.race_stop_time:.3f}s > {flk_len_s:.3f}s)")
            return

        socket_data = data.copy()
        st = self.result_manager.race_start_mono
        if st > 0: socket_data["lap_time"] = float(socket_data["lap_time"]) - st
        if "marker_id" in socket_data: socket_data.pop("marker_id")
        
        # 2. 送信成功のログ（従来の TX カテゴリ）
        self.log("TX", f"Lap to Port {port}: Seat {seat} @ {now_abs:.3f}")

        if port in self.sio_servers:
            s_info = self.sio_servers[port]
            if s_info["loop"]:
                async def do_emit(): await s_info["sio"].emit('ts_lap_data', socket_data)
                asyncio.run_coroutine_threadsafe(do_emit(), s_info["loop"])
        
        rt_update = self.result_manager.process_realtime_lap(port, data)
        if not rt_update: return
        
        if self.atem_manager:
            try:
                ports = sorted([p for p, info in self.sio_servers.items() if info["role"].startswith("server")])
                server_idx = ports.index(port) if port in ports else 0
                self.atem_manager.on_lap_event(seat=server_idx, server_id=server_idx + 1, rank=rt_update.get("position", 4), is_finished=rt_update.get("is_finished", False))
            except: pass
        if self.voice_manager and rt_update.get("event") == "lap_update":
            pilot_name = rt_update.get("pilot_name") or f"Player {rt_update.get('seat', 0) + 1}"
            laps = rt_update.get("lap_number", 0); lap_time = rt_update.get("lap_time", 0.0); total_time = sum(rt_update.get("history", [])); pos = rt_update.get("position", 0); is_finished = rt_update.get("is_finished", False)
            if self.config_manager.get("voice", "read_pilot_name"):
                speech_text = ""
                if laps == 0: speech_text = f"{pilot_name}、スタート"
                elif is_finished: speech_text = f"{pilot_name}、ゴール！ トータル {total_time:.3f}秒、 {pos}位"
                else: speech_text = f"{pilot_name}、ラップ {laps}、 {lap_time:.3f}秒"
                if speech_text: self.voice_manager.speak(speech_text)
        if self.loop:
            for p, info in self.sio_servers.items():
                if info["role"] == "web_ui":
                    async def do_web_emit(): await info["sio"].emit('race_lap_update', rt_update)
                    asyncio.run_coroutine_threadsafe(do_web_emit(), self.loop)

    def trigger_leaderboard_refresh(self):
        """Web UI (リーダーボード等) に対して強制リフレッシュを通知する"""
        if self.loop:
            for port, info in self.sio_servers.items():
                if info["role"] == "web_ui":
                    async def do_emit(): await info["sio"].emit('leaderboard_refresh', {"time": time.time()})
                    asyncio.run_coroutine_threadsafe(do_emit(), self.loop)

    async def _heartbeat_worker(self):
        while self.running:
            try:
                for port, info in self.sio_servers.items():
                    if info["role"] != "web_ui" and info["loop"]:
                        data = self.get_heartbeat_data(info["role"])
                        async def send_hb(s=info["sio"], d=data): await s.emit('heartbeat', d)
                        asyncio.run_coroutine_threadsafe(send_hb(), info["loop"])
                        if info["role"] == "server1":
                            self.log("Heartbeat", f"Heartbeat emitted to Trackside ports.")
            except: pass
            await asyncio.sleep(5.0)

    def start(self):
        web_port = self.config_manager.get("global", "web_ui_port") or 5050
        self.start_port(web_port, "web_ui")
        for i in range(1, 5):
            p = self.config_manager.get("relay", f"server{i}_port")
            if p and p > 0: self.start_port(p, f"server{i}")
        def run_hb_thread():
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(self._heartbeat_worker())
        threading.Thread(target=run_hb_thread, daemon=True).start()
