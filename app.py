import os
import json
import base64
import threading
import time
import requests
import hashlib
import hmac
import urllib.parse
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
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
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif database_url.startswith('postgresql://') and '+' not in database_url.split('://')[0]:
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'poolclass': NullPool,
    'pool_pre_ping': True,
}

db.init_app(app)
with app.app_context():
    db.create_all()
    _migrations = [
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stops TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stop_fee INTEGER DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS deposit_paid BOOLEAN DEFAULT FALSE",
        "ALTER TABLE drivers ADD COLUMN IF NOT EXISTS line_user_id VARCHAR(100) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS deadline TIMESTAMP",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS note VARCHAR(200) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS notify_customer BOOLEAN DEFAULT TRUE",
        "CREATE TABLE IF NOT EXISTS price_rules (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, airport_keyword VARCHAR(50) DEFAULT '', region_keyword VARCHAR(100) DEFAULT '', base_price INTEGER DEFAULT 0, note TEXT DEFAULT '', active BOOLEAN DEFAULT TRUE, sort_order INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS price_surcharges (id SERIAL PRIMARY KEY, key VARCHAR(50) UNIQUE NOT NULL, name VARCHAR(50) NOT NULL, amount INTEGER DEFAULT 0, enabled BOOLEAN DEFAULT TRUE, note VARCHAR(100) DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS holiday_surcharges (id SERIAL PRIMARY KEY, name VARCHAR(50) DEFAULT '', date_from VARCHAR(10) NOT NULL, date_to VARCHAR(10) NOT NULL, amount INTEGER DEFAULT 300, active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT NOW())",
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
            VehicleType(name='不指定車款', capacity=7, luggage_capacity=7, note='最多7人／最多標準29吋7件', sort_order=1),
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
            PriceSurcharge(key='pet',        name='寵物同行',                  amount=300),
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
    if PriceRule.query.count() == 0:
        defaults = [
            PriceRule(
                name='台中－桃園機場',
                airport_keyword='桃園',
                region_keyword='台中,台中市,台中縣',
                base_price=2200,
                note='不指定車款，送機／接機同價 NT$2,200',
                sort_order=1,
                active=True,
            ),
        ]
        db.session.add_all(defaults)
        db.session.commit()

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1234')
ADMIN_LINE_USER_ID = os.environ.get('ADMIN_LINE_USER_ID', '')
HUMAN_AGENT_LINE_ID = os.environ.get('HUMAN_AGENT_LINE_ID', 'rbf5256')  # 真人客服 LINE ID
SUPPORT_GROUP_ID    = os.environ.get('SUPPORT_GROUP_ID', '')             # 客服群組 ID（推播真人客服通知用）
AUTO_DISPATCH = os.environ.get('AUTO_DISPATCH', '0') == '1'
GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')
AVIATION_EDGE_KEY   = os.environ.get('AVIATION_EDGE_KEY', '')  # Aviation Edge API Key（航班查詢）

# ── 藍新金流設定（收款）────────────────────────────────────────────────
NEWEBPAY_MERCHANT_ID  = os.environ.get('NEWEBPAY_MERCHANT_ID', '')   # MS3725965371（測試）
NEWEBPAY_HASH_KEY     = os.environ.get('NEWEBPAY_HASH_KEY', '')
NEWEBPAY_HASH_IV      = os.environ.get('NEWEBPAY_HASH_IV', '')
NEWEBPAY_MODE         = os.environ.get('NEWEBPAY_MODE', 'test')       # test or prod
NEWEBPAY_DEPOSIT      = 315

# ── ezPay 電子發票設定 ──────────────────────────────────────────────
EZPAY_MERCHANT_ID = os.environ.get('EZPAY_MERCHANT_ID', '338919792')
EZPAY_HASH_KEY    = os.environ.get('EZPAY_HASH_KEY', '')
EZPAY_HASH_IV     = os.environ.get('EZPAY_HASH_IV', '')
EZPAY_MODE        = os.environ.get('EZPAY_MODE', 'test')  # 'test' or 'prod'

# ── ezPay 電子發票設定 ────────────────────────────────────────────────
EZPAY_MERCHANT_ID     = os.environ.get('EZPAY_MERCHANT_ID', '338919792')
EZPAY_HASH_KEY        = os.environ.get('EZPAY_HASH_KEY', 'uXbTWrmBjLArC0Ln93CZEqC20eY5jBE0')
EZPAY_HASH_IV         = os.environ.get('EZPAY_HASH_IV', 'PjvPgMj6OJppH8vC')
EZPAY_MODE            = os.environ.get('EZPAY_MODE', 'prod')           # test or prod

def newebpay_api_url():
    if NEWEBPAY_MODE == 'prod':
        return 'https://core.newebpay.com/MPG/mpg_gateway'
    return 'https://ccore.newebpay.com/MPG/mpg_gateway'

def newebpay_encrypt(trade_info: str) -> str:
    key = NEWEBPAY_HASH_KEY.encode('utf-8')
    iv  = NEWEBPAY_HASH_IV.encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(trade_info.encode('utf-8'), AES.block_size))
    return encrypted.hex()

def newebpay_sha256(trade_info_enc: str) -> str:
    raw = f'HashKey={NEWEBPAY_HASH_KEY}&{trade_info_enc}&HashIV={NEWEBPAY_HASH_IV}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest().upper()

def newebpay_decrypt(trade_info_enc: str) -> dict:
    try:
        key = NEWEBPAY_HASH_KEY.encode('utf-8')
        iv  = NEWEBPAY_HASH_IV.encode('utf-8')
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(bytes.fromhex(trade_info_enc)), AES.block_size)
        return dict(urllib.parse.parse_qsl(decrypted.decode('utf-8')))
    except Exception as e:
        app.logger.error(f'Newebpay decrypt error: {e}')
        return {}

def build_newebpay_form(order_id: int, line_user_id: str, amt: int = NEWEBPAY_DEPOSIT) -> str:
    base_url = os.environ.get('RENDER_EXTERNAL_URL', 'https://airport-reservation.onrender.com')
    trade_info = urllib.parse.urlencode({
        'MerchantID':     NEWEBPAY_MERCHANT_ID,
        'RespondType':    'JSON',
        'TimeStamp':      int(datetime.now().timestamp()),
        'Version':        '2.0',
        'MerchantOrderNo': f'DEP{order_id}',
        'Amt':            amt,
        'ItemDesc':       f'機場接送定金（含稅）訂單#{order_id}',
        'Email':          '',
        'LoginType':      0,
        'CREDIT':         1,   # 信用卡（含分期）
        'ANDROIDPAY':     0,   # Google Pay（需另外申請）
        'SAMSUNGPAY':     0,   # Samsung Pay（需另外申請）
        'APPLEPAY':       0,   # Apple Pay（需另外申請，關閉避免直接跳轉）
        'WEBATM':         1,   # 網路ATM
        'VACC':           1,   # 虛擬帳號
        'CVS':            0,   # 超商代碼
        'BARCODE':        0,   # 超商條碼
        'ReturnURL':      f'{base_url}/newebpay/return',
        'NotifyURL':      f'{base_url}/newebpay/notify',
        'CustomerURL':    f'{base_url}/newebpay/return',
        'ClientBackURL':  f'{base_url}/newebpay/cancel',
    })
    trade_info_enc = newebpay_encrypt(trade_info)
    trade_sha      = newebpay_sha256(trade_info_enc)
    api_url        = newebpay_api_url()
    html = f"""<!DOCTYPE html><html><body onload="document.forms[0].submit()">
<form method="POST" action="{api_url}">
  <input type="hidden" name="MerchantID"  value="{NEWEBPAY_MERCHANT_ID}">
  <input type="hidden" name="TradeInfo"   value="{trade_info_enc}">
  <input type="hidden" name="TradeSha"    value="{trade_sha}">
  <input type="hidden" name="Version"     value="2.0">
</form>
<p>正在前往付款頁面...</p>
</body></html>"""
    return html


# ── ezPay 電子發票開立 ────────────────────────────────────────────────
def ezpay_invoice_encrypt(data_str: str) -> str:
    """AES-256-CBC 加密 ezPay 發票 RespondType"""
    key = EZPAY_HASH_KEY.encode('utf-8')
    iv  = EZPAY_HASH_IV.encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(data_str.encode('utf-8'), AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')

def issue_ezpay_invoice(order, invoice_type, carrier='', tax_id='', company_name=''):
    """呼叫 ezPay API 開立電子發票
    invoice_type: 'personal' / 'company' / ''
    """
    try:
        if EZPAY_MODE == 'prod':
            api_url = 'https://inv.ezpay.com.tw/Api/invoice_issue'
        else:
            api_url = 'https://cinv.ezpay.com.tw/Api/invoice_issue'

        # 買受人資訊
        if invoice_type == 'company':
            buyer_name    = company_name or order.name
            buyer_uni_no  = tax_id
            carrier_type  = ''
            carrier_num   = ''
            print_flag    = '1'   # 紙本發票
        elif invoice_type == 'personal' and carrier:
            buyer_name    = order.name
            buyer_uni_no  = ''
            carrier_type  = '0'   # 手機條碼
            carrier_num   = carrier
            print_flag    = '0'
        else:
            # 不需要 or 個人雲端（無載具）
            buyer_name    = order.name
            buyer_uni_no  = ''
            carrier_type  = ''
            carrier_num   = ''
            print_flag    = '0'

        # 稅額計算（含稅 315 元，稅率 5%）
        amt        = NEWEBPAY_DEPOSIT          # 315 含稅
        tax_amt    = round(amt - amt / 1.05)   # 約 15 元
        amt_excl   = amt - tax_amt             # 未稅 300

        timestamp = int(datetime.now().timestamp())
        resend_mark = '0'

        params = {
            'RespondType':  'JSON',
            'Version':      '1.4',
            'TimeStamp':    timestamp,
            'MerchantOrderNo': f'INV{order.id}',
            'Status':       '1',               # 立即開立
            'Category':     'B2C' if not buyer_uni_no else 'B2B',
            'BuyerName':    buyer_name,
            'BuyerEmail':   order.email or '',
            'BuyerUBN':     buyer_uni_no,
            'CarrierType':  carrier_type,
            'CarrierNum':   carrier_num,
            'PrintFlag':    print_flag,
            'TaxType':      '1',               # 應稅
            'TaxRate':      '5',
            'Amt':          amt_excl,
            'TaxAmt':       tax_amt,
            'TotalAmt':     amt,
            'ItemName':     f'機場接送定金（訂單#{order.id}）',
            'ItemCount':    '1',
            'ItemUnit':     '筆',
            'ItemAmt':      amt_excl,
            'ItemTaxAmt':   tax_amt,
            'Comment':      '',
        }

        post_data_str = urllib.parse.urlencode(params)
        post_data_enc = ezpay_invoice_encrypt(post_data_str)

        resp = requests.post(api_url, data={
            'MerchantID_': EZPAY_MERCHANT_ID,
            'PostData_':   post_data_enc,
        }, timeout=10)

        result = resp.json()
        app.logger.info(f'ezPay invoice result: {result}')

        if result.get('Status') == 'SUCCESS':
            inv_data = result.get('Result', {})
            inv_no   = inv_data.get('InvoiceNumber', '')
            inv_date = inv_data.get('InvoiceDate', '')
            app.logger.info(f'Invoice issued: {inv_no} ({inv_date})')
            return inv_no
        else:
            app.logger.warning(f'ezPay invoice failed: {result.get("Message")}')
            return None

    except Exception as e:
        app.logger.error(f'issue_ezpay_invoice error: {e}')
        return None

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

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
    with app.app_context():
        now = datetime.utcnow() + timedelta(hours=8)
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
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "您的司機資料", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": f"訂單 #{order.id}", "color": "#DDDDDD", "size": "sm", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                make_info_row("出發時間", f"{order.booking_date} {order.booking_time}"),
                make_info_row("服務", order.service_name),
                make_info_row("地點", order.pickup_location),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "司機資訊", "weight": "bold", "margin": "md", "color": "#1A2B4A", "wrap": True},
                make_info_row("司機姓名", driver.name),
                make_info_row("聯絡電話", driver.phone),
                make_info_row("車輛", f"{driver.car_brand}"),
                make_info_row("車牌", driver.car_plate),
                make_info_row("車身顏色", driver.car_color),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "如有任何問題請直接致電司機，祝您旅途愉快！",
                 "size": "xs", "color": "#888888", "margin": "md", "wrap": True}
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
    return {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "xs", "color": "#888888", "wrap": True},
        {"type": "text", "text": str(value), "size": "sm", "color": "#333333", "wrap": True, "margin": "xs"}
    ]}

def parse_date(text):
    text = text.strip().replace('。', '').replace(' ', '')
    now = datetime.now()
    formats = ['%Y-%m-%d', '%Y/%m/%d', '%m/%d', '%m-%d']
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
    digits = ''.join(filter(str.isdigit, text))
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, '%Y%m%d').strftime('%Y-%m-%d')
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
        print('InvalidSignatureError - 請確認 LINE_CHANNEL_SECRET 環境變數正確')
        abort(400)
    except Exception as e:
        import traceback
        print('Callback error:', traceback.format_exc())
        abort(500)
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
    order.driver_notified = False
    db.session.commit()
    flash(f'已指派司機，將於出發前 {notify_at} 小時自動發送司機資料給客人')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/order/<int:order_id>/notify_now', methods=['POST'])
@admin_required
def admin_notify_now(order_id):
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
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "新訂單搶單通知", "color": "#FFFFFF", "size": "lg", "weight": "bold", "wrap": True},
                {"type": "text", "text": f"訂單 #{order.id}　第一個搶到確認！", "color": "#8BA3C7", "size": "sm", "wrap": True}
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
                {"type": "button", "action": {"type": "postback", "label": "我要接單", "data": f"grab:{job.id}"},
                 "style": "primary", "color": "#4A9B8F", "flex": 1},
                {"type": "button", "action": {"type": "postback", "label": "略過", "data": f"skip:{job.id}"},
                 "style": "secondary", "flex": 1}
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
    (5,   200),
    (12,  300),
    (18,  400),
    (999, 500),
]


def query_flight_info(flight_number: str, date_str: str = '') -> dict | None:
    """
    呼叫 Aviation Edge API 查詢航班資訊。
    策略：先查即時（flights），查無再查時刻表（timetable）。
    回傳統一格式 dict 或 None。
    """
    if not AVIATION_EDGE_KEY or not flight_number:
        return None

    fn = flight_number.strip().upper().replace(' ', '')

    def fmt_time(t):
        if not t:
            return ''
        try:
            return datetime.fromisoformat(t[:16]).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return t[:16]

    def parse_record(f):
        dep = f.get('departure', {}) or {}
        arr = f.get('arrival',   {}) or {}
        airline = f.get('airline', {}) or {}
        flight  = f.get('flight',  {}) or {}
        dep_delay = dep.get('delay', 0) or 0
        arr_delay = arr.get('delay', 0) or 0
        return {
            'flight':        fn,
            'airline':       airline.get('name', ''),
            'status':        f.get('status', ''),
            # 出發
            'dep_airport':   dep.get('airport', dep.get('iataCode', '未知')),
            'dep_iata':      dep.get('iataCode', ''),
            'dep_terminal':  dep.get('terminal', ''),
            'dep_gate':      dep.get('gate', ''),
            'dep_scheduled': fmt_time(dep.get('scheduledTime', '')),
            'dep_estimated': fmt_time(dep.get('estimatedTime', '')),
            'dep_actual':    fmt_time(dep.get('actualTime', '')),
            'dep_delay':     int(dep_delay),
            # 抵達
            'arr_airport':   arr.get('airport', arr.get('iataCode', '未知')),
            'arr_iata':      arr.get('iataCode', ''),
            'arr_terminal':  arr.get('terminal', ''),
            'arr_gate':      arr.get('gate', ''),
            'arr_baggage':   arr.get('baggage', ''),
            'arr_scheduled': fmt_time(arr.get('scheduledTime', '')),
            'arr_estimated': fmt_time(arr.get('estimatedTime', '')),
            'arr_actual':    fmt_time(arr.get('actualTime', '')),
            'arr_delay':     int(arr_delay),
        }

    try:
        # ── 方法 1：即時追蹤（航班在空中時有效）──
        resp = requests.get(
            'https://aviation-edge.com/v2/public/flights',
            params={'key': AVIATION_EDGE_KEY, 'flightIata': fn},
            timeout=8
        )
        data = resp.json()
        if isinstance(data, list) and data:
            # 即時 API 格式略不同，轉換欄位
            f = data[0]
            dep = f.get('departure', {}) or {}
            arr = f.get('arrival',   {}) or {}
            airline = f.get('airline', {}) or {}
            geo = f.get('geography', {}) or {}
            return {
                'flight':        fn,
                'airline':       airline.get('iataCode', ''),
                'status':        f.get('status', ''),
                'dep_airport':   dep.get('iataCode', '未知'),
                'dep_iata':      dep.get('iataCode', ''),
                'dep_terminal':  '', 'dep_gate':      '',
                'dep_scheduled': '', 'dep_estimated': '', 'dep_actual': '',
                'dep_delay':     0,
                'arr_airport':   arr.get('iataCode', '未知'),
                'arr_iata':      arr.get('iataCode', ''),
                'arr_terminal':  '', 'arr_gate':      '', 'arr_baggage': '',
                'arr_scheduled': '', 'arr_estimated': '', 'arr_actual': '',
                'arr_delay':     0,
                'altitude':      geo.get('altitude', ''),
                'live':          True,
            }
    except Exception as e:
        app.logger.warning(f'aviation_edge flights error: {e}')

    try:
        # ── 方法 2：時刻表（timetable，起降前後最準確）──
        # 用出發機場查詢，需先知道出發機場 IATA → 用 flight_iata 直接查
        resp = requests.get(
            'https://aviation-edge.com/v2/public/timetable',
            params={
                'key':         AVIATION_EDGE_KEY,
                'flight_iata': fn,
                'type':        'departure',
            },
            timeout=8
        )
        data = resp.json()
        if isinstance(data, list) and data:
            return parse_record(data[0])

        # 試試 arrival
        resp = requests.get(
            'https://aviation-edge.com/v2/public/timetable',
            params={
                'key':         AVIATION_EDGE_KEY,
                'flight_iata': fn,
                'type':        'arrival',
            },
            timeout=8
        )
        data = resp.json()
        if isinstance(data, list) and data:
            return parse_record(data[0])

    except Exception as e:
        app.logger.warning(f'aviation_edge timetable error: {e}')

    return None

def get_distance_km(origin, destination):
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
    if distance_km is None:
        return None, None
    for limit, fee in EXTRA_STOP_TIERS:
        if distance_km <= limit:
            return fee, distance_km
    return 500, distance_km


# ── 報價計算 ─────────────────────────────────────────────────────────
def calculate_quote(order):
    result = {
        'base_price': 0,
        'base_rule': '未設定區域報價',
        'surcharges': [],
        'holiday': None,
        'holiday_amount': 0,
        'total': 0,
        'breakdown': [],
    }
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

    surcharge_map = {s.key: s for s in PriceSurcharge.query.filter_by(enabled=True).all()}
    if order.night_fee and 'night' in surcharge_map:
        amt = surcharge_map['night'].amount
        result['surcharges'].append({'label': surcharge_map['night'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['night'].name, 'amount': amt})

    if order.sign_board and 'sign_board' in surcharge_map:
        amt = surcharge_map['sign_board'].amount
        result['surcharges'].append({'label': surcharge_map['sign_board'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['sign_board'].name, 'amount': amt})

    if order.child_seat_count and 'child_seat' in surcharge_map:
        amt = surcharge_map['child_seat'].amount * order.child_seat_count
        label = f'{surcharge_map["child_seat"].name} x{order.child_seat_count}'
        result['surcharges'].append({'label': label, 'amount': amt})
        result['breakdown'].append({'label': label, 'amount': amt})

    if order.pet and 'pet' in surcharge_map:
        amt = surcharge_map['pet'].amount
        result['surcharges'].append({'label': surcharge_map['pet'].name, 'amount': amt})
        result['breakdown'].append({'label': surcharge_map['pet'].name, 'amount': amt})

    try:
        from datetime import date
        booking = date.fromisoformat(order.booking_date)
        today = date.today()
        days_ahead = (booking - today).days
        if days_ahead <= 7 and 'short_book' in surcharge_map:
            amt = surcharge_map['short_book'].amount
            result['surcharges'].append({'label': surcharge_map['short_book'].name, 'amount': amt})
            result['breakdown'].append({'label': surcharge_map['short_book'].name, 'amount': amt})
        if days_ahead <= 3 and 'urgent' in surcharge_map:
            amt = surcharge_map['urgent'].amount
            result['surcharges'].append({'label': surcharge_map['urgent'].name, 'amount': amt})
            result['breakdown'].append({'label': surcharge_map['urgent'].name, 'amount': amt})
    except Exception:
        pass

    try:
        booking_md = order.booking_date[5:]
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


def _send_quote_bubble(order, quote):
    rows = [make_info_row(item['label'], f"NT${item['amount']:,}") for item in quote['breakdown']]
    rows.append({"type": "separator", "margin": "md"})
    rows.append({
        "type": "box", "layout": "horizontal", "margin": "md",
        "contents": [
            {"type": "text", "text": "預估總費用", "weight": "bold", "flex": 3, "size": "md", "wrap": True},
            {"type": "text", "text": f"NT${quote['total']:,}", "weight": "bold", "flex": 5,
             "color": "#E05C00", "size": "lg", "align": "end", "wrap": True}
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
                 "color": "#8BA3C7", "size": "sm", "wrap": True}
            ]},
        "body": {"type": "box", "layout": "vertical", "contents": rows}
    }
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(
                to=order.line_user_id,
                messages=[FlexMessage(alt_text='預約報價明細', contents=FlexContainer.from_dict(bubble))]
            ))
    except Exception as e:
        app.logger.error(f'send_quote_to_customer error: {e}')


def send_quote_to_customer(order):
    import json as _json
    extra_stops = []
    try:
        extra_stops = _json.loads(order.extra_stops) if order.extra_stops else []
    except Exception:
        pass

    if extra_stops:
        last_stop = extra_stops[-1]
        rules = PriceRule.query.filter_by(active=True).order_by(PriceRule.sort_order).all()
        matched_rule = None
        for rule in rules:
            airport_match = not rule.airport_keyword or rule.airport_keyword in order.airport
            region_match  = not rule.region_keyword or any(
                kw.strip() in last_stop for kw in rule.region_keyword.split(',')
            )
            if airport_match and region_match:
                matched_rule = rule
                break
        if matched_rule:
            original_pickup = order.pickup_location
            order.pickup_location = last_stop
            order.extra_stop_fee  = 0
            quote = calculate_quote(order)
            order.pickup_location = original_pickup
            if quote['breakdown']:
                quote['breakdown'][0]['label'] = f'基本車資（{matched_rule.name}，途經 {original_pickup}）'
            if quote['base_price'] == 0:
                return
            _send_quote_bubble(order, quote)
            return

    quote = calculate_quote(order)
    if order.extra_stop_fee:
        quote['breakdown'].append({'label': '多點加收', 'amount': order.extra_stop_fee})
        quote['total'] += order.extra_stop_fee
    if quote['base_price'] == 0:
        return
    _send_quote_bubble(order, quote)


# ── 藍新金流路由 ─────────────────────────────────────────────────────
@app.route('/pay/<int:order_id>')
def pay_deposit(order_id):
    order = Order.query.get_or_404(order_id)
    if order.status != '待付款':
        return '<h2>此訂單已完成付款或不需付款</h2>', 400
    if not NEWEBPAY_MERCHANT_ID:
        return '<h2>金流尚未設定，請聯繫客服</h2>', 500
    return build_newebpay_form(order_id, order.line_user_id)


@app.route('/newebpay/notify', methods=['POST'])
def newebpay_notify():
    status         = request.form.get('Status')
    trade_info_enc = request.form.get('TradeInfo', '')
    trade_sha      = request.form.get('TradeSha', '')

    expected = newebpay_sha256(trade_info_enc)
    if trade_sha.upper() != expected.upper():
        app.logger.warning('Newebpay TradeSha mismatch')
        return 'FAIL', 400

    if status != 'SUCCESS':
        app.logger.info(f'Newebpay payment failed: {status}')
        return 'OK'

    data = newebpay_decrypt(trade_info_enc)
    merchant_order_no = data.get('MerchantOrderNo', '')
    if not merchant_order_no.startswith('DEP'):
        return 'OK'

    order_id = int(merchant_order_no[3:])
    order = Order.query.get(order_id)
    if not order:
        return 'OK'

    order.status = '待確認'
    db.session.commit()

    notice_text = (
        "✅ 定金支付成功！\n\n"
        f"訂單編號：#{order.id}\n"
        "我們將盡快與您確認訂單，感謝您的預約！\n\n"
        "預約須知與注意事項\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "【接機說明】\n"
        "• 接機以航班實際落地時間為準，等待 90 分鐘。\n"
        "• 取好行李後請主動聯繫司機，司機將告知見面地點與車牌。\n"
        "• 若等候超過 90 分鐘未能聯繫，預約將自動取消並離開現場。\n\n"
        "【行李說明】\n"
        "• 超過 28 吋或大型行李箱、胖胖箱等非標準行李，請事先告知。\n"
        "• 若到場後人數及行李與預約不符，司機有權拒絕載送，並不退費。\n\n"
        "【異動與取消】\n"
        "• 任何異動（包含行李件數）請於七天前告知。\n"
        "• 七天內任何理由均無法異動或取消，定金恕不退還。\n\n"
        "【保險】\n"
        "• 所有車輛均投保乘客險 500 萬元以上／每人。\n\n"
        "如有任何問題，請隨時聯繫客服，感謝您的配合！"
    )
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=order.line_user_id,
                    messages=[TextMessage(text=notice_text)]
                )
            )
        send_quote_to_customer(order)
    except Exception as e:
        app.logger.error(f'Post-payment push error: {e}')

    # ── 自動開立 ezPay 電子發票 ──────────────────────────────────
    try:
        # 從 order.note 解析發票資訊
        note = order.note or ''
        inv_type = ''
        carrier  = ''
        tax_id   = ''
        company_name = ''
        if '【發票】公司抬頭：' in note:
            inv_type = 'company'
            # 格式: 【發票】公司抬頭：XXX（統編 XXXXXXXX）
            import re
            m = re.search(r'【發票】公司抬頭：(.+?)（統編 (.+?)）', note)
            if m:
                company_name = m.group(1)
                tax_id       = m.group(2)
        elif '【發票】手機載具：' in note:
            inv_type = 'personal'
            m = re.search(r'【發票】手機載具：(.+)', note)
            if m:
                carrier = m.group(1).strip()
        elif '【發票】個人雲端發票' in note:
            inv_type = 'personal'

        inv_no = issue_ezpay_invoice(order, inv_type, carrier, tax_id, company_name)
        if inv_no:
            # 推播發票號碼給客人
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=order.line_user_id,
                        messages=[TextMessage(text=f'🧾 電子發票已開立\n發票號碼：{inv_no}\n\n如需查詢發票，請至財政部電子發票整合服務平台查詢。')]
                    )
                )
    except Exception as e:
        app.logger.error(f'ezPay invoice error: {e}')

    if AUTO_DISPATCH:
        try:
            auto_dispatch_order(order.id)
        except Exception as e:
            app.logger.error(f'Auto dispatch error: {e}')

    return 'OK'


@app.route('/newebpay/return', methods=['POST', 'GET'])
def newebpay_return():
    status = request.form.get('Status', request.args.get('Status', ''))
    if status == 'SUCCESS':
        return """<html><head><meta charset="utf-8">
        <style>body{font-family:sans-serif;text-align:center;padding:60px;background:#f4f6f8}
        .box{background:#fff;border-radius:12px;padding:40px;max-width:400px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,0.1)}
        h2{color:#4A9B8F} p{color:#555}</style></head>
        <body><div class="box"><h2>✅ 定金支付成功！</h2>
        <p>感謝您的預約，我們將盡快與您確認訂單。</p>
        <p style="color:#999;font-size:13px">您可以關閉此頁面，回到 LINE 查看訂單資訊。</p>
        </div></body></html>"""
    else:
        return """<html><head><meta charset="utf-8">
        <style>body{font-family:sans-serif;text-align:center;padding:60px;background:#f4f6f8}
        .box{background:#fff;border-radius:12px;padding:40px;max-width:400px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,0.1)}
        h2{color:#cc3333} p{color:#555}</style></head>
        <body><div class="box"><h2>❌ 付款未完成</h2>
        <p>您的付款尚未完成或已取消，請回到 LINE 重新點擊付款連結。</p>
        </div></body></html>"""


@app.route('/newebpay/cancel', methods=['POST', 'GET'])
def newebpay_cancel():
    return redirect('/newebpay/return')


# ── Admin: Pricing ────────────────────────────────────────────────────
@app.route('/admin/pricing')
@admin_required
def admin_pricing():
    rules = PriceRule.query.order_by(PriceRule.sort_order).all()
    surcharges = PriceSurcharge.query.all()
    holidays = HolidaySurcharge.query.order_by(HolidaySurcharge.date_from).all()
    newebpay_mode = os.environ.get('NEWEBPAY_MODE', 'test')
    ezpay_mode    = os.environ.get('EZPAY_MODE', 'test')
    ezpay_merchant_id = os.environ.get('EZPAY_MERCHANT_ID', '338919792')
    ezpay_key_set = bool(os.environ.get('EZPAY_HASH_KEY', ''))
    return render_template('admin/pricing.html', rules=rules, surcharges=surcharges, holidays=holidays,
                           newebpay_mode=newebpay_mode,
                           ezpay_mode=ezpay_mode,
                           ezpay_merchant_id=ezpay_merchant_id,
                           ezpay_key_set=ezpay_key_set)


@app.route('/admin/pricing/newebpay_mode', methods=['POST'])
@admin_required
def admin_set_newebpay_mode():
    mode = request.form.get('mode', 'test')
    if mode in ('test', 'prod'):
        os.environ['NEWEBPAY_MODE'] = mode
        global NEWEBPAY_MODE
        NEWEBPAY_MODE = mode
        flash(f'藍新金流已切換為：{"正式環境" if mode=="prod" else "測試環境"}')
    return redirect(url_for('admin_pricing'))

@app.route('/admin/pricing/ezpay_mode', methods=['POST'])
@admin_required
def admin_set_ezpay_mode():
    mode = request.form.get('mode', 'test')
    if mode in ('test', 'prod'):
        os.environ['EZPAY_MODE'] = mode
        global EZPAY_MODE
        EZPAY_MODE = mode
        flash(f'ezPay 電子發票已切換為：{"正式環境" if mode=="prod" else "測試環境"}')
    return redirect(url_for('admin_pricing'))

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
    try:
        _handle_message_inner(event)
    except Exception as e:
        import traceback
        print('handle_message error:', traceback.format_exc())

def _handle_message_inner(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    session = user_sessions.get(user_id, {})
    step = session.get('step', '')

    if text in ['我的ID', 'myid', 'MY ID']:
        reply_text(event.reply_token, f'您的 LINE User ID：\n{user_id}\n\n請將此 ID 提供給管理員，設定後即可接收搶單通知。')
        return

    if text in ['群組ID', 'groupid', 'GROUP ID']:
        source_type = event.source.type
        if source_type == 'group':
            group_id = event.source.group_id
            reply_text(event.reply_token, f'此群組 ID：\n{group_id}\n\n請將此 ID 填入 Render 環境變數 SUPPORT_GROUP_ID')
        elif source_type == 'room':
            room_id = event.source.room_id
            reply_text(event.reply_token, f'此聊天室 ID：\n{room_id}')
        else:
            reply_text(event.reply_token, '請在群組裡輸入此指令才能取得群組 ID。')
        return

    if text == '取消':
        user_sessions.pop(user_id, None)
        reply_text(event.reply_token, '已取消操作。')
        send_main_menu(event.reply_token)
        return

    if text in ['開始', 'hi', 'Hi', 'HI', 'hello', 'Hello', '你好', '哈囉'] and not step:
        send_main_menu(event.reply_token)
        return

    if text in ['預約', '訂車', '機場接送', '開始預約']:
        user_sessions[user_id] = {'step': 'choose_service'}
        send_service_menu(event.reply_token)
        return

    if text == '查詢訂單':
        user_sessions[user_id] = {'step': 'query_name'}
        reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
        return

    if step == 'ai_chat':
        if any(kw in text for kw in ['預約', '訂車', '我要訂', '我想訂', '幫我訂']):
            user_sessions[user_id] = {'step': 'choose_service'}
            reply_text(event.reply_token, '好的！幫您切換到預約流程 ✈️')
            send_service_menu(event.reply_token)
            return
        if any(kw in text for kw in ['查詢', '我的訂單', '訂單狀態']):
            user_sessions[user_id] = {'step': 'query_name'}
            reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
            return
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
            from datetime import date as _date
            dt = datetime.strptime(text, '%Y-%m-%d')
            days_ahead = (dt.date() - _date.today()).days
            if days_ahead < 0:
                reply_text(event.reply_token, '日期已過期，請重新輸入，例如：' + datetime.now().strftime('%Y-%m-%d'))
            elif days_ahead < 8:
                reply_text(event.reply_token,
                    f'⚠️ 線上預約系統僅開放 8 天後以上的日期。\n\n'
                    f'7 天內預約請直接聯繫客服，由真人為您服務，謝謝！\n\n'
                    f'請重新輸入 8 天後的日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：'
                )
            else:
                session['date'] = text
                session['step'] = 'input_time'
                user_sessions[user_id] = session
                reply_text(event.reply_token, '請輸入接送時間（格式：08:30）：\n\n注意：22:00～06:00 為夜間時段，目前不指定優惠方案不加收費用。')
        except ValueError:
            reply_text(event.reply_token, '日期格式錯誤，請重新輸入，例如：2025-06-15')

    elif step == 'input_time':
        try:
            datetime.strptime(text, '%H:%M')
            session['time'] = text
            session['night_fee'] = is_night_time(text)
            session['step'] = 'input_passengers'
            user_sessions[user_id] = session
            night_msg = ''
            reply_text(event.reply_token, f'已記錄時間：{text}{night_msg}\n\n請輸入乘客人數，最多7人（數字）：')
        except ValueError:
            reply_text(event.reply_token, '時間格式錯誤，請重新輸入，例如：08:30')

    elif step == 'input_passengers':
        if text.isdigit() and 1 <= int(text) <= 20:
            session['passengers'] = text
            session['step'] = 'input_luggage'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入行李件數，最多7件（數字）：')
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
        if text == '無':
            session['flight'] = ''
            session['step'] = 'ask_child_seat'
            user_sessions[user_id] = session
            send_child_seat_menu(event.reply_token)
        else:
            fn = text.strip().upper().replace(' ', '')
            session['flight'] = fn
            if not AVIATION_EDGE_KEY:
                session['step'] = 'ask_child_seat'
                user_sessions[user_id] = session
                send_child_seat_menu(event.reply_token)
            else:
                # 先 reply 保住 token，查詢改用背景執行緒
                reply_text(event.reply_token, '正在查詢航班資訊，請稍候...')
                import threading
                _sess = dict(session)
                def _do_flight_query(uid=user_id, f=fn, s=_sess):
                    try:
                        finfo = query_flight_info(f, s.get('date', ''))
                        if finfo:
                            s['flight_info'] = finfo
                            s['step'] = 'confirm_flight'
                            user_sessions[uid] = s
                            _push_flight_confirm(uid, f, finfo)
                        else:
                            s['step'] = 'ask_child_seat'
                            user_sessions[uid] = s
                            with ApiClient(configuration) as api_client:
                                MessagingApi(api_client).push_message(
                                    PushMessageRequest(
                                        to=uid,
                                        messages=[TextMessage(text='查無此航班資訊，已記錄號碼，繼續下一步：')]
                                    )
                                )
                            _push_child_seat_menu(uid)
                    except Exception as e:
                        app.logger.error(f'flight query thread error: {e}')
                        s['step'] = 'ask_child_seat'
                        user_sessions[uid] = s
                        _push_child_seat_menu(uid)
                threading.Thread(target=_do_flight_query, daemon=True).start()

    elif step == 'confirm_flight':
        # 客人確認或修改航班資訊後繼續
        if text in ['確認', '對', 'yes', 'YES', 'Yes', '是']:
            session['step'] = 'ask_child_seat'
            user_sessions[user_id] = session
            send_child_seat_menu(event.reply_token)
        else:
            # 重新輸入航班號
            session['step'] = 'input_flight'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請重新輸入航班號碼：')

    elif step == 'input_child_seat_count':
        if text.isdigit() and 1 <= int(text) <= 2:
            session['child_seat_count'] = int(text)
            session['step'] = 'ask_sign_board'
            user_sessions[user_id] = session
            send_sign_board_menu(event.reply_token)
        else:
            reply_text(event.reply_token, '每座加收 NT$200，每車最多 2 座\n如需超過 2 座請聯繫客服\n請輸入需要幾座（1 或 2）：')

    elif step == 'input_note':
        session['note'] = '' if text == '無' else text
        # ── 新功能：詢問電子發票 ──
        session['step'] = 'ask_invoice'
        user_sessions[user_id] = session
        send_invoice_menu(event.reply_token)

    # ── 新功能：電子發票輸入步驟 ──
    elif step == 'input_carrier':
        session['invoice_carrier'] = '' if text == '無' else text.strip()
        session['step'] = 'ask_extra_stops'
        session['extra_stops'] = []
        session['extra_stop_fee'] = 0
        user_sessions[user_id] = session
        _push_est_travel(user_id, session)
        send_extra_stops_menu(event.reply_token)

    elif step == 'input_tax_id':
        tax_id = text.strip()
        if len(tax_id) == 8 and tax_id.isdigit():
            session['invoice_tax_id'] = tax_id
            session['step'] = 'input_company_name'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入公司抬頭（公司名稱）：')
        else:
            reply_text(event.reply_token, '統一編號格式錯誤，請輸入 8 碼數字：')

    elif step == 'input_company_name':
        session['invoice_company_name'] = text.strip()
        session['step'] = 'ask_extra_stops'
        session['extra_stops'] = []
        session['extra_stop_fee'] = 0
        user_sessions[user_id] = session
        _push_est_travel(user_id, session)
        send_extra_stops_menu(event.reply_token)

    elif step == 'input_extra_stop':
        stop_addr = text.strip()
        stops = session.get('extra_stops', [])
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
            session['step'] = 'confirm'
            user_sessions[user_id] = session
            send_order_confirm(event.reply_token, session)

    else:
        if OPENAI_API_KEY:
            user_sessions[user_id] = {'step': 'ai_chat'}
            ai_reply = ask_openai(user_id, text)
            reply_text(event.reply_token, ai_reply)
        else:
            send_main_menu(event.reply_token)


@handler.add(PostbackEvent)
def handle_postback(event):
    try:
        _handle_postback_inner(event)
    except Exception as e:
        import traceback
        print('handle_postback error:', traceback.format_exc())

def _handle_postback_inner(event):
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
            reply_text(event.reply_token, f'已選擇：{session["child_seat"]}\n\n每座加收 NT$200，每車最多 2 座\n如需超過 2 座請聯繫客服\n請輸入需要幾座（1 或 2）：')

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
        session['pet'] = True
        session['step'] = 'input_note'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '寵物同行加收：車型加價 NT$300 + 清潔費 NT$800 = NT$1,100\n\n\n請輸入備註事項（若無請輸入「無」）：')

    elif data == 'pet_no':
        session['pet'] = False
        session['step'] = 'input_note'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入備註事項（若無請輸入「無」）：')

    elif data == 'confirm_order' or data.startswith('confirm_order:'):
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

    elif data == 'request_human':
        # 推播通知真人客服
        notify_human_agent(user_id)
        reply_text(event.reply_token,
            '✅ 已通知真人客服！\n\n'
            '客服人員收到通知後將主動與您聯繫，請稍候。\n\n'
            '如有急事，也可以直接加我們 LINE：rbf5256'
        )

    elif data == 'start_booking':
        user_sessions[user_id] = {'step': 'choose_service'}
        send_service_menu(event.reply_token)

    elif data == 'start_ai_chat':
        user_sessions[user_id] = {'step': 'ai_chat'}
        reply_text(event.reply_token,
            '您好！我是客服助理小飛 ✈️\n\n'
            '有任何關於機場接送的問題都可以問我！\n'
            '例如：費用怎麼算？可以帶寵物嗎？\n\n'
            '想要預約請說「預約」，查詢訂單請說「查詢訂單」。'
        )

    elif data == 'query_order_start':
        user_sessions[user_id] = {'step': 'query_name'}
        reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')

    # ── 新功能：電子發票 postback ──────────────────────────────────
    elif data == 'invoice_personal':
        session['invoice_type'] = 'personal'
        session['invoice_carrier'] = ''
        session['step'] = 'input_carrier'
        user_sessions[user_id] = session
        reply_text(event.reply_token,
            '請輸入手機條碼載具（格式：/XXXXXXX，共8碼）：\n\n'
            '例：/ABC1234\n\n若無載具請輸入「無」，發票將開立為個人雲端發票。'
        )

    elif data == 'invoice_company':
        session['invoice_type'] = 'company'
        session['step'] = 'input_tax_id'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入公司統一編號（8碼數字）：')

    elif data == 'invoice_none':
        session['invoice_type'] = ''
        session['invoice_carrier'] = ''
        session['step'] = 'ask_extra_stops'
        session['extra_stops'] = []
        session['extra_stop_fee'] = 0
        user_sessions[user_id] = session
        _push_est_travel(user_id, session)
        send_extra_stops_menu(event.reply_token)

    # ── 多點停靠 ─────────────────────────────────────────────────────
    elif data == 'no_extra_stops':
        session['step'] = 'confirm'
        user_sessions[user_id] = session
        send_order_confirm(event.reply_token, session)

    elif data == 'add_extra_stop':
        session['step'] = 'input_extra_stop'
        session.setdefault('extra_stops', [])
        session.setdefault('extra_stop_fee', 0)
        user_sessions[user_id] = session
        stop_num = len(session.get('extra_stops', [])) + 1
        reply_text(event.reply_token, f'請輸入第 {stop_num} 個停靠點地址：')

    # ── 搶單 ─────────────────────────────────────────────────────────
    elif data.startswith('grab:'):
        job_id = int(data.split(':')[1])
        handle_driver_grab(event.reply_token, user_id, job_id)

    elif data.startswith('skip:'):
        job_id = int(data.split(':')[1])
        with app.app_context():
            job = DispatchJob.query.get(job_id)
            driver = Driver.query.filter_by(line_user_id=user_id).first()
            if job and driver:
                existing = DispatchResponse.query.filter_by(job_id=job_id, driver_id=driver.id).first()
                if not existing:
                    db.session.add(DispatchResponse(job_id=job_id, driver_id=driver.id, action='放棄'))
                    db.session.commit()
        reply_text(event.reply_token, '已略過此訂單。')

# ── Menu senders ─────────────────────────────────────────────────────
def notify_human_agent(requester_line_id):
    """推播通知真人客服有人點了真人客服按鈕（推播給群組 + 個人）"""
    try:
        # 取得客人顯示名稱
        display_name = '客人'
        try:
            with ApiClient(configuration) as api_client:
                profile = MessagingApi(api_client).get_profile(requester_line_id)
                display_name = profile.display_name
        except Exception:
            pass

        text = (
            f"【真人客服通知】\n\n"
            f"客人：{display_name}\n"
            f"LINE ID：{requester_line_id}\n\n"
            f"客人點擊了「真人客服」按鈕，請盡快介入回覆！"
        )
        targets = []
        if SUPPORT_GROUP_ID:
            targets.append(SUPPORT_GROUP_ID)   # 群組優先
        if HUMAN_AGENT_LINE_ID:
            targets.append(HUMAN_AGENT_LINE_ID) # 個人也通知

        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            for target in targets:
                try:
                    api.push_message(
                        PushMessageRequest(
                            to=target,
                            messages=[TextMessage(text=text)]
                        )
                    )
                except Exception as e:
                    app.logger.error(f'notify_human_agent push error ({target}): {e}')
    except Exception as e:
        app.logger.error(f'notify_human_agent error: {e}')


def send_service_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("機場接送預約"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇服務類型", "size": "md", "color": "#333333", "margin": "md", "wrap": True},
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
            {"type": "text", "text": "請選擇車型", "size": "md", "color": "#333333", "wrap": True},
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
            {"type": "text", "text": "請選擇機場", "size": "md", "color": "#333333", "wrap": True}
        ] + buttons}
    }
    send_flex(reply_token, '選擇機場', bubble)


def send_flight_confirm(reply_token, flight_number, finfo):
    """顯示查到的航班資訊（付費版完整欄位），讓客人確認"""
    status_map = {
        'scheduled': '準時',
        'active':    '飛行中',
        'landed':    '已降落',
        'cancelled': '已取消',
        'incident':  '異常',
        'diverted':  '改降',
    }
    status_text = status_map.get(finfo.get('status', ''), finfo.get('status', '') or '未知')

    # 狀態顏色
    status_color = {
        'scheduled': '#38A169', 'active': '#3182CE',
        'landed':    '#38A169', 'cancelled': '#E53E3E',
        'incident':  '#E53E3E', 'diverted': '#DD6B20',
    }.get(finfo.get('status', ''), '#718096')

    def time_row(label, scheduled, actual, estimated, delay):
        """顯示時間欄位：優先實際時間，其次預估，最後預定"""
        display = actual or estimated or scheduled or '未知'
        row_items = [make_info_row(label, display)]
        if delay and delay > 0:
            row_items.append({
                "type": "text",
                "text": f"  延誤約 {delay} 分鐘",
                "size": "xs", "color": "#E53E3E", "margin": "xs"
            })
        return row_items

    dep_detail = finfo.get('dep_airport', '未知')
    if finfo.get('dep_iata'):
        dep_detail += f" ({finfo['dep_iata']})"
    if finfo.get('dep_terminal'):
        dep_detail += f" 航廈{finfo['dep_terminal']}"
    if finfo.get('dep_gate'):
        dep_detail += f" 登機門{finfo['dep_gate']}"

    arr_detail = finfo.get('arr_airport', '未知')
    if finfo.get('arr_iata'):
        arr_detail += f" ({finfo['arr_iata']})"
    if finfo.get('arr_terminal'):
        arr_detail += f" 航廈{finfo['arr_terminal']}"
    if finfo.get('arr_gate'):
        arr_detail += f" 登機門{finfo['arr_gate']}"

    body_contents = (
        [make_info_row("出發", dep_detail)]
        + time_row("出發時間",
                   finfo.get('dep_scheduled'), finfo.get('dep_actual'),
                   finfo.get('dep_estimated'), finfo.get('dep_delay', 0))
        + [{"type": "separator", "margin": "sm"}]
        + [make_info_row("抵達", arr_detail)]
        + time_row("抵達時間",
                   finfo.get('arr_scheduled'), finfo.get('arr_actual'),
                   finfo.get('arr_estimated'), finfo.get('arr_delay', 0))
    )
    if finfo.get('arr_baggage'):
        body_contents.append(make_info_row("行李轉盤", finfo['arr_baggage']))

    body_contents += [
        {"type": "separator", "margin": "sm"},
        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
            {"type": "text", "text": "航班狀態", "size": "sm", "color": "#A0AEC0", "flex": 2},
            {"type": "text", "text": status_text, "size": "sm",
             "color": status_color, "weight": "bold", "flex": 3},
        ]},
        {"type": "text", "text": "以上資訊是否正確？", "margin": "md",
         "weight": "bold", "color": "#E05C00", "size": "sm", "wrap": True},
    ]

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "航班資訊確認",
                 "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text",
                 "text": f"{finfo.get('airline', '')}  {flight_number}",
                 "color": "#8BA3C7", "size": "sm", "wrap": True},
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": body_contents
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button",
                 "action": {"type": "message", "label": "正確，繼續", "text": "確認"},
                 "style": "primary", "color": "#4A9B8F", "flex": 1},
                {"type": "button",
                 "action": {"type": "message", "label": "重新輸入", "text": "重填"},
                 "style": "secondary", "flex": 1},
            ]
        }
    }
    send_flex(reply_token, f'航班資訊 {flight_number}', bubble)

def send_child_seat_menu(reply_token):
    buttons = [make_button(name, f"child_seat_{key}") for key, name in CHILD_SEATS.items()]
    bubble = {
        "type": "bubble",
        "header": header_box("兒童安全座椅"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要兒童安全座椅？", "size": "md", "color": "#333333", "wrap": True},
            {"type": "text", "text": "每座加收 NT$200，每車最多 2 座，超過請聯繫客服", "size": "xs", "color": "#E05C00", "margin": "sm", "wrap": True},
        ] + buttons}
    }
    send_flex(reply_token, '兒童安全座椅', bubble)

def send_sign_board_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("舉牌服務"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要舉牌服務？", "size": "md", "color": "#333333", "wrap": True},
            {"type": "text", "text": "舉牌人員於接機大廳舉名牌等候，加收 NT$300", "size": "xs", "color": "#888888", "margin": "sm", "wrap": True},
            make_button("需要舉牌（+NT$300）", "sign_board_yes"),
            make_button("不需要", "sign_board_no"),
        ]}
    }
    send_flex(reply_token, '舉牌服務', bubble)

def send_pet_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("寵物同行"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否有寵物同行？", "size": "md", "color": "#333333", "wrap": True},
            {"type": "text", "text": "必須裝籠，行車中不可放出！加收 NT$300", "size": "xs", "color": "#888888", "margin": "sm", "wrap": True},
            make_button("有寵物同行（+NT$300）", "pet_yes"),
            make_button("沒有", "pet_no"),
        ]}
    }
    send_flex(reply_token, '寵物同行', bubble)

# ── 新功能 1：電子發票選單 ─────────────────────────────────────────────
def send_invoice_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#2D6A4F",
            "contents": [
                {"type": "text", "text": "電子發票", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": "請選擇發票開立方式", "color": "#B7E4C7", "size": "sm", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text",
                 "text": "定金 NT$315 將開立電子發票，請選擇收取方式：",
                 "size": "sm", "color": "#555555", "wrap": True, "margin": "sm"},
                {"type": "separator", "margin": "md"},
                make_button("個人載具（手機條碼）", "invoice_personal"),
                make_button("公司抬頭（統一編號）", "invoice_company"),
                make_button("不需要發票", "invoice_none"),
            ]
        }
    }
    send_flex(reply_token, '電子發票', bubble)

# ── 新功能 2：預估車程（push，不佔 reply_token）──────────────────────
def _push_est_travel(user_id, session):
    """呼叫 Google Maps 預估機場→目的地車程，用 push_message 傳給客人"""
    try:
        airport = session.get('airport', '')
        pickup  = session.get('pickup', '')
        if not airport or not pickup or not GOOGLE_MAPS_API_KEY:
            return
        params = {
            'origins':      airport,
            'destinations': pickup,
            'key':          GOOGLE_MAPS_API_KEY,
            'language':     'zh-TW',
            'region':       'tw',
            'mode':         'driving',
        }
        resp = requests.get(
            'https://maps.googleapis.com/maps/api/distancematrix/json',
            params=params, timeout=5
        )
        element = resp.json()['rows'][0]['elements'][0]
        if element.get('status') != 'OK':
            return
        dist_text = element['distance']['text']
        dur_text  = element['duration']['text']
        msg = f"預估車程（{airport} → {pickup}）\n距離：{dist_text}\n行車時間：{dur_text}"
        line_user_id = session.get('_line_user_id', user_id)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=line_user_id,
                    messages=[TextMessage(text=msg)]
                )
            )
    except Exception as e:
        app.logger.warning(f'_push_est_travel error: {e}')

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
- 寵物同行：+NT$300（必須裝籠，行車中不可放出）
- 七天內預約（需真人客服接單）：+NT$300
- 三天內臨時單（疊加）：再+NT$300（合計+NT$600）
- 假日/旺季期間：+NT$300
- 多點停靠：依距離 +NT$200–500

【車型說明】
我們採「不指定車款」優惠方案，一切依本公司調度派遣為主，以下為參考車款（全部為無菸車）：

轎車類（最多 4 人）：
- Lexus ES、Mercedes-Benz E-Class、Tesla Model S

休旅車類（最多 5–7 人）：
- Toyota RAV4、Luxgen N7、Mercedes-Benz EQB、Tesla Model Y、Tesla Model X

廂型車類（最多 7–9 人）：
- Toyota Sienna、Toyota Alphard、Lexus LM、KIA Carnival、Toyota Granvia
- Volkswagen Caravelle T6、Hyundai Staria、Mercedes-Benz V-Class
- Volkswagen Crafter、Mercedes-Benz Sprinter

若被問到「不指定車款有哪些」，請列出以上車款並說明一切以本公司調度為主。

【人數與行李超載說明】
- 標準容量：最多 7 人、最多 7 件標準 29 吋行李，保證載得下。
- 第 8 位乘客：加收 NT$400。
- 若超過 7 人或 7 件，以該調度車款的後行李箱實際空間為準，超過載不下須自行負責。
- 若被問到 8 位或以上費用，請說明加收 NT$400 並建議事先告知人數。

【公司資訊】
- 公司名稱：樂高小客車租賃有限公司（Le Gao Car Rental Co., Ltd.）
- 統一編號：50978670
- 汽車運輸業營業執照：交營字第40-0032736號
- 新北市小客車租賃商業同業公會：新北小車證字第189號
- 若客人詢問公司名稱、執照、統編等資訊，請如實回答以上內容。

【接機流程（客人詢問時請回覆以下內容）】
落地約 20 分鐘左右，司機會主動與您聯繫，請保持手機暢通。
待您取好行李後，請主動撥電話給司機，司機會與您約定見面地點。
若有任何問題請隨時聯繫客服。

【常見問題 FAQ】

Q1 機場接送要多久前預約？
A：建議最晚兩周前先預約。線上預約系統僅開放 8 天後以上的日期，7 天內請直接聯繫真人客服接單。溫馨提醒：7 天內加收 NT$300，3 天內再加收 NT$300（合計 NT$600）。

Q2 如果航班延誤怎麼辦？
A：我們接機是依照航班實際落地為主等待 90 分鐘，不用擔心航班有提早或延誤問題。除非耽誤超過兩個小時，超過兩小時（第三小時起算）會加收 NT$300／每小時等待費用。

Q3 半夜或清晨也可以叫車嗎？
A：我們客服跟調度人員都是 24 小時服務，隨時可以跟我們叫車。

Q4 車型可以選擇嗎？
A：可以指定車型，您提供給我人數跟行李件數，好讓我報指定車款可以乘載的車款報價給您。

Q5 行李有數量限制嗎？
A：我們規定是七人七件標準式大行李絕對載得下，如有超過會依照該車款後行李箱載的下為主，如因超過載不下要自負。

Q6 小孩需要安全座椅嗎？可以提供嗎？
A：安全座椅／增高墊加收 NT$200／座，請提供幾歲用的。

Q7 臨時更改或取消怎麼辦？
A：七天以上異動都可以，七天內無法異動，七天內要取消恕不退還。

Q8 費用如何計算？夜間會加價嗎？
A：報價請提供地區來提供報價或參考我們報價參考值。目前優惠活動不加收夜間費用，如指定車款在 22:00–06:00 會加收 NT$300。7 天內預約加收 NT$300，3 天內再加收 NT$300（合計 NT$600）。

Q9 請問公司在哪裡？
A：公司註冊在新北市板橋，桃園跟台中都有辦公室，沒對外開放。

Q10 請問司機從哪裡出發？是台中車嗎？是台北車嗎？
A：我們司機有北部也有中部，司機有送就有接，不用擔心司機是哪裡的車，主要是可以服務好每一位貴賓，安全接送最重要。

Q11 來回可以在優惠嗎？多叫一台有優惠嗎？
A：目前已經是優惠活動，沒有再有任何優惠，回程還需要關注航班、等待航班、等待您出關接您，沒有加價已經是最大優惠了。

【回覆原則】
- 全程使用繁體中文
- 回覆要簡潔口語，不要太正式或太長
- 若客人問具體訂單狀態，請他輸入「查詢訂單」由系統查詢
- 若客人要預約，請他輸入「預約」進入預約流程
- 不確定的資訊不要亂猜，誠實說不確定並建議聯繫客服
"""

def ask_openai(user_id, user_message, order_context=None):
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
            headers={'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'},
            json={'model': 'gpt-4o-mini', 'messages': messages, 'max_tokens': 400, 'temperature': 0.75},
            timeout=15
        )
        data = resp.json()
        return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'OpenAI error: {e}')
        return '抱歉，AI 客服暫時無法回應，請稍後再試，或輸入「預約」開始預約流程。'


def send_main_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "Taiwan Top Service", "color": "#4A9B8F", "size": "sm", "weight": "bold", "wrap": True},
                {"type": "text", "text": "機場接送服務", "color": "#FFFFFF", "size": "xxl", "weight": "bold", "margin": "xs"},
                {"type": "text", "text": "您好！請問需要什麼服務？", "color": "#8BA3C7", "size": "sm", "margin": "sm", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "button", "action": {"type": "postback", "label": "開始預約", "data": "start_booking"},
                 "style": "primary", "color": "#4A9B8F"},
                {"type": "button", "action": {"type": "postback", "label": "詢問客服 / 了解服務", "data": "start_ai_chat"},
                 "style": "secondary"},
                {"type": "button", "action": {"type": "postback", "label": "查詢我的訂單", "data": "query_order_start"},
                 "style": "secondary"},
                {"type": "button", "action": {"type": "postback", "label": "👤 真人客服", "data": "request_human"},
                 "style": "secondary", "color": "#E05C00"},
            ]
        }
    }
    send_flex(reply_token, 'Taiwan Top Service 機場接送服務', bubble)

def send_main_menu_after(reply_token, user_id=None):
    send_main_menu(reply_token)


def send_extra_stops_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("多點停靠服務"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否需要途中加停？", "weight": "bold", "size": "md", "wrap": True},
            {"type": "text", "text": "系統將依停靠點與出發地距離自動計算加收費用：", "size": "sm", "color": "#718096", "margin": "sm", "wrap": True},
            {"type": "separator", "margin": "md"},
            make_info_row("5 公里以內", "+NT$200"),
            make_info_row("5–12 公里", "+NT$300"),
            make_info_row("12–18 公里", "+NT$400"),
            make_info_row("超過 18 公里", "+NT$500"),
        ]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "不需要，直接確認", "data": "no_extra_stops"},
             "style": "primary", "color": "#4A9B8F"},
            {"type": "button", "action": {"type": "postback", "label": "新增停靠點", "data": "add_extra_stop"},
             "style": "secondary"},
        ]}
    }
    send_flex(reply_token, '多點停靠服務', bubble)


def build_quote_from_session(session):
    class FakeOrder:
        pass
    o = FakeOrder()
    o.airport          = session.get('airport', '')
    o.pickup_location  = session.get('pickup', '')
    o.night_fee        = session.get('night_fee', False)
    o.sign_board       = session.get('sign_board', False)
    o.child_seat_count = session.get('child_seat_count', 0)
    o.pet              = session.get('pet', False)
    o.booking_date     = session.get('date', '')
    o.extra_stop_fee   = session.get('extra_stop_fee', 0)

    extra_stops = session.get('extra_stops', [])
    if extra_stops:
        last_stop = extra_stops[-1]
        rules = PriceRule.query.filter_by(active=True).order_by(PriceRule.sort_order).all()
        matched_rule = None
        for rule in rules:
            airport_match = not rule.airport_keyword or rule.airport_keyword in o.airport
            region_match  = not rule.region_keyword or any(
                kw.strip() in last_stop for kw in rule.region_keyword.split(',')
            )
            if airport_match and region_match:
                matched_rule = rule
                break
        if matched_rule:
            o.pickup_location = last_stop
            o.extra_stop_fee  = 0
            quote = calculate_quote(o)
            origin = session.get('pickup', '')
            if quote['breakdown']:
                quote['breakdown'][0]['label'] = f'基本車資（{matched_rule.name}，途經 {origin}）'
            return quote

    quote = calculate_quote(o)
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
                {"type": "text", "text": "預估總費用", "weight": "bold", "flex": 3, "size": "sm", "wrap": True},
                {"type": "text", "text": f"NT${quote['total']:,}", "weight": "bold",
                 "flex": 5, "color": "#E05C00", "size": "lg", "align": "end", "wrap": True}
            ]
        })

    # 電子發票資訊字串
    inv_type = session.get('invoice_type', '')
    if inv_type == 'company':
        inv_text = f"公司抬頭：{session.get('invoice_company_name', '')}（統編 {session.get('invoice_tax_id', '')}）"
    elif inv_type == 'personal' and session.get('invoice_carrier'):
        inv_text = f"手機載具：{session.get('invoice_carrier', '')}"
    elif inv_type == 'personal':
        inv_text = "個人雲端發票"
    else:
        inv_text = "不需要"

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
        make_info_row("電子發票", inv_text),
    ]

    # ── 新功能 3：尾款金額 ──
    balance = max(0, quote['total'] - NEWEBPAY_DEPOSIT)

    quote_bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "預估報價明細", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": "實際費用以出發當日為準", "color": "#8BA3C7", "size": "xs", "wrap": True}
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
            {"type": "text", "text": "確認以上資料無誤後送出預約", "margin": "md",
             "color": "#E05C00", "weight": "bold", "size": "sm", "wrap": True},
            {"type": "separator", "margin": "sm"},
            {"type": "text",
             "text": f"尾款 NT${balance:,} 元（未稅）請交付給司機",
             "margin": "sm", "color": "#C53030", "weight": "bold", "size": "md", "wrap": True},
        ]},
        "footer": {"type": "box", "layout": "horizontal", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "確認送出", "data": "confirm_order"},
             "style": "primary", "color": "#4A9B8F", "flex": 1},
            {"type": "separator"},
            {"type": "button", "action": {"type": "postback", "label": "取消重填", "data": "cancel_order"},
             "style": "secondary", "flex": 1}
        ]}
    }

    carousel = {"type": "carousel", "contents": [quote_bubble, confirm_bubble]}
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token,
                messages=[FlexMessage(alt_text='預估報價與確認預約資料', contents=FlexContainer.from_dict(carousel))])
        )

def decode_session(encoded):
    try:
        return json.loads(base64.b64decode(encoded.encode()).decode())
    except Exception:
        return None

def encode_session(session):
    try:
        return base64.b64encode(json.dumps(session, ensure_ascii=False).encode()).decode()
    except Exception:
        return ''

def save_order(reply_token, session, user_id):
    # 組合發票備註
    inv_type = session.get('invoice_type', '')
    if inv_type == 'company':
        inv_note = f"【發票】公司抬頭：{session.get('invoice_company_name','')}（統編 {session.get('invoice_tax_id','')}）"
    elif inv_type == 'personal' and session.get('invoice_carrier'):
        inv_note = f"【發票】手機載具：{session.get('invoice_carrier','')}"
    elif inv_type == 'personal':
        inv_note = "【發票】個人雲端發票"
    else:
        inv_note = ""

    base_note = session.get('note', '') or ''
    full_note = (base_note + '\n' + inv_note).strip() if inv_note else base_note

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
            note=full_note,
            extra_stops=json.dumps(session.get('extra_stops', []), ensure_ascii=False),
            extra_stop_fee=session.get('extra_stop_fee', 0),
            status='待付款'
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

        base_url = os.environ.get('RENDER_EXTERNAL_URL', 'https://airport-reservation.onrender.com')
        pay_url = f'{base_url}/pay/{order_id}'

    bubble = {
        "type": "bubble",
        "header": header_box("預約資料已送出！"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": f"訂單編號：#{order_id}", "size": "lg", "weight": "bold", "color": "#4A9B8F", "wrap": True},
            {"type": "text", "text": "請於 30 分鐘內完成定金支付（NT$315 含稅），訂單才會正式成立。", "margin": "md", "wrap": True},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "定金金額：NT$315（含稅）", "margin": "md", "weight": "bold", "size": "md", "color": "#E05C00", "wrap": True},
            {"type": "button", "action": {"type": "uri", "label": "立即支付定金 NT$315（含稅）", "uri": pay_url},
             "style": "primary", "color": "#4A9B8F", "margin": "md"},
        ]}
    }
    send_flex(reply_token, '請完成定金支付', bubble)

    notice_text = (
        "預約須知與注意事項\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "【接機說明】\n"
        "• 接機以航班實際落地時間為準，等待 90 分鐘。\n"
        "• 取好行李後請主動聯繫司機，司機將告知見面地點與車牌。\n"
        "• 若等候超過 90 分鐘未能聯繫，預約將自動取消並離開現場。\n\n"
        "【行李說明】\n"
        "• 超過 28 吋或大型行李箱、胖胖箱等非標準行李，請事先告知。\n"
        "• 行李定義：行李箱、嬰兒車、登機箱、警衛包等占用後車廂空間之物件。\n"
        "• 若到場後人數及行李與預約不符，司機有權拒絕載送，並不退費。\n\n"
        "【異動與取消】\n"
        "• 任何異動（包含行李件數）請於七天前告知。\n"
        "• 七天內任何理由均無法異動或取消，定金恕不退還。\n\n"
        "【保險】\n"
        "• 所有車輛均投保乘客險 500 萬元以上／每人。\n\n"
        "如有任何問題，請隨時聯繫客服，感謝您的配合！"
    )
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=notice_text)])
            )
    except Exception as e:
        print(f'Notice push error: {e}')

def send_order_query_result(reply_token, orders):
    bubbles = []
    for order in orders:
        bubbles.append({
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F", "contents": [
                {"type": "text", "text": f"訂單 #{order.id}", "color": "#FFFFFF", "size": "lg", "weight": "bold", "wrap": True},
                {"type": "text", "text": order.created_at.strftime('%Y-%m-%d %H:%M'), "color": "#DDDDDD", "size": "sm", "wrap": True}
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
    with app.app_context():
        order = Order.query.get(order_id)
        if not order:
            return
        if hasattr(order, 'dispatch_job') and order.dispatch_job:
            return
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
        existing = DispatchResponse.query.filter_by(job_id=job_id, driver_id=driver.id).first()
        if existing:
            reply_text(reply_token, '您已回應過此訂單。')
            return
        job.status = '已結單'
        job.grabbed_by = driver.id
        job.grabbed_at = datetime.utcnow()
        order = job.order
        order.driver_id = driver.id
        order.status = '已確認'
        db.session.add(DispatchResponse(job_id=job_id, driver_id=driver.id, action='搶單'))
        db.session.commit()

        bubble = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F",
                "contents": [
                    {"type": "text", "text": "搶單成功！", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                    {"type": "text", "text": f"訂單 #{order.id}", "color": "#DDDDDD", "size": "sm", "wrap": True}
                ]},
            "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {"type": "text", "text": "客戶完整資料", "weight": "bold", "color": "#1A2B4A", "margin": "sm", "wrap": True},
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
        if job.notify_customer:
            try:
                send_driver_info_to_customer(order, driver)
                order.driver_notified = True
                db.session.commit()
            except Exception as e:
                print(f'Auto notify customer error: {e}')

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
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_notify, 'interval', minutes=1)
    scheduler.start()

    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()