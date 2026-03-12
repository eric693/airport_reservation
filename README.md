# 機場接送 LINE Bot + 管理後台

## 功能說明

- LINE Bot 引導使用者預約機場接送（送機出境 / 接機回國）
- 完整預約流程：選服務 → 選車型 → 選機場 → 填資料 → 確認
- 訂單查詢功能（姓名 + 電話）
- 管理後台：查看/搜尋/篩選訂單、更新狀態、刪除訂單
- PostgreSQL 資料庫儲存
- Keep-alive ping 防止 Render 服務睡眠

---

## 環境變數（Render Environment Variables）

| 變數名稱 | 說明 | 範例 |
|---------|------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot Channel Access Token | `xxxxxx...` |
| `LINE_CHANNEL_SECRET` | LINE Bot Channel Secret | `xxxxxx` |
| `DATABASE_URL` | PostgreSQL 連線字串（Render 自動提供） | `postgresql://...` |
| `SECRET_KEY` | Flask Session 密鑰（隨機字串） | `super-secret-key-123` |
| `ADMIN_USERNAME` | 後台管理員帳號 | `admin` |
| `ADMIN_PASSWORD` | 後台管理員密碼 | `your-password` |
| `RENDER_EXTERNAL_URL` | Render 服務對外網址（用於 keep-alive） | `https://your-app.onrender.com` |

---

## 部署步驟

### 1. 取得 LINE Bot 憑證
1. 前往 [LINE Developers](https://developers.line.biz/)
2. 建立 Messaging API Channel
3. 取得 `Channel Access Token` 和 `Channel Secret`
4. 設定 Webhook URL 為：`https://your-app.onrender.com/callback`
5. 關閉「自動回覆訊息」

### 2. 部署到 Render
1. 將程式碼推送至 GitHub
2. 前往 [Render](https://render.com/) 建立新的 Web Service
3. 連接 GitHub 儲存庫
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
6. 建立 PostgreSQL 資料庫（Render 免費方案）
7. 設定上述所有環境變數
8. 部署完成後，複製服務網址填入 `RENDER_EXTERNAL_URL`

### 3. LINE Bot Webhook 設定
- Webhook URL: `https://your-app.onrender.com/callback`
- 勾選「Use webhook」

---

## 管理後台存取

URL: `https://your-app.onrender.com/admin`
- 瀏覽器會跳出 HTTP Basic Auth 視窗
- 輸入 ADMIN_USERNAME / ADMIN_PASSWORD

---

## LINE Bot 使用說明

用戶輸入以下關鍵字啟動：
- `預約` / `訂車` / `機場接送` / `開始` — 開始預約流程
- `查詢訂單` — 查詢已有訂單
- `取消` — 取消目前操作

---

## 檔案結構

```
├── app.py              # 主程式（Flask + LINE Bot）
├── database.py         # 資料庫模型
├── requirements.txt    # Python 套件
├── Procfile            # Render 啟動指令
├── render.yaml         # Render 配置（選用）
└── templates/
    └── admin/
        ├── index.html          # 後台訂單列表
        └── order_detail.html   # 訂單詳情頁
```
