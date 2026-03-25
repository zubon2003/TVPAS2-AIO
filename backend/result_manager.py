import os
import json
import time
import re
import requests
from datetime import datetime, timezone, timedelta

class ResultManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.log_callback = None
        self._cache = {
            "meta": {}
        }
        self.race_start_mono = 0.0
        self.race_stop_time = 0.0
        self.is_race_active = False
        self.realtime_race_state = {}
        self.processed_official_laps = set()
        self.reset_realtime_state()

    def reset_realtime_state(self):
        if self.is_race_active: self.race_stop_time = time.monotonic()
        self.is_race_active = False
        self.processed_official_laps.clear()
        self.realtime_race_state = {
            i: {
                "pilot_name": f"パイロット {i+1}", "is_active": False, "lap_count": -1,
                "last_sector": 0, "last_gate_time_abs": 0.0, "last_goal_time_abs": 0.0,
                "lap_start_mono": 0.0, "sectors": {"s1": None, "s2": None, "s3": None, "s4": None},
                "history": [], "is_holeshot_active": False, "is_finished": False,
                "position": i + 1, "pilot_id": None
            } for i in range(4)
        }
        self.update_all()

    def get_realtime_state_all(self):
        return [{**state, "seat": seat} for seat, state in self.realtime_race_state.items()]

    def resolve_names_from_packet(self, data):
        """ts_race_stage から名前とIDを解決して Seat に紐付ける"""
        self.log("System", "Mapping pilot names from packet...")
        for i in range(4): self.realtime_race_state[i]["is_active"] = False
        
        # PocketBase の ID 変換用マップを作成 (内部的な紐付けにのみ使用)
        try:
            pb_pilots = self.fetch_pb("pilots", {"perPage": 500})
            uuid_to_pb_id = {p.get("sourceId"): p.get("id") for p in pb_pilots if p.get("sourceId")}
        except:
            uuid_to_pb_id = {}
        
        if isinstance(data, dict):
            # 新しい辞書形式: {"p": ["name1",...], "p_id": ["id1",...]}
            names = data.get("p", [])
            ids = data.get("p_id", [])
            for i in range(min(4, len(names))):
                name = names[i]
                uuid = ids[i] if i < len(ids) else None
                pb_id = uuid_to_pb_id.get(uuid)
                
                self.realtime_race_state[i].update({
                    "pilot_name": name, # 名前はパケットのものを優先
                    "pilot_id": pb_id,  # SSEとの紐付け用に PB ID を保持
                    "is_active": True
                })
                self.log("System", f"Mapped Seat {i}: {name} (PB_ID: {pb_id if pb_id else 'Not Found'})")
        
        elif isinstance(data, list):
            # 従来のリスト形式
            for i, info in enumerate(data):
                if i >= 4: break
                if not isinstance(info, dict): continue
                name = info.get("pilot_name") or info.get("name") or f"パイロット {i+1}"
                uuid = info.get("pilot_id")
                pb_id = uuid_to_pb_id.get(uuid)
                
                self.realtime_race_state[i].update({
                    "pilot_name": name,
                    "pilot_id": pb_id,
                    "is_active": True
                })
                self.log("System", f"Mapped Seat {i}: {name} (PB_ID: {pb_id if pb_id else 'Not Found'})")

    def process_realtime_lap(self, port, data):
        """セクターゲート (Gate 2, 3, 4) の生データ処理"""
        active_ports = {}
        for i in range(1, 5):
            p = self.config_manager.get("relay", f"server{i}_port")
            if p and p > 0: active_ports[p] = i
        server_idx = active_ports.get(port)
        if not server_idx or server_idx == 1: return None
        seat = data.get("seat", 0)
        state = self.realtime_race_state.get(seat)
        if not state or not state["is_active"] or state["is_finished"]: return None
        now_abs = float(data.get("lap_time", time.monotonic()))
        delta = now_abs - (state["last_gate_time_abs"] if state["last_gate_time_abs"] > 0 else self.race_start_mono)
        sector_map = {2: 1, 3: 2, 4: 3, 1: 4}
        current_s_idx = sector_map[server_idx]
        state["last_gate_time_abs"], state["last_sector"] = now_abs, current_s_idx
        state["sectors"][f"s{current_s_idx}"] = delta
        
        # 暫定順位の再計算 (ラップ数 > 通過セクター番号 > 通過時刻 の順でソート)
        ranking = sorted(range(4), key=lambda x: (
            -self.realtime_race_state[x]["lap_count"], 
            -self.realtime_race_state[x]["last_sector"],
            self.realtime_race_state[x]["last_gate_time_abs"] if self.realtime_race_state[x]["last_gate_time_abs"] > 0 else 9999999999.0
        ))
        for i, s_id in enumerate(ranking):
            self.realtime_race_state[s_id]["position"] = i + 1
        
        return {"event": "sector_update", "seat": seat, "pilot_name": state["pilot_name"], "server": server_idx, "sector_index": current_s_idx, "lap_time": delta, "position": state["position"], "is_finished": False}

    def process_official_lap(self, lap_record):
        """PocketBase の公式ラップ処理 (detections.valid フラグをチェック)"""
        lap_id = lap_record.get("id")
        if not lap_id or lap_id in self.processed_official_laps: return None
        det_id = lap_record.get("detection")
        det_record = self.fetch_pb_single("detections", det_id)
        if not det_record or not det_record.get("valid"): return None
        pid = det_record.get("pilot")
        seat = next((s_id for s_id, s_data in self.realtime_race_state.items() if s_data["pilot_id"] == pid), -1)
        if seat == -1: return None
        self.processed_official_laps.add(lap_id)
        state = self.realtime_race_state[seat]
        lap_num, lap_time = lap_record.get("lapNumber", 0), float(lap_record.get("lengthSeconds", 0.0))
        state["lap_count"] = lap_num
        state["last_gate_time_abs"] = state["last_goal_time_abs"] = time.monotonic()
        state["history"].append(lap_time) # HS (0) も 1周目以降もここに追加される
        state["sectors"] = {"s1": None, "s2": None, "s3": None, "s4": None}
        
        target_laps = int(self._cache.get("meta", {}).get("event_obj", {}).get("laps") or 3)
        if state["lap_count"] >= target_laps: state["is_finished"] = True
        
        ranking = sorted(range(4), key=lambda x: (-self.realtime_race_state[x]["lap_count"], -self.realtime_race_state[x]["last_sector"]))
        for i, s_id in enumerate(ranking): self.realtime_race_state[s_id]["position"] = i + 1
        
        # 全ラップ（HS含む）のトータルタイムを計算
        total_time = sum(state["history"])
        
        return {
            "event": "lap_update",
            "seat": seat,
            "pilot_name": state["pilot_name"],
            "server": 1,
            "lap_number": lap_num,
            "lap_time": lap_time,
            "total_time": total_time,
            "is_holeshot": (lap_num == 0),
            "is_finished": state["is_finished"],
            "position": state["position"]
        }

    @property
    def pb_base_url(self):
        port = self.config_manager.get("global", "drone_dashboard_port") or 8089
        return f"http://localhost:{port}/api"

    def log(self, cat, msg):
        if self.log_callback: self.log_callback(cat, msg)

    def fetch_pb(self, collection, params=None):
        url = f"{self.pb_base_url}/collections/{collection}/records"
        if params is None: params = {}
        params["_t"] = int(time.time() * 1000)
        try:
            resp = requests.get(url, params=params, timeout=3)
            if resp.status_code == 200: return resp.json().get("items", [])
        except: pass
        return []

    def fetch_pb_single(self, collection, record_id):
        url = f"{self.pb_base_url}/collections/{collection}/records/{record_id}"
        try:
            resp = requests.get(url, params={"_t": int(time.time() * 1000)}, timeout=2)
            if resp.status_code == 200: return resp.json()
        except: pass
        return None

    def calculate_metrics_for_laps(self, plens, consec_n, target_n):
        res = {"bestLap": 999.0, "consecutive": 999.0, "totalRace": 9999.0, "totalTime": sum(plens)}
        if plens:
            res["bestLap"] = min(plens)
            if len(plens) >= consec_n:
                res["consecutive"] = min([sum(plens[i:i+consec_n]) for i in range(len(plens)-consec_n+1)])
            if len(plens) >= target_n:
                res["totalRace"] = sum(plens[:target_n])
        return res

    def update_all(self):
        try:
            events = self.fetch_pb("events", params={"filter": "isCurrent = true"})
            if not events: events = self.fetch_pb("events", params={"sort": "-updated"})
            if not events: return False
            event_obj = events[-1]
            self._cache["meta"] = {"event_obj": event_obj, "event_name": event_obj.get("name")}
            return True
        except: return False

    def parse_duration_to_seconds(self, ds):
        if not ds: return 0.0
        try:
            s = str(ds).strip()
            if ":" in s:
                parts = s.split(":")
                return float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
            return float(s)
        except: return 0.0

    def get_all_sheets_data(self):
        """スプレッドシート向けの全データを集計して返す"""
        try:
            self.update_all()
            meta = self._cache.get("meta", {}).get("event_obj", {})
            eid = meta.get("id")
            if not eid: return None
            event_name = meta.get("name", "Unknown")
            pilots = {p["id"]: p for p in self.fetch_pb("pilots", {"perPage": 500})}
            races = self.fetch_pb("races", params={"filter": f"event = '{eid}' && valid = true", "sort": "start", "perPage": 500})
            r_dict = {r["id"]: r for r in races}
            rounds = {r["id"]: r for r in self.fetch_pb("rounds", params={"filter": f"event = '{eid}'", "perPage": 200})}
            laps_all = self.fetch_pb("laps", params={"filter": f"event = '{eid}'", "perPage": 5000})
            detections = {d["id"]: d for d in self.fetch_pb("detections", params={"filter": f"event = '{eid}'", "perPage": 5000})}
            results_all = self.fetch_pb("results", params={"filter": f"event = '{eid}'", "perPage": 2000})
            d_to_p = {d["id"]: d["pilot"] for d in detections.values() if d.get("pilot")}
            res_lookup = {(res["race"], res["pilot"]): res for res in results_all}
            
            p_race_map = {} # pid -> rid -> {lnum: time}
            all_individual_laps = []
            
            for l in laps_all:
                det = detections.get(l.get("detection"), {})
                if not det or not det.get("valid"): continue
                
                rid, pid = l.get("race"), d_to_p.get(l.get("detection"))
                ln, lt = l.get("lapNumber", 0), float(l.get("lengthSeconds", 0.0))
                
                if rid and pid:
                    if pid not in p_race_map: p_race_map[pid] = {}
                    if rid not in p_race_map[pid]: p_race_map[pid][rid] = {}
                    p_race_map[pid][rid][ln] = lt

            race_result_rows = []
            pilot_stats = [] # name, b1, b2, b3, brace
            target_laps_global = int(meta.get("laps", 3))

            for pid, p_info in pilots.items():
                p_name = p_info.get("name", "???")
                pb1, pb2, pb3, pbrace = {"val": 999.0, "heat": "---"}, {"val": 999.0, "heat": "---"}, {"val": 999.0, "heat": "---"}, {"val": 9999.0, "heat": "---"}
                
                for rid, laps_dict in p_race_map.get(pid, {}).items():
                    if rid not in r_dict: continue
                    r_obj = r_dict[rid]
                    rnd = rounds.get(r_obj.get("round"), {})
                    heat_name = f"Race{rnd.get('roundNumber', 0)}-{r_obj.get('raceNumber', 0)}"
                    
                    # 開始日時の取得とフォーマット (PocketBaseの値 "2023-10-27 10:00:00.000Z" 等を想定)
                    r_start = r_obj.get("start", "")
                    race_date = ""
                    race_time = ""
                    if r_start:
                        try:
                            # "YYYY-MM-DD HH:MM:SS" の部分だけを取り出す
                            base_dt = r_start.split(".")[0].replace("Z", "").strip()
                            if " " in base_dt:
                                d_part, t_part = base_dt.split(" ")
                                race_date = d_part.replace("-", "/") # YYYY/MM/DD
                                race_time = t_part                   # HH:MM:SS
                            else:
                                race_date = base_dt
                        except:
                            race_date = r_start
                    
                    l_list = [laps_dict[n] for n in sorted(laps_dict.keys()) if n >= 1]
                    for l_val in l_list:
                        all_individual_laps.append({"name": p_name, "val": l_val, "heat": heat_name})
                    
                    if not l_list: continue
                    
                    m3 = self.calculate_metrics_for_laps(l_list, 3, r_obj.get("targetLaps", 3))
                    m2 = self.calculate_metrics_for_laps(l_list, 2, r_obj.get("targetLaps", 3))
                    pr = res_lookup.get((rid, pid), {})
                    
                    b2_val = m2["consecutive"] if m2["consecutive"] < 900 else ""
                    b3_val = m3["consecutive"] if m3["consecutive"] < 900 else ""
                    
                    row = [event_name, heat_name, race_date, race_time, p_name, pr.get("position", ""), len(l_list), sum(l_list), m3["totalRace"], m3["bestLap"], b2_val, b3_val, laps_dict.get(0, "")]
                    for i in range(1, 31): row.append(laps_dict.get(i, ""))
                    race_result_rows.append(row)
                    
                    if m3["bestLap"] < pb1["val"]: pb1 = {"val": m3["bestLap"], "heat": heat_name}
                    if m2["consecutive"] < pb2["val"]: pb2 = {"val": m2["consecutive"], "heat": heat_name}
                    if m3["consecutive"] < pb3["val"]: pb3 = {"val": m3["consecutive"], "heat": heat_name}
                    if m3["totalRace"] < pbrace["val"]: pbrace = {"val": m3["totalRace"], "heat": heat_name}
                
                if pb1["val"] < 900:
                    pilot_stats.append({"name": p_name, "b1": pb1, "b2": pb2, "b3": pb3, "brace": pbrace})

            # RaceResult を日付 (index 2) と時刻 (index 3) でソート
            race_result_rows.sort(key=lambda x: (x[2], x[3]))

            return {
                "lapsToDo": target_laps_global,
                "RaceResult": race_result_rows,
                "Ranking_BestLap": [[i+1, s["name"], s["b1"]["val"], s["b1"]["heat"]] for i, s in enumerate(sorted(pilot_stats, key=lambda x: x["b1"]["val"])[:20])],
                "Ranking_Consecutive2Lap": [[i+1, s["name"], s["b2"]["val"], s["b2"]["heat"]] for i, s in enumerate(sorted(pilot_stats, key=lambda x: x["b2"]["val"])[:20]) if s["b2"]["val"] < 900],
                "Ranking_Consecutive3Lap": [[i+1, s["name"], s["b3"]["val"], s["b3"]["heat"]] for i, s in enumerate(sorted(pilot_stats, key=lambda x: x["b3"]["val"])[:20]) if s["b3"]["val"] < 900],
                "Ranking_RaceTime": [[i+1, s["name"], s["brace"]["val"], s["brace"]["heat"]] for i, s in enumerate(sorted(pilot_stats, key=lambda x: x["brace"]["val"])[:20]) if s["brace"]["val"] < 9000],
                "Ranking_MinimumLap": [[i+1, l["name"], l["val"], l["heat"]] for i, l in enumerate(sorted(all_individual_laps, key=lambda x: x["val"])[:50])]
            }
        except Exception as e:
            self.log("System", f"Sheet Data Error: {e}")
            return None
