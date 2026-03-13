import cv2
import numpy as np
import time
import threading
import math
import sys
import os
import json
from concurrent.futures import ThreadPoolExecutor

# マーカー検知ライブラリのインポート
try:
    import stag
except ImportError:
    stag = None

# 仮想カメラライブラリのインポート
try:
    import pyvirtualcam
    HAS_VCAM = True
except ImportError:
    HAS_VCAM = False

if sys.platform == 'win32':
    try:
        from pygrabber.dshow_graph import FilterGraph
        HAS_DSHOW = True
    except ImportError:
        HAS_DSHOW = False
else:
    HAS_DSHOW = False

class FrameGrabber:
    def __init__(self, device_id, width, height, fps):
        self.device_id = device_id; self.width = width; self.height = height; self.fps = fps
        self.cap = None; self.frame = None; self.capture_time = 0.0; self.running = False; self.new_frame_event = threading.Event()
    def start(self): self.running = True; self.thread = threading.Thread(target=self.run, daemon=True); self.thread.start()
    def run(self):
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                if self.cap: self.cap.release()
                self.cap = cv2.VideoCapture(self.device_id)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width); self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cap.set(cv2.CAP_PROP_FPS, self.fps); time.sleep(1.0); continue
            ret, frame = self.cap.read()
            if ret: self.frame = frame; self.capture_time = time.monotonic(); self.new_frame_event.set()
            else: self.cap.release(); time.sleep(0.5)
    def stop(self):
        self.running = False
        if hasattr(self, 'thread'): self.thread.join(timeout=1.0)
        if self.cap: self.cap.release()

class TimerManager:
    def __init__(self, config_manager, on_lap_callback=None, on_heartbeat_callback=None):
        self.config_manager = config_manager
        self.on_lap_callback = on_lap_callback
        self.on_heartbeat_callback = on_heartbeat_callback
        self.lock = threading.Lock()
        self.grabber = None; self.running = False; self.cv_thread = None
        self.vcam = None; self.vcam_w, self.vcam_h = 1280, 720
        self.log_callback = None
        
        if stag is None:
            self.log("System", "Warning: Stag library NOT found. Stag detection will be disabled.")
        else:
            self.log("System", "Stag library loaded successfully.")

        self.aruco_dict = self._load_or_create_aruco_dict()
        self.aruco_params = cv2.aruco.DetectorParameters()
        self.calib_cache = {}; self._setup_calibration()
        tc = self.config_manager.get("timer", "thread_count") or 2
        self.executor = ThreadPoolExecutor(max_workers=tc)
        self.race_start_time = time.monotonic(); self.last_heartbeat_time = 0; self.last_frame_time = time.monotonic(); self.current_fps = 0.0; self.cycle_count = 0
        self.cam_states = {i: {"is_in_gate": False, "last_marker_id": -1, "loop_time": 0.0, "rss_output": False, "current_count": 0, "flicker_endtime": 0} for i in range(4)}
        self.display_frame = None

    def log(self, cat, msg):
        if self.log_callback: self.log_callback(cat, msg)
        else: print(f"[{cat}] {msg}")

    def _setup_calibration(self):
        cal_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "camera_calibration.json")
        if not os.path.exists(cal_path):
            self.log("System", f"Calibration file not found at {cal_path}")
            return
        try:
            with open(cal_path, 'r') as f: cal = json.load(f)
            orig_mtx = np.array(cal['mtx']); dist = np.array(cal['dist']); cal_res = cal.get('resolution', [720, 960]); cal_h, cal_w = float(cal_res[0]), float(cal_res[1])
            targets = [(640, 480), (800, 600), (960, 720), (1024, 768), (1280, 720), (1280, 960), (1440, 1080), (1920, 1080)]
            for tw, th in targets:
                qw, qh = tw // 2, th // 2; mtx = orig_mtx.copy(); scale_x = qw / cal_w; scale_y = qh / cal_h
                mtx[0, 0] *= scale_x; mtx[1, 1] *= scale_y; mtx[0, 2] *= scale_x; mtx[1, 2] *= scale_y
                new_mtx, _ = cv2.getOptimalNewCameraMatrix(mtx, dist, (qw, qh), 1, (qw, qh))
                mx, my = cv2.initUndistortRectifyMap(mtx, dist, None, new_mtx, (qw, qh), cv2.CV_32FC1)
                self.calib_cache[(tw, th)] = (mx, my)
            self.log("System", f"Calibration loaded for resolutions: {[f'{k[0]}x{k[1]}' for k in self.calib_cache.keys()]}")
        except Exception as e:
            self.log("System", f"Error loading calibration: {e}")

    def _load_or_create_aruco_dict(self):
        dict_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aruco_dict_0_3.json")
        if os.path.exists(dict_path):
            try:
                with open(dict_path, 'r') as f: data = json.load(f)
                return cv2.aruco.Dictionary(np.array(data['bytesList'], dtype=np.uint8), data['markerSize'], data['maxCorrectionBits'])
            except: pass
        base_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        new_dict = cv2.aruco.Dictionary(base_dict.bytesList[:4], base_dict.markerSize, base_dict.maxCorrectionBits)
        return new_dict

    def set_race_start_time(self, start_time_mono): self.race_start_time = start_time_mono
    def get_input_devices(self):
        if HAS_DSHOW:
            try: return FilterGraph().get_input_devices()
            except: pass
        return [f"Camera {i}" for i in range(5)]

    def find_camera_id(self, target_name):
        try:
            devices = self.get_input_devices()
            for i, name in enumerate(devices):
                if target_name in name: return i
        except: pass
        return 0

    def get_camera_formats(self, camera_name):
        formats = {"1920x1080": [30, 60], "1280x720": [30, 60, 90, 120], "960x720": [30, 60], "640x480": [30, 60, 90, 120]}
        if HAS_DSHOW:
            try:
                graph = FilterGraph(); device_index = self.find_camera_id(camera_name); graph.add_video_input_device(device_index)
                raw_formats = graph.get_formats()
                for f in raw_formats:
                    parts = f.split()
                    if len(parts) >= 2:
                        res, fps_str = parts[0], parts[1].replace("fps", ""); fps = int(float(fps_str))
                        if res not in formats: formats[res] = []
                        if fps not in formats[res]: formats[res].append(fps)
            except: pass
        return formats

    def restart(self):
        self.log("System", "TimerManager: Restarting services...")
        with self.lock:
            if hasattr(self, 'executor'): self.executor.shutdown(wait=False)
            if self.grabber: self.grabber.stop(); self.grabber = None
            if self.vcam:
                try: self.vcam.close()
                except: pass
                self.vcam = None
            
            tc = self.config_manager.get("timer", "thread_count") or 2
            self.executor = ThreadPoolExecutor(max_workers=tc)
            
            t_cam = self.config_manager.get("timer", "target_camera_name")
            res_cfg = self.config_manager.get("timer", "resolution") or "1280x720"
            aspect = self.config_manager.get("timer", "aspect_ratio") or "16:9"
            fps = self.config_manager.get("timer", "target_fps") or 60
            
            try: rw, rh = map(int, res_cfg.split("x"))
            except: rw, rh = 1280, 720
            
            if rh >= 1080: self.vcam_w, self.vcam_h = 1920, 1080
            else: self.vcam_w, self.vcam_h = 1280, 720
            
            if self.config_manager.get("timer", "virtual_camera_enabled"):
                if HAS_VCAM:
                    try:
                        self.vcam = pyvirtualcam.Camera(width=self.vcam_w, height=self.vcam_h, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR)
                        self.log("System", f"Virtual Camera Started: {self.vcam_w}x{self.vcam_h} @ {fps}fps")
                    except Exception as e:
                        self.log("System", f"Virtual Camera Startup Failed: {e}")
                        self.vcam = None
            
            device_index = self.find_camera_id(t_cam)
            final_device_id = (device_index + cv2.CAP_DSHOW) if sys.platform == 'win32' else device_index
            self.grabber = FrameGrabber(final_device_id, rw, rh, fps)
            self.grabber.start()
            self.log("System", f"Camera Capture Started: {t_cam} ({rw}x{rh})")

    def _detect_markers_in_image(self, gray_img, marker_system, stag_lib, ec_rate):
        results = []
        if gray_img is None or gray_img.size == 0: return results

        if stag and marker_system in ["Stag", "Hybrid"]:
            try:
                sc, sid, _ = stag.detectMarkers(gray_img, stag_lib)
                if sid is not None and len(sid) > 0:
                    for j, c_raw in enumerate(sc):
                        results.append({"corners": c_raw[0] if len(c_raw.shape) > 2 else c_raw, "id": int(sid[j][0]), "is_stag": True})
            except: pass
        
        if marker_system in ["ArUco", "Hybrid"]:
            try:
                self.aruco_params.errorCorrectionRate = ec_rate
                detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params)
                ac, aid, _ = detector.detectMarkers(gray_img)
                if aid is not None and len(aid) > 0:
                    for j, c_raw in enumerate(ac):
                        results.append({"corners": c_raw[0] if len(c_raw.shape) > 2 else c_raw, "id": int(aid[j][0]), "is_stag": False})
            except Exception as e:
                self.log("Timer", f"ArUco Error: {e}")
        return results

    def process_frame(self, frame, capture_time, executor=None):
        if frame is None: return None
        start_proc = time.monotonic()
        
        aspect = self.config_manager.get("timer", "aspect_ratio") or "16:9"
        if aspect == "4:3":
            h, w = frame.shape[:2]
            target_w = int(h * 4 / 3)
            if w != target_w:
                frame = cv2.resize(frame, (target_w, h), interpolation=cv2.INTER_AREA)

        h, w = frame.shape[:2]; qw, qh = w//2, h//2; area_1_4 = qw*qh
        m_sys = self.config_manager.get("timer", "marker_system") or "Stag"
        m_thr = self.config_manager.get("timer", "marker_threshold") or 2
        flk_len = self.config_manager.get("timer", "flicker_length") or 150
        min_pct = self.config_manager.get("timer", "min_marker_percent") or 0.1
        ec_rate = self.config_manager.get("timer", "error_correction_rate") or 0.6
        h_dist = self.config_manager.get("timer", "hybrid_dist_threshold") or 20
        detect_mode = self.config_manager.get("timer", "detect_mode") or "Corrected"
        view_mode = self.config_manager.get("timer", "view_mode") or "Original"
        stag_lib = int(self.config_manager.get("timer", "stag_library") or 15)
        
        current_executor = executor if executor else self.executor
        maps = self.calib_cache.get((w, h)); mx, my = maps if maps else (None, None)
        if not maps: detect_mode = "Original"; view_mode = "Original"
        
        thickness, now = (4 if h >= 720 else 2), time.monotonic()
        self.current_fps = 1.0 / (now - self.last_frame_time) if now - self.last_frame_time > 0 else 0; self.last_frame_time = now
        
        def run_quad_task(idx):
            qx, qy = (idx % 2) * qw, (idx // 2) * qh
            q_orig = frame[qy:qy+qh, qx:qx+qw].copy()
            res_final = []; display_q = q_orig
            
            needs_undistort = (detect_mode in ["Corrected", "Hybrid"]) and mx is not None
            if needs_undistort:
                try:
                    q_undist = cv2.remap(q_orig, mx, my, cv2.INTER_LINEAR)
                    g_undist = cv2.cvtColor(q_undist, cv2.COLOR_BGR2GRAY)
                    u_list = self._detect_markers_in_image(g_undist, m_sys, stag_lib, ec_rate)
                    for r in u_list:
                        c_inv = r["corners"].copy()
                        for pt in c_inv:
                            ux, uy = int(pt[0]), int(pt[1])
                            if 0 <= ux < qw and 0 <= uy < qh: pt[0], pt[1] = mx[uy, ux], my[uy, ux]
                        res_final.append({"corners": c_inv, "id": r["id"], "is_stag": r["is_stag"], "color": (0, 255, 255)})
                    if view_mode == "Corrected": display_q = q_undist
                except: pass

            if detect_mode in ["Original", "Hybrid"]:
                try:
                    g_orig = cv2.cvtColor(q_orig, cv2.COLOR_BGR2GRAY)
                    o_list = self._detect_markers_in_image(g_orig, m_sys, stag_lib, ec_rate)
                    if detect_mode == "Hybrid":
                        for or_ in o_list:
                            is_dup = False; oc = np.mean(or_["corners"], axis=0)
                            for ur in [r for r in res_final if r["color"] == (0, 255, 255)]:
                                if ur["id"] == or_["id"] and ur["is_stag"] == or_["is_stag"]:
                                    if np.linalg.norm(oc - np.mean(ur["corners"], axis=0)) < h_dist: 
                                        is_dup = True; ur["color"] = (0, 255, 0); ur["corners"] = or_["corners"]; break
                            if not is_dup: or_["color"] = (255, 0, 0); res_final.append(or_)
                    else:
                        for or_ in o_list: or_["color"] = (255, 0, 0)
                        res_final = o_list
                except: pass
            return idx, res_final, display_q

        futures = [current_executor.submit(run_quad_task, i) for i in range(4)]; q_results = [None]*4; q_displays = [None]*4
        for f in futures:
            idx, res, disp = f.result(); q_results[idx] = res; q_displays[idx] = disp
        
        display_f = np.vstack((np.hstack((q_displays[0], q_displays[1])), np.hstack((q_displays[2], q_displays[3]))))
        if view_mode == "Source": display_f = cv2.resize(frame, (w, h))
        
        counts = {i: 0 for i in range(4)}; first_ids = {i: -1 for i in range(4)}; last_was_stag = {i: False for i in range(4)}
        for i in range(4):
            qx, qy = (i % 2) * qw, (i // 2) * qh
            for m in q_results[i]:
                c = m["corners"]; area_px = cv2.contourArea(c); area_pct = (area_px / area_1_4) * 100
                if area_pct < min_pct: continue
                counts[i] += 1
                if first_ids[i] == -1 or (m["is_stag"] and not last_was_stag[i]): first_ids[i], last_was_stag[i] = m["id"], m["is_stag"]
                if self.config_manager.get("timer", "show_marker_rectangle"):
                    draw_c = c.copy(); draw_c[:, 0] += qx; draw_c[:, 1] += qy; cx, cy = np.mean(draw_c[:, 0]), np.mean(draw_c[:, 1])
                    cv2.polylines(display_f, [np.int32(draw_c).reshape((-1, 1, 2))], True, m["color"], thickness); cv2.putText(display_f, f"ID:{m['id']}", (int(cx), int(cy)-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, m["color"], thickness//2)
        
        for i in range(4):
            state = self.cam_states[i]
            if counts[i] >= m_thr: state["is_in_gate"], state["last_marker_id"], state["flicker_endtime"] = True, first_ids[i], 0
            else:
                if state["is_in_gate"]:
                    if state["flicker_endtime"] == 0: state["flicker_endtime"] = now + (flk_len / 1000.0)
                    if now > state["flicker_endtime"]:
                        if self.on_lap_callback: self.on_lap_callback(i, capture_time, state["last_marker_id"])
                        state["is_in_gate"], state["rss_output"], state["flicker_endtime"] = False, True, 0
            state["current_count"] = counts[i]; state["loop_time"] = (time.monotonic() - start_proc) * 1000
            
        if view_mode != "Source": cv2.line(display_f, (qw, 0), (qw, h), (255, 255, 255), 1); cv2.line(display_f, (0, qh), (w, qh), (255, 255, 255), 1)
        if self.config_manager.get("timer", "show_detection_info"):
            for i in range(4):
                s = self.cam_states[i]; color = (0, 255, 0) if s["is_in_gate"] else (0, 165, 255)
                cv2.putText(display_f, f"Cam {i}: {s['current_count']}/{m_thr}", (int((i%2)*w/2+10), int((i//2)*h/2+35)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        self.cycle_count += 1; return display_f

    def run(self):
        self.running = True
        while self.running:
            with self.lock: g = self.grabber
            if g and g.new_frame_event.wait(timeout=0.1):
                raw_f, cap_t, exe = None, 0.0, None
                with self.lock:
                    if self.grabber == g:
                        raw_f = g.frame; cap_t = g.capture_time; g.new_frame_event.clear()
                        if self.executor and not getattr(self.executor, '_shutdown', False): exe = self.executor
                if raw_f is not None and exe is not None:
                    try:
                        self.display_frame = self.process_frame(raw_f.copy(), cap_t, executor=exe)
                        vcam_local = None
                        with self.lock: vcam_local = self.vcam
                        if vcam_local:
                            h, w = self.display_frame.shape[:2]; tw, th = self.vcam_w, self.vcam_h; canvas = np.zeros((th, tw, 3), dtype=np.uint8); scale = min(tw / w, th / h); nw, nh = int(w * scale), int(h * scale); v_frame = cv2.resize(self.display_frame, (nw, nh), interpolation=cv2.INTER_CUBIC); dx, dy = (tw - nw) // 2, (th - nh) // 2; canvas[dy:dy+nh, dx:dx+nw] = v_frame
                            with self.lock:
                                if self.vcam == vcam_local: vcam_local.send(canvas); vcam_local.sleep_until_next_frame()
                    except Exception as e:
                        if "shutdown" not in str(e): self.log("System", f"Loop Error: {e}")
            now = time.monotonic()
            if now - self.last_heartbeat_time >= 0.5:
                if self.on_heartbeat_callback: self.on_heartbeat_callback({"current_rssi": [200 if self.cam_states[i]["is_in_gate"] else 50 for i in range(4)], "frequency": self.config_manager.get("timer", "camera_frequencies") or [5705, 5740, 5800, 5820], "crossing_flag": [self.cam_states[i]["rss_output"] for i in range(4)], "loop_time": [self.cam_states[i]["loop_time"] for i in range(4)], "fps": self.current_fps})
                for i in range(4): self.cam_states[i]["rss_output"] = False
                self.last_heartbeat_time = now
            time.sleep(0.001)
        self.stop_resources()

    def stop_resources(self):
        with self.lock:
            if self.grabber: self.grabber.stop(); self.grabber = None
            if self.vcam:
                try: self.vcam.close()
                except: pass
                self.vcam = None
            if hasattr(self, 'executor'): self.executor.shutdown(wait=False)

    def start(self): self.cv_thread = threading.Thread(target=self.run, daemon=True); self.cv_thread.start()
    def stop(self): self.running = False; self.cv_thread.join(timeout=2.0) if self.cv_thread else None; self.stop_resources()
