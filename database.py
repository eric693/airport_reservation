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
        if self.pet: total += 1100
        return total


class Driver(db.Model):
    __tablename__ = 'drivers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    car_brand = db.Column(db.String(50), default='')    # 車輛品牌，例如：Toyota Camry
    car_plate = db.Column(db.String(20), default='')    # 車牌號碼
    car_color = db.Column(db.String(20), default='')    # 車身顏色
    note = db.Column(db.Text, default='')
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)