from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    line_user_id = db.Column(db.String(100), nullable=False)

    service_type = db.Column(db.String(20), nullable=False)
    service_name = db.Column(db.String(50), nullable=False)
    vehicle = db.Column(db.String(50), nullable=False)
    airport = db.Column(db.String(50), nullable=False)
    pickup_location = db.Column(db.String(200), nullable=False)
    booking_date = db.Column(db.String(20), nullable=False)
    booking_time = db.Column(db.String(10), nullable=False)
    passengers = db.Column(db.Integer, default=1)
    luggage = db.Column(db.Integer, default=0)
    name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(100), default='')
    flight_number = db.Column(db.String(20), default='')
    night_fee = db.Column(db.Boolean, default=False)
    sign_board = db.Column(db.Boolean, default=False)
    child_seat = db.Column(db.String(50), default='')
    child_seat_count = db.Column(db.Integer, default=0)
    pet = db.Column(db.Boolean, default=False)
    note = db.Column(db.Text, default='')
    extra_stops = db.Column(db.Text, default='')      # 多點地址，JSON 陣列字串
    extra_stop_fee = db.Column(db.Integer, default=0)  # 多點加收費用
    total_price = db.Column(db.Integer, default=0)        # 向客人收取總金額
    status = db.Column(db.String(20), default='待確認')

    # 司機指派
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=True)
    driver_notified = db.Column(db.Boolean, default=False)   # 是否已發送司機資料給客人
    notify_at = db.Column(db.String(10), default='')          # 幾小時前發送，例如 '2' 表示出發前2小時

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    driver = db.relationship('Driver', backref='orders')

    def extra_fees(self):
        total = 0
        if self.night_fee: total += 200
        if self.sign_board: total += 200
        if self.child_seat_count: total += self.child_seat_count * 100
        if self.pet: total += 300
        return total


class VehicleType(db.Model):
    __tablename__ = 'vehicle_types'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)        # 顯示名稱，例如：標準國產四座轎車
    capacity = db.Column(db.Integer, default=4)            # 最大乘客人數
    luggage_capacity = db.Column(db.Integer, default=2)    # 大件行李數
    note = db.Column(db.String(100), default='')           # 備註說明
    sort_order = db.Column(db.Integer, default=0)          # 排序
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AirportOption(db.Model):
    __tablename__ = 'airport_options'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)        # 顯示名稱，例如：桃園機場第一航廈
    code = db.Column(db.String(20), nullable=False)        # 代碼，例如：tpe1
    sort_order = db.Column(db.Integer, default=0)          # 排序
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Driver(db.Model):
    __tablename__ = 'drivers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    car_brand = db.Column(db.String(50), default='')    # 車輛品牌，例如：Toyota Camry
    car_plate = db.Column(db.String(20), default='')    # 車牌號碼
    car_color = db.Column(db.String(20), default='')    # 車身顏色
    note = db.Column(db.Text, default='')
    line_user_id = db.Column(db.String(100), default='')   # 司機的 LINE User ID（用於推播搶單）
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DispatchJob(db.Model):
    """搶單任務 — 一張訂單對應一個搶單任務"""
    __tablename__ = 'dispatch_jobs'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, unique=True)
    status = db.Column(db.String(20), default='開放搶單')  # 開放搶單 / 已結單 / 已取消
    grabbed_by = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=True)  # 搶到的司機
    grabbed_at = db.Column(db.DateTime, nullable=True)
    deadline = db.Column(db.DateTime, nullable=True)          # 搶單截止時間（可選）
    note = db.Column(db.String(200), default='')              # 後台備註
    driver_fee = db.Column(db.Integer, nullable=True)             # 司機出車費用（NT$）
    notify_customer = db.Column(db.Boolean, default=True)     # 搶到後自動通知客人
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    order = db.relationship('Order', backref=db.backref('dispatch_job', uselist=False))
    winner = db.relationship('Driver', foreign_keys=[grabbed_by])


class DispatchResponse(db.Model):
    """每位司機的搶單回應記錄"""
    __tablename__ = 'dispatch_responses'

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('dispatch_jobs.id'), nullable=False)
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=False)
    action = db.Column(db.String(10), default='搶單')  # 搶單 / 放棄
    responded_at = db.Column(db.DateTime, default=datetime.utcnow)

    job = db.relationship('DispatchJob', backref='responses')
    driver = db.relationship('Driver')


class PriceRule(db.Model):
    """區域基本報價規則"""
    __tablename__ = 'price_rules'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)       # 規則名稱，例如：台中-桃園機場
    airport_keyword = db.Column(db.String(50), default='') # 機場關鍵字，例如：桃園
    region_keyword = db.Column(db.String(100), default='') # 地區關鍵字（接送地點），例如：台中
    base_price = db.Column(db.Integer, default=0)          # 基本價格（送機/接機同價）
    note = db.Column(db.Text, default='')                  # 備註說明
    active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PriceSurcharge(db.Model):
    """附加費用設定（全域）"""
    __tablename__ = 'price_surcharges'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)  # 識別碼
    name = db.Column(db.String(50), nullable=False)              # 顯示名稱
    amount = db.Column(db.Integer, default=0)                    # 金額
    enabled = db.Column(db.Boolean, default=True)
    note = db.Column(db.String(100), default='')


class HolidaySurcharge(db.Model):
    """假日/旺季加價日期區間"""
    __tablename__ = 'holiday_surcharges'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), default='')   # 名稱，例如：清明連假
    date_from = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD 或 MM-DD
    date_to = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Integer, default=300)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SiteSetting(db.Model):
    """全站設定（key-value 形式）"""
    __tablename__ = 'site_settings'

    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default='')

    @classmethod
    def get(cls, key, default=''):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()