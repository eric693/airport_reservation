# 部署說明：從 Render 遷移到自架伺服器

## 前置需求

伺服器需安裝：
- Docker + Docker Compose
- Nginx
- Certbot（申請 SSL 憑證）
- postgresql-client（用於資料庫遷移，`apt install postgresql-client`）

---

## 一、設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入所有變數：

```bash
nano .env
```

---

## 二、申請 SSL 憑證

```bash
sudo certbot certonly --nginx -d airport-reservation.crownai.ink
```

---

## 三、設定 Nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/airport-reservation
sudo ln -s /etc/nginx/sites-available/airport-reservation /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 四、啟動 Docker 服務

```bash
docker compose up -d --build
```

---

## 五、遷移資料庫（從 Render）

確保已安裝 `pg_dump`：

```bash
apt install postgresql-client
```

執行遷移腳本：

```bash
bash migrate_db.sh
```

> 腳本會從 Render 匯出 `.dump` 備份，再匯入本地 PostgreSQL。

---

## 六、更新 LINE Webhook URL

登入 LINE Developers Console，將 Webhook URL 更新為：

```
https://airport-reservation.crownai.ink/callback
```

---

## 七、確認服務正常

```bash
# 查看 container 狀態
docker compose ps

# 查看 app log
docker compose logs -f web

# 測試連線
curl https://airport-reservation.crownai.ink/ping
```

---

## 日常維護

```bash
# 重啟 app
docker compose restart web

# 更新程式碼後重新部署
git pull
docker compose up -d --build

# 備份資料庫
docker compose exec db pg_dump -U airport_user airport_reservation > backup_$(date +%Y%m%d).sql
```
