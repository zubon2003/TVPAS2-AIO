# TVPAS2-AIO (TinyViewPlus As Sensors 2 - All In One)

TVPAS2-AIO は、FPVTracksideのタイミングシステムとして、ARマーカーによるラップタイム計測を行うソフトです。初代TVPASは、t-asanoさんが作られたTinyViewPlusを元にc++で動き、relay-serverというNode.jsで動作する情報変換プログラムを通し、FPVTracksideと通信していました。

今回のプログラムはPythonベースに替わっていて、一つのプログラムで以下の機能を実現しています。
なお、機能の少なくない部分は、やつとせさんが実装したものをそのまま統合させていただいています。

## 主な機能

- **対応マーカー**: ArUco に加え、STagマーカーに対応。（現状STagを使用することはなさそうではあるが。。。）
- **検出前画像補正**: 一般的にTinyWhoopで使われるFPVカメラのレンズ歪みを補正しマーカ検出を行う機能を追加。(Original Corrected Hybrid)の３つのモードを選択可能。Hybridは補正前、補正後と、１つの映像に２階検出をかけるので、検出率アップ。その他、検出ライブラリがopencvの新しいものになったこと、各種パラメータ調整により、より検出しやすくなっている。（はず。。。）
- **マルチゲート**: 旧TVPASと同様、最大４つのマーカーを配置し、４区間の個別のラップが計測できる。
- **検出画面の仮想カメラ化(experimental)** OBS Studioインストールと同時にインストールされるOBS Virtual Cameraに、FPVTrackside用の映像を出力。OBSのアプリ自体は立ち上げなくてよい。
- **GUI**:
  - すべての設定は、GUIから変更可能で、基本的に、config.jsonなどの設定ファイルをテキストエディタで書き換える必要がなくなった。
- **OBS オーバーレイ(Experimental)**: 
  - リアルタイムレースフィード(現在行われているレースの状況をリアルタイム表示・セクタータイムも表示)
  - カウントダウン機能（標準のFPVTracksideでは難しかったカウントダウンスタートに対応）
  - 特定のラウンドのみを対象としたランキング表の出力
  - レース結果/次のレースの紹介ページを表示
- **外部連携**:
  - **Google スプレッドシート**: レース結果の集計をGoogle Spreadsheetに書き込み
  - **レーススタート・ラップ通過・フィニッシュ時の日本語音声による読み上げ**: 標準に加えVoiceBoxによるナレーションに対応
  - **ATEM Switcher**: 複数ゲートを開智した際、ゲート通過状況により、ATEM Mini等のBlackmagic社スイッチャーへの自動スイッチング機能
  - **Drone Dashboard 統合**: Drone Dashboard を自動起動し、各種集計にDrone Dashboardのバックエンドを利用。

## セットアップ手順

### 1. 準備
- Windows 10/11
- Python 3.10 以上
- HDZero Event VRXやHawkeye等の4 in 1HDMI映像をUSBにキャプチャする機器

### 2. インストール
リポジトリをクローンまたはダウンロードし、フォルダ内の `setup.bat` を実行すると、必要なライブラリが仮想環境（venv）に自動インストールされます。

```bash
./setup.bat
```

### 3. Google Spreadsheet連携をしたい場合
credentials.json(今まで使っていたもので大丈夫)をstart.bat等のファイルと横並びに配置してください。

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

- **Author**: zubon2003
- **License**: MIT License

---
*Powered by TinyViewPlus technology.*
