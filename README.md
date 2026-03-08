# TVPAS2-AIO (TinyViewPlus As Sensors 2 - All In One)

TVPAS2-AIO は、ドローンレース向けのオールインワン・ラップタイマーおよびリレーシステムです。
カメラ映像から STag や ArUco マーカーを検出し、リアルタイムでラップタイムを計測・配信します。

## 主な機能

- **高精度マーカー検知**: STag および ArUco マーカーに対応。レンズ歪み補正（Hybridモード）による広角カメラでの検知精度向上。
- **マルチサーバーリレー**: 最大4つのリレーポート（UDP/TCP）をサポートし、Drone Dashboard 等へデータを中継。
- **Web UI & OBS オーバーレイ**: 
  - リアルタイムレースフィード
  - カウントダウンアニメーション（音声同期）
  - リーダーボード（順位表）
  - 前回のヒート結果 / 次のヒート予告
- **外部連携**:
  - **Google スプレッドシート**: レース結果を自動的に集計・書き込み。
  - **VOICEVOX / pyttsx3**: パイロット名やラップタイムの音声読み上げ。
  - **ATEM Switcher**: レース状況に応じた自動カメラ切り替え。
- **Drone Dashboard 統合**: 内蔵の Drone Dashboard を自動起動し、Webブラウザから詳細なレース管理が可能。

## セットアップ手順

### 1. 準備
- Windows 10/11
- Python 3.10 以上
- Webカメラ または キャプチャボード

### 2. インストール
リポジトリをクローンまたはダウンロードし、フォルダ内の `setup.bat` を実行してください。
必要なライブラリが仮想環境（venv）に自動インストールされます。

```bash
./setup.bat
```

### 3. 起動
`start.bat` を実行すると、アプリケーションが起動します。

```bash
./start.bat
```

## 設定方法

- **config.json**: 初回起動時に自動生成されます。GUI上の各設定項目を変更すると、このファイルに保存されます。
- **credentials.json**: Googleスプレッドシート連携を使用する場合は、Google Cloud Console で作成したサービスアカウントのキーファイルを `credentials.json` という名前で本フォルダに配置してください。
- **camera_calibration.json**: カメラの歪み補正を使用する場合は、キャリブレーションデータを配置してください。

## 使い方（GUI 各タブ）

1. **TIMER**: カメラの選択、解像度、検知モード（Hybrid推奨）を設定します。
2. **RELAY**: 送信先のポートやID割り当てを設定します。
3. **ATEM**: ATEMスイッチャーのIPアドレスと自動切り替えモードを設定します。
4. **RESULT**: FPVTracksideのパス設定、集計ソート順、OBSオーバーレイのリンク取得が可能です。
5. **VOICE**: 音声読み上げのエンジン（VOICEVOX 等）を選択します。
6. **SHEETS**: 書き込み先のスプレッドシートIDを設定します。

## 開発・ライセンス

- **License**: MIT License

---
*Powered by TinyViewPlus technology.*
