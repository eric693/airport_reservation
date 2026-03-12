import os
import json
import threading
import time
import requests
from datetime import datetime
from flask import Flask, request, abort, render_template, redirect, url_for, flash, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, FlexMessage,
    FlexContainer, PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from linebot.v3.messaging.models import (
    FlexBubble, FlexBox, FlexText, FlexButton, FlexSeparator,
    URIAction, PostbackAction, MessageAction,
    QuickReply, QuickReplyItem
)
from database import db, Order, init_db
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///airport.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# User session storage (in-memory for flow tracking)
user_sessions = {}

VEHICLE_TYPES = {
    '1': '標準國產四座轎車',
    '2': '商務六座廂型車',
    '3': '豪華七座SUV',
    '4': '九座廂型車',
}

AIRPORTS = {
    'tpe1': '桃園機場第一航廈',
    'tpe2': '桃園機場第二航廈',
    'tsa': '松山機場',
}

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1234')

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return ('請輸入帳號密碼', 401, {'WWW-Authenticate': 'Basic realm="Admin"'})
        return f(*args, **kwargs)
    return decorated

# ─── Keep-alive ping ───────────────────────────────────────────────
def keep_alive():
    url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if url:
        while True:
            try:
                requests.get(f"{url}/ping", timeout=10)
            except Exception:
                pass
            time.sleep(840)  # ping every 14 minutes

# ─── LINE Bot webhook ───────────────────────────────────────────────
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

# ─── Admin routes ───────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_index():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin/index.html', orders=orders)

@app.route('/admin/order/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin/order_detail.html', order=order)

@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
@admin_required
def admin_update_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    order.status = new_status
    db.session.commit()
    flash('訂單狀態已更新')
    return redirect(url_for('admin_order_detail', order_id=order_id))

@app.route('/admin/order/<int:order_id>/delete', methods=['POST'])
@admin_required
def admin_delete_order(order_id):
    order = Order.query.get_or_404(order_id)
    db.session.delete(order)
    db.session.commit()
    flash('訂單已刪除')
    return redirect(url_for('admin_index'))

@app.route('/admin/api/stats')
@admin_required
def admin_stats():
    total = Order.query.count()
    pending = Order.query.filter_by(status='待確認').count()
    confirmed = Order.query.filter_by(status='已確認').count()
    completed = Order.query.filter_by(status='已完成').count()
    cancelled = Order.query.filter_by(status='已取消').count()
    return jsonify({
        'total': total, 'pending': pending,
        'confirmed': confirmed, 'completed': completed, 'cancelled': cancelled
    })

# ─── LINE message handlers ──────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ['預約', '訂車', '機場接送', '開始']:
        user_sessions[user_id] = {'step': 'choose_service'}
        send_service_menu(event.reply_token)
    elif text == '查詢訂單':
        user_sessions[user_id] = {'step': 'query_name'}
        reply_text(event.reply_token, '請輸入您預約時留的中文姓名：')
    elif text == '取消':
        user_sessions.pop(user_id, None)
        reply_text(event.reply_token, '已取消操作。\n\n輸入「預約」開始新的預約，或輸入「查詢訂單」查詢訂單。')
    else:
        session = user_sessions.get(user_id, {})
        step = session.get('step')

        if step == 'query_name':
            session['query_name'] = text
            session['step'] = 'query_phone'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '請輸入您預約時留的手機號碼（例：0912345678）：')
        elif step == 'query_phone':
            name = session.get('query_name')
            phone = text
            orders = Order.query.filter_by(name=name, phone=phone).order_by(Order.created_at.desc()).limit(5).all()
            user_sessions.pop(user_id, None)
            if orders:
                send_order_query_result(event.reply_token, orders)
            else:
                reply_text(event.reply_token, f'查無符合資料。\n姓名：{name}\n電話：{phone}\n\n請確認資料是否正確，或輸入「預約」重新預約。')
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
                reply_text(event.reply_token, '請輸入接送時間（格式：08:30）：')
            except ValueError:
                reply_text(event.reply_token, '日期格式錯誤，請輸入正確格式，例如：2025-06-15')
        elif step == 'input_time':
            try:
                datetime.strptime(text, '%H:%M')
                session['time'] = text
                session['step'] = 'input_passengers'
                user_sessions[user_id] = session
                reply_text(event.reply_token, '請輸入乘客人數（數字）：')
            except ValueError:
                reply_text(event.reply_token, '時間格式錯誤，請輸入正確格式，例如：08:30')
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
                reply_text(event.reply_token, '請輸入您的姓名（中文）：')
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
                session['step'] = 'input_flight'
                user_sessions[user_id] = session
                reply_text(event.reply_token, '請輸入航班號碼（若無可輸入「無」）：')
            else:
                reply_text(event.reply_token, '請輸入有效的手機號碼（例：0912345678）：')
        elif step == 'input_flight':
            session['flight'] = text
            session['step'] = 'input_note'
            user_sessions[user_id] = session
            reply_text(event.reply_token, '是否有備註事項？（無備註請輸入「無」）：')
        elif step == 'input_note':
            session['note'] = text if text != '無' else ''
            session['step'] = 'confirm'
            user_sessions[user_id] = session
            send_order_confirm(event.reply_token, session)
        else:
            send_main_menu(event.reply_token)

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    session = user_sessions.get(user_id, {})

    if data == 'service_departure':
        session = {'step': 'choose_vehicle', 'service': 'departure', 'service_name': '送機(出境)'}
        user_sessions[user_id] = session
        send_vehicle_menu(event.reply_token)
    elif data == 'service_arrival':
        session = {'step': 'choose_vehicle', 'service': 'arrival', 'service_name': '接機(回國)'}
        user_sessions[user_id] = session
        send_vehicle_menu(event.reply_token)
    elif data.startswith('vehicle_'):
        v_key = data.replace('vehicle_', '')
        session['vehicle'] = VEHICLE_TYPES.get(v_key, '標準國產四座轎車')
        session['step'] = 'choose_airport'
        user_sessions[user_id] = session
        send_airport_menu(event.reply_token)
    elif data.startswith('airport_'):
        a_key = data.replace('airport_', '')
        session['airport'] = AIRPORTS.get(a_key, '桃園機場第一航廈')
        session['step'] = 'input_pickup'
        user_sessions[user_id] = session
        service = session.get('service')
        if service == 'departure':
            reply_text(event.reply_token, '請輸入接送地點（起點）：\n例：台北市信義區忠孝東路五段1號')
        else:
            reply_text(event.reply_token, '請輸入目的地（終點）：\n例：台北市信義區忠孝東路五段1號')
    elif data == 'confirm_order':
        save_order(event.reply_token, session, user_id)
        user_sessions.pop(user_id, None)
    elif data == 'cancel_order':
        user_sessions.pop(user_id, None)
        reply_text(event.reply_token, '已取消預約。\n\n輸入「預約」重新開始，或輸入「查詢訂單」查詢訂單。')

# ─── Helper functions ───────────────────────────────────────────────
def reply_text(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

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
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "機場接送預約", "color": "#FFFFFF", "size": "xl", "weight": "bold"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "請選擇服務類型", "size": "md", "color": "#333333", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "預約送機（出境）", "data": "service_departure"},
                    "style": "primary",
                    "color": "#4A9B8F",
                    "margin": "md"
                },
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "預約接機（回國）", "data": "service_arrival"},
                    "style": "secondary",
                    "margin": "md"
                }
            ]
        }
    }
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='選擇服務類型', contents=FlexContainer.from_dict(bubble))]
            )
        )

def send_vehicle_menu(reply_token):
    buttons = []
    for key, name in VEHICLE_TYPES.items():
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": name, "data": f"vehicle_{key}"},
            "style": "secondary",
            "margin": "sm"
        })
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "選擇車型", "color": "#FFFFFF", "size": "xl", "weight": "bold"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": "請選擇您需要的車型", "size": "md", "color": "#333333"}] + buttons
        }
    }
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='選擇車型', contents=FlexContainer.from_dict(bubble))]
            )
        )

def send_airport_menu(reply_token):
    buttons = []
    for key, name in AIRPORTS.items():
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": name, "data": f"airport_{key}"},
            "style": "secondary",
            "margin": "sm"
        })
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "選擇機場", "color": "#FFFFFF", "size": "xl", "weight": "bold"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": "請選擇目的機場", "size": "md", "color": "#333333"}] + buttons
        }
    }
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='選擇機場', contents=FlexContainer.from_dict(bubble))]
            )
        )

def send_order_confirm(reply_token, session):
    service = session.get('service_name', '')
    vehicle = session.get('vehicle', '')
    airport = session.get('airport', '')
    pickup = session.get('pickup', '')
    date = session.get('date', '')
    time_val = session.get('time', '')
    passengers = session.get('passengers', '')
    luggage = session.get('luggage', '')
    name = session.get('name', '')
    phone = session.get('phone', '')
    flight = session.get('flight', '')
    note = session.get('note', '')

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "確認預約資料", "color": "#FFFFFF", "size": "xl", "weight": "bold"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                make_info_row("服務類型", service),
                make_info_row("車型", vehicle),
                make_info_row("機場", airport),
                make_info_row("接送地點", pickup),
                make_info_row("日期", date),
                make_info_row("時間", time_val),
                make_info_row("乘客人數", f"{passengers} 人"),
                make_info_row("行李件數", f"{luggage} 件"),
                make_info_row("姓名", name),
                make_info_row("電話", phone),
                make_info_row("航班", flight),
                make_info_row("備註", note if note else "無"),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "以上資料是否正確？", "margin": "md", "color": "#E05C00", "weight": "bold"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "確認送出", "data": "confirm_order"},
                    "style": "primary",
                    "color": "#4A9B8F",
                    "flex": 1
                },
                {"type": "separator"},
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "取消重填", "data": "cancel_order"},
                    "style": "secondary",
                    "flex": 1
                }
            ]
        }
    }
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='確認預約資料', contents=FlexContainer.from_dict(bubble))]
            )
        )

def make_info_row(label, value):
    return {
        "type": "box",
        "layout": "horizontal",
        "margin": "sm",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#888888", "flex": 3},
            {"type": "text", "text": str(value), "size": "sm", "color": "#333333", "flex": 5, "wrap": True}
        ]
    }

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
            flight_number=session.get('flight', ''),
            note=session.get('note', ''),
            status='待確認'
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A9B8F",
            "contents": [
                {"type": "text", "text": "預約成功！", "color": "#FFFFFF", "size": "xl", "weight": "bold"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"訂單編號：#{order_id}", "size": "lg", "weight": "bold", "color": "#4A9B8F"},
                {"type": "text", "text": "我們將盡快與您確認。", "margin": "md", "wrap": True},
                {"type": "text", "text": "如需查詢訂單，請輸入「查詢訂單」。", "margin": "sm", "size": "sm", "color": "#888888", "wrap": True}
            ]
        }
    }
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='預約成功', contents=FlexContainer.from_dict(bubble))]
            )
        )

def send_order_query_result(reply_token, orders):
    bubbles = []
    for order in orders:
        status_color = {'待確認': '#E05C00', '已確認': '#4A9B8F', '已完成': '#888888', '已取消': '#CC0000'}.get(order.status, '#333333')
        bubbles.append({
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#4A9B8F",
                "contents": [
                    {"type": "text", "text": f"訂單 #{order.id}", "color": "#FFFFFF", "size": "lg", "weight": "bold"},
                    {"type": "text", "text": order.created_at.strftime('%Y-%m-%d %H:%M'), "color": "#DDDDDD", "size": "sm"}
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    make_info_row("狀態", order.status),
                    make_info_row("服務", order.service_name),
                    make_info_row("車型", order.vehicle),
                    make_info_row("機場", order.airport),
                    make_info_row("日期", order.booking_date),
                    make_info_row("時間", order.booking_time),
                ]
            }
        })
    carousel = {"type": "carousel", "contents": bubbles}
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text='訂單查詢結果', contents=FlexContainer.from_dict(carousel))]
            )
        )

if __name__ == '__main__':
    with app.app_context():
        init_db(app)
    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
