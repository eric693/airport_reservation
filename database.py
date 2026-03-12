from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    line_user_id = db.Column(db.String(100), nullable=False)
    service_type = db.Column(db.String(20), nullable=False)  # departure / arrival
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
    flight_number = db.Column(db.String(20), default='')
    note = db.Column(db.Text, default='')
    status = db.Column(db.String(20), default='待確認')  # 待確認/已確認/已完成/已取消
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'service_type': self.service_type,
            'service_name': self.service_name,
            'vehicle': self.vehicle,
            'airport': self.airport,
            'pickup_location': self.pickup_location,
            'booking_date': self.booking_date,
            'booking_time': self.booking_time,
            'passengers': self.passengers,
            'luggage': self.luggage,
            'name': self.name,
            'phone': self.phone,
            'flight_number': self.flight_number,
            'note': self.note,
            'status': self.status,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
        }

def init_db(app):
    with app.app_context():
        db.create_all()
