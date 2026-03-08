import requests
import json
import os
import time
import threading
import io
import urllib.parse
import pygame
import queue
import sys
from concurrent.futures import ThreadPoolExecutor

try:
    import pyttsx3
    HAS_PYTTSX3 = True
except ImportError:
    HAS_PYTTSX3 = False

if sys.platform == 'win32':
    try:
        import pythoncom
        HAS_PYTHONCOM = True
    except ImportError:
        HAS_PYTHONCOM = False
else:
    HAS_PYTHONCOM = False

class VoiceManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.msg_queue = queue.Queue() # 再生待ちキュー
        self.running = True
        self.log_callback = None
        
        # 音声生成用のスレッドプール (並列でAPIを叩く)
        self.executor = ThreadPoolExecutor(max_workers=4)
        
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        except Exception as e:
            print(f"Failed to initialize pygame mixer: {e}")
        
        self.playback_thread = threading.Thread(target=self._worker, daemon=True)
        self.playback_thread.start()

    def log(self, cat, msg):
        if self.log_callback: self.log_callback(cat, msg)
        else: print(f"[{cat}] {msg}")

    def _worker(self):
        """再生専用スレッド: キューにある準備完了済みの音声を一つずつ再生する"""
        self.log("Voice", "Voice worker started (High-Performance Parallel Mode).")

        while self.running:
            try:
                # 再生リクエストを取得
                item = self.msg_queue.get(timeout=0.5)
                
                # アイテムが「生成中」のFutureオブジェクトである場合、完了を待つ
                if "future" in item:
                    # APIの応答を待機
                    audio_item = item["future"].result()
                    if not audio_item:
                        self.msg_queue.task_done()
                        continue
                    item = audio_item

                text = item.get("content", "")
                engine_type = item.get("engine", "pyttsx3")
                
                if item["type"] == "data": # VOICEVOX
                    try:
                        sound = pygame.mixer.Sound(io.BytesIO(item["content"]))
                        channel = sound.play()
                        if channel:
                            while channel.get_busy() and self.running:
                                time.sleep(0.01)
                    except Exception as e:
                        self.log("Voice", f"Pygame play error: {e}")
                
                elif item["type"] == "text": # pyttsx3
                    if HAS_PYTTSX3:
                        if HAS_PYTHONCOM: pythoncom.CoInitialize()
                        try:
                            tmp_engine = pyttsx3.init()
                            speed = self.config_manager.get("voice", "speed") or 1.0
                            vol = self.config_manager.get("voice", "volume") or 1.0
                            tmp_engine.setProperty('rate', int(150 * speed))
                            tmp_engine.setProperty('volume', vol)
                            
                            self.log("Voice", f"Speaking: {text}")
                            tmp_engine.say(text)
                            tmp_engine.runAndWait()
                            del tmp_engine
                        except Exception as e:
                            self.log("Voice", f"pyttsx3 error: {e}")
                        finally:
                            if HAS_PYTHONCOM: pythoncom.CoUninitialize()
                
                self.msg_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                self.log("Voice", f"Worker loop error: {e}")

    def speak(self, text):
        """読み上げリクエスト。エンジンに応じて即座に生成を開始する"""
        if not self.config_manager.get("voice", "enabled") or not text:
            return
        
        engine_type = self.config_manager.get("voice", "engine") or "pyttsx3"
        
        if engine_type == "voicevox":
            # 裏側で並列生成を開始し、その予約票(Future)を再生キューに入れる
            future = self.executor.submit(self._generate_voicevox_audio_task, text)
            self.msg_queue.put({"future": future, "text": text, "engine": "voicevox"})
        else:
            # pyttsx3は逐次処理のため直接キューへ
            self.msg_queue.put({"type": "text", "content": text, "engine": "pyttsx3"})

    def _generate_voicevox_audio_task(self, text):
        """スレッドプール内で実行される生成タスク"""
        url = self.config_manager.get("voicevox", "url") or "http://localhost:5021"
        speaker = self.config_manager.get("voicevox", "speaker") or 1
        speed = self.config_manager.get("voice", "speed") or 1.0
        volume = self.config_manager.get("voice", "volume") or 1.0
        
        try:
            # audio_query
            query_url = f"{url}/audio_query?text={urllib.parse.quote(text)}&speaker={speaker}"
            res = requests.post(query_url, timeout=2)
            if res.status_code != 200: return None
            
            query = res.json()
            query['speedScale'] = speed
            query['volumeScale'] = volume
            
            # synthesis
            synth_url = f"{url}/synthesis?speaker={speaker}"
            res = requests.post(synth_url, json=query, timeout=5)
            if res.status_code == 200:
                return {"type": "data", "content": res.content, "content_text": text, "engine": "voicevox"}
        except Exception as e:
            self.log("Voice", f"VOICEVOX API Error: {e}")
        
        # 失敗時はpyttsx3用のテキスト形式で返す
        return {"type": "text", "content": text, "engine": "pyttsx3"}

    def clear_queue(self):
        while not self.msg_queue.empty():
            try: self.msg_queue.get_nowait(); self.msg_queue.task_done()
            except queue.Empty: break

    def stop(self):
        self.running = False
        self.clear_queue()
        self.executor.shutdown(wait=False)
        try: pygame.mixer.quit()
        except: pass
