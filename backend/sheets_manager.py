import gspread
from google.oauth2.service_account import Credentials
import os
import time

class SheetsManager:
    def __init__(self, config_manager, credentials_path="credentials.json"):
        self.config_manager = config_manager
        self.credentials_path = credentials_path
        self.client = None
        self.log_callback = None

    def log(self, message):
        if self.log_callback: self.log_callback("Sheet", message)
        else: print(f"[Sheet] {message}")

    def authenticate(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cred_path = os.path.join(base_dir, self.credentials_path)
        if not os.path.exists(cred_path): 
            self.log("credentials.json not found.")
            return False
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
            credentials = Credentials.from_service_account_file(cred_path, scopes=scopes)
            self.client = gspread.authorize(credentials)
            return True
        except Exception as e:
            self.log(f"Auth Error: {e}"); return False

    def _get_or_create_doc(self):
        spreadsheet_id = self.config_manager.get("result_formatter", "google_spreadsheet_id")
        if not self.client and not self.authenticate(): return None
        
        if spreadsheet_id:
            try:
                return self.client.open_by_key(spreadsheet_id)
            except Exception:
                self.log("Existing Spreadsheet ID invalid, creating new one...")
        
        try:
            new_doc = self.client.create("FPV Race Results")
            self.config_manager.set("result_formatter", "google_spreadsheet_id", new_doc.id)
            self.log(f"Created new spreadsheet: FPV Race Results (ID: {new_doc.id})")
            return new_doc
        except Exception as e:
            self.log(f"Failed to create spreadsheet: {e}")
            return None

    def update_all_ranking_sheets(self, data_dict):
        if not self.config_manager.get("result_formatter", "enable_spreadsheet_writing"): return
        doc = self._get_or_create_doc()
        if not doc: return

        laps_to_do = data_dict.get("lapsToDo", 4)
        configs = [
            {"name": "RaceResult", "index": 0, "headers": [
                'Event名', 'HEAT', '日付', 'スタート時刻', 'Pilot', 'Position', 'Lap数', 'Finish time', 
                f'Race Time ({laps_to_do}Lap)', 'Best LAP', '連続2周', '連続3周', 'HS(LAP0)',
                'LAP1', 'LAP2', 'LAP3', 'LAP4', 'LAP5', 'LAP6', 'LAP7', 'LAP8', 'LAP9', 'LAP10',
                'LAP11', 'LAP12', 'LAP13', 'LAP14', 'LAP15', 'LAP16', 'LAP17', 'LAP18', 'LAP19', 'LAP20',
                'LAP21', 'LAP22', 'LAP23', 'LAP24', 'LAP25', 'LAP26', 'LAP27', 'LAP28', 'LAP29', 'LAP30'
            ], "key": "RaceResult"},
            {"name": "Best Lap", "index": 1, "headers": ['Rank', 'Pilot', 'Time', 'HEAT'], "key": "Ranking_BestLap"},
            {"name": "Best 2-Lap", "index": 2, "headers": ['Rank', 'Pilot', 'Time', 'HEAT'], "key": "Ranking_Consecutive2Lap"},
            {"name": "Best 3-Lap", "index": 3, "headers": ['Rank', 'Pilot', 'Time', 'HEAT'], "key": "Ranking_Consecutive3Lap"},
            {"name": "Best Race Time", "index": 4, "headers": ['Rank', 'Pilot', 'Time', 'HEAT'], "key": "Ranking_RaceTime"},
            {"name": "Minimum Lap Ranking", "index": 5, "headers": ['Rank', 'Pilot', 'Time', 'HEAT'], "key": "Ranking_MinimumLap"}
        ]

        try:
            # --- STEP 1: 全シートの既存の縞模様を確実に削除 ---
            # 最新のメタデータを取得して既存の bandedRangeId をすべて抽出
            doc_meta = doc.fetch_sheet_metadata()
            delete_requests = []
            for sheet_meta in doc_meta.get('sheets', []):
                for br in sheet_meta.get('bandedRanges', []):
                    delete_requests.append({"deleteBanding": {"bandedRangeId": br['bandedRangeId']}})
            
            if delete_requests:
                try:
                    doc.batch_update({"requests": delete_requests})
                    # サーバー側の反映を待つために少し長めに待機
                    time.sleep(1.0)
                except Exception as e:
                    self.log(f"Warning: Failed to delete some bandings: {e}")

            # --- STEP 2: 各シートのデータを更新 ---
            for cfg in configs:
                sheet_name = cfg["name"]
                header = cfg["headers"]
                rows = data_dict.get(cfg["key"], [])
                
                try:
                    sheet = doc.worksheet(sheet_name)
                except gspread.WorksheetNotFound:
                    sheet = doc.add_worksheet(title=sheet_name, rows="1000", cols="100")
                
                s_id = sheet.id
                requests = []

                # セルのクリア（値とフォーマットの両方）
                requests.append({"updateCells": {"range": {"sheetId": s_id}, "fields": "userEnteredValue,userEnteredFormat"}})
                requests.append({"updateSheetProperties": {"properties": {"sheetId": s_id, "index": cfg["index"]}, "fields": "index"}})
                
                start_row = 0
                if sheet_name != "RaceResult":
                    # タイトル行の設定
                    requests.append({
                        "updateCells": {
                            "rows": [{"values": [{"userEnteredValue": {"stringValue": sheet_name}, "userEnteredFormat": {"textFormat": {"fontSize": 14, "bold": True}, "horizontalAlignment": "CENTER"}}]}],
                            "range": {"sheetId": s_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4},
                            "fields": "userEnteredValue,userEnteredFormat"
                        }
                    })
                    requests.append({"mergeCells": {"range": {"sheetId": s_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4}, "mergeType": "MERGE_ALL"}})
                    start_row = 2

                # ヘッダー行の設定
                requests.append({
                    "updateCells": {
                        "rows": [{"values": [{"userEnteredValue": {"stringValue": h}, "userEnteredFormat": {"backgroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "horizontalAlignment": "RIGHT"}} for h in header]}],
                        "range": {"sheetId": s_id, "startRowIndex": start_row, "endRowIndex": start_row + 1, "startColumnIndex": 0, "endColumnIndex": len(header)},
                        "fields": "userEnteredValue,userEnteredFormat"
                    }
                })

                # データ行の構築
                if rows:
                    sheet_data_rows = []
                    for row in rows:
                        row_values = []
                        for c_idx, cell in enumerate(row):
                            val = {"userEnteredValue": {}, "userEnteredFormat": {"horizontalAlignment": "RIGHT"}}
                            if isinstance(cell, (int, float)):
                                val["userEnteredValue"]["numberValue"] = float(cell)
                                if sheet_name == "RaceResult":
                                    if c_idx == 2: val["userEnteredFormat"]["numberFormat"] = {"type": "DATE", "pattern": "yyyy/mm/dd"}
                                    elif c_idx == 3: val["userEnteredFormat"]["numberFormat"] = {"type": "TIME", "pattern": "hh:mm:ss"}
                                    elif 7 <= c_idx <= 42: val["userEnteredFormat"]["numberFormat"] = {"type": "NUMBER", "pattern": "0.000"}
                                elif c_idx == 2:
                                    val["userEnteredFormat"]["numberFormat"] = {"type": "NUMBER", "pattern": "0.000"}
                            else:
                                val["userEnteredValue"]["stringValue"] = str(cell) if cell is not None else ""
                            row_values.append(val)
                        sheet_data_rows.append({"values": row_values})
                    
                    requests.append({
                        "updateCells": {
                            "rows": sheet_data_rows,
                            "range": {"sheetId": s_id, "startRowIndex": start_row + 1, "startColumnIndex": 0},
                            "fields": "userEnteredValue,userEnteredFormat"
                        }
                    })

                    # 新規バンディング設定を追加 (既存の削除後なので安全)
                    requests.append({
                        "addBanding": {
                            "bandedRange": {
                                "range": {"sheetId": s_id, "startRowIndex": start_row + 1, "endRowIndex": start_row + 1 + len(rows), "startColumnIndex": 0, "endColumnIndex": len(header)},
                                "rowProperties": {
                                    "firstBandColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                                    "secondBandColor": {"red": 1, "green": 1, "blue": 1}
                                }
                            }
                        }
                    })

                # 行固定と列幅の設定
                requests.append({"updateSheetProperties": {"properties": {"sheetId": s_id, "gridProperties": {"frozenRowCount": start_row + 1}}, "fields": "gridProperties.frozenRowCount"}})
                
                if sheet_name == "RaceResult":
                    widths = [150, 130, 100, 100, 150, 60, 60, 80, 80, 80, 80, 80, 80]
                    for i, w in enumerate(widths):
                        requests.append({"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": w}, "fields": "pixelSize"}})
                    requests.append({"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS", "startIndex": 13, "endIndex": 43}, "properties": {"pixelSize": 60}, "fields": "pixelSize"}})
                else:
                    widths = [50, 150, 60, 130]
                    for i, w in enumerate(widths):
                        requests.append({"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i+1}, "properties": {"pixelSize": w}, "fields": "pixelSize"}})

                # 外枠の設定 (RaceResult以外)
                if sheet_name != "RaceResult":
                    border = {"style": "SOLID", "width": 1}
                    requests.append({"updateBorders": {"range": {"sheetId": s_id, "startRowIndex": 0, "endRowIndex": start_row + 1 + len(rows), "startColumnIndex": 0, "endColumnIndex": 4}, "top": border, "bottom": border, "left": border, "right": border}})

                # 最終的な一括更新
                doc.batch_update({"requests": requests})

            self.log(f"Spreadsheets updated successfully (Node.js style matched).")
        except Exception as e:
            self.log(f"Spreadsheet Error: {e}")
        except Exception as e:
            self.log(f"Spreadsheet Error: {e}")

    def update_results(self, dummy): pass
