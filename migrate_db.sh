#!/bin/bash
# 從 Render PostgreSQL 匯出資料並匯入本地 Docker PostgreSQL
set -e

RENDER_DB_URL="postgresql://airport_reservation_user:yDqOxM8nBCN8Uy36etMBPo79wxWy4eq5@dpg-d6pb1qn5gffc7392bpv0-a/airport_reservation"
DUMP_FILE="render_backup_$(date +%Y%m%d_%H%M%S).dump"

echo "==> 從 Render 匯出資料庫..."
pg_dump "$RENDER_DB_URL" -Fc -f "$DUMP_FILE"
echo "==> 匯出完成：$DUMP_FILE"

echo "==> 等待本地資料庫就緒..."
sleep 5

# 從 .env 讀取密碼
source .env
LOCAL_DB_URL="postgresql://airport_user:${DB_PASSWORD}@127.0.0.1:5432/airport_reservation"

# 需要先把 port 5432 從 docker-compose 暴露出來（臨時用）
# 或改用 docker exec 執行 pg_restore
echo "==> 匯入資料到本地資料庫..."
docker compose exec -T db pg_restore \
  -U airport_user \
  -d airport_reservation \
  --clean --if-exists \
  < "$DUMP_FILE"

echo "==> 資料庫遷移完成！"
echo "==> 備份檔案保留在：$DUMP_FILE"
