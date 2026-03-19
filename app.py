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
from database import db, Order, Driver, VehicleType, AirportOption, DispatchJob, DispatchResponse, PriceRule, PriceSurcharge, HolidaySurcharge, SiteSetting, LineVisitor
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

import json as _json

@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return _json.loads(s)
    except Exception:
        return []

db.init_app(app)
with app.app_context():
    db.create_all()
    _migrations = [
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stops TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extra_stop_fee INTEGER DEFAULT 0",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS driver_fee INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_price INTEGER DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS deposit_paid BOOLEAN DEFAULT FALSE",
        "ALTER TABLE drivers ADD COLUMN IF NOT EXISTS line_user_id VARCHAR(100) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS deadline TIMESTAMP",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS note VARCHAR(200) DEFAULT ''",
        "ALTER TABLE dispatch_jobs ADD COLUMN IF NOT EXISTS notify_customer BOOLEAN DEFAULT TRUE",
        "CREATE TABLE IF NOT EXISTS price_rules (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, airport_keyword VARCHAR(50) DEFAULT '', region_keyword VARCHAR(100) DEFAULT '', base_price INTEGER DEFAULT 0, note TEXT DEFAULT '', active BOOLEAN DEFAULT TRUE, sort_order INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS price_surcharges (id SERIAL PRIMARY KEY, key VARCHAR(50) UNIQUE NOT NULL, name VARCHAR(50) NOT NULL, amount INTEGER DEFAULT 0, enabled BOOLEAN DEFAULT TRUE, note VARCHAR(100) DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS holiday_surcharges (id SERIAL PRIMARY KEY, name VARCHAR(50) DEFAULT '', date_from VARCHAR(10) NOT NULL, date_to VARCHAR(10) NOT NULL, amount INTEGER DEFAULT 300, active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS line_visitors (id SERIAL PRIMARY KEY, line_user_id VARCHAR(100) UNIQUE NOT NULL, display_name VARCHAR(100) DEFAULT '', picture_url VARCHAR(300) DEFAULT '', first_seen TIMESTAMP DEFAULT NOW(), last_seen TIMESTAMP DEFAULT NOW(), message_count INTEGER DEFAULT 0)",
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
HUMAN_AGENT_LINE_ID = os.environ.get('HUMAN_AGENT_LINE_ID', 'rbf5256')
DRIVER_GROUP_ID     = os.environ.get('DRIVER_GROUP_ID', '')           # 司機搶單群組 ID  # 真人客服 LINE ID
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

# ── ezPay 電子發票設定 ────────────────────────────────────────────────
EZPAY_MERCHANT_ID     = os.environ.get('EZPAY_MERCHANT_ID', '')
EZPAY_HASH_KEY        = os.environ.get('EZPAY_HASH_KEY', '')   # ezPay 電子發票專用金鑰（非藍新）
EZPAY_HASH_IV         = os.environ.get('EZPAY_HASH_IV', '')    # ezPay 電子發票專用 IV
EZPAY_MODE            = os.environ.get('EZPAY_MODE', 'prod')           # test or prod
DONATE_LOVE_CODE      = os.environ.get('DONATE_LOVE_CODE', '024')      # 捐贈發票愛心碼（家扶基金會 024）

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
    # 動態取得付款方式設定
    try:
        _pm_str = SiteSetting.get('payment_methods', 'CREDIT,WEBATM,VACC')
        _payment_methods = set(_pm_str.split(','))
    except Exception:
        _payment_methods = {'CREDIT', 'WEBATM', 'VACC'}
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
        # ── 付款方式（從後台 SiteSetting 動態讀取）──
        **{m: (1 if m in _payment_methods else 0) for m in
           ['CREDIT','ANDROIDPAY','SAMSUNGPAY','APPLEPAY','WEBATM','VACC','CVS','BARCODE']},
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
            love_code     = ''
        elif invoice_type == 'personal' and carrier:
            buyer_name    = order.name
            buyer_uni_no  = ''
            carrier_type  = '0'   # 手機條碼
            carrier_num   = carrier
            print_flag    = '0'
            love_code     = ''
        elif invoice_type == 'donate':
            # 捐贈發票
            buyer_name    = order.name
            buyer_uni_no  = ''
            carrier_type  = ''
            carrier_num   = ''
            print_flag    = '0'
            love_code     = DONATE_LOVE_CODE   # 愛心碼（待填入）
        else:
            # 個人雲端（無載具）
            buyer_name    = order.name
            buyer_uni_no  = ''
            carrier_type  = ''
            carrier_num   = ''
            print_flag    = '0'
            love_code     = ''

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
            'LoveCode':     love_code if invoice_type == 'donate' else '',
        }

        post_data_str = urllib.parse.urlencode(params)
        post_data_enc = ezpay_invoice_encrypt(post_data_str)

        resp = requests.post(api_url, data={
            'MerchantID_': EZPAY_MERCHANT_ID,
            'PostData_':   post_data_enc,
        }, timeout=10)

        result = resp.json()
        app.logger.info(f'ezPay invoice result: {result}')
        print(f'ezPay invoice result: {result}', flush=True)

        if result.get('Status') == 'SUCCESS':
            inv_data = result.get('Result', {})
            inv_no   = inv_data.get('InvoiceNumber', '')
            inv_date = inv_data.get('InvoiceDate', '')
            app.logger.info(f'Invoice issued: {inv_no} ({inv_date})')
            return inv_no
        else:
            app.logger.warning(f'ezPay invoice failed: {result.get("Message")}')
            print(f'ezPay invoice failed: {result.get("Message")}', flush=True)
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
                {"type": "text", "text": "為了確保乘車安全與服務品質，請務必確認車款、車號及司機資訊與客服通知相符，若有任何異常，請立即連繫客服，避免誤搭及後續爭議。",
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
    # LINE Flex Message 不允許空字串 text，用「-」代替
    safe_value = str(value).strip() if value is not None else '-'
    if not safe_value:
        safe_value = '-'
    return {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "xs", "color": "#888888", "wrap": True},
        {"type": "text", "text": safe_value, "size": "sm", "color": "#333333", "wrap": True, "margin": "xs"}
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
@app.route('/admin/ai', methods=['GET'])
@admin_required
def admin_ai_control():
    # 列出目前所有 human_mode 的用戶
    paused = [uid for uid, s in user_sessions.items() if s.get('step') == 'human_mode']
    return render_template('admin/ai_control.html', paused=paused)

@app.route('/admin/visitors')
@admin_required
def admin_visitors():
    visitors = LineVisitor.query.order_by(LineVisitor.last_seen.desc()).all()
    return render_template('admin/visitors.html', visitors=visitors)

@app.route('/admin/ai/pause/direct', methods=['POST'])
@admin_required
def admin_ai_pause_direct():
    uid = request.form.get('line_user_id', '').strip()
    next_url = request.form.get('next', '/admin/ai')
    if uid:
        user_sessions[uid] = {'step': 'human_mode'}
        flash(f'已暫停 AI 客服：{uid}')
    else:
        flash('請輸入有效的 LINE User ID')
    return redirect(next_url)

@app.route('/admin/ai/resume/direct', methods=['POST'])
@admin_required
def admin_ai_resume_direct():
    uid = request.form.get('line_user_id', '').strip()
    next_url = request.form.get('next', '/admin/ai')
    if uid:
        user_sessions[uid] = {'step': 'ai_chat'}
        flash(f'已恢復 AI 客服：{uid}')
    else:
        flash('請輸入有效的 LINE User ID')
    return redirect(next_url)

@app.route('/admin')
@admin_required
def admin_index():
    sort  = request.args.get('sort', 'booking_date')
    order_dir = request.args.get('dir', 'asc')
    status_filter = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')

    q = Order.query
    if status_filter:
        q = q.filter(Order.status == status_filter)
    if date_from:
        q = q.filter(Order.booking_date >= date_from)
    if date_to:
        q = q.filter(Order.booking_date <= date_to)

    sort_col = {
        'booking_date': Order.booking_date,
        'booking_time': Order.booking_time,
        'created_at':   Order.created_at,
        'status':       Order.status,
        'name':         Order.name,
    }.get(sort, Order.booking_date)

    if order_dir == 'desc':
        if sort == 'booking_date':
            q = q.order_by(Order.booking_date.desc(), Order.booking_time.asc())
        else:
            q = q.order_by(sort_col.desc())
    else:
        if sort == 'booking_date':
            q = q.order_by(Order.booking_date.asc(), Order.booking_time.asc())
        else:
            q = q.order_by(sort_col.asc())

    orders = q.all()
    from datetime import datetime as _dt
    return render_template('admin/index.html', orders=orders,
                           sort=sort, dir=order_dir,
                           status_filter=status_filter,
                           date_from=date_from, date_to=date_to,
                           now=_dt.now())

@app.route('/admin/order/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    drivers = Driver.query.filter_by(active=True).all()
    return render_template('admin/order_detail.html', order=order, drivers=drivers)


@app.route('/admin/order/new', methods=['GET', 'POST'])
@admin_required
def admin_new_order():
    """後台手動新增訂單（真人客服接單用）"""
    airports = AirportOption.query.filter_by(active=True).all()
    vehicles = VehicleType.query.filter_by(active=True).all()
    if request.method == 'POST':
        import json as _json
        stops_raw = request.form.get('extra_stops', '')
        stops = [s.strip() for s in stops_raw.splitlines() if s.strip()]
        svc_name = request.form.get('service_name', '送機（出境）')
        svc_type = 'arrival' if '接機' in svc_name else 'departure'
        order = Order(
            line_user_id   = request.form.get('line_user_id', 'manual'),
            service_type   = svc_type,
            service_name   = svc_name,
            vehicle        = request.form.get('vehicle', '不指定車款'),
            airport        = request.form.get('airport', ''),
            pickup_location= request.form.get('pickup_location', ''),
            booking_date   = request.form.get('booking_date', ''),
            booking_time   = request.form.get('booking_time', ''),
            passengers     = int(request.form.get('passengers') or 1),
            luggage        = int(request.form.get('luggage') or 0),
            name           = request.form.get('name', ''),
            phone          = request.form.get('phone', ''),
            email          = request.form.get('email', ''),
            flight_number  = request.form.get('flight_number', ''),
            note           = request.form.get('note', ''),
            extra_stops    = _json.dumps(stops, ensure_ascii=False),
            extra_stop_fee = int(request.form.get('extra_stop_fee') or 0),
            total_price    = int(request.form.get('total_price') or 0),
            status         = request.form.get('status', '待確認'),
        )
        db.session.add(order)
        db.session.commit()
        flash(f'訂單 #{order.id} 已手動建立')
        return redirect(url_for('admin_order_detail', order_id=order.id))
    return render_template('admin/order_edit.html',
        order=None, airports=airports, vehicles=vehicles, stops_text='')


@app.route('/admin/order/<int:order_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_order(order_id):
    order = Order.query.get_or_404(order_id)
    airports = AirportOption.query.filter_by(active=True).all()
    vehicles = VehicleType.query.filter_by(active=True).all()
    if request.method == 'POST':
        import json as _json
        svc_name = request.form.get('service_name', order.service_name)
        order.service_type   = 'arrival' if '接機' in svc_name else 'departure'
        order.service_name   = svc_name
        order.vehicle        = request.form.get('vehicle', order.vehicle)
        order.airport        = request.form.get('airport', order.airport)
        order.pickup_location= request.form.get('pickup_location', order.pickup_location)
        order.booking_date   = request.form.get('booking_date', order.booking_date)
        order.booking_time   = request.form.get('booking_time', order.booking_time)
        order.passengers     = int(request.form.get('passengers') or order.passengers)
        order.luggage        = int(request.form.get('luggage') or 0)
        order.name           = request.form.get('name', order.name)
        order.phone          = request.form.get('phone', order.phone)
        order.email          = request.form.get('email', order.email)
        order.flight_number  = request.form.get('flight_number', order.flight_number)
        order.note           = request.form.get('note', order.note)
        order.total_price    = int(request.form.get('total_price') or 0)
        stops_raw = request.form.get('extra_stops', '')
        stops = [s.strip() for s in stops_raw.splitlines() if s.strip()]
        order.extra_stops    = _json.dumps(stops, ensure_ascii=False)
        order.extra_stop_fee = int(request.form.get('extra_stop_fee') or 0)
        db.session.commit()
        # 若已指派司機，推送更新通知
        if order.driver_id:
            driver = Driver.query.get(order.driver_id)
            if driver and driver.line_user_id:
                try:
                    stops_list = []
                    try: stops_list = _json.loads(order.extra_stops or '[]')
                    except: pass
                    stops_text = '\n'.join([f'  停靠{i+1}：{s}' for i,s in enumerate(stops_list)])
                    update_msg = (
                        f'⚠️ 訂單 #{order.id} 資料已更新\n\n'
                        f'日期時間：{order.booking_date} {order.booking_time}\n'
                        f'接送地點：{order.pickup_location}\n'
                        + (stops_text + '\n' if stops_text else '')
                        + f'乘客/行李：{order.passengers}人 / {order.luggage}件\n'
                        f'航班：{order.flight_number or "無"}\n'
                        f'客人電話：{order.phone}\n'
                        f'備註：{order.note or "無"}\n\n'
                        f'請確認最新資料，如有疑問請聯繫調度。'
                    )
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(
                            PushMessageRequest(
                                to=driver.line_user_id,
                                messages=[TextMessage(text=update_msg)]
                            )
                        )
                    flash(f'訂單已更新，已推送通知給司機 {driver.name}')
                except Exception as e:
                    app.logger.error(f'push driver update error: {e}')
                    flash('訂單已更新，但推送司機通知失敗')
            else:
                flash('訂單已更新')
        else:
            flash('訂單已更新')
        return redirect(url_for('admin_order_detail', order_id=order_id))

    import json as _json
    stops_list = []
    try: stops_list = _json.loads(order.extra_stops or '[]')
    except: pass
    stops_text = '\n'.join(stops_list)
    return render_template('admin/order_edit.html',
        order=order, airports=airports, vehicles=vehicles, stops_text=stops_text)


@app.route('/admin/order/<int:order_id>/message', methods=['POST'])
@admin_required
def admin_send_message(order_id):
    """後台直接發 LINE 訊息給客人"""
    order = Order.query.get_or_404(order_id)
    msg = request.form.get('message', '').strip()
    if not msg:
        flash('訊息內容不能為空')
        return redirect(url_for('admin_order_detail', order_id=order_id))

    if not order.line_user_id or order.line_user_id == 'manual':
        flash('此訂單為人工建立，無 LINE User ID，無法發送訊息')
        return redirect(url_for('admin_order_detail', order_id=order_id))

    release = request.form.get('release_human_mode') == '1'
    try:
        full_msg = f'【樂高客服】\n\n{msg}\n\n如有疑問請回覆此訊息，感謝您。'
        try:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=order.line_user_id,
                        messages=[TextMessage(text=full_msg)]
                    )
                )
        except Exception as e:
            app.logger.error(f'admin_send_message push error detail: {repr(e)}')
            flash(f'發送失敗：{repr(e)[:200]}')
            return redirect(url_for('admin_order_detail', order_id=order_id))
        # 若勾選「解除真人客服模式」，恢復 AI 客服
        if release:
            uid = order.line_user_id
            if uid in user_sessions and user_sessions[uid].get('step') == 'human_mode':
                user_sessions[uid] = {'step': 'ai_chat'}
            flash(f'訊息已發送，已恢復 AI 客服模式（訂單 #{order_id}）')
        else:
            flash(f'訊息已成功發送給客人（訂單 #{order_id}）')
    except Exception as e:
        app.logger.error(f'admin_send_message error: {e}')
        flash(f'發送失敗：{str(e)[:100]}')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/ai/pause/<line_user_id>', methods=['POST'])
@admin_required
def admin_ai_pause(line_user_id):
    user_sessions[line_user_id] = {'step': 'human_mode'}
    flash(f'已暫停 AI 客服（{line_user_id[:12]}...）')
    next_url = request.form.get('next') or url_for('admin_index')
    return redirect(next_url)

@app.route('/admin/ai/resume/<line_user_id>', methods=['POST'])
@admin_required
def admin_ai_resume(line_user_id):
    user_sessions[line_user_id] = {'step': 'ai_chat'}
    flash(f'已恢復 AI 客服（{line_user_id[:12]}...）')
    next_url = request.form.get('next') or url_for('admin_index')
    return redirect(next_url)

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
    notify_at = request.form.get('notify_at', '48')
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
    fee_raw = request.form.get('driver_fee', '')
    job = DispatchJob(
        order_id=order_id,
        status='開放搶單',
        note=request.form.get('dispatch_note', ''),
        notify_customer=request.form.get('notify_customer') == '1',
        driver_fee=int(fee_raw) if fee_raw.strip().isdigit() else None,
    )
    # 同步更新訂單的 total_price（若有填）
    total_raw = request.form.get('total_price', '')
    if total_raw.strip().isdigit():
        order.total_price = int(total_raw)
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
    # 同時推播到司機群組
    try:
        push_dispatch_to_group(order, job)
    except Exception as e:
        app.logger.error(f'group dispatch error: {e}')
    flash(f'搶單任務已發布，共通知 {sent} 位司機')
    return redirect(url_for('admin_order_detail', order_id=order_id))


@app.route('/admin/dispatch/<int:job_id>/reopen', methods=['POST'])
@admin_required
def admin_reopen_dispatch(job_id):
    """重新開放搶單（清除已搶司機，通知所有司機）"""
    job = DispatchJob.query.get_or_404(job_id)
    order = job.order

    # 清除原本搶到的司機
    old_driver_id = job.grabbed_by
    job.status     = '開放搶單'
    job.grabbed_by = None
    job.grabbed_at = None
    order.driver_id = None
    order.status    = '搶單中'
    db.session.commit()

    # 通知原司機已被取消
    if old_driver_id:
        old_driver = Driver.query.get(old_driver_id)
        if old_driver and old_driver.line_user_id:
            try:
                with ApiClient(configuration) as api_client:
                    MessagingApi(api_client).push_message(
                        PushMessageRequest(
                            to=old_driver.line_user_id,
                            messages=[TextMessage(
                                text=f'訂單 #{order.id} 的接單已由管理員取消，此訂單重新開放搶單。'
                            )]
                        )
                    )
            except Exception as e:
                app.logger.error(f'reopen notify old driver error: {e}')

    # 重新推播給所有司機
    drivers = Driver.query.filter(Driver.active == True, Driver.line_user_id != '').all()
    sent = 0
    for driver in drivers:
        try:
            push_dispatch_to_driver(driver, order, job)
            sent += 1
        except Exception as e:
            app.logger.error(f'reopen push error driver {driver.id}: {e}')

    # 同時推播到司機群組
    try:
        push_dispatch_to_group(order, job)
    except Exception as e:
        app.logger.error(f'group reopen dispatch error: {e}')
    flash(f'搶單已重新開放，共通知 {sent} 位司機')
    return redirect(url_for('admin_order_detail', order_id=job.order_id))

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

def push_dispatch_to_group(order, job):
    """推播搶單通知到司機群組"""
    if not DRIVER_GROUP_ID:
        return
    try:
        fee_line = ('車資：NT${:,}'.format(job.driver_fee)) if job.driver_fee else '車資：請洽調度'
        msg = '\n'.join([
            '【樂高預約單】',
            '訂單 #' + str(order.id),
            '─────────────',
            '服務：' + order.service_name,
            '機場：' + order.airport,
            '地點：' + _mask_address(order.pickup_location),
            '日期：' + order.booking_date + ' ' + order.booking_time,
            '乘客/行李：' + str(order.passengers) + '人 / ' + str(order.luggage) + '件',
            *([f'停靠點{i+1}：' + s for i, s in enumerate(__import__("json").loads(order.extra_stops or '[]'))]),
            '航班：' + (order.flight_number or '無'),
            '─────────────',
            fee_line,
            '─────────────',
            '請私訊回覆：' + str(order.id),
        ])
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=DRIVER_GROUP_ID,
                    messages=[TextMessage(text=msg)]
                )
            )
    except Exception as e:
        app.logger.error('push_dispatch_to_group error: ' + str(e))

@app.route('/admin/dispatch')
@admin_required
def admin_dispatch_list():
    sort      = request.args.get('sort', 'booking_date')
    dir_      = request.args.get('dir', 'asc')
    status_f  = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    date_exact= request.args.get('date_exact', '')  # 單日快速篩選

    q = DispatchJob.query.join(Order)
    if status_f:
        q = q.filter(DispatchJob.status == status_f)
    if date_exact:
        q = q.filter(Order.booking_date == date_exact)
    else:
        if date_from:
            q = q.filter(Order.booking_date >= date_from)
        if date_to:
            q = q.filter(Order.booking_date <= date_to)

    if sort == 'booking_date':
        col = Order.booking_date
    elif sort == 'created_at':
        col = DispatchJob.created_at
    else:
        col = Order.booking_date

    q = q.order_by(col.asc() if dir_ == 'asc' else col.desc(),
                   Order.booking_time.asc())
    jobs = q.all()

    from datetime import datetime as _dt
    return render_template('admin/dispatch.html', jobs=jobs,
                           sort=sort, dir=dir_, status_filter=status_f,
                           date_from=date_from, date_to=date_to,
                           date_exact=date_exact, now=_dt.now())


def _mask_address(addr):
    """地址只顯示到路/街/段，隱藏門號、巷弄"""
    if not addr:
        return addr
    import re as _re
    # 保留到「X路X段」或「X路」為止（支援中文/數字段數）
    m = _re.search(r'(.+?(?:路|街|大道)(?:[一二三四五六七八九十百0-9０-９]+段)?)', addr)
    if m:
        return m.group(1)
    return addr

def push_dispatch_to_driver(driver, order, job):
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "樂高搶單通知", "color": "#FFFFFF", "size": "lg", "weight": "bold", "wrap": True},
                {"type": "text", "text": f"訂單 #{order.id}　第一個搶到確認！", "color": "#8BA3C7", "size": "sm", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                make_info_row("服務", order.service_name),
                make_info_row("車型需求", order.vehicle),
                make_info_row("機場", order.airport),
                make_info_row("接送地點", _mask_address(order.pickup_location)),
                *([make_info_row(f"停靠點 {i+1}", _mask_address(s))
                    for i, s in enumerate(__import__("json").loads(order.extra_stops or '[]'))]),
                make_info_row("日期時間", f"{order.booking_date} {order.booking_time}"),
                make_info_row("乘客/行李", f"{order.passengers}人 / {order.luggage}件"),
                {"type": "separator", "margin": "sm"},
                # 費用資訊
                *(
                    [make_info_row("車資", f"NT${job.driver_fee:,}")]
                    if job.driver_fee else
                    [make_info_row("車資", "請洽調度確認")]
                ),
                {"type": "separator", "margin": "sm"},
                make_info_row("航班", order.flight_number or '無'),
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
                messages=[FlexMessage(alt_text=f'樂高預約單 #{order.id}', contents=FlexContainer.from_dict(bubble))]
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
    # 每次動態讀取，避免啟動時尚未設定
    api_key = os.environ.get('AVIATION_EDGE_KEY', '')
    app.logger.info(f'query_flight_info: key={api_key[:8] if api_key else "未設定"!r}, fn={flight_number!r}')
    if not api_key or not flight_number:
        return None

    fn = flight_number.strip().upper().replace(' ', '')

    # Aviation Edge 不接受前置零，例如 BR0851 → BR851
    import re as _re
    fn_no_zero = _re.sub(r'^([A-Z]{1,3})0+([0-9]+)$', r'\1\2', fn)

    def fmt_time(t):
        if not t:
            return ''
        try:
            dt = datetime.fromisoformat(t[:16])
            weekdays = ['一','二','三','四','五','六','日']
            wd = weekdays[dt.weekday()]
            return dt.strftime(f'%m/%d（週{wd}）%H:%M')
        except Exception:
            return t[:16]

    # 常見機場 IATA → 中文名稱對照（300+ 機場）
    IATA_NAMES = {
        # ── 台灣 ──
        'TPE':'桃園國際機場','TSA':'台北松山機場','RMQ':'台中清泉崗機場',
        'KHH':'高雄小港機場','TNN':'台南機場','HUN':'花蓮機場',
        'TTT':'台東機場','KNH':'金門機場','MZG':'澎湖馬公機場',
        'LZN':'馬祖南竿機場','MFK':'馬祖北竿機場',
        # ── 日本 ──
        'NRT':'東京成田機場','HND':'東京羽田機場','KIX':'大阪關西機場',
        'ITM':'大阪伊丹機場','NGO':'名古屋中部機場','FUK':'福岡機場',
        'OKA':'沖繩那霸機場','CTS':'札幌新千歲機場','HIJ':'廣島機場',
        'KMJ':'熊本機場','KOJ':'鹿兒島機場','OIT':'大分機場',
        'UKB':'神戶機場','MMY':'宮古島機場','ISG':'石垣機場',
        'SDJ':'仙台機場','KMQ':'小松機場','TOY':'富山機場',
        'MYJ':'松山機場（日本）','TAK':'高松機場','TKS':'德島機場',
        # ── 韓國 ──
        'ICN':'首爾仁川機場','GMP':'首爾金浦機場','PUS':'釜山金海機場',
        'CJU':'濟州機場','TAE':'大邱機場','CJJ':'清州機場',
        # ── 中國 ──
        'PVG':'上海浦東機場','SHA':'上海虹橋機場','PEK':'北京首都機場',
        'PKX':'北京大興機場','CAN':'廣州白雲機場','SZX':'深圳寶安機場',
        'CTU':'成都天府機場','TFU':'成都雙流機場','XIY':'西安咸陽機場',
        'KMG':'昆明長水機場','CKG':'重慶江北機場','WUH':'武漢天河機場',
        'HGH':'杭州蕭山機場','NKG':'南京祿口機場','XMN':'廈門高崎機場',
        'CSX':'長沙黃花機場','HAK':'海口美蘭機場','SYX':'三亞鳳凰機場',
        'TAO':'青島膠東機場','TNA':'濟南遙牆機場','CGO':'鄭州新鄭機場',
        'SHE':'瀋陽桃仙機場','HRB':'哈爾濱太平機場','DLC':'大連周水子機場',
        'URC':'烏魯木齊地窩堡機場','TSN':'天津濱海機場','HET':'呼和浩特白塔機場',
        # ── 香港、澳門 ──
        'HKG':'香港國際機場','MFM':'澳門國際機場',
        # ── 東南亞 ──
        'SIN':'新加坡樟宜機場','BKK':'曼谷素萬那普機場','DMK':'曼谷廊曼機場',
        'KUL':'吉隆坡國際機場','KBR':'哥打巴魯機場','PEN':'檳城機場',
        'CGK':'雅加達蘇加諾機場','DPS':'峇里島機場','SUB':'泗水機場',
        'MNL':'馬尼拉機場','CEB':'宿霧機場','HKT':'普吉機場',
        'CNX':'清邁機場','USM':'蘇梅島機場','HAN':'河內內排機場',
        'SGN':'胡志明市機場','DAD':'峴港機場','RGN':'仰光機場',
        'PNH':'金邊機場','REP':'暹粒機場','VTE':'永珍瓦岱機場',
        'BWN':'汶萊機場','CXR':'金蘭機場','VII':'榮市機場',
        # ── 南亞 ──
        'DEL':'新德里英迪拉甘地機場','BOM':'孟買賈特拉帕蒂機場',
        'MAA':'清奈機場','BLR':'班加羅爾機場','CCU':'加爾各答機場',
        'HYD':'海得拉巴機場','COK':'科欽機場','CMB':'可倫坡機場',
        'DAC':'達卡機場','KTM':'加德滿都機場',
        # ── 中東 ──
        'DXB':'杜拜國際機場','AUH':'阿布達比機場','DOH':'多哈哈馬德機場',
        'KWI':'科威特機場','BAH':'巴林機場','MCT':'馬斯喀特機場',
        'AMM':'安曼皇后機場','BEY':'貝魯特機場','TLV':'特拉維夫機場',
        'IST':'伊斯坦堡機場','SAW':'伊斯坦堡薩比哈機場','ESB':'安卡拉機場',
        # ── 歐洲 ──
        'LHR':'倫敦希斯洛機場','LGW':'倫敦蓋威克機場','STN':'倫敦斯坦斯特機場',
        'LCY':'倫敦城市機場','MAN':'曼徹斯特機場','EDI':'愛丁堡機場',
        'CDG':'巴黎戴高樂機場','ORY':'巴黎奧利機場','FRA':'法蘭克福機場',
        'MUC':'慕尼黑機場','BER':'柏林布蘭登堡機場','HAM':'漢堡機場',
        'AMS':'阿姆斯特丹史基浦機場','BRU':'布魯塞爾機場','ZRH':'蘇黎世機場',
        'GVA':'日內瓦機場','VIE':'維也納機場','PRG':'布拉格機場',
        'WAW':'華沙機場','BUD':'布達佩斯機場','ATH':'雅典機場',
        'FCO':'羅馬菲烏米奇諾機場','MXP':'米蘭馬爾彭薩機場','LIN':'米蘭林納特機場',
        'MAD':'馬德里機場','BCN':'巴塞隆納機場','LIS':'里斯本機場',
        'CPH':'哥本哈根機場','ARN':'斯德哥爾摩阿蘭達機場','OSL':'奧斯陸機場',
        'HEL':'赫爾辛基機場','DUB':'都柏林機場','KEF':'雷克雅維克機場',
        # ── 北美 ──
        'LAX':'洛杉磯國際機場','JFK':'紐約甘迺迪機場','EWR':'紐瓦克機場',
        'LGA':'紐約拉瓜迪亞機場','SFO':'舊金山機場','ORD':'芝加哥奧海爾機場',
        'MDW':'芝加哥中途機場','SEA':'西雅圖機場','ATL':'亞特蘭大機場',
        'DFW':'達拉斯沃斯堡機場','IAH':'休士頓喬治布希機場','MIA':'邁阿密機場',
        'BOS':'波士頓機場','DEN':'丹佛機場','LAS':'拉斯維加斯機場',
        'PHX':'鳳凰城機場','MSP':'明尼阿波利斯機場','DTW':'底特律機場',
        'PHL':'費城機場','CLT':'夏洛特機場','IAD':'華盛頓杜勒斯機場',
        'DCA':'華盛頓雷根機場','SAN':'聖地牙哥機場','PDX':'波特蘭機場',
        'SJC':'聖荷西機場','OAK':'奧克蘭機場','SNA':'橘郡機場',
        'HNL':'檀香山機場','ANC':'安克拉治機場',
        'YVR':'溫哥華機場','YYZ':'多倫多皮爾遜機場','YUL':'蒙特婁機場',
        'YYC':'卡加利機場','YEG':'艾德蒙頓機場','YOW':'渥太華機場',
        'MEX':'墨西哥城機場','GDL':'瓜達拉哈拉機場','CUN':'坎昆機場',
        # ── 大洋洲 ──
        'SYD':'雪梨機場','MEL':'墨爾本機場','BNE':'布里斯本機場',
        'PER':'伯斯機場','ADL':'阿得雷德機場','CBR':'坎培拉機場',
        'AKL':'奧克蘭機場','CHC':'基督城機場','WLG':'威靈頓機場',
        # ── 南美 ──
        'GRU':'聖保羅瓜魯柳斯機場','GIG':'里約熱內盧機場','EZE':'布宜諾斯艾利斯機場',
        'SCL':'聖地牙哥機場','BOG':'波哥大機場','LIM':'利馬機場',
        # ── 非洲 ──
        'JNB':'約翰尼斯堡奧坦博機場','CPT':'開普敦機場','NBO':'奈洛比機場',
        'CAI':'開羅機場','CMN':'卡薩布蘭加機場','ADD':'阿迪斯阿貝巴機場',
        'LOS':'拉哥斯機場','ACC':'阿克拉機場',
        # ── 中亞/俄羅斯 ──
        'SVO':'莫斯科謝列梅捷沃機場','DME':'莫斯科多莫傑多沃機場',
        'LED':'聖彼得堡機場','ALA':'阿拉木圖機場','TAS':'塔什干機場',
    }

    # 航空公司 IATA → 中文名稱對照
    AIRLINE_NAMES = {
        # 台灣
        'BR':'長榮航空','CI':'中華航空','B7':'立榮航空','AE':'華信航空',
        'IT':'台灣虎航','GE':'復興航空',
        # 日本
        'JL':'日本航空','NH':'全日空','JW':'香草航空','MM':'樂桃航空',
        'GK':'捷星日本','7G':'星悅航空','NU':'日本越洋','BC':'天馬航空',
        # 韓國
        'KE':'大韓航空','OZ':'韓亞航空','7C':'濟州航空','LJ':'真航空',
        'RS':'韓釜航空','ZE':'易斯達航空','TW':'德威航空','4V':'飛天航空',
        # 中國
        'CA':'中國國際航空','MU':'中國東方航空','CZ':'中國南方航空',
        'HU':'海南航空','3U':'四川航空','ZH':'深圳航空','FM':'上海航空',
        'KN':'中國聯合航空','8L':'祥鵬航空','9C':'春秋航空','GJ':'長龍航空',
        # 香港/澳門
        'CX':'國泰航空','HX':'香港航空','UO':'香港快運','NX':'澳門航空',
        # 東南亞
        'SQ':'新加坡航空','MI':'勝安航空','TR':'酷航','3K':'捷星亞洲',
        'TZ':'酷航（舊）','TG':'泰國航空','FD':'泰亞洲航空','DD':'諾克航空',
        'PG':'曼谷航空','QV':'老撾航空','VN':'越南航空','VJ':'越捷航空',
        'BL':'太平洋航空','MH':'馬來西亞航空','AK':'亞洲航空','FY':'飛螢航空',
        'OD':'馬印航空','GA':'鷹航印尼','JT':'獅子航空','QZ':'印尼亞航',
        'PR':'菲律賓航空','5J':'宿霧太平洋航空','Z2':'菲律賓亞航',
        # 中東
        'EK':'阿聯酋航空','EY':'阿提哈德航空','QR':'卡達航空',
        'GF':'海灣航空','KU':'科威特航空','WY':'阿曼航空',
        # 歐美
        'UA':'聯合航空','AA':'美國航空','DL':'達美航空','AS':'阿拉斯加航空',
        'B6':'捷藍航空','WN':'西南航空','AC':'加拿大航空','WS':'西捷航空',
        'BA':'英國航空','LH':'漢莎航空','AF':'法國航空','KL':'荷蘭皇家航空',
        'LX':'瑞士航空','OS':'奧地利航空','SK':'北歐航空','AY':'芬蘭航空',
        'IB':'伊比利亞航空','TP':'葡萄牙航空','AZ':'義大利航空',
        'TK':'土耳其航空','RO':'羅馬尼亞航空',
        # 大洋洲
        'QF':'澳洲航空','JQ':'捷星航空','VA':'維珍澳洲','NZ':'紐西蘭航空',
        # 其他
        'FZ':'阿聯酋快運','WB':'盧安達航空','ET':'衣索比亞航空',
        'JX':'星宇航空',
    }

    def airline_name_zh(code):
        if not code:
            return ''
        return AIRLINE_NAMES.get(code.upper(), code)

    def iata_name(code):
        if not code:
            return '未知'
        return IATA_NAMES.get(code, code)

    def parse_record(f):
        dep = f.get('departure', {}) or {}
        arr = f.get('arrival',   {}) or {}
        airline = f.get('airline', {}) or {}
        codeshared = f.get('codeshared', {}) or {}
        dep_delay = dep.get('delay', 0) or 0
        arr_delay = arr.get('delay', 0) or 0
        dep_iata = dep.get('iataCode', '') or ''
        arr_iata = arr.get('iataCode', '') or ''
        # 航空公司名稱：優先用中文對照，再用英文名，最後用 iataCode
        _iata = airline.get('iataCode', '') or ''
        _name = airline.get('name', '') or ''
        _cs_iata = (codeshared.get('airline', {}) or {}).get('iataCode', '') or ''
        airline_name = (airline_name_zh(_iata) if _iata else '') or _name or airline_name_zh(_cs_iata) or _iata
        return {
            'flight':        fn,
            'airline':       airline_name,
            'status':        f.get('status', ''),
            # 出發
            'dep_airport':   iata_name(dep_iata),
            'dep_iata':      dep_iata,
            'dep_terminal':  dep.get('terminal', '') or '',
            'dep_gate':      dep.get('gate', '') or '',
            'dep_scheduled': fmt_time(dep.get('scheduledTime', '')),
            'dep_estimated': fmt_time(dep.get('estimatedTime', '')),
            'dep_actual':    fmt_time(dep.get('actualTime', '')),
            'dep_delay':     int(dep_delay),
            # 抵達
            'arr_airport':   iata_name(arr_iata),
            'arr_iata':      arr_iata,
            'arr_terminal':  arr.get('terminal', '') or '',
            'arr_gate':      arr.get('gate', '') or '',
            'arr_baggage':   arr.get('baggage', '') or '',
            'arr_scheduled': fmt_time(arr.get('scheduledTime', '')),
            'arr_estimated': fmt_time(arr.get('estimatedTime', '')),
            'arr_actual':    fmt_time(arr.get('actualTime', '')),
            'arr_delay':     int(arr_delay),
        }

    try:
        # ── 方法 1：即時追蹤（航班在空中時有效）──
        resp = requests.get(
            'https://aviation-edge.com/v2/public/flights',
            params={'key': api_key, 'flightIata': fn_no_zero},
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
            _live_iata = airline.get('iataCode', '') or ''
            _dep_iata  = dep.get('iataCode', '') or ''
            _arr_iata  = arr.get('iataCode', '') or ''
            return {
                'flight':        fn,
                'airline':       airline_name_zh(_live_iata) or _live_iata,
                'status':        f.get('status', ''),
                'dep_airport':   iata_name(_dep_iata),
                'dep_iata':      _dep_iata,
                'dep_terminal':  '', 'dep_gate':      '',
                'dep_scheduled': '', 'dep_estimated': '', 'dep_actual': '',
                'dep_delay':     0,
                'arr_airport':   iata_name(_arr_iata),
                'arr_iata':      _arr_iata,
                'arr_terminal':  '', 'arr_gate':      '', 'arr_baggage': '',
                'arr_scheduled': '', 'arr_estimated': '', 'arr_actual': '',
                'arr_delay':     0,
                'altitude':      geo.get('altitude', ''),
                'live':          True,
            }
    except Exception as e:
        app.logger.warning(f'aviation_edge flights error: {e}')

    try:
        # ── 方法 2：時刻表（timetable）──
        # Aviation Edge timetable 支援用 date 參數過濾指定日期
        timetable_params_base = {
            'key':         api_key,
            'flight_iata': fn_no_zero,
        }
        # 不帶 date 參數查詢（Aviation Edge date 參數不穩定），查到後再用日期篩選
        app.logger.info(f'query_flight_info: 查詢 {fn_no_zero}，預約日期={date_str}')

        for t in ['departure', 'arrival']:
            params = {**timetable_params_base, 'type': t}
            resp = requests.get(
                'https://aviation-edge.com/v2/public/timetable',
                params=params,
                timeout=8
            )
            data = resp.json()
            app.logger.info(f'aviation_edge timetable {t}: {data[:1] if isinstance(data,list) else data}')
            if isinstance(data, list) and data:
                # 若有多筆且有預約日期，優先找日期相符的
                record = data[0]
                if date_str and len(data) > 1:
                    for f in data:
                        dep = f.get('departure', {}) or {}
                        arr = f.get('arrival', {}) or {}
                        t_str = dep.get('scheduledTime') or arr.get('scheduledTime') or ''
                        if t_str[:10] == date_str:
                            record = f
                            break
                return parse_record(record)

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
        if 4 <= days_ahead <= 7 and 'short_book' in surcharge_map:
            amt = surcharge_map['short_book'].amount
            result['surcharges'].append({'label': '四至七天內預約加收', 'amount': amt})
            result['breakdown'].append({'label': '四至七天內預約加收', 'amount': amt})
        elif 2 <= days_ahead <= 3 and 'short_book' in surcharge_map and 'urgent' in surcharge_map:
            amt = surcharge_map['short_book'].amount + surcharge_map['urgent'].amount
            result['surcharges'].append({'label': '二至三天內預約加收', 'amount': amt})
            result['breakdown'].append({'label': '二至三天內預約加收', 'amount': amt})
    except Exception:
        pass

    try:
        booking_md = order.booking_date[5:] if order.booking_date and len(order.booking_date) >= 7 else ''
        booking_full = order.booking_date or ''
        holidays = HolidaySurcharge.query.filter_by(active=True).all()
        for h in holidays:
            df = h.date_from or ''
            dt = h.date_to or ''
            if len(df) == 5:  # MM-DD 格式（每年重複）
                match = booking_md and df <= booking_md <= dt
            else:  # YYYY-MM-DD 格式（指定年份）
                match = booking_full and df <= booking_full <= dt
            if match:
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

    quote = calculate_quote(order)

    if order.extra_stop_fee:
        quote['breakdown'].append({'label': f'多點停靠加收（{len(extra_stops)} 點）' if extra_stops else '多點加收', 'amount': order.extra_stop_fee})
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
    import re

    app.logger.info(f'Newebpay notify received: form={dict(request.form)}')
    status         = request.form.get('Status')
    trade_info_enc = request.form.get('TradeInfo', '')
    trade_sha      = request.form.get('TradeSha', '')

    expected = newebpay_sha256(trade_info_enc)
    app.logger.info(f'Newebpay TradeSha check: received={trade_sha[:20] if trade_sha else ""}... expected={expected[:20]}...')
    if trade_sha.upper() != expected.upper():
        app.logger.warning(f'Newebpay TradeSha mismatch! received={trade_sha} expected={expected}')
        return 'FAIL', 400

    if status != 'SUCCESS':
        app.logger.info(f'Newebpay payment not success: status={status}')
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
        "【送機說明】\n"
        "• 送機以預約時間為主，等待超過 16 分鐘起，加收 NT$800 元／每小時。\n\n"
        "【接機說明】\n"
        "• 接機以航班實際落地時間為準，等待 90 分鐘。\n"
        "• 超過 91 分鐘起，加收 NT$800 元／每小時。\n"
        "• 如航班延誤超過兩小時，第三個小時起加收 NT$300 元／每小時（依機場航班動態為準）。\n"
        "• 取好行李後請主動聯繫司機，司機將告知見面地點與車牌。\n"
        "• 若等候超過 90 分鐘未能聯繫，預約將自動取消並離開現場。\n\n"
        "【行李說明】\n"
        "• 超過 30 吋或大型行李箱、胖胖箱等非標準行李，請事先告知。\n"
        "• 行李定義：行李箱、嬰兒車、登機箱、警衛包等占用後車廂空間之物件。\n"
        "• 若到場後人數及行李載不下時，司機有權拒絕載送，並不退費。\n\n"
        "【異動與取消】\n"
        "• 任何異動（包含行李件數）請於八天前告知。\n"
        "• 七天內任何理由均無法異動或取消，定金恕不退還。\n\n"
        "【保險】\n"
        "• 所有車輛均投保乘客險每人 500 萬元以上。\n\n"
        "【特別提醒】\n"
        "• 本公司車輛皆為合法合規營運，請配合服務流程。\n"
        "• 若無法聯繫上司機與客服，請勿自行搭乘他車，我們無法對非本公司安排行為負責。\n\n"
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
        note = order.note or ''
        inv_type     = ''
        carrier      = ''
        tax_id       = ''
        company_name = ''

        if '【發票】公司抬頭：' in note:
            inv_type = 'company'
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
        elif '【發票】捐贈發票' in note:
            inv_type = 'donate'

        app.logger.info(f'ezPay invoice: inv_type={inv_type!r}, carrier={carrier!r}, order_id={order.id}')

        inv_no = issue_ezpay_invoice(order, inv_type, carrier, tax_id, company_name)
        if inv_no:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=order.line_user_id,
                        messages=[TextMessage(text=f'🧾 電子發票已開立\n發票號碼：{inv_no}\n\n如需查詢發票，請至財政部電子發票整合服務平台查詢。')]
                    )
                )
        else:
            app.logger.warning(f'ezPay invoice not issued for order {order.id}')
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
    payment_methods = SiteSetting.get('payment_methods', 'CREDIT,WEBATM,VACC').split(',')
    return render_template('admin/pricing.html', rules=rules, surcharges=surcharges, holidays=holidays,
                           newebpay_mode=newebpay_mode,
                           ezpay_mode=ezpay_mode,
                           ezpay_merchant_id=ezpay_merchant_id,
                           ezpay_key_set=ezpay_key_set,
                           payment_methods=payment_methods)


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


@app.route('/admin/test_flight')
@admin_required
def admin_test_flight():
    fn = request.args.get('fn', '').strip().upper().replace(' ', '')
    result = {}
    raw = {}
    if fn and AVIATION_EDGE_KEY:
        import requests as _req
        # 測試 1: flights (即時)
        try:
            r = _req.get('https://aviation-edge.com/v2/public/flights',
                params={'key': api_key, 'flightIata': fn}, timeout=10)
            raw['flights'] = r.json()
        except Exception as e:
            raw['flights'] = str(e)
        # 測試 2: timetable departure
        try:
            r = _req.get('https://aviation-edge.com/v2/public/timetable',
                params={'key': AVIATION_EDGE_KEY, 'flight_iata': fn, 'type': 'departure'}, timeout=10)
            raw['timetable_dep'] = r.json()
        except Exception as e:
            raw['timetable_dep'] = str(e)
        # 測試 3: timetable arrival
        try:
            r = _req.get('https://aviation-edge.com/v2/public/timetable',
                params={'key': AVIATION_EDGE_KEY, 'flight_iata': fn, 'type': 'arrival'}, timeout=10)
            raw['timetable_arr'] = r.json()
        except Exception as e:
            raw['timetable_arr'] = str(e)
        # 測試 4: 用 query_flight_info
        result = query_flight_info(fn, '')

    import json
    raw_json = json.dumps(raw, ensure_ascii=False, indent=2)
    result_json = json.dumps(result, ensure_ascii=False, indent=2) if result else 'None（查無）'

    html = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>航班 API 測試</title>
<style>body{{font-family:sans-serif;padding:20px;max-width:900px;margin:0 auto}}
input{{padding:8px;font-size:16px;width:200px}}
button{{padding:8px 16px;font-size:16px;background:#4A9B8F;color:#fff;border:none;border-radius:4px;cursor:pointer}}
pre{{background:#1a1a1a;color:#0f0;padding:16px;border-radius:8px;overflow-x:auto;font-size:12px;white-space:pre-wrap}}
h2{{color:#2D3748}}h3{{color:#4A9B8F}}</style>
</head><body>
<h2>Aviation Edge API 測試</h2>
<form method="GET">
  <input name="fn" value="{fn}" placeholder="航班號例如 BR0851">
  <button type="submit">查詢</button>
</form>
{'<h3>✅ query_flight_info 結果：</h3><pre>' + result_json + '</pre>' if fn else ''}
{'<h3>📡 原始 API 回傳：</h3><pre>' + raw_json + '</pre>' if fn else ''}
</body></html>"""
    return html

@app.route('/admin/pricing/payment_methods', methods=['POST'])
@admin_required
def admin_set_payment_methods():
    methods = request.form.getlist('methods')
    SiteSetting.set('payment_methods', ','.join(methods) if methods else 'CREDIT')
    flash('付款方式已更新')
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
    # 記錄訪客
    if event.source.type == 'user':
        try:
            visitor = LineVisitor.query.filter_by(line_user_id=user_id).first()
            if not visitor:
                display_name = ''
                picture_url = ''
                try:
                    with ApiClient(configuration) as _ac:
                        profile = MessagingApi(_ac).get_profile(user_id)
                        display_name = profile.display_name or ''
                        picture_url = getattr(profile, 'picture_url', '') or ''
                except Exception:
                    pass
                visitor = LineVisitor(
                    line_user_id=user_id,
                    display_name=display_name,
                    picture_url=picture_url,
                )
                db.session.add(visitor)
            else:
                visitor.last_seen = datetime.utcnow()
            visitor.message_count = (visitor.message_count or 0) + 1
            db.session.commit()
        except Exception as _e:
            app.logger.warning(f'visitor log error: {_e}')
        
    text = event.message.text.strip()
    session = user_sessions.get(user_id, {})
    step = session.get('step', '')

    # ── 群組訊息：只處理指令，其餘完全忽略 ──
    source_type = event.source.type
    if source_type == 'group':
        if text in ['群組ID', 'groupid', 'GROUP ID']:
            group_id = event.source.group_id
            reply_text(event.reply_token, f'此群組 ID：\n{group_id}\n\n請將此 ID 填入 Render 環境變數 SUPPORT_GROUP_ID')
        # 群組裡其他訊息一律不回應
        return

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

    if text in ['開始', 'hi', 'Hi', 'HI', 'hello', 'Hello', '你好', '哈囉', '選單', '主選單', 'menu', 'Menu']:
        user_sessions.pop(user_id, None)
        send_main_menu(event.reply_token)
        return
    
    if text in ['真人客服', '真人', '人工客服', '客服']:
        notify_human_agent(user_id)
        reply_text(event.reply_token,
            '已通知真人客服！\n\n'
            '客服人員收到通知後將主動與您聯繫，請稍候。\n\n'
            '如是一般問題可以直接打字詢問，AI 可以回答您 80% 的問題。\n\n'
            '如有急事，可以撥打 04-26318898、0968685835'
        )
        return

    if text in ['預約', '訂車', '機場接送', '開始預約']:
        user_sessions[user_id] = {'step': 'choose_service'}
        send_service_menu(event.reply_token)
        return

    if text in ['報價', '我要報價', '查詢報價', '快速報價']:
        user_sessions[user_id] = {'step': 'quote_service'}
        send_quote_service_menu(event.reply_token)
        return
    
    if text == '查詢訂單':
        user_sessions[user_id] = {'step': 'query_name'}
        reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
        return

    if step == 'human_mode':
        # 真人客服模式：AI 靜默，客人輸入「預約」可重新進入預約流程
        if any(kw in text for kw in ['預約', '訂車', '我要訂', '我想訂', '幫我訂']):
            user_sessions[user_id] = {'step': 'choose_service'}
            reply_text(event.reply_token, '好的！幫您切換到預約流程。')
            send_service_menu(event.reply_token)
        elif any(kw in text for kw in ['查詢', '我的訂單', '訂單狀態']):
            user_sessions[user_id] = {'step': 'query_name'}
            reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
        # 其餘訊息靜默，等真人客服回應
        return

    if step == 'ai_chat':
        if any(kw in text for kw in ['預約', '訂車', '我要訂', '我想訂', '幫我訂']):
            user_sessions[user_id] = {'step': 'choose_service'}
            reply_text(event.reply_token, '好的！幫您切換到預約流程。')
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
        session['step'] = 'ask_extra_stops'
        session['extra_stops'] = []
        session['extra_stop_fee'] = 0
        user_sessions[user_id] = session
        send_extra_stops_menu(event.reply_token)

    elif step == 'input_date':
        try:
            from datetime import date as _date
            dt = datetime.strptime(text, '%Y-%m-%d')
            days_ahead = (dt.date() - _date.today()).days
            if days_ahead < 0:
                reply_text(event.reply_token, '日期已過期，請重新輸入，例如：' + datetime.now().strftime('%Y-%m-%d'))
            elif days_ahead <= 1:
                reply_text(event.reply_token,
                    f'⚠️ 當天及前一天的預約請直接聯繫真人客服處理，謝謝！\n\n'
                    f'如有急事，可以撥打 04-26318898、0968685835'
                )
            elif days_ahead > 240:
                reply_text(event.reply_token,
                    f'⚠️ 線上預約系統僅開放 8 個月（240 天）內的日期。\n\n'
                    f'如需預約更遠的日期，請聯繫客服人員協助處理，謝謝！\n\n'
                    f'請重新輸入日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：'
                )
            else:
                session['date'] = text
                session['step'] = 'input_time'
                # 若標記需要推車程，在問時間前先 push（背景）
                if session.pop('_push_travel_on_date', False):
                    import threading
                    threading.Thread(target=_push_est_travel, args=(user_id, dict(session)), daemon=True).start()
                user_sessions[user_id] = session
                _reply_time_hint(event.reply_token, session)
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
            reply_text(event.reply_token, f'已記錄時間：{text}\n\n請輸入乘客人數（請輸入 1-7 單一數字）：')
        except ValueError:
            reply_text(event.reply_token, '時間格式錯誤，請重新輸入，例如：08:30')

    elif step == 'input_passengers':
        import re as _re
        _m = _re.search(r'([0-9]+)', text)
        if _m and 1 <= int(_m.group(1)) <= 7:
            session['passengers'] = _m.group(1)
            session['8th_guest'] = False
            session['8th_guest_fee'] = 0
            session['step'] = 'input_luggage'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入行李件數，最多 7 件（含推車），請輸入 1-7 單一數字：')
        else:
            reply_text(event.reply_token,
                '⚠️ 考量貴賓們有寬敞舒適的體驗，最多 7 人 7 件，恕不開放第八位貴賓。\n\n'
                '請輸入 1-7 的數字：'
            )

    elif step == 'input_luggage':
        import re as _re
        _m = _re.search(r'([0-9]+)', text)  # 相容「3件」「2個」等格式
        if _m and 1 <= int(_m.group(1)) <= 7:
            session['luggage'] = _m.group(1)
            session['step'] = 'input_name'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入您的中文姓名：')
        else:
            reply_text(event.reply_token, '請輸入有效的行李件數（1-7）：')

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
        reply_text(event.reply_token,
            '請輸入您的航班號碼：\n例：BR166、CI688、JX200\n\n'
            '（航班號碼為必填，沒有航班號碼無法完成預約）'
        )

    elif step == 'input_flight':
        fn = text.strip().upper().replace(' ', '')
        import re as _re
        if not _re.match(r'^[A-Z0-9]{2,8}$', fn):
            reply_text(event.reply_token,
                f'「{text}」不是有效的航班號碼格式。\n\n'
                '請重新輸入，例：BR166、CI688、JX200\n\n'
                '（沒有航班號碼無法完成預約，如有疑問請點「真人客服」）'
            )
        else:
            session['flight'] = fn
            session['step'] = 'ask_child_seat'
            user_sessions[user_id] = session
            send_child_seat_menu(event.reply_token)

    elif step == 'confirm_flight':
        if text in ['確認', '對', 'yes', 'YES', 'Yes', '是']:
            session['step'] = 'ask_child_seat'
            user_sessions[user_id] = session
            send_child_seat_menu(event.reply_token)
        else:
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
        session['step'] = 'confirm'
        user_sessions[user_id] = session
        send_order_confirm(event.reply_token, session)

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
        session['step'] = 'confirm'
        user_sessions[user_id] = session
        send_order_confirm(event.reply_token, session)

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
            # 所有停靠點填完 → 標記需要推車程，問日期
            session['step'] = 'input_date'
            session['_push_travel_on_date'] = True
            user_sessions[user_id] = session
            reply_text(event.reply_token, f'請輸入接送日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：')

    elif step == 'quote_pickup':
        addr = text.strip()
        # 簡單驗證：至少要有縣市關鍵字
        import re as _re
        if len(addr) < 4:
            reply_text(event.reply_token, '請輸入更完整的地址（至少含縣市及鄉鎮區市）：\n例：台中市西區、彰化縣員林市')
        else:
            session['quote_pickup'] = addr
            session['step'] = 'quote_ask_stop'
            session['quote_stops'] = []
            session['quote_stop_fee'] = 0
            user_sessions[user_id] = session
            send_quote_stop_menu(event.reply_token)

    elif step == 'quote_stop_input':
        stop_addr = text.strip()
        stops = session.get('quote_stops', [])
        origin = stops[-1] if stops else session.get('quote_pickup', '')
        distance_km = get_distance_km(origin, stop_addr)
        fee, km = calc_extra_stop_fee(distance_km)
        stops.append(stop_addr)
        session['quote_stops'] = stops
        if fee:
            session['quote_stop_fee'] = session.get('quote_stop_fee', 0) + fee
            dist_text = f'（距離約 {km} 公里，加收 NT${fee}）'
        else:
            dist_text = '（距離計算失敗，費用待確認）'
        session['step'] = 'quote_stop_more_ask'
        user_sessions[user_id] = session

        notice_msg = f'已新增停靠點：{stop_addr}\n{dist_text}\n\n目前多點加收合計：NT${session["quote_stop_fee"]}'

        # 全部改用 push，不用 reply_token
        def _push_stop_and_menu(uid, msg):
            try:
                with ApiClient(configuration) as api_client:
                    api = MessagingApi(api_client)
                    # 先發文字
                    api.push_message(PushMessageRequest(
                        to=uid, messages=[TextMessage(text=msg)]
                    ))
                    # 再發選單
                    bubble = {
                        "type": "bubble",
                        "header": header_box("快速報價", "#2B6CB0"),
                        "body": {"type": "box", "layout": "vertical", "contents": [
                            {"type": "text", "text": "還有其他停靠點嗎？", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
                            make_button("繼續新增停靠點", "quote_stop_more"),
                            make_button("完成，直接報價", "quote_stop_done", "primary"),
                        ]}
                    }
                    api.push_message(PushMessageRequest(
                        to=uid,
                        messages=[FlexMessage(alt_text='繼續新增停靠點', contents=FlexContainer.from_dict(bubble))]
                    ))
            except Exception as e:
                app.logger.error(f'push stop and menu error: {e}')

        import threading
        threading.Thread(target=_push_stop_and_menu, args=(user_id, notice_msg), daemon=True).start()
        # reply_token 回一個空白確認，讓 LINE 不報錯
        reply_text(event.reply_token, '正在計算距離，請稍候...')

    elif step == 'quote_date':
        try:
            from datetime import date as _date
            dt = datetime.strptime(text, '%Y-%m-%d')
            days_ahead = (dt.date() - _date.today()).days
            if days_ahead < 0:
                reply_text(event.reply_token, f'日期已過期，請重新輸入，例如：{datetime.now().strftime("%Y-%m-%d")}')
            else:
                session['quote_date'] = text
                user_sessions[user_id] = session
                _show_quote_result(event.reply_token, session, user_id)
                user_sessions.pop(user_id, None)
        except ValueError:
            reply_text(event.reply_token, f'日期格式錯誤，請重新輸入，例如：{datetime.now().strftime("%Y-%m-%d")}')
            
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
        tb = traceback.format_exc()
        print('handle_postback error:', tb)
        app.logger.error(f'handle_postback error: {tb}')
        try:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text='系統處理時發生錯誤，請稍後再試或聯繫客服。')]
                    )
                )
        except Exception:
            pass

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
        reply_text(event.reply_token, '寵物同行加收：NT$300\n\n請輸入備註事項（若無請輸入「無」）：')

    elif data == 'guest_8th_yes':
        session['8th_guest'] = True
        session['8th_guest_fee'] = 400
        session['passengers'] = str(int(session.get('passengers', 7)) + 1)
        session['step'] = 'input_luggage'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '已記錄第八位貴賓，加收 NT$400。\n\n請輸入行李件數，最多7件（數字）：')

    elif data == 'guest_8th_no':
        session['8th_guest'] = False
        session['8th_guest_fee'] = 0
        session['step'] = 'input_luggage'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入行李件數，最多7件（數字）：')
        
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
        notify_human_agent(user_id)
        # 不進入 human_mode，AI 繼續回應
        reply_text(event.reply_token,
            '已通知真人客服！\n\n'
            '客服人員收到通知後將主動與您聯繫，請稍候。\n\n'
            '如是一般問題可以直接打字詢問，AI 可以回答您 80% 的問題。\n\n'
            '如有急事，可以撥打 04-26318898、0968685835'
        )

    elif data == 'start_quote':
        user_sessions[user_id] = {'step': 'quote_service'}
        send_quote_service_menu(event.reply_token)

    elif data == 'quote_service_departure':
        session['quote_service'] = '送機'
        session['step'] = 'quote_airport'
        user_sessions[user_id] = session
        send_quote_airport_menu(event.reply_token)

    elif data == 'quote_service_arrival':
        session['quote_service'] = '接機'
        session['step'] = 'quote_airport'
        user_sessions[user_id] = session
        send_quote_airport_menu(event.reply_token)

    elif data.startswith('quote_airport_'):
        a_id = data.replace('quote_airport_', '')
        apt = AirportOption.query.get(int(a_id))
        session['quote_airport'] = apt.name if apt else ''
        session['step'] = 'quote_pickup'
        user_sessions[user_id] = session
        svc = session.get('quote_service', '')
        if svc == '送機':
            reply_text(event.reply_token, '請輸入出發地址（至少含縣市及鄉鎮區市）：\n例：台中市西區、彰化縣員林市')
        else:
            reply_text(event.reply_token, '請輸入目的地址（至少含縣市及鄉鎮區市）：\n例：台中市西區、彰化縣員林市')

    elif data == 'quote_stop_yes':
        session['step'] = 'quote_stop_input'
        session.setdefault('quote_stops', [])
        user_sessions[user_id] = session
        reply_text(event.reply_token, f'請輸入第 {len(session["quote_stops"]) + 1} 個停靠點地址：')

    elif data == 'quote_stop_no':
        session['step'] = 'quote_date'
        user_sessions[user_id] = session
        reply_text(event.reply_token, f'請輸入預計日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：')

    elif data == 'quote_stop_more':
        session['step'] = 'quote_stop_input'
        user_sessions[user_id] = session
        reply_text(event.reply_token, f'請輸入第 {len(session.get("quote_stops", [])) + 1} 個停靠點地址：')

    elif data == 'quote_stop_done':
        session['step'] = 'quote_date'
        user_sessions[user_id] = session
        reply_text(event.reply_token, f'請輸入預計日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：')
        
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
            '例：/ABC1234'
        )

    elif data == 'invoice_company':
        session['invoice_type'] = 'company'
        session['step'] = 'input_tax_id'
        user_sessions[user_id] = session
        reply_text(event.reply_token, '請輸入公司統一編號（8碼數字）：')

    elif data == 'invoice_donate':
        # 捐贈發票（愛心碼）→ 直接跳確認
        session['invoice_type'] = 'donate'
        session['invoice_carrier'] = ''
        session['step'] = 'confirm'
        user_sessions[user_id] = session
        send_order_confirm(event.reply_token, session)

    # invoice_none 已移除（強制開立發票）

    # ── 多點停靠 ─────────────────────────────────────────────────────
    elif data == 'no_extra_stops':
        if not session.get('pickup') or not session.get('service'):
            reply_text(event.reply_token, '預約資料已逾時，請重新點選「開始預約」。')
            user_sessions.pop(user_id, None)
            return
        # 所有地址確認完 → 標記需要推車程，問日期
        session['step'] = 'input_date'
        session['_push_travel_on_date'] = True   # 收到日期後再 push 車程
        user_sessions[user_id] = session
        reply_text(event.reply_token, f'請輸入接送日期（格式：{datetime.now().strftime("%Y-%m-%d")}）：')

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



def _build_flight_bubble(flight_number, finfo):
    """建立航班確認 Flex Bubble（供 push 和 reply 共用）"""

    # 出發地
    dep_iata = finfo.get('dep_iata', '') or ''
    dep_name = finfo.get('dep_airport', dep_iata or '未知')
    dep_label = f"{dep_name}（{dep_iata}）" if dep_iata and dep_iata not in dep_name else dep_name
    if finfo.get('dep_terminal'): dep_label += f"  T{finfo['dep_terminal']}"
    if finfo.get('dep_gate'):     dep_label += f"  Gate {finfo['dep_gate']}"

    # 抵達地
    arr_iata = finfo.get('arr_iata', '') or ''
    arr_name = finfo.get('arr_airport', arr_iata or '未知')
    arr_label = f"{arr_name}（{arr_iata}）" if arr_iata and arr_iata not in arr_name else arr_name
    if finfo.get('arr_terminal'): arr_label += f"  T{finfo['arr_terminal']}"
    if finfo.get('arr_gate'):     arr_label += f"  Gate {finfo['arr_gate']}"

    # 時間（標註「以實際當天為準」）
    dep_time = finfo.get('dep_scheduled') or finfo.get('dep_estimated') or ''
    arr_time = finfo.get('arr_scheduled') or finfo.get('arr_estimated') or ''

    # 只取 HH:MM 部分顯示（支援 ISO 8601: 2026-03-14T04:00:00.000 和 空格格式）
    def hhmm(t):
        if not t: return '未提供'
        try:
            # ISO 格式：2026-03-14T04:00:00.000
            if 'T' in t:
                return t.split('T')[1][:5]
            # fmt_time 處理後格式：03/14（週六）04:00 → 取最後 HH:MM
            import re as _re
            m = _re.search(r'(\d{2}:\d{2})$', t.strip())
            if m:
                return m.group(1)
            # 純時間：04:00
            if len(t) <= 5:
                return t
            return t[11:16] if len(t) > 10 else t
        except Exception:
            return t

    body = [
        make_info_row("出發機場", dep_label),
        make_info_row("預計起飛", hhmm(dep_time)),
        {"type": "separator", "margin": "sm"},
        make_info_row("抵達機場", arr_label),
        make_info_row("預計抵達", hhmm(arr_time)),
        {"type": "separator", "margin": "sm"},
        {"type": "text",
         "text": "⚠️ 以上時間為班表參考，實際時間以航空公司當天公告為準",
         "size": "xs", "color": "#A0AEC0", "wrap": True, "margin": "sm"},
        {"type": "text",
         "text": "請確認此為您的航班路線是否正確？",
         "margin": "md", "weight": "bold", "color": "#E05C00", "size": "sm", "wrap": True},
    ]

    airline_str = finfo.get('airline', '') or ''
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1A2B4A",
            "contents": [
                {"type": "text", "text": "航班路線確認",
                 "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text",
                 "text": f"{airline_str}  {flight_number}" if airline_str else flight_number,
                 "color": "#8BA3C7", "size": "sm", "wrap": True},
            ]
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
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


def _push_flight_confirm(user_id, flight_number, finfo):
    """用 push_message 傳送航班確認卡片（背景執行緒用）"""
    try:
        bubble = _build_flight_bubble(flight_number, finfo)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[FlexMessage(
                        alt_text=f'航班資訊 {flight_number}',
                        contents=FlexContainer.from_dict(bubble)
                    )]
                )
            )
    except Exception as e:
        app.logger.error(f'_push_flight_confirm error: {e}')


def _push_child_seat_menu(user_id):
    """用 push_message 傳送兒童安全座椅選單（背景執行緒用）"""
    try:
        buttons = [make_button(name, f"child_seat_{key}") for key, name in CHILD_SEATS.items()]
        bubble = {
            "type": "bubble",
            "header": header_box("兒童安全座椅"),
            "body": {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "是否需要兒童安全座椅？",
                 "size": "md", "color": "#333333", "wrap": True},
                {"type": "text",
                 "text": "每座加收 NT$200，每車最多 2 座，超過請聯繫客服",
                 "size": "xs", "color": "#E05C00", "margin": "sm", "wrap": True},
            ] + buttons}
        }
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[FlexMessage(
                        alt_text='兒童安全座椅',
                        contents=FlexContainer.from_dict(bubble)
                    )]
                )
            )
    except Exception as e:
        app.logger.error(f'_push_child_seat_menu error: {e}')


def send_flight_confirm(reply_token, flight_number, finfo):
    """顯示查到的航班資訊，讓客人確認（reply 版）"""
    try:
        bubble = _build_flight_bubble(flight_number, finfo)
        send_flex(reply_token, f'航班資訊 {flight_number}', bubble)
    except Exception as e:
        app.logger.error(f'send_flight_confirm error: {e}')
        reply_text(reply_token, f'航班 {flight_number} 資訊載入失敗，請稍後再試。')


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
    
def send_8th_guest_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("第八位貴賓"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否有第八位貴賓同行？", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
            {"type": "text", "text": "第八位乘客加收 NT$400，含司機共 9 人為上限。", "size": "xs", "color": "#888888", "margin": "sm", "wrap": True},
            make_button("是，加收 NT$400", "guest_8th_yes", "primary"),
            make_button("否，繼續", "guest_8th_no"),
        ]}
    }
    send_flex(reply_token, '第八位貴賓', bubble)

def send_quote_service_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("快速報價", "#2B6CB0"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇服務類型", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
            make_button("送機（出境）", "quote_service_departure", "primary"),
            make_button("接機（回國）", "quote_service_arrival"),
        ]}
    }
    send_flex(reply_token, '快速報價', bubble)


def send_quote_airport_menu(reply_token):
    airports = get_airports()
    buttons = [make_button(a.name, f"quote_airport_{a.id}") for a in airports]
    bubble = {
        "type": "bubble",
        "header": header_box("快速報價", "#2B6CB0"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇機場", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
        ] + buttons}
    }
    send_flex(reply_token, '選擇機場', bubble)


def send_quote_stop_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("快速報價", "#2B6CB0"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "是否有中途停靠點？", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
            {"type": "text", "text": "停靠費用依距離計算：5公里內 +NT$200、12公里內 +NT$300、18公里內 +NT$400、超過 +NT$500", "size": "xs", "color": "#888888", "margin": "sm", "wrap": True},
            make_button("有停靠點", "quote_stop_yes"),
            make_button("沒有，直接報價", "quote_stop_no", "primary"),
        ]}
    }
    send_flex(reply_token, '是否有停靠點', bubble)


def send_quote_stop_more_menu(reply_token):
    bubble = {
        "type": "bubble",
        "header": header_box("快速報價", "#2B6CB0"),
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "還有其他停靠點嗎？", "size": "md", "color": "#333333", "weight": "bold", "wrap": True},
            make_button("繼續新增停靠點", "quote_stop_more"),
            make_button("完成，直接報價", "quote_stop_done", "primary"),
        ]}
    }
    send_flex(reply_token, '繼續新增停靠點', bubble)


def _show_quote_result(reply_token, session, user_id):
    """計算並顯示報價結果"""
    class FakeOrder:
        pass
    o = FakeOrder()
    o.airport         = session.get('quote_airport', '')
    o.pickup_location = session.get('quote_pickup', '')
    o.night_fee       = False
    o.sign_board      = False
    o.child_seat_count = 0
    o.pet             = False
    o.booking_date    = session.get('quote_date', '')
    o.extra_stop_fee  = 0

    quote = calculate_quote(o)

    # 多點加收
    stops = session.get('quote_stops', [])
    stop_fee = session.get('quote_stop_fee', 0)
    if stops and stop_fee:
        quote['breakdown'].append({'label': f'多點停靠加收（{len(stops)} 點）', 'amount': stop_fee})
        quote['total'] += stop_fee

    # 組合報價卡片
    svc = session.get('quote_service', '')
    airport = session.get('quote_airport', '')
    pickup = session.get('quote_pickup', '')
    date = session.get('quote_date', '')

    rows = []
    rows.append(make_info_row("服務", svc))
    rows.append(make_info_row("機場", airport))
    rows.append(make_info_row("地址", pickup))
    if stops:
        for i, s in enumerate(stops, 1):
            rows.append(make_info_row(f"停靠點 {i}", s))
    rows.append(make_info_row("日期", date))
    rows.append({"type": "separator", "margin": "md"})

    if quote['base_price'] == 0:
        rows.append({
            "type": "text",
            "text": "此區域尚未設定報價，請聯繫客服為您報價，謝謝！",
            "size": "sm", "color": "#E05C00", "wrap": True, "margin": "md"
        })
    else:
        for item in quote['breakdown']:
            rows.append(make_info_row(item['label'], f"NT${item['amount']:,}"))
        rows.append({"type": "separator", "margin": "sm"})
        rows.append({
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": "預估總費用", "weight": "bold", "flex": 3, "size": "sm", "wrap": True},
                {"type": "text", "text": f"NT${quote['total']:,}", "weight": "bold",
                 "flex": 5, "color": "#E05C00", "size": "lg", "align": "end", "wrap": True}
            ]
        })
        rows.append({
            "type": "text",
            "text": "以上為預估報價（不含舉牌、兒童座椅、寵物等加購），實際費用以預約時系統計算為準。",
            "size": "xs", "color": "#A0AEC0", "margin": "md", "wrap": True
        })

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#2B6CB0",
            "contents": [
                {"type": "text", "text": "快速報價結果", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
                {"type": "text", "text": f"{svc}　{date}", "color": "#BEE3F8", "size": "sm", "wrap": True}
            ]
        },
        "body": {"type": "box", "layout": "vertical", "contents": rows}
    }

    send_flex(reply_token, '快速報價結果', bubble)

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
                 "text": "定金 NT$315 將依法開立電子發票，請選擇收取方式：",
                 "size": "sm", "color": "#555555", "wrap": True, "margin": "sm"},
                {"type": "separator", "margin": "md"},
                make_button("個人載具（手機條碼）", "invoice_personal"),
                make_button("公司抬頭（統一編號）", "invoice_company"),
                make_button("捐贈發票（家扶基金會 愛心碼 024）", "invoice_donate"),
            ]
        }
    }
    send_flex(reply_token, '電子發票', bubble)

# ── 機場名稱 → 地址對照表（供 Google Maps 使用）──
AIRPORT_ADDRESS_MAP = {
    '桃園機場第一航廈': '桃園國際機場第一航廈, 大園區, 桃園市',
    '桃園機場第二航廈': '桃園國際機場第二航廈, 大園區, 桃園市',
    '松山機場':         '台北松山機場, 敦化北路, 台北市',
    '台中清泉崗機場':   '台中國際機場, 清水區, 台中市',
    '高雄小港機場':     '高雄國際機場, 小港區, 高雄市',
    '基隆港':           '基隆港, 中正區, 基隆市',
}

# ── 新功能 2：預估車程（push，不佔 reply_token）──────────────────────
def _reply_time_hint(reply_token, session):
    """用 reply_token 傳送時間輸入提示"""
    svc = session.get('service', '')
    if svc == 'arrival':
        hint = (
            '請輸入航班預計抵達時間（24小時制，格式：08:30）：\n\n'
            '例：上午8點半 → 08:30\n'
            '    下午3點   → 15:00\n'
            '    晚上11點  → 23:00\n\n'
            '接機說明：\n'
            '我們以航班實際落地時間為主，\n'
            '於航班落地後等待最多 90 分鐘。\n\n'
            '如是接機服務請直接填寫預計抵達時間。'
        )
    else:
        hint = (
            '請輸入從府上出發時間（24小時制，格式：08:30）：\n\n'
            '例：上午8點半 → 08:30\n'
            '    下午3點   → 15:00\n'
            '    晚上11點  → 23:00\n\n'
            '送機建議：\n'
            '建議航班起飛前 3 小時抵達機場，\n'
            '請依此預估您的出發時間。'
        )
    reply_text(reply_token, hint)


def _push_time_hint(user_id, session):
    """用 push_message 傳送時間輸入提示（背景執行緒用）"""
    svc = session.get('service', '')
    if svc == 'arrival':
        hint = (
            '請輸入航班預計抵達時間（24小時制，格式：08:30）：\n\n'
            '例：上午8點半 → 08:30\n'
            '    下午3點   → 15:00\n'
            '    晚上11點  → 23:00\n\n'
            '接機說明：\n'
            '我們以航班實際落地時間為主，\n'
            '於航班落地後等待最多 90 分鐘。\n\n'
            '如是接機服務請直接填寫預計抵達時間。'
        )
    else:
        hint = (
            '請輸入從府上出發時間（24小時制，格式：08:30）：\n\n'
            '例：上午8點半 → 08:30\n'
            '    下午3點   → 15:00\n'
            '    晚上11點  → 23:00\n\n'
            '送機建議：\n'
            '建議航班起飛前 3 小時抵達機場，\n'
            '請依此預估您的出發時間。'
        )
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=hint)]
                )
            )
    except Exception as e:
        app.logger.error(f'_push_time_hint error: {e}')


def _push_est_travel(user_id, session):
    """呼叫 Google Maps 預估機場→最終目的地車程（含所有停靠點），用 push_message 傳給客人"""
    try:
        airport_name = session.get('airport', '')
        pickup       = session.get('pickup', '')
        extra_stops  = session.get('extra_stops', [])

        # 終點：若有停靠點，以最後一個停靠點為終點（與報價邏輯一致）
        final_dest   = extra_stops[-1] if extra_stops else pickup
        # 顯示用標籤
        dest_label   = final_dest

        app.logger.info(f'_push_est_travel: airport={airport_name!r}, final_dest={final_dest!r}, key={bool(GOOGLE_MAPS_API_KEY)}')
        if not airport_name or not final_dest or not GOOGLE_MAPS_API_KEY:
            app.logger.warning(f'_push_est_travel: 缺少必要參數，略過')
            return
        airport_addr = AIRPORT_ADDRESS_MAP.get(airport_name, airport_name)

        # waypoints：途經各停靠點（除最後一點）
        waypoints = [pickup] + extra_stops[:-1] if extra_stops else []

        if waypoints:
            # 用 Directions API 算含途經點的路線
            params = {
                'origin':      airport_addr,
                'destination': final_dest,
                'waypoints':   '|'.join(waypoints),
                'key':         GOOGLE_MAPS_API_KEY,
                'language':    'zh-TW',
                'region':      'tw',
                'mode':        'driving',
            }
            resp = requests.get(
                'https://maps.googleapis.com/maps/api/directions/json',
                params=params, timeout=8
            )
            result = resp.json()
            app.logger.info(f'_push_est_travel Directions result status: {result.get("status")}')
            if result.get('status') == 'OK':
                legs = result['routes'][0]['legs']
                total_dist = sum(l['distance']['value'] for l in legs)
                total_dur  = sum(l['duration']['value'] for l in legs)
                dist_text = f'{total_dist/1000:.1f} 公里'
                hrs, mins = divmod(total_dur // 60, 60)
                dur_text  = f'{hrs} 小時 {mins} 分鐘' if hrs else f'{mins} 分鐘'
                stops_info = f'，途經 {len(waypoints)} 個停靠點' if waypoints else ''
                msg = f'預估車程（{airport_name} → {dest_label}{stops_info}）\n距離：{dist_text}\n行車時間：{dur_text}'
            else:
                return
        else:
            params = {
                'origins':      airport_addr,
                'destinations': final_dest,
                'key':          GOOGLE_MAPS_API_KEY,
                'language':     'zh-TW',
                'region':       'tw',
                'mode':         'driving',
            }
            resp = requests.get(
                'https://maps.googleapis.com/maps/api/distancematrix/json',
                params=params, timeout=8
            )
            result = resp.json()
            app.logger.info(f'_push_est_travel Google Maps result: {result}')
            element = result['rows'][0]['elements'][0]
            if element.get('status') != 'OK':
                app.logger.warning(f'_push_est_travel: element status={element.get("status")}，略過')
                return
            dist_text = element['distance']['text']
            dur_text  = element['duration']['text']
            msg = f'預估車程（{airport_name} → {dest_label}）\n距離：{dist_text}\n行車時間：{dur_text}'
        line_user_id = session.get('_line_user_id', user_id)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=line_user_id,
                    messages=[TextMessage(text=msg)]
                )
            )
        app.logger.info(f'_push_est_travel: 推送成功 → {line_user_id}')
    except Exception as e:
        app.logger.warning(f'_push_est_travel error: {e}', exc_info=True)

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
- 夜間費（22:00–06:00）：目前不指定車款優惠活動不加收夜間費用
- 舉牌服務：+NT$300
- 兒童安全座椅：+NT$200 / 張（最多2張）
- 寵物同行：+NT$300（必須裝籠，行車中不可放出）
- 假日/旺季期間：+NT$300
- 多點停靠：+NT$200（5公里內）/ +NT$300（12公里內）/ +NT$400（18公里內）/ +NT$500（18公里以上）
- 四至七天內預約：+NT$300
- 二至三天內預約：+NT$600
- 當天及前一天：請聯繫真人客服

【車型說明】
我們採「不指定車款」優惠方案，一切依本公司調度派遣為主，以下為參考車款（全部為無菸車）：

五座轎車類（最多 3 人 3 件）：
- Lexus ES、Mercedes-Benz E-Class、Tesla Model S

五到七座車款（最多 4 人 4 件）：
- Toyota RAV4、Luxgen N7、Mercedes-Benz EQB、Tesla Model Y、Tesla Model X
- Toyota Sienna、Toyota Alphard、Lexus LM

九人座車款（最多 7 人 7 件）：
- KIA Carnival、Toyota Granvia、Volkswagen Caravelle T6、Hyundai Staria
- Mercedes-Benz V-Class、Volkswagen Crafter、Mercedes-Benz Sprinter

若被問到「不指定車款有哪些」，請列出以上車款並說明一切以本公司調度為主。

【人數與行李超載說明】
- 不指定車款優惠活動僅限於「最多」7 人 7 件標準 30 吋（含推車）。
- 若客人詢問人數、行李、幾個人可以坐等問題，一律回答：「不指定車款優惠活動僅限於「最多」七人七件標準30吋（含推車），如需進一步預約或報價，請輸入「預約」或「報價」，謝謝您！」

【公司資訊】
- 公司名稱：樂高小客車租賃有限公司（Le Gao Car Rental Co., Ltd.）
- 統一編號：50978670
- 汽車運輸業營業執照：交營字第40-0032736號
- 新北市小客車租賃商業同業公會：新北小車證字第189號
- FB：Taiwan Top Service（有認證）
- 官方 LINE：Taiwan Top Service（藍色盾牌，有認證）
- LINE ID：@taiwantop
- 若客人詢問公司名稱、執照、統編、社群帳號等資訊，請如實回答以上內容。

【接機流程（客人詢問時請回覆以下內容）】
我們流程是這樣，您落地 20 分鐘左右時，司機會與您聯繫，請您保持手機暢通，待您拿到行李後打給司機，司機會跟您約見面點，謝謝您。

【常見問題 FAQ】

Q1 機場接送要多久前預約？
A：建議最晚兩周前先預約。線上預約系統僅開放 8 天後以上的日期，7 天內請直接聯繫真人客服接單。溫馨提醒：7 天內加收 NT$300，3 天內再加收 NT$300（合計 NT$600）。

Q2 如果航班延誤怎麼辦？
A：我們接機是依照航班實際落地為主等待 90 分鐘，不用擔心航班有提早或延誤問題。除非耽誤超過兩個小時，超過兩小時（第三小時起算）會加收 NT$300／每小時等待費用。

Q3 半夜或清晨也可以叫車嗎？
A：我們客服跟調度人員都是 24 小時服務，隨時可以跟我們叫車。

Q4 車款有哪些？／不指定車款有哪些？
A：以下為不指定車款優惠活動參考車款，全部為無菸車，一切依照本公司調度派遣為主：

五座轎車類（最多 3 人 3 件）：Lexus ES、Mercedes-Benz E-Class、Tesla Model S

五到七座車款（最多 4 人 4 件）：Toyota RAV4、Luxgen N7、Mercedes-Benz EQB、Tesla Model Y、Tesla Model X、Toyota Sienna、Toyota Alphard、Lexus LM

九人座車款（最多 7 人 7 件）：KIA Carnival、Toyota Granvia、Volkswagen Caravelle T6、Hyundai Staria、Mercedes-Benz V-Class、Volkswagen Crafter、Mercedes-Benz Sprinter

Q4b 車型可以選擇嗎？
A：可以指定車型，您提供給我人數跟行李件數，好讓我報指定車款可以乘載的車款報價給您。

Q5 行李有數量限制嗎？人數上限是幾人？
A：不指定車款優惠活動僅限於「最多」七人七件標準 30 吋（含推車）。如需進一步預約或報價，請輸入「預約」或「報價」，讓我幫您進入下一個流程，謝謝您！

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

Q12 多點停靠費用怎麼算？
A：多點報價如下，每增加一個停靠點依距離加收：5公里內 +NT$200、12公里內 +NT$300、18公里內 +NT$400、18公里以上 +NT$500。

Q13 接機的時候司機在哪裡等？怎麼聯絡？
A：我們流程是這樣，您落地 20 分鐘左右時，司機會與您聯繫，請您保持手機暢通，待您拿到行李後打給司機，司機會跟您約見面點，謝謝您。

Q14 你們公司資訊是什麼？FB 或 LINE 在哪？
A：我們是樂高小客車租賃有限公司，統編 50978670，持有汽車運輸業營業執照（交營字第40-0032736號）。FB 和官方 LINE 搜尋「Taiwan Top Service」，有藍色盾牌認證，LINE ID：@taiwantop。

Q15 你們是白牌嗎？合法嗎？
A：我們是合法合規的租賃小客車公司經營，持有汽車運輸業營業執照及新北市小客車租賃商業同業公會認證，請放心搭乘。

Q16 你們有保險嗎？有乘客險嗎？
A：我們是合法合規租賃小客車公司，每台車都有投保乘客險，每人保額 500 萬元以上，請放心。

Q17 什麼時候會給司機資料？
A：預約完成後，我們會在您出發日期的兩天前，將司機資料傳送給您，請留意 LINE 通知，謝謝您！

Q18 是出門當天再匯款嗎？
A：您好！需要先完成支付定金，這樣才能確保您的預約順利成立。如有特殊情況，歡迎直接聯繫我們的客服處理，謝謝！

Q19 是否有夜間收費？
A：目前不指定車款優惠活動不加收夜間費用，請放心預約！

Q20 請問有收據或預約完成證明嗎？
A：我們無法提供收據，如需開立發票會加收 5% 費用，謝謝您！

Q21 當天價格還會異動嗎？
A：現在預約完成，依照上述費用就不會再有變動，請放心！謝謝您。

Q22 如何分享給朋友或家人預約？
A：您好！只要分享我們的官方 LINE 帳號給他們，就可以直接預約囉！

LINE ID：@taiwantop
或點此連結加入：https://line.me/R/ti/p/@taiwantop

加入後直接說「預約」就可以開始了，謝謝您！

Q23 我們是拼車嗎？
A：我們是合法合規小客車租賃公司經營，不做拼車行為，我們都是專車服務，不會有拼車問題，謝謝您！如有需要預約可以輸入「預約」，或者想先取得快速報價可以輸入「報價」。

Q24 是專車嗎？
A：是的，每一次服務都是專車服務。如有需要預約可以輸入「預約」，或者想先取得快速報價可以輸入「報價」。

Q25 是包車嗎？
A：我們完全都是專車服務，不會有任何拼車問題。如有需要預約可以輸入「預約」，或者想先取得快速報價可以輸入「報價」。

Q26 我要指定車款
A：需要指定車款或者商務包車，請輸入「真人客服」，稍後會有專人為您服務。

Q27 這是一個人的報價嗎？
A：我們是以車為單位報價，不是以人數計算，一位到七位都是一樣的價錢。如有需要預約可以輸入「預約」，或者想先取得快速報價請輸入「報價」哦！

【回覆原則】
- 全程使用繁體中文
- 回覆要簡潔口語，不要太正式或太長
- 任何涉及人數、行李件數的問題，一律回答「不指定車款優惠活動僅限於「最多」七人七件標準30吋（含推車）」，並引導輸入「預約」或「報價」
- 若客人問具體訂單狀態，請他輸入「查詢訂單」由系統查詢
- 若客人要預約，請他輸入「預約」進入預約流程
- 不確定的資訊不要亂猜，誠實說不確定並建議聯繫客服
- 不要自行計算或判斷七天內加收費用，一律請客人輸入「預約」進入系統，由系統自動計算所有費用
- 若客人提供預約資料詢問費用，不要幫客人整理確認單或計算加收費用，直接請他輸入「預約」讓系統處理
- 若客人詢問任何地區或路線的費用、車資、多少錢等問題，不要直接報價，請引導他輸入「報價」使用快速報價系統取得準確報價
- 若客人提到異動、更改、取消、退款、找真人、真人服務等，一律回答：「請輸入『真人客服』，稍後會有真人客服人員來服務您，請稍候！」
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
                {"type": "button", "action": {"type": "postback", "label": "我要報價", "data": "start_quote"},
                "style": "secondary", "color": "#2B6CB0"},
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
            {"type": "text", "text": "注意：系統將以最後一個停靠點為終點重新計算車資，確保報價公平合理。", "size": "sm", "color": "#E05C00", "margin": "sm", "wrap": True},
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
    o.pickup_location  = session.get('pickup', '')  # 永遠用出發地比對報價
    o.night_fee        = session.get('night_fee', False)
    o.sign_board       = session.get('sign_board', False)
    o.child_seat_count = session.get('child_seat_count', 0)
    o.pet              = session.get('pet', False)
    o.booking_date     = session.get('date', '')
    o.extra_stop_fee   = session.get('extra_stop_fee', 0)
    o._8th_guest_fee   = session.get('8th_guest_fee', 0)

    extra_stops = session.get('extra_stops', [])

    quote = calculate_quote(o)

    # 多點停靠加收
    if extra_stops and o.extra_stop_fee:
        quote['breakdown'].append({'label': f'多點停靠加收（{len(extra_stops)} 點）', 'amount': o.extra_stop_fee})
        quote['surcharges'].append({'label': '多點停靠加收', 'amount': o.extra_stop_fee})
        quote['total'] += o.extra_stop_fee
    elif o.extra_stop_fee:
        quote['breakdown'].append({'label': '多點加收', 'amount': o.extra_stop_fee})
        quote['surcharges'].append({'label': '多點加收', 'amount': o.extra_stop_fee})
        quote['total'] += o.extra_stop_fee

    # 第八位貴賓加收
    if getattr(o, '_8th_guest_fee', 0):
        quote['surcharges'].append({'label': '第八位貴賓加收', 'amount': o._8th_guest_fee})
        quote['breakdown'].append({'label': '第八位貴賓加收', 'amount': o._8th_guest_fee})
        quote['total'] += o._8th_guest_fee

    return quote

def send_order_confirm(reply_token, session):
    extras = []
    if session.get('night_fee'): extras.append('夜間服務費')
    if session.get('sign_board'): extras.append('舉牌服務')
    if session.get('child_seat_count', 0):
        extras.append(f'兒童安全座椅 x{session["child_seat_count"]}')
    if session.get('pet'): extras.append('寵物同行')
    if session.get('8th_guest'): extras.append('第八位貴賓（+NT$400）')
    extra_stops = session.get('extra_stops', [])

    quote = build_quote_from_session(session)
    # 把報價總額存入 session，讓 save_order 可以帶入 total_price
    session['_quoted_total'] = quote.get('total', 0)
    user_sessions_ref = None
    try:
        from flask import current_app
        # 更新 session 中的報價
        if hasattr(current_app, '_get_current_object'):
            pass
    except Exception:
        pass

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
    elif inv_type == 'donate':
        inv_text = "捐贈發票（愛心碼）"
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

    # ── 尾款金額：總價 - 定金未稅金額(NT$300) ──
    # 定金 NT$315 含稅，未稅本金 NT$300，尾款以未稅計算交付司機現金
    DEPOSIT_PRETAX = 300
    balance = max(0, quote['total'] - DEPOSIT_PRETAX)

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
             "text": f"尾款 NT${balance:,} 元請交付現金給司機",
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
    elif inv_type == 'donate':
        inv_note = "【發票】捐贈發票（愛心碼）"
    elif inv_type == 'personal' and session.get('invoice_carrier'):
        inv_note = f"【發票】手機載具：{session.get('invoice_carrier','')}"
    elif inv_type == 'personal':
        inv_note = "【發票】個人雲端發票"
    else:
        inv_note = ""

    base_note = session.get('note', '') or ''
    if session.get('8th_guest'):
        guest8_note = '【第八位貴賓】加收 NT$400'
        base_note = (base_note + '\n' + guest8_note).strip() if base_note else guest8_note
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
            status='待付款',
            total_price=session.get('_quoted_total', 0),
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
        "【送機說明】\n"
        "• 送機以預約時間為主，等待超過 16 分鐘起，加收 NT$800 元／每小時。\n\n"
        "【接機說明】\n"
        "• 接機以航班實際落地時間為準，等待 90 分鐘。\n"
        "• 超過 91 分鐘起，加收 NT$800 元／每小時。\n"
        "• 如航班延誤超過兩小時，第三個小時起加收 NT$300 元／每小時（依機場航班動態為準）。\n"
        "• 取好行李後請主動聯繫司機，司機將告知見面地點與車牌。\n"
        "• 若等候超過 90 分鐘未能聯繫，預約將自動取消並離開現場。\n\n"
        "【行李說明】\n"
        "• 超過 30 吋或大型行李箱、胖胖箱等非標準行李，請事先告知。\n"
        "• 行李定義：行李箱、嬰兒車、登機箱、警衛包等占用後車廂空間之物件。\n"
        "• 若到場後人數及行李載不下時，司機有權拒絕載送，並不退費。\n\n"
        "【異動與取消】\n"
        "• 任何異動（包含行李件數）請於八天前告知。\n"
        "• 七天內任何理由均無法異動或取消，定金恕不退還。\n\n"
        "【保險】\n"
        "• 所有車輛均投保乘客險每人 500 萬元以上。\n\n"
        "【特別提醒】\n"
        "• 本公司車輛皆為合法合規營運，請配合服務流程。\n"
        "• 若無法聯繫上司機與客服，請勿自行搭乘他車，我們無法對非本公司安排行為負責。\n\n"
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
    import json as _json
    status_map = {
        '已確認': '預約成功',
        '待確認': '待確認',
        '待付款': '待付款',
        '已完成': '已完成',
        '已取消': '已取消',
        '搶單中': '搶單中',
    }
    bubbles = []
    for order in orders:
        display_status = status_map.get(order.status, order.status)
        contents = [
            make_info_row("狀態", display_status),
            make_info_row("服務", order.service_name),
            make_info_row("車型", order.vehicle),
            make_info_row("機場", order.airport),
            make_info_row("日期", order.booking_date),
            make_info_row("時間", order.booking_time),
            make_info_row("地點", order.pickup_location),
        ]
        try:
            stops = _json.loads(order.extra_stops or '[]')
            for i, stop in enumerate(stops, 1):
                contents.append(make_info_row(f"停靠點 {i}", stop))
            if stops and order.extra_stop_fee:
                contents.append(make_info_row("多點加收", f"NT${order.extra_stop_fee:,}"))
        except Exception:
            pass
        if order.total_price:
            contents.append({"type": "separator", "margin": "sm"})
            contents.append(make_info_row("總金額", f"NT${order.total_price:,}"))

        bubbles.append({
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A9B8F", "contents": [
                {"type": "text", "text": f"訂單 #{order.id}", "color": "#FFFFFF", "size": "lg", "weight": "bold", "wrap": True},
                {"type": "text", "text": order.created_at.strftime('%Y-%m-%d %H:%M'), "color": "#DDDDDD", "size": "sm", "wrap": True}
            ]},
            "body": {"type": "box", "layout": "vertical", "contents": contents}
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
                    make_info_row("姓名", (order.name[0] + "O" + order.name[-1]) if order.name and len(order.name) >= 2 else order.name),
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
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": "費用結算", "weight": "bold", "color": "#1A2B4A", "margin": "sm"},
                    {"type": "separator", "margin": "sm"},
                    *(
                        [
                            make_info_row("向客人收取", f"NT${order.total_price:,}" if order.total_price else "依報價單"),
                            make_info_row("司機車資", f"NT${job.driver_fee:,}"),
                        ] if job.driver_fee else [
                            make_info_row("車資", "請洽調度確認"),
                        ]
                    ),
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