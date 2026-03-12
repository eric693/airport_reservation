import os
import json
import base64
import threading
import time
import requests
from datetime import datetime, timedelta
from sqlalchemy.pool import NullPool
from flask import Flask, request, abort, render_template, redirect, url_for, flash, jsonify, session as flask_session
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, FlexMessage, FlexContainer,
    PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from apscheduler.schedulers.background import BackgroundScheduler
from database import db, Order, Driver, VehicleType, AirportOption, DispatchJob, DispatchResponse, PriceRule, PriceSurcharge, HolidaySurcharge
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///airport.db')
# 將 postgres:// 轉為 postgresql+psycopg:// 以使用 psycopg v3 驅動
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif database_url.startswith('postgresql://') and '+' not in database_url.split('://')[0]:
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 修正 psycopg v3 SSL 連線逾時問題：定期回收連線
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'poolclass': NullPool,   # 每次請求用完即關閉連線，解決 SSL 逾時問題
    'pool_pre_ping': True,
}

db.init_app(app)
with app.app_context():
    db.create_all()
    # 自動補上新增的欄位（ALTER TABLE，已存在時略過）
    _migrations = [
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stops TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stop_fee INTEGER DEFAULT 0",
        "ALTER TABLE drivers ADD COLUMN IF NOT EXISTS line_user_id VARCHAR(100) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS deadline TIMESTAMP",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS note VARCHAR(200) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS notify_customer BOOLEAN DEFAULT TRUE",
    ]
    with db.engine.connect() as _conn:
        for _sql in _migrations:
            try:
                _conn.execute(db.text(_sql))
            except Exception as _e:
                print(f'Migration skip: {_e}')
        _conn.commit()
    if VehicleType.query.count() == 0:
        defaults = [
            VehicleType(name='標準國產四座轎車', capacity=4, luggage_capacity=2, sort_order=1),
            VehicleType(name='商務六座廂型車',   capacity=6, luggage_capacity=4, sort_order=2),
            VehicleType(name='豪華七座SUV',      capacity=7, luggage_capacity=4, sort_order=3),
            VehicleType(name='九座廂型車',       capacity=9, luggage_capacity=6, sort_order=4),
        ]
        db.session.add_all(defaults)
        db.session.commit()
    if AirportOption.query.count() == 0:
        defaults = [
            AirportOption(name='桃園機場第一航廈', code='tpe1', sort_order=1),
            AirportOption(name='桃園機場第二航廈', code='tpe2', sort_order=2),
            AirportOption(name='松山機場',         code='tsa',  sort_order=3),
            AirportOption(name='台中清泉崗機場',   code='rmq',  sort_order=4),
            AirportOption(name='高雄小港機場',     code='khh',  sort_order=5),
        ]
        db.session.add_all(defaults)
        db.session.commit()
    if PriceSurcharge.query.count() == 0:
        defaults = [
            PriceSurcharge(key='night',      name='夜間服務費（22:00-06:00）', amount=200),
            PriceSurcharge(key='sign_board', name='舉牌服務',                  amount=300),
            PriceSurcharge(key='child_seat', name='兒童安全座椅（每張）',      amount=200),
            PriceSurcharge(key='pet',        name='寵物同行',                  amount=1100),
            PriceSurcharge(key='extra_stop', name='多點加收（每點/5公里內）',  amount=200),
            PriceSurcharge(key='invoice',    name='開立發票加收',              amount=0, note='基本價5%'),
            PriceSurcharge(key='short_book', name='七天內預約加收',            amount=300),
            PriceSurcharge(key='urgent',     name='三天內臨時單加收',          amount=300),
        ]
        db.session.add_all(defaults)
        db.session.commit()
    if HolidaySurcharge.query.count() == 0:
        defaults = [
            HolidaySurcharge(name='清明連假',   date_from='04-02', date_to='04-07', amount=300),
            HolidaySurcharge(name='勞動節連假', date_from='04-30', date_to='05-04', amount=300),
            HolidaySurcharge(name='端午連假',   date_from='06-18', date_to='06-22', amount=300),
            HolidaySurcharge(name='中秋連假',   date_from='09-24', date_to='09-29', amount=300),
            HolidaySurcharge(name='國慶連假',   date_from='10-08', date_to='10-12', amount=300),
            HolidaySurcharge(name='重陽連假',   date_from='10-23', date_to='10-27', amount=300),
            HolidaySurcharge(name='耶誕連假',   date_from='12-24', date_to='12-28', amount=300),
        ]
        db.session.add_all(defaults)
        db.session.commit()

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1234')
ADMIN_LINE_USER_ID = os.environ.get('ADMIN_LINE_USER_ID', '')  # 後台管理員的 LINE User ID（搶單成功時收通知）
AUTO_DISPATCH = os.environ.get('AUTO_DISPATCH', '0') == '1'   # 設為 1 時，客人送出預約後自動發布搶單
GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')  # Google Maps API Key（用於多點距離計算）
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')             # OpenAI API Key（用於 AI 客服對話）

user_sessions = {}

def get_vehicles():
    return VehicleType.query.filter_by(active=True).order_by(VehicleType.sort_order).all()

def get_airports():
    return AirportOption.query.filter_by(active=True).order_by(AirportOption.sort_order).all()

CHILD_SEATS = {
    'baby': '嬰兒幼童座椅型 (0-1歲)',
    'child': '兒童座椅型 (1-4歲)',
    'booster': '增高座墊 (4-12歲)',
    'none': '不需要',
}

# ── Keep-alive ──────────────────────────────────────────────────────
def keep_alive():
    url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if url:
        while True:
            try:
                requests.get(f"{url}/ping", timeout=10)
            except Exception:
                pass
            time.sleep(840)

# ── Auto-notify scheduler ───────────────────────────────────────────
def check_and_notify():
    """每分鐘檢查是否有訂單需要發送司機資料給客人"""
    with app.app_context():
        now = datetime.utcnow() + timedelta(hours=8)  # 轉為台灣時間
        orders = Order.query.filter(
            Order.status == '已確認',
            Order.driver_id != None,
            Order.driver_notified == False,
            Order.notify_at != '',
            Order.notify_at != None,
        ).all()

        for order in orders:
            try:
                booking_dt = datetime.strptime(
                    f"{order.booking_date} {order.booking_time}", '%Y-%m-%d %H:%M'
                )
                hours_before = int(order.notify_at)
                notify_time = booking_dt - timedelta(hours=hours_before)

                if now >= notify_time:
                    driver = order.driver
                    if driver:
                        send_driver_info_to_customer(order, driver)
                        order.driver_notified = True
                        db.session.commit()
            except Exception as e:
                print(f"Notify error for order {order.id}: {e}")

def send_driver_info_to_customer(order, driver):
    """推播司機資料給客人的 LINE"""
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "您的司機資料", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": f"訂單 #{order.id}", "color": "#DDDDDD", "size": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                make_info_row("出發時間", f"{order.booking_date} {order.booking_time}"),
                make_info_row("服務", order.service_name),
                make_info_row("地點", order.pickup_location),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "司機資訊", "weight": "bold", "margin": "md", "color": "#1A2B4A"},
                make_info_row("司機姓名", driver.name),
                make_info_row("聯絡電話", driver.phone),
                make_info_row("車輛", f"{driver.car_brand}"),
                make_info_row("車牌", driver.car_plate),
                make_info_row("車身顏色", driver.car_color),
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "如有任何問題請直接致電司機，祝您旅途愉快！",
                    "size": "xs", "color": "#888888", "margin": "md", "wrap": True
                }
            ]
        }
    }
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=order.line_user_id,
                    messages=[FlexMessage(alt_text='您的司機資料已送出', contents=FlexContainer.from_dict(bubble))]
                )
            )
    except Exception as e:
        print(f"Push message error: {e}")

# ── Helpers ─────────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            flask_session['admin_logged_in'] = True
            flask_session.permanent = True
            next_url = request.args.get('next') or url_for('admin_index')
            return redirect(next_url)
        error = '帳號或密碼錯誤，請重新輸入'
    return render_template('admin/login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    flask_session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

def reply_text(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )

def send_flex(reply_token, alt_text, contents):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text=alt_text, contents=FlexContainer.from_dict(contents))]
            )
        )

def make_info_row(label, value):
    return {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "sm", "color": "#888888", "flex": 3},
        {"type": "text", "text": str(value), "size": "sm", "color": "#333333", "flex": 5, "wrap": True}
    ]}

def parse_date(text):
    """支援多種日期格式，回傳 YYYY-MM-DD 字串，失敗回傳 None"""
    text = text.strip().replace('。', '').replace(' ', '')
    now = datetime.now()
    formats = [
        '%Y-%m-%d',   # 2025-06-15
        '%Y/%m/%d',   # 2025/06/15
        '%m/%d',      # 06/15
        '%m-%d',      # 06-15
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt in ('%m/%d', '%m-%d'):
                dt = dt.replace(year=now.year)
                if dt.date() < now.date():
                    dt = dt.replace(year=now.year + 1)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass
    # 純數字：615 → 06/15，20250615 → 2025-06-15
    digits = ''.join(filter(str.isdigit, text))
    if len(digits) == 8:
        try:
            dt = datetime.strptime(digits, '%Y%m%d')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass
    if len(digits) == 4:
        try:
            dt = datetime.strptime(digits, '%m%d').replace(year=now.year)
            if dt.date() < now.date():
                dt = dt.replace(year=now.year + 1)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None

def is_night_time(time_str):
    try:
        h = int(time_str.split(':')[0])
        return h >= 22 or h <= 6
    except Exception:
        return False

def make_button(label, data, style='secondary'):
    return {"type": "button", "action": {"type": "postback", "label": label, "data": data},
            "style": style, "margin": "sm"}

def header_box(title, color="#4A9B8F"):
    return {"type": "box", "layout": "vertical", "backgroundColor": color,
            "contents": [{"type": "text", "text": title, "color": "#FFFFFF", "size": "xl", "weight": "bold"}]}

# ── Routes ───────────────────────────────────────────────────────────
@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/ping')
def ping():
    return 'pong'

# ── Admin: Orders ────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_index():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin/index.html', orders=orders)

@app.route('/admin/order/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    drivers = Driver.query.filter_by(active=True).all()
    return render_template('admin/order_detail.html', order=order, drivers=drivers)

@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
@admin_required
def admin_update_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    order.status = new_status
    db.session.commit()
    # 後台手動將狀態改為「已確認」且尚無搶單任務 → 詢問是否要發布搶單（透過 flash 提示）
    if new_status == '已確認' and not order.dispatch_job:
        flash('訂單已確認。若要發布搶單，請至右側「搶單模組」操作。')
    else:
        flash('訂單狀態已更新')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/order/<int:order_id>/assign', methods=['POST'])
@admin_required
def admin_assign_driver(order_id):
    order = Order.query.get_or_404(order_id)
    driver_id = request.form.get('driver_id')
    notify_at = request.form.get('notify_at', '2')

    order.driver_id = int(driver_id) if driver_id else None
    order.notify_at = notify_at
    order.driver_notified = False  # 重置，允許重新發送
    db.session.commit()
    flash(f'已指派司機，將於出發前 {notify_at} 小時自動發送司機資料給客人')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/order/<int:order_id>/notify_now', methods=['POST'])
@admin_required
def admin_notify_now(order_id):
    """立即發送司機資料"""
    order = Order.query.get_or_404(order_id)
    if order.driver:
        send_driver_info_to_customer(order, order.driver)
        order.driver_notified = True
        db.session.commit()
        flash('已立即發送司機資料給客人')
    else:
        flash('請先指派司機')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/order/<int:order_id>/delete', methods=['POST'])
@admin_required
def admin_delete_order(order_id):
    order = Order.query.get_or_404(order_id)
    db.session.delete(order)
    db.session.commit()
    flash('訂單已刪除')
    return redirect(url_for('admin_index'))

# ── Admin: Drivers ───────────────────────────────────────────────────
@app.route('/admin/drivers')
@admin_required
def admin_drivers():
    drivers = Driver.query.order_by(Driver.created_at.desc()).all()
    return render_template('admin/drivers.html', drivers=drivers)

@app.route('/admin/drivers/add', methods=['POST'])
@admin_required
def admin_add_driver():
    driver = Driver(
        name=request.form.get('name'),
        phone=request.form.get('phone'),
        car_brand=request.form.get('car_brand', ''),
        car_plate=request.form.get('car_plate', ''),
        car_color=request.form.get('car_color', ''),
        note=request.form.get('note', ''),
        line_user_id=request.form.get('line_user_id', ''),
    )
    db.session.add(driver)
    db.session.commit()
    flash('司機已新增')
    return redirect(url_for('admin_drivers'))

@app.route('/admin/drivers/<int:driver_id>/edit', methods=['POST'])
@admin_required
def admin_edit_driver(driver_id):
    driver = Driver.query.get_or_404(driver_id)
    driver.name = request.form.get('name')
    driver.phone = request.form.get('phone')
    driver.car_brand = request.form.get('car_brand', '')
    driver.car_plate = request.form.get('car_plate', '')
    driver.car_color = request.form.get('car_color', '')
    driver.note = request.form.get('note', '')
    driver.line_user_id = request.form.get('line_user_id', '')
    driver.active = request.form.get('active') == '1'
    db.session.commit()
    flash('司機資料已更新')
    return redirect(url_for('admin_drivers'))

@app.route('/admin/drivers/<int:driver_id>/delete', methods=['POST'])
@admin_required
def admin_delete_driver(driver_id):
    driver = Driver.query.get_or_404(driver_id)
    db.session.delete(driver)
    db.session.commit()
    flash('司機已刪除')
    return redirect(url_for('admin_drivers'))

# ── Admin: Vehicles ─────────────────────────────────────────────────
@app.route('/admin/vehicles')
@admin_required
def admin_vehicles():
    vehicles = VehicleType.query.order_by(VehicleType.sort_order).all()
    return render_template('admin/vehicles.html', vehicles=vehicles)

@app.route('/admin/vehicles/add', methods=['POST'])
@admin_required
def admin_add_vehicle():
    v = VehicleType(
        name=request.form.get('name'),
        capacity=int(request.form.get('capacity', 4)),
        luggage_capacity=int(request.form.get('luggage_capacity', 2)),
        note=request.form.get('note', ''),
        sort_order=int(request.form.get('sort_order', 99)),
    )
    db.session.add(v)
    db.session.commit()
    flash('車型已新增')
    return redirect(url_for('admin_vehicles'))

@app.route('/admin/vehicles/<int:vid>/edit', methods=['POST'])
@admin_required
def admin_edit_vehicle(vid):
    v = VehicleType.query.get_or_404(vid)
    v.name = request.form.get('name')
    v.capacity = int(request.form.get('capacity', 4))
    v.luggage_capacity = int(request.form.get('luggage_capacity', 2))
    v.note = request.form.get('note', '')
    v.sort_order = int(request.form.get('sort_order', 99))
    v.active = request.form.get('active') == '1'
    db.session.commit()
    flash('車型已更新')
    return redirect(url_for('admin_vehicles'))

@app.route('/admin/vehicles/<int:vid>/delete', methods=['POST'])
@admin_required
def admin_delete_vehicle(vid):
    v = VehicleType.query.get_or_404(vid)
    db.session.delete(v)
    db.session.commit()
    flash('車型已刪除')
    return redirect(url_for('admin_vehicles'))

# ── Admin: Airports ──────────────────────────────────────────────────
@app.route('/admin/airports')
@admin_required
def admin_airports():
    airports = AirportOption.query.order_by(AirportOption.sort_order).all()
    return render_template('admin/airports.html', airports=airports)

@app.route('/admin/airports/add', methods=['POST'])
@admin_required
def admin_add_airport():
    a = AirportOption(
        name=request.form.get('name'),
        code=request.form.get('code', ''),
        sort_order=int(request.form.get('sort_order', 99)),
    )
    db.session.add(a)
    db.session.commit()
    flash('機場已新增')
    return redirect(url_for('admin_airports'))

@app.route('/admin/airports/<int:aid>/edit', methods=['POST'])
@admin_required
def admin_edit_airport(aid):
    a = AirportOption.query.get_or_404(aid)
    a.name = request.form.get('name')
    a.code = request.form.get('code', '')
    a.sort_order = int(request.form.get('sort_order', 99))
    a.active = request.form.get('active') == '1'
    db.session.commit()
    flash('機場已更新')
    return redirect(url_for('admin_airports'))

@app.route('/admin/airports/<int:aid>/delete', methods=['POST'])
@admin_required
def admin_delete_airport(aid):
    a = AirportOption.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    flash('機場已刪除')
    return redirect(url_for('admin_airports'))

# ── Admin: Dispatch (搶單) ───────────────────────────────────────────
@app.route('/admin/order/<int:order_id>/dispatch', methods=['POST'])
@admin_required
def admin_create_dispatch(order_id):
    """建立搶單任務並推播給所有啟用司機"""
    order = Order.query.get_or_404(order_id)
    if hasattr(order, 'dispatch_job') and order.dispatch_job:
        flash('此訂單已有搶單任務')
        return redirect(url_for('admin_order_detail', order_id=order_id))
    from datetime import timezone
    job = DispatchJob(
        order_id=order_id,
        status='開放搶單',
        note=request.form.get('dispatch_note', ''),
        notify_customer=request.form.get('notify_customer') == '1',
    )
    db.session.add(job)
    db.session.commit()
    # 推播給所有有 line_user_id 的啟用司機
    drivers = Driver.query.filter(Driver.active == True, Driver.line_user_id != '').all()
    sent = 0
    for driver in drivers:
        try:
            push_dispatch_to_driver(driver, order, job)
            sent += 1
        except Exception as e:
            print(f'Dispatch push error driver {driver.id}: {e}')
    order.status = '搶單中'
    db.session.commit()
    flash(f'搶單任務已發布，共通知 {sent} 位司機')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/dispatch/<int:job_id>/cancel', methods=['POST'])
@admin_required
def admin_cancel_dispatch(job_id):
    job = DispatchJob.query.get_or_404(job_id)
    job.status = '已取消'
    if job.order:
        job.order.status = '待確認'
    db.session.commit()
    flash('搶單任務已取消')
    return redirect(url_for('admin_order_detail', order_id=job.order_id))

@app.route('/admin/dispatch')
@admin_required
def admin_dispatch_list():
    jobs = DispatchJob.query.order_by(DispatchJob.created_at.desc()).all()
    return render_template('admin/dispatch.html', jobs=jobs)

def push_dispatch_to_driver(driver, order, job):
    """推播搶單通知給單一司機"""
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "新訂單搶單通知", "color": "#FFFFFF", "size": "lg", "weight": "bold"},
                {"type": "text", "text": f"訂單 #{order.id}　第一個搶到確認！", "color": "#8BA3C7", "size": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                make_info_row("服務", order.service_name),
                make_info_row("車型需求", order.vehicle),
                make_info_row("機場", order.airport),
                make_info_row("接送地點", order.pickup_location),
                make_info_row("日期時間", f"{order.booking_date} {order.booking_time}"),
                make_info_row("乘客/行李", f"{order.passengers}人 / {order.luggage}件"),
                make_info_row("航班", order.flight_number or '無'),
                {"type": "separator", "margin": "md"},
                *([make_info_row("備註", job.note)] if job.note else []),
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "我要接單", "data": f"grab:{job.id}"},
                    "style": "primary", "color": "#4A9B8F", "flex": 1
                },
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "略過", "data": f"skip:{job.id}"},
                    "style": "secondary", "flex": 1
                }
            ]
        }
    }
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=driver.line_user_id,
                messages=[FlexMessage(alt_text=f'新訂單搶單 #{order.id}', contents=FlexContainer.from_dict(bubble))]
            )
        )

# ── 距離計算（Google Maps）────────────────────────────────────────────
EXTRA_STOP_TIERS = [
    (5,   200),   # 0-5 公里
    (12,  300),   # 5-12 公里
    (18,  400),   # 12-18 公里
    (999, 500),   # 超過 18 公里
]

def get_distance_km(origin, destination):
    """呼叫 Google Maps Distance Matrix API 取得兩點距離（公里）"""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        url = 'https://maps.googleapis.com/maps/api/distancematrix/json'
        params = {
            'origins': origin,
            'destinations': destination,
            'key': GOOGLE_MAPS_API_KEY,
            'language': 'zh-TW',
            'region': 'tw',
        }
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        meters = data['rows'][0]['elements'][0]['distance']['value']
        return round(meters / 1000, 1)
    except Exception as e:
        print(f'Distance API error: {e}')
        return None

def calc_extra_stop_fee(distance_km):
    """根據距離計算多點加收金額"""
    if distance_km is None:
        return None, None
    for limit, fee in EXTRA_STOP_TIERS:
        if distance_km <= limit:
            return fee, distance_km
    return 500, distance_km


# ── 報價計算 ─────────────────────────────────────────────────────────
def calculate_quote(order):
    """根據訂單內容計算報價，回傳 dict"""
    result = {
        'base_price': 0,
        'base_rule': '未設定區域報價',
        'surcharges': [],
        'holiday': None,
        'holiday_amount': 0,
        'total': 0,
        'breakdown': [],
    }
    # 1. 基本價：比對區域規則
    rules = PriceRule.query.filter_by(active=True).order_by(PriceRule.sort_order).all()
    for rule in rules:
        airport_match = not rule.airport_keyword or rule.airport_keyword in order.airport
        region_match = not rule.region_keyword or any(
            kw.strip() in order.pickup_location
            for kw in rule.region_keyword.split(',')
        )
        if airport_match and region_match:
            result['base_price'] = rule.base_price
            result['base_rule'] = rule.name
            result['breakdown'].append({'label': f'基本車資（{rule.name}）', 'amount': rule.base_price})
            break

    # 2. 夜間加費
    surcharge_map = {s.key: s for s in PriceSurcharge.query.filter_by(enabled=True).all()}
    if order.night_fee and 'night' in surcharge_map:
        amt = surcharge_map['night'].amount
        result['surcharges'].append({'label': surcharge_map['night'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['night'].name, 'amount': amt})

    # 3. 舉牌服務
    if order.sign_board and 'sign_board' in surcharge_map:
        amt = surcharge_map['sign_board'].amount
        result['surcharges'].append({'label': surcharge_map['sign_board'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['sign_board'].name, 'amount': amt})

    # 4. 兒童安全座椅
    if order.child_seat_count and 'child_seat' in surcharge_map:
        amt = surcharge_map['child_seat'].amount * order.child_seat_count
        label = f'{surcharge_map["child_seat"].name} x{order.child_seat_count}'
        result['surcharges'].append({'label': label, 'amount': amt})
        result['breakdown'].append({'label': label, 'amount': amt})

    # 5. 寵物同行
    if order.pet and 'pet' in surcharge_map:
        amt = surcharge_map['pet'].amount
        result['surcharges'].append({'label': surcharge_map['pet'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['pet'].name, 'amount': amt})

    # 6. 七天內 / 三天內預約加收
    try:
        from datetime import date
        booking = date.fromisoformat(order.booking_date)
        today = date.today()
        days_ahead = (booking - today).days
        if days_ahead <= 3 and 'urgent' in surcharge_map:
            amt = surcharge_map['urgent'].amount
            result['surcharges'].append({'label': surcharge_map['urgent'].name, 'amount': amt})
            result['breakdown'].append({'label': surcharge_map['urgent'].name, 'amount': amt})
        elif days_ahead <= 7 and 'short_book' in surcharge_map:
            amt = surcharge_map['short_book'].amount
            result['surcharges'].append({'label': surcharge_map['short_book'].name, 'amount': amt})
            result['breakdown'].append({'label': surcharge_map['short_book'].name, 'amount': amt})
    except Exception:
        pass

    # 7. 假日加收
    try:
        booking_md = order.booking_date[5:]  # MM-DD
        holidays = HolidaySurcharge.query.filter_by(active=True).all()
        for h in holidays:
            if h.date_from <= booking_md <= h.date_to:
                result['holiday'] = h.name
                result['holiday_amount'] = h.amount
                result['surcharges'].append({'label': f'假日加收（{h.name}）', 'amount': h.amount})
                result['breakdown'].append({'label': f'假日加收（{h.name}）', 'amount': h.amount})
                break
    except Exception:
        pass

    result['total'] = result['base_price'] + sum(s['amount'] for s in result['surcharges'])
    return result


def send_quote_to_customer(order):
    """預約成功後推播報價給客人"""
    quote = calculate_quote(order)
    if quote['base_price'] == 0:
        return  # 無報價規則，不推播

    rows = [make_info_row(item['label'], f"NT${item['amount']:,}") for item in quote['breakdown']]
    rows.append({"type": "separator", "margin": "md"})
    rows.append({
        "type": "box", "layout": "horizontal", "margin": "md",
        "contents": [
            {"type": "text", "text": "預估總費用", "weight": "bold", "flex": 3, "size": "md"},
            {"type": "text", "text": f"NT${quote['total']:,}", "weight": "bold", "flex": 5,
             "color": "#E05C00", "size": "lg", "align": "end"}
        ]
    })
    rows.append({
        "type": "text",
        "text": "以上為預估報價，實際費用以出發當日為準。如有疑問請聯繫客服。",
        "size": "xs", "color": "#A0AEC0", "margin": "md", "wrap": True
    })

    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "預約報價明細", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": f"訂單 #{order.id}　{order.booking_date} {order.booking_time}",
                 "color": "#8BA3C7", "size": "sm"}
            ]},
        "body": {"type": "box", "layout": "vertical", "contents": rows}
    }
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=order.line_user_id,
                    messages=[FlexMessage(alt_text='您的預約報價明細', contents=FlexContainer.from_dict(bubble))]
                )
            )
    except Exception as e:
        print(f'Quote push error: {e}')


# ── Admin: Pricing ────────────────────────────────────────────────────
@app.route('/admin/pricing')
@admin_required
def admin_pricing():
    rules = PriceRule.query.order_by(PriceRule.sort_order).all()
    surcharges = PriceSurcharge.query.all()
    holidays = HolidaySurcharge.query.order_by(HolidaySurcharge.date_from).all()
    return render_template('admin/pricing.html', rules=rules, surcharges=surcharges, holidays=holidays)

@app.route('/admin/pricing/rules/add', methods=['POST'])
@admin_required
def admin_add_price_rule():
    r = PriceRule(
        name=request.form.get('name'),
        airport_keyword=request.form.get('airport_keyword',''),
        region_keyword=request.form.get('region_keyword',''),
        base_price=int(request.form.get('base_price',0)),
        note=request.form.get('note',''),
        sort_order=int(request.form.get('sort_order',99)),
    )
    db.session.add(r); db.session.commit()
    flash('報價規則已新增'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/rules/<int:rid>/edit', methods=['POST'])
@admin_required
def admin_edit_price_rule(rid):
    r = PriceRule.query.get_or_404(rid)
    r.name = request.form.get('name')
    r.airport_keyword = request.form.get('airport_keyword','')
    r.region_keyword = request.form.get('region_keyword','')
    r.base_price = int(request.form.get('base_price',0))
    r.note = request.form.get('note','')
    r.sort_order = int(request.form.get('sort_order',99))
    r.active = request.form.get('active') == '1'
    db.session.commit()
    flash('報價規則已更新'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/rules/<int:rid>/delete', methods=['POST'])
@admin_required
def admin_delete_price_rule(rid):
    r = PriceRule.query.get_or_404(rid)
    db.session.delete(r); db.session.commit()
    flash('報價規則已刪除'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/surcharges/<int:sid>/edit', methods=['POST'])
@admin_required
def admin_edit_surcharge(sid):
    s = PriceSurcharge.query.get_or_404(sid)
    s.amount = int(request.form.get('amount', s.amount))
    s.enabled = request.form.get('enabled') == '1'
    s.note = request.form.get('note', '')
    db.session.commit()
    flash(f'{s.name} 已更新'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/holidays/add', methods=['POST'])
@admin_required
def admin_add_holiday():
    h = HolidaySurcharge(
        name=request.form.get('name',''),
        date_from=request.form.get('date_from'),
        date_to=request.form.get('date_to'),
        amount=int(request.form.get('amount',300)),
    )
    db.session.add(h); db.session.commit()
    flash('假日加收已新增'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/holidays/<int:hid>/edit', methods=['POST'])
@admin_required
def admin_edit_holiday(hid):
    h = HolidaySurcharge.query.get_or_404(hid)
    h.name = request.form.get('name','')
    h.date_from = request.form.get('date_from')
    h.date_to = request.form.get('date_to')
    h.amount = int(request.form.get('amount',300))
    h.active = request.form.get('active') == '1'
    db.session.commit()
    flash('假日加收已更新'); return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/holidays/<int:hid>/delete', methods=['POST'])
@admin_required
def admin_delete_holiday(hid):
    h = HolidaySurcharge.query.get_or_404(hid)
    db.session.delete(h); db.session.commit()
    flash('假日加收已刪除'); return redirect(url_for('admin_pricing'))

# ── LINE Handlers ────────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    session = user_sessions.get(user_id, {})
    step = session.get('step', '')

    # ── 特殊指令（優先處理）────────────────────────────────────────
    if text in ['我的ID', 'myid', 'MY ID']:
        reply_text(event.reply_token, f'您的 LINE User ID：\n{user_id}\n\n請將此 ID 提供給管理員，設定後即可接收搶單通知。')
        return

    if text == '取消':
        user_sessions.pop(user_id, None)
        reply_text(event.reply_token, '已取消操作。')
        send_main_menu(event.reply_token)
        return

    # ── 觸發主選單關鍵字 ──────────────────────────────────────────
    if text in ['開始', 'hi', 'Hi', 'HI', 'hello', 'Hello', '你好', '哈囉'] and not step:
        send_main_menu(event.reply_token)
        return

    # ── 預約相關關鍵字（直接跳入預約流程）──────────────────────────
    if text in ['預約', '訂車', '機場接送', '開始預約']:
        user_sessions[user_id] = {'step': 'choose_service'}
        send_service_menu(event.reply_token)
        return

    if text == '查詢訂單':
        user_sessions[user_id] = {'step': 'query_name'}
        reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
        return

    # ── AI 聊天模式 ────────────────────────────────────────────────
    if step == 'ai_chat':
        # 在 AI 模式中，若客人說要預約就切換流程
        if any(kw in text for kw in ['預約', '訂車', '我要訂', '我想訂', '幫我訂']):
            user_sessions[user_id] = {'step': 'choose_service'}
            reply_text(event.reply_token, '好的！幫您切換到預約流程 ✈️')
            send_service_menu(event.reply_token)
            return
        if any(kw in text for kw in ['查詢', '我的訂單', '訂單狀態']):
            user_sessions[user_id] = {'step': 'query_name'}
            reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
            return
        # 一般 AI 對話（無記憶，每次獨立）
        ai_reply = ask_openai(user_id, text)
        reply_text(event.reply_token, ai_reply)
        return

    if step == 'query_name':
        session['query_name'] = text
        session['step'] = 'query_phone'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入您預約時留的手機號碼（例：0912345678）：')

    elif step == 'query_phone':
        orders = Order.query.filter_by(name=session.get('query_name'), phone=text)\
                            .order_by(Order.created_at.desc()).limit(5).all()
        user_sessions.pop(user_id, None)
        if orders:
            send_order_query_result(event.reply_token, orders)
        else:
            reply_text(event.reply_token, f'查無符合資料。\n姓名：{session.get("query_name")}\n電話：{text}\n\n請確認資料是否正確。')

    elif step == 'input_pickup':
        session['pickup'] = text
        session['step'] = 'input_date'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入接送日期（格式：2025-06-15）：')

    elif step == 'input_date':
        try:
            datetime.strptime(text, '%Y-%m-%d')
            session['date'] = text
            session['step'] = 'input_time'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入接送時間（格式：08:30）：\n\n注意：22:00～06:00 為夜間時段，需加收 NT$200 夜間服務費。')
        except ValueError:
            reply_text(event.reply_token, '日期格式錯誤，請重新輸入，例如：2025-06-15')

    elif step == 'input_time':
        try:
            datetime.strptime(text, '%H:%M')
            session['time'] = text
            session['night_fee'] = is_night_time(text)
            session['step'] = 'input_passengers'
            user_sessions[user_id] = session
            night_msg = '\n（夜間時段，將加收 NT$200）' if session['night_fee'] else ''
            reply_text(event.reply_token, f'已記錄時間：{text}{night_msg}\n\n請輸入乘客人數（數字）：')
        except ValueError:
            reply_text(event.reply_token, '時間格式錯誤，請重新輸入，例如：08:30')

    elif step == 'input_passengers':
        if text.isdigit() and 1 <= int(text) <= 20:
            session['passengers'] = text
            session['step'] = 'input_luggage'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入行李件數（數字）：')
        else:
            reply_text(event.reply_token, '請輸入有效的乘客人數（1-20）：')

    elif step == 'input_luggage':
        if text.isdigit():
            session['luggage'] = text
            session['step'] = 'input_name'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入您的中文姓名：')
        else:
            reply_text(event.reply_token, '請輸入有效的行李件數（數字）：')

    elif step == 'input_name':
        session['name'] = text
        session['step'] = 'input_phone'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入您的手機號碼（例：0912345678）：')

    elif step == 'input_phone':
        if len(text) == 10 and text.startswith('09'):
            session['phone'] = text
            session['step'] = 'input_email'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入您的電子信箱（若無請輸入「無」）：')
        else:
            reply_text(event.reply_token, '請輸入有效的手機號碼（例：0912345678）：')

    elif step == 'input_email':
        session['email'] = '' if text == '無' else text
        session['step'] = 'input_flight'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入航班號碼（若無請輸入「無」）：')

    elif step == 'input_flight':
        session['flight'] = '' if text == '無' else text
        session['step'] = 'ask_child_seat'
        user_sessions[user_id] = session
        send_child_seat_menu(event.reply_token)

    elif step == 'input_child_seat_count':
        if text.isdigit() and 1 <= int(text) <= 2:
            session['child_seat_count'] = int(text)
            session['step'] = 'ask_sign_board'
            user_sessions[user_id] = session
            send_sign_board_menu(event.reply_token)
        else:
            reply_text(event.reply_token, '每車最多 2 張，請輸入 1 或 2：')

    elif step == 'input_note':
        session['note'] = '' if text == '無' else text
        session['step'] = 'ask_extra_stops'
        session['extra_stops'] = []
        session['extra_stop_fee'] = 0
        user_sessions[user_id] = session
        send_extra_stops_menu(event.reply_token)

    elif step == 'input_extra_stop':
        stop_addr = text.strip()
        stops = session.get('extra_stops', [])
        # 計算與主接送地點或上一個點的距離
        origin = stops[-1] if stops else session.get('pickup', '')
        distance_km = get_distance_km(origin, stop_addr)
        fee, km = calc_extra_stop_fee(distance_km)
        stops.append(stop_addr)
        session['extra_stops'] = stops
        if fee:
            session['extra_stop_fee'] = session.get('extra_stop_fee', 0) + fee
            dist_text = f'（距離約 {km} 公里，加收 NT${fee}）'
        else:
            dist_text = '（距離計算失敗，費用待確認）'
        session['step'] = 'ask_more_stops'
        user_sessions[user_id] = session
        total_stops = len(stops)
        reply_text(event.reply_token,
            f'已新增第 {total_stops} 個停靠點：{stop_addr}\n{dist_text}\n\n'
            f'目前多點加收合計：NT${session["extra_stop_fee"]}\n\n'
            f'是否還要新增停靠點？\n輸入「繼續」新增下一點\n輸入「完成」進入確認'
        )

    elif step == 'ask_more_stops':
        if text == '繼續':
            session['step'] = 'input_extra_stop'
            user_sessions[user_id] = session
            stop_num = len(session.get('extra_stops', [])) + 1
            reply_text(event.reply_token, f'請輸入第 {stop_num} 個停靠點地址：')
        else:
            # 完成 or 任何其他輸入都進入確認
            session['step'] = 'confirm'
            user_sessions[user_id] = session
            send_order_confirm(event.reply_token, session)

    else:
        # 沒有進行中流程 → AI 回覆（若有 OpenAI Key）或顯示主選單
        if OPENAI_API_KEY:
            user_sessions[user_id] = {'step': 'ai_chat'}
            ai_reply = ask_openai(user_id, text)
            reply_text(event.reply_token, ai_reply)
        else:
            send_main_menu(event.reply_token)


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    session = user_sessions.get(user_id, {})

    if data == 'service_departure':
        session = {'step': 'choose_vehicle', 'service': 'departure', 'service_name': '送機（出境）'}
        user_sessions[user_id] = session
        send_vehicle_menu(event.reply_token)

    elif data == 'service_arrival':
        session = {'step': 'choose_vehicle', 'service': 'arrival', 'service_name': '接機（回國）'}
        user_sessions[user_id] = session
        send_vehicle_menu(event.reply_token)

    elif data.startswith('vehicle_'):
        v_id = data.replace('vehicle_', '')
        veh = VehicleType.query.get(int(v_id))
        session['vehicle'] = veh.name if veh else '標準國產四座轎車'
        session['step'] = 'choose_airport'
        user_sessions[user_id] = session
        send_airport_menu(event.reply_token)

    elif data.startswith('airport_'):
        a_id = data.replace('airport_', '')
        apt = AirportOption.query.get(int(a_id))
        session['airport'] = apt.name if apt else '桃園機場第一航廈'
        session['step'] = 'input_pickup'
        user_sessions[user_id] = session
        if session.get('service') == 'departure':
            reply_text(event.reply_token, '請輸入接送地點（起點）：\n例：台北市信義區忠孝東路五段1號')
        else:
            reply_text(event.reply_token, '請輸入目的地（終點）：\n例：台北市信義區忠孝東路五段1號')

    elif data.startswith('child_seat_'):
        seat_key = data.replace('child_seat_', '')
        if seat_key == 'none':
            session['child_seat'] = ''
            session['child_seat_count'] = 0
            session['step'] = 'ask_sign_board'
            user_sessions[user_id] = session
            send_sign_board_menu(event.reply_token)
        else:
            session['child_seat'] = CHILD_SEATS.get(seat_key, '')
            session['step'] = 'input_child_seat_count'
            user_sessions[user_id] = session
            reply_text(event.reply_token, f'已選擇：{session["child_seat"]}\n\n請輸入需要幾張安全座椅（最多 2 張）：')

    elif data == 'sign_board_yes':
        session['sign_board'] = True
        session['step'] = 'ask_pet'
        user_sessions[user_id] = session
        send_pet_menu(event.reply_token)

    elif data == 'sign_board_no':
        session['sign_board'] = False
        session['step'] = 'ask_pet'
        user_sessions[user_id] = session
        send_pet_menu(event.reply_token)

    elif data == 'pet_yes':
        if '九座' not in session.get('vehicle', ''):
            session['vehicle'] = '九座廂型車'
        session['pet'] = True
        session['step'] = 'input_note'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '寵物同行加收：車型加價 NT$300 + 清潔費 NT$800 = NT$1,100\n（已自動調整為九座廂型車）\n\n請輸入備註事項（若無請輸入「無」）：')

    elif data == 'pet_no':
        session['pet'] = False
        session['step'] = 'input_note'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入備註事項（若無請輸入「無」）：')

    elif data == 'confirm_order' or data.startswith('confirm_order:'):
        # 優先從 postback data 解碼 session，避免 worker 重啟後 memory session 丟失
        if ':' in data:
            decoded = decode_session(data.split(':', 1)[1])
            if decoded:
                session = decoded
        if not session or not session.get('name'):
            reply_text(event.reply_token, '預約資料已逾時，請輸入「預約」重新填寫。')
        else:
            save_order(event.reply_token, session, user_id)
        user_sessions.pop(user_id, None)

    elif data == 'cancel_order':
        user_sessions.pop(user_id, None)
        reply_text(event.reply_token, '已取消預約。\n\n輸入「預約」重新開始。')

# ── Menu senders ─────────────────────────────────────────────────────
def send_main_menu(reply_token):
    reply_text(reply_token,
        '歡迎使用機場接送服務！\n\n'
        '請輸入以下指令：\n'
        '「預約」- 開始預約接送\n'
        '「查詢訂單」- 查詢訂單狀態\n'
        '「取消」- 取消目前操作'
    )

def send_service_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("機場接送預約"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇服務類型", "size": "md", "color": "#333333", "margin": "md"},
            {"type": "separator", "margin": "md"},
            make_button("預約送機（出境）", "service_departure", "primary"),
            make_button("預約接機（回國）", "service_arrival"),
        ]}
    }
    send_flex(reply_token, '選擇服務類型', bubble)

def send_vehicle_menu(reply_token):
    vehicles = get_vehicles()
    buttons = [make_button(v.name, f"vehicle_{v.id}") for v in vehicles]
    desc_lines = [f"• {v.name}：最多{v.capacity}人 / 大件{v.luggage_capacity}件" for v in vehicles]
    bubble = {
        "type": "bubble",
        "header": header_box("選擇車型"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇車型", "size": "md", "color": "#333333"},
            {"type": "text", "text": "\n".join(desc_lines),
             "size": "xs", "color": "#888888", "margin": "sm", "wrap": True},
        ] + buttons}
    }
    send_flex(reply_token, '選擇車型', bubble)

def send_airport_menu(reply_token):
    airports = get_airports()
    buttons = [make_button(a.name, f"airport_{a.id}") for a in airports]
    bubble = {
        "type": "bubble",
        "header": header_box("選擇機場"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇機場", "size": "md", "color": "#333333"}
        ] + buttons}
    }
    send_flex(reply_token, '選擇機場', bubble)

def send_child_seat_menu(reply_token):
    buttons = [make_button(name, f"child_seat_{key}") for key, name in CHILD_SEATS.items()]
    bubble = {
        "type": "bubble",
        "header": header_box("兒童安全座椅"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要兒童安全座椅？", "size": "md", "color": "#333333"},
            {"type": "text", "text": "每張加收 NT$100，每車最多 2 張", "size": "xs", "color": "#E05C00", "margin": "sm"},
        ] + buttons}
    }
    send_flex(reply_token, '兒童安全座椅', bubble)

def send_sign_board_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("舉牌服務"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要舉牌服務？", "size": "md", "color": "#333333"},
            {"type": "text", "text": "司機於接機出口舉名牌等候，加收 NT$200", "size": "xs", "color": "#888888", "margin": "sm"},
            make_button("需要舉牌（+NT$200）", "sign_board_yes"),
            make_button("不需要", "sign_board_no"),
        ]}
    }
    send_flex(reply_token, '舉牌服務', bubble)

def send_pet_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("寵物同行"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否有寵物同行？", "size": "md", "color": "#333333"},
            {"type": "text", "text": "需指定九座車，加收車型費 NT$300 + 清潔費 NT$800", "size": "xs", "color": "#888888", "margin": "sm"},
            make_button("有寵物同行（+NT$1,100）", "pet_yes"),
            make_button("沒有", "pet_no"),
        ]}
    }
    send_flex(reply_token, '寵物同行', bubble)

# ── OpenAI AI 客服 ────────────────────────────────────────────────────
AI_SYSTEM_PROMPT = """你是「機場接送服務」的親切客服助理，名字叫「小飛」。

【個性】
親切、有溫度，像朋友一樣，但仍保持專業。可以輕鬆閒聊，但永遠把服務放在心上。

【你能做的事】
1. 回答機場接送相關問題（費用、流程、車型、注意事項）
2. 查詢客人的訂單（需要姓名或電話，請引導他們輸入「查詢訂單」由系統處理）
3. 引導客人預約（請他輸入「預約」進入正式預約流程）
4. 一般閒聊，讓客人感到輕鬆

【費用資訊】
- 基本車資依出發地區域與機場而定（例：台中→桃園機場約 NT$2,200）
- 夜間費（22:00–06:00）：+NT$200
- 舉牌服務：+NT$300
- 兒童安全座椅：+NT$200 / 張（最多2張）
- 寵物同行：+NT$1,100（自動升等九座廂型車）
- 七天內預約：+NT$300
- 三天內臨時單：+NT$300
- 假日/旺季期間：+NT$300
- 多點停靠：依距離 +NT$200–500

【車型】
- 標準四座轎車：最多4人
- 商務六座廂型車：最多6人
- 豪華七座SUV：最多7人
- 九座廂型車：最多9人

【回覆原則】
- 全程使用繁體中文
- 回覆要簡潔口語，不要太正式或太長
- 若客人問具體訂單狀態，請他輸入「查詢訂單」由系統查詢
- 若客人要預約，請他輸入「預約」進入預約流程
- 不確定的資訊不要亂猜，誠實說不確定並建議聯繫客服
"""

def ask_openai(user_id, user_message, order_context=None):
    """呼叫 OpenAI API（無記憶模式），回傳 AI 回應文字"""
    if not OPENAI_API_KEY:
        return '目前 AI 客服功能未啟用，請輸入「預約」開始預約，或輸入「查詢訂單」查詢訂單。'

    system = AI_SYSTEM_PROMPT
    if order_context:
        system += f"\n\n【客人訂單資料（僅供參考）】\n{order_context}"

    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user_message},
    ]

    try:
        resp = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': 'gpt-4o-mini',
                'messages': messages,
                'max_tokens': 400,
                'temperature': 0.75,
            },
            timeout=15
        )
        data = resp.json()
        return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'OpenAI error: {e}')
        return '抱歉，AI 客服暫時無法回應，請稍後再試，或輸入「預約」開始預約流程。'


def send_main_menu(reply_token):
    """主選單：選擇 AI 聊天 或 開始預約"""
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "機場接送服務", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": "您好！請問需要什麼服務？", "color": "#8BA3C7", "size": "sm", "margin": "sm"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "開始預約", "data": "start_booking"},
                    "style": "primary", "color": "#4A9B8F",
                },
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "詢問客服 / 了解服務", "data": "start_ai_chat"},
                    "style": "secondary",
                },
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "查詢我的訂單", "data": "query_order_start"},
                    "style": "secondary",
                },
            ]
        }
    }
    send_flex(reply_token, '機場接送服務', bubble)

def send_main_menu_after(reply_token, user_id=None):
    """送出主選單（用 reply_token）"""
    send_main_menu(reply_token)


def send_extra_stops_menu(reply_token):
    """詢問是否有多點停靠"""
    bubble = {
        "type": "bubble",
        "header": header_box("多點停靠服務"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要途中加停？", "weight": "bold", "size": "md"},
            {"type": "text", "text": "系統將依停靠點與出發地距離自動計算加收費用：", "size": "sm", "color": "#718096", "margin": "sm", "wrap": True},
            {"type": "separator", "margin": "md"},
            make_info_row("5 公里以內", "+NT$200"),
            make_info_row("5–12 公里", "+NT$300"),
            make_info_row("12–18 公里", "+NT$400"),
            make_info_row("超過 18 公里", "+NT$500"),
        ]},
        "footer": {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "不需要，直接報價", "data": "no_extra_stops"},
             "style": "primary", "color": "#4A9B8F", "flex": 1},
            {"type": "button", "action": {"type": "postback", "label": "新增停靠點", "data": "add_extra_stop"},
             "style": "secondary", "flex": 1},
        ]}
    }
    send_flex(reply_token, '多點停靠服務', bubble)


def build_quote_from_session(session):
    """從 session 預先計算報價（訂單未儲存，用假 Order 物件）"""
    class FakeOrder:
        pass
    o = FakeOrder()
    o.airport        = session.get('airport', '')
    o.pickup_location= session.get('pickup', '')
    o.night_fee      = session.get('night_fee', False)
    o.sign_board     = session.get('sign_board', False)
    o.child_seat_count = session.get('child_seat_count', 0)
    o.pet            = session.get('pet', False)
    o.booking_date   = session.get('date', '')
    o.extra_stop_fee = session.get('extra_stop_fee', 0)
    quote = calculate_quote(o)
    # 加入多點費用
    if o.extra_stop_fee:
        quote['surcharges'].append({'label': '多點加收', 'amount': o.extra_stop_fee})
        quote['breakdown'].append({'label': '多點加收', 'amount': o.extra_stop_fee})
        quote['total'] += o.extra_stop_fee
    return quote


def send_order_confirm(reply_token, session):
    extras = []
    if session.get('night_fee'): extras.append('夜間服務費')
    if session.get('sign_board'): extras.append('舉牌服務')
    if session.get('child_seat_count', 0):
        extras.append(f'兒童安全座椅 x{session["child_seat_count"]}')
    if session.get('pet'): extras.append('寵物同行')
    extra_stops = session.get('extra_stops', [])

    # 計算報價
    quote = build_quote_from_session(session)

    # 報價明細 rows
    quote_rows = []
    for item in quote['breakdown']:
        quote_rows.append(make_info_row(item['label'], f"NT${item['amount']:,}"))
    if quote['total'] > 0:
        quote_rows.append({"type": "separator", "margin": "sm"})
        quote_rows.append({
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": "預估總費用", "weight": "bold", "flex": 3, "size": "sm"},
                {"type": "text", "text": f"NT${quote['total']:,}", "weight": "bold",
                 "flex": 5, "color": "#E05C00", "size": "lg", "align": "end"}
            ]
        })

    # 行程資料 rows
    body_rows = [
        make_info_row("服務類型", session.get('service_name', '')),
        make_info_row("車型", session.get('vehicle', '')),
        make_info_row("機場", session.get('airport', '')),
        make_info_row("出發地", session.get('pickup', '')),
    ]
    if extra_stops:
        for i, stop in enumerate(extra_stops, 1):
            body_rows.append(make_info_row(f"停靠點 {i}", stop))
    body_rows += [
        make_info_row("日期", session.get('date', '')),
        make_info_row("時間", session.get('time', '')),
        make_info_row("乘客", f"{session.get('passengers', '')} 人"),
        make_info_row("行李", f"{session.get('luggage', '')} 件"),
        make_info_row("姓名", session.get('name', '')),
        make_info_row("電話", session.get('phone', '')),
        make_info_row("信箱", session.get('email', '') or '無'),
        make_info_row("航班", session.get('flight', '') or '無'),
        make_info_row("加購", '、'.join(extras) if extras else '無'),
        make_info_row("備註", session.get('note', '') or '無'),
    ]

    # 兩個 bubble：1. 報價 2. 確認資料
    quote_bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "預估報價明細", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": "實際費用以出發當日為準", "color": "#8BA3C7", "size": "xs"}
            ]},
        "body": {"type": "box", "layout": "vertical", "contents":
            quote_rows if quote_rows else [{"type": "text", "text": "尚未設定此區域報價，將由客服確認", "color": "#A0AEC0", "size": "sm", "wrap": True}]
        }
    }

    confirm_bubble = {
        "type": "bubble",
        "header": header_box("確認預約資料"),
        "body": {"type": "box", "layout": "vertical", "contents": body_rows + [
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "確認以上資料無誤後送出預約", "margin": "md", "color": "#E05C00", "weight": "bold", "size": "sm", "wrap": True}
        ]},
        "footer": {"type": "box", "layout": "horizontal", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "確認送出", "data": "confirm_order"},
             "style": "primary", "color": "#4A9B8F", "flex": 1},
            {"type": "separator"},
            {"type": "button", "action": {"type": "postback", "label": "取消重填", "data": "cancel_order"},
             "style": "secondary", "flex": 1}
        ]}
    }

    carousel = {
        "type": "carousel",
        "contents": [quote_bubble, confirm_bubble]
    }
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token,
                messages=[FlexMessage(alt_text='預估報價與確認預約資料', contents=FlexContainer.from_dict(carousel))])
        )

def save_order(reply_token, session, user_id):
    with app.app_context():
        order = Order(
            line_user_id=user_id,
            service_type=session.get('service', ''),
            service_name=session.get('service_name', ''),
            vehicle=session.get('vehicle', ''),
            airport=session.get('airport', ''),
            pickup_location=session.get('pickup', ''),
            booking_date=session.get('date', ''),
            booking_time=session.get('time', ''),
            passengers=int(session.get('passengers', 1)),
            luggage=int(session.get('luggage', 0)),
            name=session.get('name', ''),
            phone=session.get('phone', ''),
            email=session.get('email', ''),
            flight_number=session.get('flight', ''),
            night_fee=session.get('night_fee', False),
            sign_board=session.get('sign_board', False),
            child_seat=session.get('child_seat', ''),
            child_seat_count=session.get('child_seat_count', 0),
            pet=session.get('pet', False),
            note=session.get('note', ''),
            extra_stops=json.dumps(session.get('extra_stops', []), ensure_ascii=False),
            extra_stop_fee=session.get('extra_stop_fee', 0),
            status='待確認'
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id
        # 若開啟自動搶單模式，立即發布給所有司機
        if AUTO_DISPATCH:
            try:
                auto_dispatch_order(order_id)
            except Exception as e:
                print(f'Auto dispatch error: {e}')
        # 推播報價給客人
        try:
            send_quote_to_customer(order)
        except Exception as e:
            print(f'Quote error: {e}')

    bubble = {
        "type": "bubble",
        "header": header_box("預約成功！"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": f"訂單編號：#{order_id}", "size": "lg", "weight": "bold", "color": "#4A9B8F"},
            {"type": "text", "text": "我們將盡快與您確認訂單。", "margin": "md", "wrap": True},
            {"type": "text", "text": "如需查詢訂單狀態，請輸入「查詢訂單」。", "margin": "sm", "size": "sm", "color": "#888888", "wrap": True},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "注意事項", "margin": "md", "weight": "bold", "size": "sm"},
            {"type": "text", "text": "• 請於出發日 48 小時前預約\n• 需變更請於 48 小時前來電\n• 車輛均投保 300 萬乘客險",
             "size": "xs", "color": "#888888", "margin": "sm", "wrap": True}
        ]}
    }
    send_flex(reply_token, '預約成功', bubble)

def send_order_query_result(reply_token, orders):
    bubbles = []
    for order in orders:
        bubbles.append({
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F", "contents": [
                {"type": "text", "text": f"訂單 #{order.id}", "color": "#FFFFFF", "size": "lg", "weight": "bold"},
                {"type": "text", "text": order.created_at.strftime('%Y-%m-%d %H:%M'), "color": "#DDDDDD", "size": "sm"}
            ]},
            "body": {"type": "box", "layout": "vertical", "contents": [
                make_info_row("狀態", order.status),
                make_info_row("服務", order.service_name),
                make_info_row("車型", order.vehicle),
                make_info_row("機場", order.airport),
                make_info_row("日期", order.booking_date),
                make_info_row("時間", order.booking_time),
                make_info_row("地點", order.pickup_location),
            ]}
        })
    send_flex(reply_token, '訂單查詢結果', {"type": "carousel", "contents": bubbles})

def notify_admin_grab(order, driver):
    """搶單成功時推播通知後台管理員"""
    if not ADMIN_LINE_USER_ID:
        return
    try:
        text = (
            f"搶單成功通知\n"
            f"訂單 #{order.id}\n"
            f"司機：{driver.name}（{driver.phone}）\n"
            f"客戶：{order.name}（{order.phone}）\n"
            f"日期：{order.booking_date} {order.booking_time}\n"
            f"機場：{order.airport}\n"
            f"車牌：{driver.car_plate or '未填'}"
        )
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=ADMIN_LINE_USER_ID, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        print(f'Admin notify error: {e}')


def auto_dispatch_order(order_id):
    """訂單確認後自動發布搶單（需在後台設定啟用）"""
    with app.app_context():
        order = Order.query.get(order_id)
        if not order:
            return
        if hasattr(order, 'dispatch_job') and order.dispatch_job:
            return  # 已有搶單任務
        job = DispatchJob(order_id=order_id, status='開放搶單', notify_customer=True)
        db.session.add(job)
        db.session.commit()
        drivers = Driver.query.filter(Driver.active == True, Driver.line_user_id != '').all()
        order.status = '搶單中'
        db.session.commit()
        for d in drivers:
            try:
                push_dispatch_to_driver(d, order, job)
            except Exception as e:
                print(f'Auto dispatch push error driver {d.id}: {e}')


def handle_driver_grab(reply_token, driver_line_id, job_id):
    """處理司機搶單邏輯"""
    with app.app_context():
        job = DispatchJob.query.get(job_id)
        if not job:
            reply_text(reply_token, '查無此搶單任務。')
            return
        if job.status != '開放搶單':
            reply_text(reply_token, f'此訂單已{job.status}，搶單結束。')
            return
        driver = Driver.query.filter_by(line_user_id=driver_line_id).first()
        if not driver:
            reply_text(reply_token, '查無您的司機資料，請聯繫管理員。')
            return
        # 已搶過了
        existing = DispatchResponse.query.filter_by(job_id=job_id, driver_id=driver.id).first()
        if existing:
            reply_text(reply_token, '您已回應過此訂單。')
            return
        # 搶單成功：更新任務、訂單
        job.status = '已結單'
        job.grabbed_by = driver.id
        job.grabbed_at = datetime.utcnow()
        order = job.order
        order.driver_id = driver.id
        order.status = '已確認'
        db.session.add(DispatchResponse(job_id=job_id, driver_id=driver.id, action='搶單'))
        db.session.commit()

        # 通知搶到的司機完整客戶資料
        bubble = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F",
                "contents": [
                    {"type": "text", "text": "搶單成功！", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                    {"type": "text", "text": f"訂單 #{order.id}", "color": "#DDDDDD", "size": "sm"}
                ]},
            "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {"type": "text", "text": "客戶完整資料", "weight": "bold", "color": "#1A2B4A", "margin": "sm"},
                    {"type": "separator", "margin": "sm"},
                    make_info_row("姓名", order.name),
                    make_info_row("電話", order.phone),
                    make_info_row("信箱", order.email or '無'),
                    make_info_row("航班", order.flight_number or '無'),
                    {"type": "separator", "margin": "sm"},
                    make_info_row("服務", order.service_name),
                    make_info_row("車型", order.vehicle),
                    make_info_row("機場", order.airport),
                    make_info_row("接送地點", order.pickup_location),
                    make_info_row("日期時間", f"{order.booking_date} {order.booking_time}"),
                    make_info_row("乘客/行李", f"{order.passengers}人 / {order.luggage}件"),
                    make_info_row("備註", order.note or '無'),
                ]}
        }
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token,
                    messages=[FlexMessage(alt_text='搶單成功！客戶資料如下', contents=FlexContainer.from_dict(bubble))])
            )
        # 自動通知客人司機資料
        if job.notify_customer:
            try:
                send_driver_info_to_customer(order, driver)
                order.driver_notified = True
                db.session.commit()
            except Exception as e:
                print(f'Auto notify customer error: {e}')

        # 通知後台管理員
        notify_admin_grab(order, driver)

# ── Start ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_notify, 'interval', minutes=1)
    scheduler.start()

    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
else:
    # 在 gunicorn 下也啟動排程
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_notify, 'interval', minutes=1)
    scheduler.start()

    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()