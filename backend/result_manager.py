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
            "ranking": [], "pmap": {}, "meta": {}, 
            "next_heat": {"name": "---", "pilots": []}, 
            "last_heat": {"name": "---", "pilots": []}
        }
        self.race_start_mono = 0.0
        self.realtime_race_state = {}
        self.reset_realtime_state()

    def reset_realtime_state(self):
        """レース状態の完全リセット。デフォルト名を廃止し、is_active フラグを導入"""
        self.realtime_race_state = {
            i: {
                "pilot_name": None, # 初期値は None
                "is_active": False,  # 正式にアサインされた場合のみ True
                "lap_count": -1,
                "last_sector": 0,
                "last_gate_time_abs": 0.0,
                "last_goal_time_abs": 0.0,
                "lap_start_mono": 0.0,
                "sectors": {"s1": None, "s2": None, "s3": None, "s4": None},
                "history": [],
                "is_holeshot_active": False,
                "is_finished": False,
                "position": i + 1,
                "pilot_id": None
            } for i in range(4)
        }
        self.update_all()
        
        # フォールバック: データベースの現在のヒート情報からアサイン
        try:
            cam_freqs = self.config_manager.get("timer", "camera_frequencies") or []
            active_pilots = self._cache.get("next_heat", {}).get("pilots", [])
            if not active_pilots: active_pilots = self._cache.get("last_heat", {}).get("pilots", [])
            
            for p in active_pilots:
                p_freq = p.get("frequency")
                if p_freq and int(p_freq) in cam_freqs:
                    seat = cam_freqs.index(int(p_freq))
                    if seat < 4:
                        self.realtime_race_state[seat]["pilot_name"] = p.get("pilotName")
                        self.realtime_race_state[seat]["pilot_id"] = p.get("pilotId")
                        self.realtime_race_state[seat]["is_active"] = True # アクティブ化
        except: pass
        self.log("System", "Realtime state reset (strict filtering active).")

    def get_realtime_state_all(self):
        return [{**state, "seat": seat} for seat, state in self.realtime_race_state.items()]

    def resolve_names_from_packet(self, pilot_list):
        """FPVTracksideからのパケットを解析し、アサインされたパイロットのみをアクティブにする"""
        try:
            cam_freqs = self.config_manager.get("timer", "camera_frequencies") or []
            # パケットが来たら一旦全シートの active を解除（packetを最優先）
            for i in range(4): self.realtime_race_state[i]["is_active"] = False

            for p_info in pilot_list:
                if not isinstance(p_info, dict): continue
                name = p_info.get("Name") or p_info.get("Pilot") or p_info.get("pilotName") or p_info.get("pilot_name")
                freq = p_info.get("Frequency") or p_info.get("frequency") or p_info.get("f")
                
                if name and freq:
                    f_int = int(freq)
                    if f_int in cam_freqs:
                        seat = cam_freqs.index(f_int)
                        if seat < 4:
                            self.realtime_race_state[seat]["pilot_name"] = name
                            self.realtime_race_state[seat]["is_active"] = True # 正式にアサイン
                            self.log("System", f"Race Assigned: Seat {seat} -> {name}")
        except Exception as e:
            self.log("System", f"Packet Assignment Error: {e}")

    def process_realtime_lap(self, port, lap_data):
        seat = lap_data.get("seat", 0)
        if seat not in self.realtime_race_state: return None
        state = self.realtime_race_state[seat]
        
        # 【重要】正式にアサインされていないシートの信号は無視する
        if not state["is_active"]: return None
        
        if state["is_finished"]: return None

        now_abs = time.monotonic()
        active_ports = {}
        for i in range(1, 5):
            p = self.config_manager.get("relay", f"server{i}_port")
            if p and p > 0: active_ports[p] = i
        if port not in active_ports: return None
        server_idx = active_ports[port]
        sector_map = {2: 1, 3: 2, 4: 3, 1: 4}; current_s_idx = sector_map[server_idx]

        events = self._cache.get("meta", {}).get("event_obj", {})
        try: min_lap_time = float(events.get("minLapTime") or 3.0)
        except: min_lap_time = 3.0
        try: target_laps = int(events.get("laps") or 3)
        except: target_laps = 3

        if state["last_sector"] == current_s_idx and (now_abs - state["last_gate_time_abs"]) < 1.0:
            return None

        if state["last_gate_time_abs"] > 0: calculated_delta = now_abs - state["last_gate_time_abs"]
        else: calculated_delta = (now_abs - self.race_start_mono) if self.race_start_mono > 0 else lap_data.get("lap_time", 0.0)
            
        state["last_gate_time_abs"] = now_abs
        state["last_sector"] = current_s_idx
        state["sectors"][f"s{current_s_idx}"] = calculated_delta

        update_data = {
            "seat": seat, "pilot_name": state["pilot_name"], "server": server_idx,
            "sector_index": current_s_idx, "lap_time": calculated_delta, "event": "sector_update",
            "sectors": state["sectors"].copy(), "history": state["history"],
            "lap_number": state["lap_count"], "is_holeshot": state["is_holeshot_active"],
            "is_finished": state["is_finished"], "lap_start_mono": state["lap_start_mono"],
            "server_now": now_abs
        }

        if server_idx == 1: # Goal
            if state["last_goal_time_abs"] > 0 and (now_abs - state["last_goal_time_abs"]) < min_lap_time:
                return None

            is_holeshot_cfg = (events.get("primaryTimingSystemLocation") == "Holeshot")
            if state["lap_count"] == -1:
                if is_holeshot_cfg:
                    state["lap_count"], state["is_holeshot_active"] = 0, True
                    final_lap_total = (now_abs - self.race_start_mono) if self.race_start_mono > 0 else calculated_delta
                else:
                    state["lap_count"], state["is_holeshot_active"] = 1, False
                    final_lap_total = (now_abs - self.race_start_mono) if self.race_start_mono > 0 else calculated_delta
            else:
                state["lap_count"] += 1; state["is_holeshot_active"] = False
                final_lap_total = now_abs - state["last_goal_time_abs"]

            state["last_goal_time_abs"] = now_abs
            state["lap_start_mono"] = now_abs
            state["history"].append(final_lap_total)
            
            if state["lap_count"] >= target_laps: state["is_finished"] = True

            update_data.update({
                "event": "lap_update", "lap_number": state["lap_count"],
                "is_holeshot": state["is_holeshot_active"], "final_sectors": state["sectors"].copy(),
                "lap_time": final_lap_total, "is_finished": state["is_finished"],
                "lap_start_mono": state["lap_start_mono"], "history": state["history"]
            })
            state["sectors"] = {"s1": None, "s2": None, "s3": None, "s4": None}
            # ラップ加算時、セクター位置をスタートライン(0)にリセットする
            state["last_sector"] = 0

        # 順位計算 (is_active を最優先キーにする)
        ranking = sorted(range(4), key=lambda x: (
            -self.realtime_race_state[x]["is_active"], # アクティブな人を上に
            -self.realtime_race_state[x]["lap_count"],
            -self.realtime_race_state[x]["last_sector"],
            self.realtime_race_state[x]["last_gate_time_abs"]
        ))

        all_pos = {}
        for i, s_id in enumerate(ranking):
            self.realtime_race_state[s_id]["position"] = i + 1
            all_pos[s_id] = i + 1
        update_data["position"] = state["position"]; update_data["all_positions"] = all_pos
        return update_data

    @property
    def pb_base_url(self):
        port = self.config_manager.get("global", "drone_dashboard_port") or 8089
        return f"http://localhost:{port}/api"

    def log(self, cat, msg):
        if self.log_callback: self.log_callback(cat, msg)
        else: print(f"[{cat}] {msg}")

    def fetch_pb(self, collection, params=None):
        url = f"{self.pb_base_url}/collections/{collection}/records"
        try:
            resp = requests.get(url, params=params, timeout=3)
            if resp.status_code == 200: return resp.json().get("items", [])
        except: pass
        return []

    def load_local_pilot_photos(self, event_source_id):
        fpv_root = self.config_manager.get("result_formatter", "fpvtrackside_dir_path")
        if not fpv_root or not event_source_id: return {}
        photo_map = {}
        try:
            path = os.path.join(fpv_root, "Events", event_source_id, "Pilots.json")
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for p in data:
                        p_id = str(p.get("ID")).lower().strip().replace("-", "")
                        photo_map[p_id] = p.get("PhotoPath") or p.get("Photo")
        except: pass
        return photo_map

    def calculate_metrics_for_laps(self, laps_list, consec_n, target_n):
        plens = []
        if laps_list and isinstance(laps_list[0], dict):
            laps_sorted = sorted(laps_list, key=lambda x: (x.get("lapNumber", 0), x.get("id", "")))
            seen = set()
            for lap in laps_sorted:
                ln = lap.get("lapNumber", 0)
                if ln not in seen and ln >= 1:
                    plens.append(lap["lengthSeconds"]); seen.add(ln)
        else: plens = laps_list
        res = {"bestLap": 999.0, "consecutive": 999.0, "firstLap": 9999.0, "totalRace": 9999.0, "totalLaps": len(plens), "totalTime": sum(plens)}
        if plens:
            res["bestLap"] = min(plens)
            if len(plens) >= consec_n:
                res["consecutive"] = min([sum(plens[i : i + consec_n]) for i in range(len(plens) - consec_n + 1)])
                res["firstLap"] = sum(plens[:consec_n])
            if len(plens) >= target_n: res["totalRace"] = sum(plens[:target_n])
        return res

    def is_race_finished(self, r):
        end_time = r.get("end")
        if not end_time: return False
        s = str(end_time).strip()
        return not (s == "" or s.startswith("0001") or s == "null")

    def update_all(self):
        try:
            events = self.fetch_pb("events", params={"filter": "isCurrent = true"})
            if not events: events = self.fetch_pb("events", params={"sort": "-updated"})
            if not events: return False
            event_obj = events[-1]; eid = event_obj["id"]; source_eid = event_obj.get("sourceId")
            sort_by = self.config_manager.get("result_formatter", "sorted_by") or "consecutive"
            round_id = self.config_manager.get("result_formatter", "leaderboard_round") or "all"
            consec_n = self.config_manager.get("result_formatter", "consecutive_n") or 3
            local_photos = self.load_local_pilot_photos(source_eid)
            pilots_list = self.fetch_pb("pilots", {"perPage": 500})
            pmap = {p["id"]: {**p, "photoPath": local_photos.get(str(p.get("sourceId", "")).lower().strip().replace("-", "")) or p.get("photoPath")} for p in pilots_list}
            races_all = self.fetch_pb("races", params={"filter": f"event = '{eid}'", "sort": "raceOrder,raceNumber", "perPage": 500})
            r_dict = {r["id"]: r for r in races_all}
            detections = {d["id"]: d for d in self.fetch_pb("detections", params={"filter": f"event = '{eid}'", "perPage": 5000})}
            laps_all = self.fetch_pb("laps", params={"filter": f"event = '{eid}'", "perPage": 5000})
            rounds_map = {r["id"]: r for r in self.fetch_pb("rounds", params={"filter": f"event = '{eid}'"})}
            pilot_channels = self.fetch_pb("pilotChannels", params={"filter": f"event = '{eid}'", "perPage": 2000})
            channels = {c["id"]: c for c in self.fetch_pb("channels")}
            d_to_p = {d["id"]: d["pilot"] for d in detections.values() if d.get("pilot")}
            
            p_race_map = {}
            for l in laps_all:
                rid = l.get("race"); pid = d_to_p.get(l.get("detection"))
                if rid and pid:
                    if pid not in p_race_map: p_race_map[pid] = {}
                    if rid not in p_race_map[pid]: p_race_map[pid][rid] = []
                    p_race_map[pid][rid].append(l)

            # 現在進行中のレースを特定し、リアルタイム状態を同期
            next_race = next((r for r in sorted(races_all, key=lambda x: (x["raceOrder"], x["raceNumber"])) if r.get("valid") and not self.is_race_finished(r)), None)
            if next_race:
                curr_rid = next_race["id"]
                pcs = [pc for pc in pilot_channels if pc.get("race") == curr_rid]
                cam_freqs = self.config_manager.get("timer", "camera_frequencies") or []
                for pc in pcs:
                    pid = pc.get("pilot"); chid = pc.get("channel")
                    freq = channels.get(chid, {}).get("frequency")
                    if freq and int(freq) in cam_freqs:
                        seat = cam_freqs.index(int(freq))
                        official_laps = p_race_map.get(pid, {}).get(curr_rid, [])
                        if official_laps:
                            laps_sorted = sorted(official_laps, key=lambda x: x.get("lapNumber", 0))
                            official_history = [l.get("lengthSeconds", 0.0) for l in laps_sorted]
                            self.realtime_race_state[seat]["history"] = official_history
                            self.realtime_race_state[seat]["lap_count"] = laps_sorted[-1].get("lapNumber", 0)
                            self.realtime_race_state[seat]["pilot_name"] = pmap.get(pid, {}).get("name", self.realtime_race_state[seat]["pilot_name"])
                            self.realtime_race_state[seat]["is_active"] = True # ここでもアクティブ化を確認

            if round_id == "all":
                target_ids = {r["id"] for r in races_all if r.get("valid") is not False}
            elif str(round_id).startswith("type:"):
                target_type = round_id.split(":", 1)[1]
                target_rounds = {rid for rid, r in rounds_map.items() if r.get("eventType") == target_type}
                target_ids = {r["id"] for r in races_all if r.get("round") in target_rounds}
            else:
                target_ids = {round_id} if round_id in rounds_map else {r["id"] for r in races_all if r.get("round") == round_id}
            
            disp_ranking = []
            for pid, r_data in p_race_map.items():
                p_target_rids = [rid for rid in r_data if rid in target_ids]
                if not p_target_rids:
                    # このパイロットのラップがターゲットレースに含まれていない場合
                    continue
                
                b = {"bestLap": 999.0, "consecutive": 999.0, "firstLap": 9999.0, "totalRace": 9999.0, "totalLaps": 0, "totalTime": 0.0}
                for rid in p_target_rids:
                    m = self.calculate_metrics_for_laps(r_data[rid], consec_n, r_dict[rid].get("targetLaps", consec_n))
                    for k in ["bestLap", "consecutive", "firstLap", "totalRace"]: b[k] = min(b[k], m[k])
                    b["totalLaps"] += m["totalLaps"]; b["totalTime"] += m["totalTime"]
                
                t_val = b.get(sort_by, 9999)
                d_time = (f"{b['totalLaps']} Laps {b['totalTime']:.3f}s" if sort_by == "totalLaps" else f"{t_val:.3f}s") if t_val < 9000 else ""
                disp_ranking.append({"id": pid, "name": pmap[pid].get("name", "???"), "time": t_val if t_val < 9000 else None, "displayTime": d_time, "count": b["totalLaps"], "stats": b})
            
            if sort_by == "totalLaps": disp_ranking.sort(key=lambda x: (-x["count"], x["stats"]["totalTime"]))
            else: disp_ranking.sort(key=lambda x: (x["time"] is None, x["time"]))
            
            rank_lookup = {r["id"]: i+1 for i, r in enumerate(disp_ranking)}
            def build_pilot_list(race_obj, mode):
                if not race_obj: return []
                p_list = []
                pcs = [pc for pc in pilot_channels if pc.get("race") == race_obj["id"]]
                for pc in pcs:
                    pid = pc.get("pilot"); chid = pc.get("channel")
                    if pid in pmap:
                        ch_obj = channels.get(chid, {})
                        m_curr = self.calculate_metrics_for_laps(p_race_map.get(pid, {}).get(race_obj["id"], []), consec_n, race_obj.get("targetLaps", consec_n))
                        display_val = f"RECORD: {m_curr['bestLap']:.3f}s" if m_curr['bestLap'] < 900 else "RECORD: NO RECORD"
                        p_list.append({"pilotId": pid, "pilotName": pmap[pid].get("name", "???"), "photopath": pmap[pid].get("photoPath", ""), "band": ch_obj.get("displayName", "---")[:2], "frequency": ch_obj.get("frequency", 9999), "rank": rank_lookup.get(pid), "displayValue": display_val})
                p_list.sort(key=lambda x: x.get("frequency", 9999))
                return p_list

            last_race = next((r for r in sorted(races_all, key=lambda x: x.get("end", ""), reverse=True) if r.get("valid") and self.is_race_finished(r)), None)

            self._cache = {
                "ranking": disp_ranking, 
                "next_heat": {"name": f"Race{rounds_map.get(next_race['round'], {}).get('roundNumber', 0)}-{next_race['raceNumber']}" if next_race else "---", "pilots": build_pilot_list(next_race, "next")},
                "last_heat": {"name": f"Race{rounds_map.get(last_race['round'], {}).get('roundNumber', 0)}-{last_race['raceNumber']}" if last_race else "---", "pilots": build_pilot_list(last_race, "result")},
                "meta": {"sort": sort_by, "event_name": event_obj.get("name"), "event_obj": event_obj}
            }
            return True
        except Exception as e:
            self.log("System", f"Update Error: {e}"); return False

    def get_round_list(self, event_id_unused=None):
        try:
            events = self.fetch_pb("events", params={"filter": "isCurrent = true"})
            if not events: events = self.fetch_pb("events", params={"sort": "-updated"})
            eid = events[-1]["id"]
            rounds = self.fetch_pb("rounds", params={"filter": f"event = '{eid}' && valid = true", "sort": "roundNumber"})
            
            res = [{"id": "all", "name": "Total (All Sessions)"}]
            
            # ユニークな eventType を抽出して一括オプションを追加
            event_types = sorted(list({r.get("eventType") for r in rounds if r.get("eventType")}))
            for etype in event_types:
                res.append({"id": f"type:{etype}", "name": f"All {etype} Sessions"})
            
            # 個別のラウンドを追加
            for r in rounds:
                res.append({"id": r["id"], "name": f"{r.get('eventType', 'Round')} {r.get('roundNumber', '')}"})
            return res
        except: return [{"id": "all", "name": "Total (All Sessions)"}]

    def get_leaderboard_data(self):
        self.update_all(); c = self._cache; sort_by = c.get("meta", {}).get("sort")
        web_ranking = [{"pilotName": r["name"], "displayTime": r["displayTime"], "time": r["time"], "count": r["count"], "id": r["id"]} for r in c.get("ranking", [])]
        return {"ranking": web_ranking, "nextHeatName": c["next_heat"]["name"], "nextHeatPilots": c["next_heat"]["pilots"], "lastHeatName": c["last_heat"]["name"], "lastHeatResult": c["last_heat"]["pilots"], "round": self.config_manager.get("result_formatter", "leaderboard_round"), "sortedBy": sort_by, "eventName": c.get("meta", {}).get("event_name")}

    def parse_duration_to_seconds(self, duration_str):
        if not duration_str: return 0.0
        s = str(duration_str).strip()
        if ":" in s:
            try:
                parts = s.split(":"); return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            except: pass
        try: return float(s)
        except: return 0.0

    def get_all_sheets_data(self):
        try:
            events = self.fetch_pb("events", params={"filter": "isCurrent = true"})
            if not events: events = self.fetch_pb("events", params={"sort": "-updated"})
            if not events: return None
            event_obj = events[-1]; eid = event_obj["id"]; event_name = event_obj.get("name", "Unknown Event")
            min_lap_time = self.parse_duration_to_seconds(event_obj.get("minLapTime"))
            pilots = {p["id"]: p for p in self.fetch_pb("pilots", {"perPage": 500})}
            races = self.fetch_pb("races", params={"filter": f"event = '{eid}'", "sort": "raceOrder,raceNumber", "perPage": 500})
            rounds = {r["id"]: r for r in self.fetch_pb("rounds", params={"filter": f"event = '{eid}'"})}
            laps_all = self.fetch_pb("laps", params={"filter": f"event = '{eid}'", "perPage": 5000})
            detections = {d["id"]: d for d in self.fetch_pb("detections", params={"filter": f"event = '{eid}'", "perPage": 5000})}
            pilot_channels = self.fetch_pb("pilotChannels", params={"filter": f"event = '{eid}'", "perPage": 2000})
            results_all = self.fetch_pb("results", params={"filter": f"event = '{eid}'", "perPage": 2000})
            d_to_p = {d["id"]: d["pilot"] for d in detections.values() if d.get("pilot")}
            res_lookup = {(res["race"], res["pilot"]): res for res in results_all}
            p_race_map = {}
            for l in laps_all:
                rid = l.get("race"); did = l.get("detection"); pid = d_to_p.get(did)
                lnum = l.get("lapNumber", 0); l_time = float(l.get("lengthSeconds", 0))
                if lnum > 0 and l_time < min_lap_time: continue
                if rid and pid:
                    if pid not in p_race_map: p_race_map[pid] = {}
                    if rid not in p_race_map[pid]: p_race_map[pid][rid] = {}
                    p_race_map[pid][rid][lnum] = l_time
            race_result_rows = []
            for r in races:
                rid = r["id"]; rnd = rounds.get(r.get("round"), {})
                for pid in pilots:
                    l_dict = p_race_map.get(pid, {}).get(rid, {})
                    if not l_dict: continue
                    p_laps_v = [l_dict[n] for n in sorted(l_dict.keys()) if n >= 1]
                    m3 = self.calculate_metrics_for_laps(p_laps_v, 3, r.get("targetLaps", 3))
                    p_res = res_lookup.get((rid, pid), {})
                    row = [event_name, f"Race{rnd.get('roundNumber', 0)}-{r.get('raceNumber', 0)}", "", "", pilots[pid].get("name", "???"), p_res.get("position", ""), len(p_laps_v), m3["totalTime"], m3["totalRace"], m3["bestLap"], "", "", l_dict.get(0, "")]
                    for i in range(1, 31): row.append(l_dict.get(i, ""))
                    race_result_rows.append(row)
            return {"lapsToDo": event_obj.get("laps", 3), "RaceResult": race_result_rows}
        except: return None
