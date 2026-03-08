import cv2
import numpy as np
import time
import threading
import stag
import math
import pyvirtualcam
import sys
import os
import json
from concurrent.futures import ThreadPoolExecutor

# --- OS 依存のインポート ---
if sys.platform == 'win32':
    try:
        from pygrabber.dshow_graph import FilterGraph
        HAS_DSHOW = True
    except ImportError:
        HAS_DSHOW = False
else:
    HAS_DSHOW = False

class FrameGrabber(threading.Thread):
    def __init__(self, device_id, width, height, target_fps):
        super().__init__(daemon=True)
        # device_id は cv2.CAP_DSHOW を含めたフラグ付きインデックス
        self.device_id, self.width, self.height, self.target_fps = device_id, width, height, target_fps
        self.frame = None; self.running = True; self.ret = False; self.cap = None; self.new_frame_event = threading.Event()
        self.capture_time = 0.0
    def run(self):
        try:
            max_retries = 5
            for i in range(max_retries):
                if not self.running: return
                
                # 指定されたデバイスを DSHOW モードで開く
                self.cap = cv2.VideoCapture(self.device_id)
                
                if not self.cap.isOpened():
                    # 失敗時のフォールバック (フラグを外して再試行)
                    base_id = self.device_id & 0xFF
                    self.cap = cv2.VideoCapture(base_id)

                if self.cap.isOpened():
                    print(f"Camera opened successfully on attempt {i+1}")
                    break
                else:
                    print(f"Camera Open Retry {i+1}/{max_retries}...")
                    if self.cap: self.cap.release(); self.cap = None
                    time.sleep(1.0)

            if not self.cap or not self.cap.isOpened():
                print("Failed to open camera after multiple attempts.")
                return

            # プロパティの設定 (DSHOW はオープン直後に行うのが鉄則)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            actual_w = self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            actual_h = self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
            print(f"Camera Active: {actual_w}x{actual_h} @ {actual_fps}fps")

            while self.running:
                if self.cap and self.cap.isOpened():
                    self.ret, frame = self.cap.read()
                    if self.ret:
                        self.capture_time = time.monotonic()
                        if frame.shape[1] != self.width or frame.shape[0] != self.height:
                            self.frame = cv2.resize(frame, (self.width, self.height))
                        else:
                            self.frame = frame
                        self.new_frame_event.set()
                    else: time.sleep(0.01)
                else: break
                time.sleep(0.001)
        except Exception as e: print(f"Camera Grabber Error: {e}")
        finally:
            if self.cap: self.cap.release(); self.cap = None
    def stop(self): self.running = False

class TimerManager:
    def __init__(self, config_manager, on_lap_callback=None, on_heartbeat_callback=None):
        self.config_manager = config_manager
        self.on_lap_callback = on_lap_callback
        self.on_heartbeat_callback = on_heartbeat_callback
        self.grabber = None; self.running = False; self.cv_thread = None
        self.vcam = None
        self.vcam_w, self.vcam_h = 1280, 720
        
        self.aruco_dict = self._load_or_create_aruco_dict()
        self.aruco_params = cv2.aruco.DetectorParameters()
        self.aruco_detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params)
        self.calib_cache = {}
        self._setup_calibration()

        tc = self.config_manager.get("timer", "thread_count") or 2
        self.executor = ThreadPoolExecutor(max_workers=tc)
        self.race_start_time = time.monotonic(); self.last_heartbeat_time = 0; self.last_frame_time = time.monotonic(); self.current_fps = 0.0
        self.cycle_count = 0
        self.cam_states = {i: {"is_in_gate": False, "last_marker_id": -1, "loop_time": 0.0, "rss_output": False, "current_count": 0, "flicker_endtime": 0} for i in range(4)}
        self.display_frame = None

    def _setup_calibration(self):
        cal_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "camera_calibration.json")
        if not os.path.exists(cal_path): return
        try:
            with open(cal_path, 'r') as f: cal = json.load(f)
            orig_mtx = np.array(cal['mtx']); dist = np.array(cal['dist'])
            cal_res = cal.get('resolution', [720, 960]); cal_h, cal_w = float(cal_res[0]), float(cal_res[1])
            targets = [(960, 720), (1440, 1080)]
            for tw, th in targets:
                qw, qh = tw // 2, th // 2
                mtx = orig_mtx.copy(); scale_x = qw / cal_w; scale_y = qh / cal_h
                mtx[0, 0] *= scale_x; mtx[1, 1] *= scale_y; mtx[0, 2] *= scale_x; mtx[1, 2] *= scale_y
                new_mtx, _ = cv2.getOptimalNewCameraMatrix(mtx, dist, (qw, qh), 1, (qw, qh))
                mx, my = cv2.initUndistortRectifyMap(mtx, dist, None, new_mtx, (qw, qh), cv2.CV_32FC1)
                self.calib_cache[(tw, th)] = (mx, my)
        except Exception as e: print(f"Calibration Load Error: {e}")

    def _load_or_create_aruco_dict(self):
        dict_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "aruco_dict_0_3.json")
        if os.path.exists(dict_path):
            try:
                with open(dict_path, 'r') as f: data = json.load(f)
                return cv2.aruco.Dictionary(np.array(data['bytesList'], dtype=np.uint8), data['markerSize'], data['maxCorrectionBits'])
            except: pass
        base_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        new_dict = cv2.aruco.Dictionary(base_dict.bytesList[:4], base_dict.markerSize, base_dict.maxCorrectionBits)
        try:
            save_data = {'bytesList': new_dict.bytesList.tolist(), 'markerSize': new_dict.markerSize, 'maxCorrectionBits': new_dict.maxCorrectionBits}
            with open(dict_path, 'w') as f: json.dump(save_data, f)
        except: pass
        return new_dict

    def set_race_start_time(self, start_time_mono): self.race_start_time = start_time_mono
    def get_input_devices(self):
        if HAS_DSHOW:
            try: return FilterGraph().get_input_devices()
            except: pass
        devices = []
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened(): devices.append(f"Camera {i}"); cap.release()
        return devices

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
        sorted_formats = {}
        for k in sorted(formats.keys(), key=lambda x: int(x.split('x')[0]), reverse=True): sorted_formats[k] = sorted(list(set(formats[k])), reverse=True)
        return sorted_formats

    def restart(self):
        print("TimerManager restart initiated...")
        tc = self.config_manager.get("timer", "thread_count") or 2
        if hasattr(self, 'executor'): self.executor.shutdown(wait=False)
        self.executor = ThreadPoolExecutor(max_workers=tc)
        
        if self.grabber: 
            self.grabber.stop()
            self.grabber.join(timeout=2.0) # Wait longer for release
            self.grabber = None
            
        if self.vcam: 
            v_tmp = self.vcam
            self.vcam = None # 先に None にしてループ側での使用を防ぐ
            v_tmp.close()
        
        t_cam = self.config_manager.get("timer", "target_camera_name")
        res_cfg = self.config_manager.get("timer", "resolution") or "1280x720"
        aspect = self.config_manager.get("timer", "aspect_ratio") or "16:9"
        fps = self.config_manager.get("timer", "target_fps") or 60
        
        try: rw, rh = map(int, res_cfg.split("x"))
        except: rw, rh = 1280, 720
        capture_w, capture_h = (int(rh*4/3), rh) if aspect=="4:3" else (rw, rh)

        if "1920" in res_cfg or rh >= 1080: self.vcam_w, self.vcam_h = 1920, 1080
        else: self.vcam_w, self.vcam_h = 1280, 720

        self._setup_calibration()
        if self.config_manager.get("timer", "virtual_camera_enabled"):
            try:
                self.vcam = pyvirtualcam.Camera(width=self.vcam_w, height=self.vcam_h, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR)
                print(f"Virtual Camera (16:9) started: {self.vcam_w}x{self.vcam_h} @ {fps}fps")
            except Exception as e: print(f"vCam Start Error: {e}")

        # Windowsの場合、DSHOWフラグを付与したIDを生成
        device_index = self.find_camera_id(t_cam)
        final_device_id = (device_index + cv2.CAP_DSHOW) if sys.platform == 'win32' else device_index
        
        self.grabber = FrameGrabber(final_device_id, capture_w, capture_h, fps)
        self.grabber.start()

    def _detect_markers_in_image(self, gray_img, marker_system, stag_lib, ec_rate):
        results = []
        if marker_system in ["Stag", "Hybrid"]:
            try: sc, sid, _ = stag.detectMarkers(gray_img, stag_lib)
            except: sid = None
            if sid is not None:
                for j, c_raw in enumerate(sc):
                    c = c_raw[0] if len(c_raw.shape) > 2 else c_raw
                    results.append({"corners": c, "id": int(sid[j][0]), "is_stag": True})
        if marker_system in ["ArUco", "Hybrid"]:
            self.aruco_params.errorCorrectionRate = ec_rate
            ac, aid, _ = self.aruco_detector.detectMarkers(gray_img)
            if aid is not None:
                for j, c_raw in enumerate(ac):
                    c = c_raw[0] if len(c_raw.shape) > 2 else c_raw
                    results.append({"corners": c, "id": int(aid[j][0]), "is_stag": False})
        return results

    def process_frame(self, frame, capture_time):
        start_proc = time.monotonic(); h, w = frame.shape[:2]; qw, qh = w//2, h//2; area_1_4 = qw*qh
        m_sys, m_thr, flk_len, min_pct, ec_rate, use_abs = self.config_manager.get("timer", "marker_system"), self.config_manager.get("timer", "marker_threshold"), self.config_manager.get("timer", "flicker_length"), self.config_manager.get("timer", "min_marker_percent") or 0.1, self.config_manager.get("timer", "error_correction_rate") or 0.6, self.config_manager.get("timer", "use_absolute_timestamp")
        show_det, show_fps, show_m_rect = self.config_manager.get("timer", "show_detection_info"), self.config_manager.get("timer", "show_fps"), self.config_manager.get("timer", "show_marker_rectangle")
        detect_mode = self.config_manager.get("timer", "detect_mode") or "Corrected"
        view_mode = self.config_manager.get("timer", "view_mode") or "Original"
        maps = self.calib_cache.get((w, h)); mx, my = maps if maps else (None, None)
        if not maps: detect_mode = "Original"; view_mode = "Original"
        stag_lib = int(self.config_manager.get("timer", "stag_library"))
        thickness, now = (4 if h >= 720 else 2), time.monotonic()
        self.current_fps = 1.0 / (now - self.last_frame_time) if now - self.last_frame_time > 0 else 0
        self.last_frame_time = now

        def run_quad_task(idx):
            qx, qy = (idx % 2) * qw, (idx // 2) * qh
            q_orig = frame[qy:qy+qh, qx:qx+qw].copy(); res_final = []
            needs_undistort = (detect_mode in ["Corrected", "Hybrid"]) and mx is not None
            if needs_undistort:
                q_undist = cv2.remap(q_orig, mx, my, cv2.INTER_LINEAR); g_undist = cv2.cvtColor(q_undist, cv2.COLOR_BGR2GRAY)
                u_list = self._detect_markers_in_image(g_undist, m_sys, stag_lib, ec_rate)
                for r in u_list:
                    c_inv = r["corners"].copy()
                    for pt in c_inv:
                        ux, uy = int(pt[0]), int(pt[1])
                        if 0 <= ux < qw and 0 <= uy < qh: pt[0], pt[1] = mx[uy, ux], my[uy, ux]
                    res_final.append({"corners": c_inv, "id": r["id"], "is_stag": r["is_stag"], "color": (0, 255, 255)})
                display_q = q_undist if view_mode == "Corrected" else q_orig
            else: display_q = q_orig
            if detect_mode in ["Original", "Hybrid"]:
                g_orig = cv2.cvtColor(q_orig, cv2.COLOR_BGR2GRAY); o_list = self._detect_markers_in_image(g_orig, m_sys, stag_lib, ec_rate)
                if detect_mode == "Hybrid":
                    for or_ in o_list:
                        is_dup = False; oc = np.mean(or_["corners"], axis=0)
                        for ur in [r for r in res_final if r["color"] == (0, 255, 255)]:
                            if ur["id"] == or_["id"] and ur["is_stag"] == or_["is_stag"]:
                                if np.linalg.norm(oc - np.mean(ur["corners"], axis=0)) < 20: is_dup = True; break
                        or_["color"] = (0, 255, 0) if is_dup else (255, 0, 0); res_final.append(or_)
                else:
                    for or_ in o_list: or_["color"] = (255, 0, 0)
                    if detect_mode == "Original": res_final = o_list
            return idx, res_final, display_q

        futures = [self.executor.submit(run_quad_task, i) for i in range(4)]
        q_results = [None]*4; q_displays = [None]*4
        for f in futures: idx, res, disp = f.result(); q_results[idx] = res; q_displays[idx] = disp
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
                if show_m_rect:
                    draw_c = c.copy(); draw_c[:, 0] += qx; draw_c[:, 1] += qy
                    cx, cy = np.mean(draw_c[:, 0]), np.mean(draw_c[:, 1])
                    cv2.polylines(display_f, [np.int32(draw_c).reshape((-1, 1, 2))], True, m["color"], thickness)
                    cv2.putText(display_f, f"ID:{m['id']} ({area_pct:.1f}%)", (int(cx), int(cy)-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, m["color"], thickness//2)

        for i in range(4):
            state = self.cam_states[i]
            if counts[i] >= m_thr: state["is_in_gate"], state["last_marker_id"], state["flicker_endtime"] = True, first_ids[i], 0
            else:
                if state["is_in_gate"]:
                    if state["flicker_endtime"] == 0: state["flicker_endtime"] = now + (flk_len / 1000.0)
                    if now > state["flicker_endtime"]:
                        lap_val = capture_time if use_abs else (capture_time - self.race_start_time)
                        if self.on_lap_callback: self.on_lap_callback(i, lap_val, state["last_marker_id"])
                        state["is_in_gate"], state["rss_output"], state["flicker_endtime"] = False, True, 0
            state["current_count"] = counts[i]; state["loop_time"] = (time.monotonic() - start_proc) * 1000
        if view_mode != "Source":
            cv2.line(display_f, (qw, 0), (qw, h), (255, 255, 255), 1); cv2.line(display_f, (0, qh), (w, qh), (255, 255, 255), 1)
        if show_det:
            for i in range(4):
                s = self.cam_states[i]; color = (0, 255, 0) if s["is_in_gate"] else (0, 165, 255)
                cv2.putText(display_f, f"Cam {i}: {s['current_count']}/{m_thr}", (int((i%2)*w/2+10), int((i//2)*h/2+35)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        self.cycle_count += 1
        return display_f

    def run(self):
        # 起動時の冗長な restart() 呼び出しを削除
        self.running = True
        print("TimerManager main loop started.")
        while self.running:
            if self.grabber and self.grabber.new_frame_event.wait(timeout=0.1):
                self.grabber.new_frame_event.clear(); raw_f = self.grabber.frame; cap_t = self.grabber.capture_time
                if raw_f is not None:
                    self.display_frame = self.process_frame(raw_f.copy(), cap_t)
                    
                    # vcam へのアクセスをスレッドセーフにする
                    vcam_local = self.vcam
                    if vcam_local:
                        try:
                            h, w = self.display_frame.shape[:2]
                            tw, th = self.vcam_w, self.vcam_h
                            canvas = np.zeros((th, tw, 3), dtype=np.uint8)
                            scale = min(tw / w, th / h)
                            nw, nh = int(w * scale), int(h * scale)
                            v_frame = cv2.resize(self.display_frame, (nw, nh), interpolation=cv2.INTER_CUBIC)
                            dx, dy = (tw - nw) // 2, (th - nh) // 2
                            canvas[dy:dy+nh, dx:dx+nw] = v_frame
                            vcam_local.send(canvas); vcam_local.sleep_until_next_frame()
                        except: pass # Restart 中のクローズなど
            now = time.monotonic()
            if now - self.last_heartbeat_time >= 0.5:
                if self.on_heartbeat_callback:
                    self.on_heartbeat_callback({"current_rssi": [200 if self.cam_states[i]["is_in_gate"] else 50 for i in range(4)], "frequency": self.config_manager.get("timer", "camera_frequencies") or [5705, 5800, 5845, 5885], "crossing_flag": [self.cam_states[i]["rss_output"] for i in range(4)], "loop_time": [self.cam_states[i]["loop_time"] for i in range(4)], "fps": self.current_fps})
                for i in range(4): self.cam_states[i]["rss_output"] = False
                self.last_heartbeat_time = now
            time.sleep(0.001)
        if self.grabber: self.grabber.stop()
        if self.vcam: self.vcam.close(); self.vcam = None
    def start(self): self.cv_thread = threading.Thread(target=self.run, daemon=True); self.cv_thread.start()
    def stop(self): 
        self.running = False
        if self.cv_thread and self.cv_thread.is_alive():
            self.cv_thread.join(timeout=2.0)
