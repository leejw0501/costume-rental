from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, g, send_from_directory
import sqlite3
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import uuid
import secrets
import os
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import urllib.parse
from functools import wraps

BASE = Path(__file__).resolve().parent
# Railway Volume이 연결되면 RAILWAY_VOLUME_MOUNT_PATH(예: /data)를 영구 저장소로 사용합니다.
DATA_DIR = Path(os.environ.get('APP_DATA_DIR') or os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') or BASE).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'rental.db'
UPLOAD_DIR = DATA_DIR / 'uploads' if DATA_DIR != BASE else BASE / 'static' / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
NOTIFICATION_SECRET_FILE = DATA_DIR / '.notification_secrets.json'
SOLAPI_SEND_URL = 'https://api.solapi.com/messages/v4/send-many/detail'
SOLAPI_GROUP_MESSAGES_URL = 'https://api.solapi.com/messages/v4/groups/{group_id}/messages?limit=500'
PAYMENT_SECRET_FILE = DATA_DIR / '.payment_secrets.json'
TOSS_CONFIRM_URL = 'https://api.tosspayments.com/v1/payments/confirm'
TOSS_CANCEL_URL = 'https://api.tosspayments.com/v1/payments/{payment_key}/cancel'

app = Flask(__name__)
# Railway/Reverse proxy 환경에서 실제 https scheme과 client IP를 올바르게 인식합니다.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
SECRET_FILE = DATA_DIR / '.secret_key'
env_secret = os.environ.get('APP_SECRET_KEY', '').strip()
if env_secret:
    app.secret_key = env_secret
elif SECRET_FILE.exists():
    app.secret_key = SECRET_FILE.read_text(encoding='utf-8').strip()
else:
    app.secret_key = secrets.token_hex(32)
    try:
        SECRET_FILE.write_text(app.secret_key, encoding='utf-8')
    except OSError:
        pass
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = (os.environ.get('FLASK_HTTPS', '').lower() in {'1','true','yes'} or bool(os.environ.get('RAILWAY_ENVIRONMENT')))
DEFAULT_OUTBOUND_SHIPPING_FEE = 3500
DEFAULT_RETURN_SHIPPING_FEE = 3500
DEFAULT_DEPOSIT = 30000


def seed_persistent_storage():
    """First Railway boot: copy bundled test DB/images into the mounted volume."""
    if DATA_DIR == BASE:
        return
    bundled_db = BASE / 'rental.db'
    if not DB.exists() and bundled_db.exists():
        import shutil
        shutil.copy2(bundled_db, DB)
    bundled_uploads = BASE / 'static' / 'uploads'
    if bundled_uploads.exists():
        import shutil
        for src in bundled_uploads.iterdir():
            if src.is_file():
                dst = UPLOAD_DIR / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route('/health')
def health():
    try:
        conn = db()
        ok = conn.execute('PRAGMA quick_check').fetchone()[0]
        conn.close()
        if ok != 'ok':
            return jsonify({'status':'error','database':ok}), 503
        return jsonify({'status':'ok','database':'ok','version':'V3.3.2'}), 200
    except Exception as e:
        return jsonify({'status':'error','error':str(e)}), 503


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def column_names(conn, table):
    return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def add_column_if_missing(conn, table, name, definition):
    if name not in column_names(conn, table):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def order_no(rid, created_at=None):
    if created_at:
        digits = ''.join(ch for ch in created_at[:10] if ch.isdigit())
    else:
        digits = date.today().strftime('%Y%m%d')
    return f'R{digits}-{rid:05d}'


def init_db():
    conn = db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        size TEXT DEFAULT '',
        stock INTEGER NOT NULL DEFAULT 0,
        daily_price INTEGER NOT NULL DEFAULT 0,
        deposit INTEGER NOT NULL DEFAULT 0,
        description TEXT DEFAULT '',
        image_filename TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS product_sizes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        size_name TEXT NOT NULL,
        stock INTEGER NOT NULL DEFAULT 0,
        UNIQUE(product_id, size_name),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS customer_profiles (
        phone TEXT PRIMARY KEY,
        customer_note TEXT DEFAULT '',
        damage_loss_note TEXT DEFAULT '',
        caution INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '관리자',
        must_change_password INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '',
        last_login_at TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS member_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        phone_key TEXT NOT NULL UNIQUE,
        email TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        zipcode TEXT DEFAULT '',
        address1 TEXT DEFAULT '',
        address2 TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '',
        last_login_at TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS damage_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        issue_type TEXT NOT NULL DEFAULT '파손',
        qty INTEGER NOT NULL DEFAULT 1,
        amount INTEGER NOT NULL DEFAULT 0,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        size_id INTEGER,
        order_no TEXT DEFAULT '',
        customer_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        qty INTEGER NOT NULL DEFAULT 1,
        rental_days INTEGER NOT NULL DEFAULT 1,
        total_price INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '예약',
        shipping_status TEXT NOT NULL DEFAULT '발송전',
        return_status TEXT NOT NULL DEFAULT '회수전',
        memo TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(size_id) REFERENCES product_sizes(id)
    );
    ''')
    conn.execute("""CREATE TABLE IF NOT EXISTS reservation_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id INTEGER NOT NULL, product_id INTEGER NOT NULL, size_id INTEGER,
        qty INTEGER NOT NULL DEFAULT 1, daily_price INTEGER NOT NULL DEFAULT 0, rental_days INTEGER NOT NULL DEFAULT 1,
        total_price INTEGER NOT NULL DEFAULT 0, deposit_total INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE,
        FOREIGN KEY(product_id) REFERENCES products(id), FOREIGN KEY(size_id) REFERENCES product_sizes(id))""")
    add_column_if_missing(conn, 'reservation_items', 'extra_daily_price', 'INTEGER NOT NULL DEFAULT 0')

    add_column_if_missing(conn, 'products', 'image_filename', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'products', 'is_active', 'INTEGER NOT NULL DEFAULT 1')
    add_column_if_missing(conn, 'products', 'is_test', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'products', 'components', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'products', 'size_guide', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'products', 'rental_notes', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'products', 'extra_daily_price', 'INTEGER NOT NULL DEFAULT 0')
    conn.execute("""CREATE TABLE IF NOT EXISTS product_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        image_filename TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT '',
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    )""")
    add_column_if_missing(conn, 'reservations', 'size_id', 'INTEGER')
    add_column_if_missing(conn, 'reservations', 'rental_days', 'INTEGER NOT NULL DEFAULT 1')
    add_column_if_missing(conn, 'reservations', 'total_price', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'order_no', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'shipping_status', "TEXT NOT NULL DEFAULT '발송전'")
    add_column_if_missing(conn, 'reservations', 'return_status', "TEXT NOT NULL DEFAULT '회수전'")
    add_column_if_missing(conn, 'reservations', 'memo', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'recipient_name', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'zipcode', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'address1', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'address2', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'shipping_fee', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'return_shipping_fee', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'deposit_total', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'final_amount', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'outbound_carrier', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'outbound_tracking_no', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'return_carrier', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'return_tracking_no', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'dispatch_date', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'pickup_date', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'deposit_received', 'INTEGER NOT NULL DEFAULT 0')
    # V3.3 보증금은 카드결제와 분리하여 계좌입금으로만 관리합니다.
    add_column_if_missing(conn, 'reservations', 'deposit_payment_status', "TEXT NOT NULL DEFAULT '입금대기'")
    add_column_if_missing(conn, 'reservations', 'deposit_received_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'deposit_payment_note', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'damage_deduction', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'additional_charge', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'refund_amount', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'refund_status', "TEXT NOT NULL DEFAULT '미정산'")
    add_column_if_missing(conn, 'reservations', 'settlement_note', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'refund_date', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'is_test', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'payment_status', "TEXT NOT NULL DEFAULT '결제완료'")
    add_column_if_missing(conn, 'reservations', 'payment_method', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'paid_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'order_source', "TEXT NOT NULL DEFAULT '온라인'")
    add_column_if_missing(conn, 'reservations', 'member_id', 'INTEGER')
    add_column_if_missing(conn, 'member_accounts', 'toss_customer_key', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'payment_token', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'payment_failure_reason', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'payment_updated_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'payment_expires_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'paid_order_amount', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'order_refund_total', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'order_extra_paid_total', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'cancellation_fee', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'cancellation_refund_amount', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'cancellation_rate', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'reservations', 'cancellation_policy', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'cancelled_at', "TEXT DEFAULT ''")
    # V3.0 Toss Payments
    add_column_if_missing(conn, 'reservations', 'toss_order_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_payment_key', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_payment_status', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_method', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_approved_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_receipt_url', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_last_error', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_raw_json', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'reservations', 'toss_confirm_idempotency_key', "TEXT DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS payment_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        method TEXT DEFAULT '',
        amount INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL,
        message TEXT DEFAULT '',
        processed_at TEXT NOT NULL,
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_payment_transactions_reservation ON payment_transactions(reservation_id)')
    add_column_if_missing(conn, 'payment_transactions', 'provider', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_transactions', 'provider_payment_key', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_transactions', 'provider_order_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_transactions', 'provider_status', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_transactions', 'provider_raw_json', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_transactions', 'refunded_amount', 'INTEGER NOT NULL DEFAULT 0')
    conn.execute("""CREATE TABLE IF NOT EXISTS payment_adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        adjustment_type TEXT NOT NULL,
        amount INTEGER NOT NULL DEFAULT 0,
        reason TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT '처리대기',
        payment_method TEXT DEFAULT '',
        token TEXT DEFAULT '',
        requested_by TEXT DEFAULT '관리자',
        created_at TEXT NOT NULL,
        completed_at TEXT DEFAULT '',
        memo TEXT DEFAULT '',
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_payment_adjustments_reservation ON payment_adjustments(reservation_id)')
    add_column_if_missing(conn, 'payment_adjustments', 'toss_order_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_adjustments', 'toss_payment_key', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_adjustments', 'toss_payment_status', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_adjustments', 'toss_raw_json', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'payment_adjustments', 'provider_refunded_amount', 'INTEGER NOT NULL DEFAULT 0')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_adjustments_token ON payment_adjustments(token) WHERE token<>''")

    # V2.6 약관 문서와 전자 동의 이력
    conn.execute("""CREATE TABLE IF NOT EXISTS legal_documents (
        doc_type TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        version TEXT NOT NULL,
        content TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS consent_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER,
        reservation_id INTEGER,
        phone TEXT DEFAULT '',
        consent_type TEXT NOT NULL,
        document_title TEXT NOT NULL,
        document_version TEXT NOT NULL,
        content_snapshot TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        FOREIGN KEY(member_id) REFERENCES member_accounts(id),
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
    )""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_consent_member ON consent_records(member_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_consent_reservation ON consent_records(reservation_id)')

    # V2.7 고객 알림 템플릿과 발송 대기열
    conn.execute("""CREATE TABLE IF NOT EXISTS notification_templates (
        event_type TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT '알림톡',
        message_template TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_key TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL DEFAULT '알림톡',
        recipient_name TEXT DEFAULT '',
        recipient_phone TEXT DEFAULT '',
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '발송대기',
        created_at TEXT NOT NULL,
        sent_at TEXT DEFAULT '',
        error_message TEXT DEFAULT '',
        FOREIGN KEY(reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
    )""")
    # V2.8 실제 메시지 제공업체 연동 필드
    add_column_if_missing(conn, 'notification_templates', 'provider_template_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_group_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_message_id', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_response', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'last_attempt_at', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'attempt_count', 'INTEGER NOT NULL DEFAULT 0')
    # V2.9 실제 발송 결과 조회 필드
    add_column_if_missing(conn, 'notification_queue', 'provider_status_code', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_status_reason', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_message_type', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_replacement', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_missing(conn, 'notification_queue', 'provider_queue_summary', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_date_processed', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_date_reported', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'provider_date_received', "TEXT DEFAULT ''")
    add_column_if_missing(conn, 'notification_queue', 'result_checked_at', "TEXT DEFAULT ''")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_notification_queue_status ON notification_queue(status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_notification_queue_reservation ON notification_queue(reservation_id)')

    notification_defaults = {
        '주문접수': ('주문접수 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no}이 접수되었습니다. 카드결제 {card_amount}원, 보증금 계좌입금 {deposit_amount}원입니다.'),
        '결제완료': ('결제완료 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no} 결제가 완료되었습니다. 발송예정일은 {dispatch_date}입니다.'),
        '발송완료': ('상품 발송 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no} 상품이 발송되었습니다. {carrier} {tracking_no}'),
        '회수예정': ('회수예정 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no}의 회수예정일은 {pickup_date}입니다. 구성품을 확인해 포장해주세요.'),
        '반납완료': ('반납완료 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no} 반납 처리가 완료되었습니다. 이용해주셔서 감사합니다.'),
        '주문취소': ('주문취소 안내','알림톡','[코스튬 대여몰] {customer_name}님, 주문 {order_no}이 취소되었습니다. 환불이 필요한 경우 주문조회에서 처리상태를 확인해주세요.'),
    }
    for event_type,(title,channel,message_template) in notification_defaults.items():
        conn.execute('INSERT OR IGNORE INTO notification_templates(event_type,title,channel,message_template,is_active,updated_at) VALUES(?,?,?,?,1,?)',
                     (event_type,title,channel,message_template,datetime.now().strftime('%Y-%m-%d %H:%M')))
    old_order_tpl='[코스튬 대여몰] {customer_name}님, 주문 {order_no}이 접수되었습니다. 대여기간 {start_date}~{end_date}, 결제예정금액 {final_amount}원입니다.'
    conn.execute("UPDATE notification_templates SET message_template=?,updated_at=? WHERE event_type='주문접수' AND message_template=?",
                 (notification_defaults['주문접수'][2],datetime.now().strftime('%Y-%m-%d %H:%M'),old_order_tpl))

    legal_defaults = {
        'terms': (
            '이용약관', '2026.09.08',
            '본 약관은 코스튬 대여몰의 회원가입, 상품 대여, 주문 및 서비스 이용에 관한 기본 사항을 정합니다.\n\n'
            '1. 고객은 주문 시 정확한 이름, 연락처, 배송정보를 제공해야 합니다.\n'
            '2. 상품의 대여 가능 여부는 선택한 대여기간과 실제 재고를 기준으로 확정됩니다.\n'
            '3. 결제, 배송, 취소, 환불 및 보증금 정산은 주문 당시 고지된 조건과 운영정책을 따릅니다.\n'
            '4. 고객의 고의 또는 과실로 상품이 훼손·분실된 경우 별도 비용이 청구될 수 있습니다.\n'
            '5. 서비스 운영상 필요한 경우 약관이 변경될 수 있으며 변경된 약관은 이후 신규 동의부터 적용합니다.\n\n'
            '※ 이 문구는 시스템 구축용 기본안입니다. 실제 영업 공개 전 사업자 정보와 법률 검토를 반영해 수정하세요.'
        ),
        'privacy': (
            '개인정보 수집 및 이용 안내', '2026.09.08',
            '코스튬 대여몰은 회원관리, 주문처리, 배송, 반품·회수, 고객문의 및 정산을 위해 필요한 개인정보를 처리합니다.\n\n'
            '수집 항목: 이름, 연락처, 이메일(선택), 우편번호, 주소, 주문·결제·대여 이력\n'
            '이용 목적: 회원 식별, 주문 및 대여계약 이행, 배송·회수, 결제·환불, 고객응대\n'
            '보유 기간: 관련 법령 및 사업자 운영정책에서 정한 기간 또는 이용 목적 달성 시까지\n'
            '동의를 거부할 수 있으나 필수 정보 처리에 동의하지 않으면 회원가입 또는 주문 서비스 이용이 제한될 수 있습니다.\n\n'
            '※ 실제 공개 전 사업자명, 개인정보 보호책임자, 처리위탁사, 보유기간 등 사업자 실제 정보를 반영해 수정하세요.'
        ),
        'rental': (
            '대여·파손·분실 및 취소 규정', '2026.09.08',
            '1. 기본 대여기간은 1일이며 추가 대여일마다 상품별 추가 대여료가 합산됩니다.\n'
            '2. 고객은 예약한 대여기간과 회수일정을 준수해야 하며 연장이 필요한 경우 사전에 변경 절차를 진행해야 합니다.\n'
            '3. 반납된 상품은 세탁·검수 후 정상 반납 여부를 확정합니다.\n'
            '4. 통상적인 사용 흔적을 제외한 파손, 심한 오염, 구성품 누락 또는 분실이 확인되면 실제 손해 범위에서 보증금 차감 또는 추가 비용이 발생할 수 있습니다.\n'
            '5. 취소수수료는 상품 대여료를 기준으로 하며 주문 당시 화면에 표시된 취소·환불 규정을 적용합니다.\n'
            '6. 상품 수령 즉시 구성품과 상태를 확인하고 이상이 있으면 가능한 한 빠르게 관리자에게 알려야 합니다.\n\n'
            '※ 파손·분실 비용 기준과 취소정책은 실제 운영기준에 맞게 관리자 화면에서 수정하세요.'
        ),
    }
    for doc_type, (title, version, content) in legal_defaults.items():
        conn.execute('INSERT OR IGNORE INTO legal_documents(doc_type,title,version,content,updated_at) VALUES(?,?,?,?,?)',
                     (doc_type,title,version,content,datetime.now().strftime('%Y-%m-%d %H:%M')))

    defaults = {
        'outbound_shipping_fee': str(DEFAULT_OUTBOUND_SHIPPING_FEE),
        'return_shipping_fee': str(DEFAULT_RETURN_SHIPPING_FEE),
        'default_deposit': str(DEFAULT_DEPOSIT),
        'deposit_bank_name': '',
        'deposit_account_number': '',
        'deposit_account_holder': '',
        'dispatch_lead_business_days': '1',
        'holiday_dates': '',
        'cancel_free_days': '3',
        'cancel_late_fee_percent': '20',
        'cancel_same_day_fee_percent': '50',
        'cancel_after_start_fee_percent': '100',
        'notification_provider': '테스트',
        'notification_sender_phone': '',
        'notification_kakao_pf_id': '',
        'notification_sms_fallback': '1',
        'notification_auto_send': '0',
        'notification_auto_result_sync': '0',
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))

    # V2.1 최초 관리자 계정. 첫 로그인 후 반드시 비밀번호를 변경해야 합니다.
    if conn.execute('SELECT COUNT(*) c FROM admin_users').fetchone()['c'] == 0:
        conn.execute('''INSERT INTO admin_users(username,password_hash,display_name,must_change_password,is_active,created_at)
                        VALUES(?,?,?,?,?,?)''',
                     ('admin', generate_password_hash('admin1234'), '관리자', 1, 1, datetime.now().strftime('%Y-%m-%d %H:%M')))

    # V2.2 테스트 회원. 기존 테스트 주문을 전화번호 기준으로 연결합니다.
    if conn.execute("SELECT COUNT(*) c FROM member_accounts").fetchone()['c'] == 0 and conn.execute("SELECT COUNT(*) c FROM reservations WHERE is_test=1 AND REPLACE(REPLACE(phone,'-',''),' ','')='01090001001'").fetchone()['c'] > 0:
        cur = conn.execute('''INSERT INTO member_accounts(name,phone,phone_key,email,password_hash,zipcode,address1,address2,is_active,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           ('김민수','010-9000-1001','01090001001','test@example.com',generate_password_hash('test1234'),'28500','충북 청주시 테스트로 1','101호',1,datetime.now().strftime('%Y-%m-%d %H:%M')))
        test_member_id = cur.lastrowid
        conn.execute("UPDATE reservations SET member_id=? WHERE REPLACE(REPLACE(phone,'-',''),' ','')='01090001001'", (test_member_id,))

    # V0.1/V0.2 데이터 마이그레이션
    products = conn.execute('SELECT * FROM products').fetchall()
    for p in products:
        exists = conn.execute('SELECT COUNT(*) c FROM product_sizes WHERE product_id=?', (p['id'],)).fetchone()['c']
        if exists == 0:
            size_name = (p['size'] or 'FREE').strip() if 'size' in p.keys() else 'FREE'
            stock = p['stock'] if 'stock' in p.keys() else 0
            conn.execute('INSERT OR IGNORE INTO product_sizes(product_id,size_name,stock) VALUES(?,?,?)',
                         (p['id'], size_name or 'FREE', stock))

    # 기존 예약에 주문번호 자동 부여
    rows = conn.execute("SELECT id, created_at FROM reservations WHERE COALESCE(order_no,'')='' ").fetchall()
    for r in rows:
        conn.execute('UPDATE reservations SET order_no=? WHERE id=?', (order_no(r['id'], r['created_at']), r['id']))

    # 결제대기 주문은 외부 결제화면에서 사용할 임의 토큰을 부여합니다.
    pending_rows = conn.execute("SELECT id,payment_token,payment_expires_at FROM reservations WHERE payment_status='결제대기'").fetchall()
    for r in pending_rows:
        token = r['payment_token'] or secrets.token_urlsafe(24)
        expires = r['payment_expires_at'] or (datetime.now() + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        conn.execute('UPDATE reservations SET payment_token=?,payment_expires_at=? WHERE id=?',(token,expires,r['id']))

    # V0.8 물류 일정 보정: 기존 주문은 대여 시작 1일 전 발송, 반납 다음날 회수 예정으로 기본 설정
    # V2.5 실제 수납된 주문금액과 주문금액 조정(추가결제/부분환불/전액환불)을 분리 관리합니다.
    # V3.3 카드/PG 결제액은 보증금을 제외한 대여료+배송비만 의미합니다.
    conn.execute("UPDATE reservations SET paid_order_amount=MAX(0,final_amount-COALESCE(deposit_total,0)) WHERE payment_status='결제완료' AND COALESCE(paid_order_amount,0)=0")
    migrated_v32=get_text_setting(conn,'deposit_separate_payment_migrated_v32','0')
    if migrated_v32!='1':
        conn.execute("""UPDATE reservations
                        SET paid_order_amount=MAX(0,final_amount-COALESCE(deposit_total,0))
                        WHERE payment_status='결제완료'
                          AND COALESCE(paid_order_amount,0)=COALESCE(final_amount,0)
                          AND COALESCE(deposit_total,0)>0""")
        conn.execute("""UPDATE reservations SET deposit_payment_status=CASE
                        WHEN COALESCE(deposit_total,0)=0 THEN '해당없음'
                        WHEN COALESCE(deposit_received,0)>=COALESCE(deposit_total,0) THEN '입금확인'
                        ELSE '입금대기' END
                        WHERE COALESCE(deposit_payment_status,'') IN ('','입금대기')""")
        conn.execute("""UPDATE reservations SET deposit_received_at=COALESCE(NULLIF(paid_at,''),NULLIF(created_at,''),'')
                        WHERE deposit_payment_status='입금확인' AND COALESCE(deposit_received_at,'')=''""")
        set_setting(conn,'deposit_separate_payment_migrated_v32','1')
    # 과거에 이미 취소된 결제완료 주문은 전액환불 대기 건으로 한 번만 이관합니다.
    cancelled_paid = conn.execute("SELECT id,final_amount,deposit_total,paid_order_amount,order_extra_paid_total,order_refund_total FROM reservations WHERE status='취소' AND payment_status='결제완료'").fetchall()
    for cr in cancelled_paid:
        exists = conn.execute("SELECT COUNT(*) c FROM payment_adjustments WHERE reservation_id=? AND adjustment_type='전액환불' AND status IN ('처리대기','처리완료')", (cr['id'],)).fetchone()['c']
        fallback_paid=max(0,int(cr['final_amount'] or 0)-int(cr['deposit_total'] or 0))
        net_paid = int(cr['paid_order_amount'] or fallback_paid) + int(cr['order_extra_paid_total'] or 0) - int(cr['order_refund_total'] or 0)
        if not exists and net_paid > 0:
            conn.execute("INSERT INTO payment_adjustments(reservation_id,adjustment_type,amount,reason,status,token,requested_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (cr['id'],'전액환불',net_paid,'기존 취소 주문 환불 이관','처리대기','', '시스템', datetime.now().strftime('%Y-%m-%d %H:%M')))

    logistics_rows = conn.execute("SELECT id,start_date,end_date,dispatch_date,pickup_date FROM reservations").fetchall()
    for r in logistics_rows:
        try:
            dispatch = r['dispatch_date'] or (datetime.strptime(r['start_date'],'%Y-%m-%d').date() - timedelta(days=1)).isoformat()
            pickup = r['pickup_date'] or (datetime.strptime(r['end_date'],'%Y-%m-%d').date() + timedelta(days=1)).isoformat()
            conn.execute('UPDATE reservations SET dispatch_date=?, pickup_date=? WHERE id=?', (dispatch,pickup,r['id']))
        except (ValueError, TypeError):
            pass

    # V0.5 금액 필드 보정: 기존 주문은 기존 대여료를 기준으로 보증금/최종금액을 계산합니다.
    old_rows = conn.execute('''SELECT r.id,r.qty,r.total_price,r.shipping_fee,r.return_shipping_fee,r.deposit_total,r.final_amount,p.deposit
                               FROM reservations r JOIN products p ON p.id=r.product_id''').fetchall()
    for r in old_rows:
        deposit_total = r['deposit_total'] if r['deposit_total'] else (r['deposit'] * r['qty'])
        final_amount = r['final_amount'] if r['final_amount'] else (r['total_price'] + r['shipping_fee'] + r['return_shipping_fee'] + deposit_total)
        conn.execute('UPDATE reservations SET deposit_total=?, final_amount=? WHERE id=?', (deposit_total, final_amount, r['id']))

    # V1.1 보증금 정산 필드 기본값 보정
    settlement_rows = conn.execute('SELECT id,deposit_total,deposit_received,damage_deduction,refund_amount,refund_status FROM reservations').fetchall()
    for r in settlement_rows:
        received = r['deposit_received'] if r['deposit_received'] else r['deposit_total']
        refund_amount = max(0, received - (r['damage_deduction'] or 0))
        refund_status = r['refund_status'] or '미정산'
        conn.execute('UPDATE reservations SET deposit_received=?, refund_amount=?, refund_status=? WHERE id=?',
                     (received, refund_amount, refund_status, r['id']))

    # V1.3 기존 단일상품 주문을 주문품목 테이블로 자동 변환
    for r in conn.execute('SELECT * FROM reservations').fetchall():
        exists=conn.execute('SELECT COUNT(*) c FROM reservation_items WHERE reservation_id=?',(r['id'],)).fetchone()['c']
        if exists==0:
            pr=conn.execute('SELECT daily_price,deposit FROM products WHERE id=?',(r['product_id'],)).fetchone()
            if pr:
                conn.execute('INSERT INTO reservation_items(reservation_id,product_id,size_id,qty,daily_price,extra_daily_price,rental_days,total_price,deposit_total) VALUES(?,?,?,?,?,?,?,?,?)',
                    (r['id'],r['product_id'],r['size_id'],r['qty'],pr['daily_price'],pr['extra_daily_price'] if 'extra_daily_price' in pr.keys() else 0,r['rental_days'],r['total_price'],r['deposit_total']))

    count = conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']
    if count == 0:
        samples = [
            ('마법사 로브 세트','마법사',15000,30000,'로브 + 모자 구성'),
            ('천사 코스튬 세트','천사',18000,30000,'의상 + 날개 + 머리띠 구성'),
            ('수도사 코스튬','종교/중세',16000,30000,'로브 + 허리끈 구성')
        ]
        for name, category, price, deposit, desc in samples:
            cur = conn.execute('INSERT INTO products(name,category,size,stock,daily_price,deposit,description,image_filename,is_active) VALUES(?,?,?,?,?,?,?,?,1)',
                               (name, category, '', 0, price, deposit, desc, ''))
            pid = cur.lastrowid
            sizes = [('M', 2), ('L', 2)] if '마법사' in name else ([('FREE', 2)] if '천사' in name else [('L', 2), ('XL', 1)])
            conn.executemany('INSERT INTO product_sizes(product_id,size_name,stock) VALUES(?,?,?)',
                             [(pid, s, st) for s, st in sizes])
    conn.commit()
    conn.close()



def get_setting(conn, key, default=0):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    try:
        return int(row['value']) if row else int(default)
    except (TypeError, ValueError):
        return int(default)


def set_setting(conn, key, value):
    conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def get_text_setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def get_legal_document(conn, doc_type):
    return conn.execute('SELECT * FROM legal_documents WHERE doc_type=?',(doc_type,)).fetchone()


def legal_documents_map(conn):
    rows=conn.execute("SELECT * FROM legal_documents ORDER BY CASE doc_type WHEN 'terms' THEN 1 WHEN 'privacy' THEN 2 WHEN 'rental' THEN 3 ELSE 9 END").fetchall()
    return {r['doc_type']:r for r in rows}


def record_consent(conn, consent_type, document, member_id=None, reservation_id=None, phone='', content_override=None):
    if not document:
        raise ValueError('동의 문서를 찾을 수 없습니다.')
    content = content_override if content_override is not None else document['content']
    conn.execute("""INSERT INTO consent_records(member_id,reservation_id,phone,consent_type,document_title,document_version,content_snapshot,accepted_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (member_id,reservation_id,phone or '',consent_type,document['title'],document['version'],content,datetime.now().strftime('%Y-%m-%d %H:%M:%S')))


def parse_holidays(raw):
    values = set()
    for token in (raw or '').replace(',', '\n').splitlines():
        token = token.strip()
        if not token:
            continue
        try:
            datetime.strptime(token, '%Y-%m-%d')
            values.add(token)
        except ValueError:
            pass
    return values

def previous_business_day(target_date, business_days, holidays):
    d = target_date
    moved = 0
    while moved < max(0, business_days):
        d -= timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            moved += 1
    return d

def next_business_day(target_date, holidays):
    d = target_date + timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d += timedelta(days=1)
    return d

def auto_logistics_dates(conn, start_date, end_date):
    start_d = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_d = datetime.strptime(end_date, '%Y-%m-%d').date()
    lead = get_setting(conn, 'dispatch_lead_business_days', 1)
    holidays = parse_holidays(get_text_setting(conn, 'holiday_dates', ''))
    return previous_business_day(start_d, lead, holidays).isoformat(), next_business_day(end_d, holidays).isoformat()

def get_fees(conn):
    return {
        'outbound_shipping_fee': get_setting(conn, 'outbound_shipping_fee', DEFAULT_OUTBOUND_SHIPPING_FEE),
        'return_shipping_fee': get_setting(conn, 'return_shipping_fee', DEFAULT_RETURN_SHIPPING_FEE),
        'default_deposit': get_setting(conn, 'default_deposit', DEFAULT_DEPOSIT),
        'deposit_bank_name': get_text_setting(conn, 'deposit_bank_name', ''),
        'deposit_account_number': get_text_setting(conn, 'deposit_account_number', ''),
        'deposit_account_holder': get_text_setting(conn, 'deposit_account_holder', ''),
        'dispatch_lead_business_days': get_setting(conn, 'dispatch_lead_business_days', 1),
        'holiday_dates': get_text_setting(conn, 'holiday_dates', ''),
        'cancel_free_days': get_setting(conn, 'cancel_free_days', 3),
        'cancel_late_fee_percent': get_setting(conn, 'cancel_late_fee_percent', 20),
        'cancel_same_day_fee_percent': get_setting(conn, 'cancel_same_day_fee_percent', 50),
        'cancel_after_start_fee_percent': get_setting(conn, 'cancel_after_start_fee_percent', 100),
    }

def get_product(conn, product_id):
    p = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not p:
        return None, []
    sizes = conn.execute('SELECT * FROM product_sizes WHERE product_id=? ORDER BY id', (product_id,)).fetchall()
    return p, sizes


def available_stock(conn, size_id, start_date, end_date, exclude_reservation_id=None):
    row=conn.execute('SELECT stock FROM product_sizes WHERE id=?',(size_id,)).fetchone()
    if not row: return 0
    now_text=datetime.now().strftime('%Y-%m-%d %H:%M')
    sql="""SELECT COALESCE(SUM(ri.qty),0) used
        FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id
        WHERE ri.size_id=? AND r.status IN ('예약','대여중')
          AND (COALESCE(r.payment_status,'결제완료')='결제완료'
               OR (r.payment_status='결제대기' AND (COALESCE(r.payment_expires_at,'')='' OR r.payment_expires_at>=?)))
          AND NOT (r.end_date < ? OR r.start_date > ?)"""
    params=[size_id,now_text,start_date,end_date]
    if exclude_reservation_id:
        sql+=' AND r.id<>?'; params.append(exclude_reservation_id)
    scheduled=conn.execute(sql,params).fetchone()['used']
    cleaning=conn.execute("SELECT COALESCE(SUM(ri.qty),0) used FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id WHERE ri.size_id=? AND r.status='세탁/검수중'",(size_id,)).fetchone()['used']
    return max(0,row['stock']-scheduled-cleaning)


def payment_token_for_order(conn, reservation_id):
    row=conn.execute('SELECT payment_token FROM reservations WHERE id=?',(reservation_id,)).fetchone()
    if not row: return ''
    token=row['payment_token'] or secrets.token_urlsafe(24)
    if not row['payment_token']:
        conn.execute('UPDATE reservations SET payment_token=? WHERE id=?',(token,reservation_id))
    return token


def log_payment_transaction(conn, reservation_id, method, amount, result, message='', provider='', provider_payment_key='', provider_order_id='', provider_status='', provider_data=None):
    conn.execute('''INSERT INTO payment_transactions(reservation_id,method,amount,result,message,processed_at,provider,provider_payment_key,provider_order_id,provider_status,provider_raw_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                 (reservation_id,method or '',int(amount or 0),result,message,datetime.now().strftime('%Y-%m-%d %H:%M:%S'),provider or '',provider_payment_key or '',provider_order_id or '',provider_status or '',provider_raw(provider_data) if provider_data is not None else ''))


def card_payment_amount(order):
    """카드/PG로 결제할 금액: 상품 대여료 + 왕복 배송비. 보증금은 계좌입금 별도."""
    if not order:
        return 0
    try:
        return max(0, int(order['final_amount'] or 0) - int(order['deposit_total'] or 0))
    except (KeyError, TypeError, ValueError):
        return 0


def net_order_paid(order):
    return max(0, int(order['paid_order_amount'] or 0) + int(order['order_extra_paid_total'] or 0) - int(order['order_refund_total'] or 0))


def cancel_pending_adjustments(conn, reservation_id, memo='주문금액 재계산으로 기존 미처리 건 취소'):
    conn.execute("UPDATE payment_adjustments SET status='취소',memo=CASE WHEN COALESCE(memo,'')='' THEN ? ELSE memo END WHERE reservation_id=? AND status='처리대기'", (memo,reservation_id))


def create_payment_adjustment(conn, reservation_id, adjustment_type, amount, reason, requested_by='관리자'):
    amount=max(0,int(amount or 0))
    if amount <= 0:
        return None
    if adjustment_type not in ('추가결제','부분환불','전액환불'):
        raise ValueError('지원하지 않는 결제 조정 유형입니다.')
    token=secrets.token_urlsafe(24) if adjustment_type=='추가결제' else ''
    cur=conn.execute('''INSERT INTO payment_adjustments(reservation_id,adjustment_type,amount,reason,status,payment_method,token,requested_by,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                     (reservation_id,adjustment_type,amount,reason,'처리대기','',token,requested_by,datetime.now().strftime('%Y-%m-%d %H:%M')))
    return cur.lastrowid


def queue_amount_adjustment(conn, order, new_payment_amount, reason, requested_by='관리자'):
    cancel_pending_adjustments(conn, order['id'])
    net_paid=net_order_paid(order)
    new_final=max(0,int(new_payment_amount or 0))
    if new_final > net_paid:
        create_payment_adjustment(conn,order['id'],'추가결제',new_final-net_paid,reason,requested_by)
        return '추가결제', new_final-net_paid
    if new_final < net_paid:
        create_payment_adjustment(conn,order['id'],'부분환불',net_paid-new_final,reason,requested_by)
        return '부분환불', net_paid-new_final
    return '',0


def cancellation_quote(conn, order, as_of=None):
    """취소 시점 기준 예상 수수료/환불액. 수수료는 상품 대여료에만 적용합니다."""
    if not order:
        return {'rate':0,'fee':0,'refund':0,'days_before':0,'label':'','policy':''}
    policy=get_fees(conn)
    today=as_of or date.today()
    try:
        start=datetime.strptime(order['start_date'],'%Y-%m-%d').date()
        days_before=(start-today).days
    except (ValueError,TypeError):
        days_before=0
    free_days=max(1,int(policy['cancel_free_days'] or 3))
    if days_before >= free_days:
        rate=0; label=f'대여 {free_days}일 전까지'
    elif days_before >= 1:
        rate=max(0,min(100,int(policy['cancel_late_fee_percent'] or 0))); label=f'대여 {days_before}일 전'
    elif days_before == 0:
        rate=max(0,min(100,int(policy['cancel_same_day_fee_percent'] or 0))); label='대여 당일'
    else:
        rate=max(0,min(100,int(policy['cancel_after_start_fee_percent'] or 0))); label='대여 시작 후'
    rental_fee=max(0,int(order['total_price'] or 0))
    fee=min(rental_fee, round(rental_fee*rate/100))
    paid=net_order_paid(order) if order['payment_status']=='결제완료' else 0
    refund=max(0,paid-fee)
    policy_text=f'{label} 취소수수료 {rate}% (상품 대여료 기준)'
    return {'rate':rate,'fee':fee,'refund':refund,'days_before':days_before,'label':label,'policy':policy_text,'paid':paid,'rental_fee':rental_fee}


def cancellation_policy_summary(conn):
    p=get_fees(conn); d=max(1,int(p['cancel_free_days'] or 3))
    late=max(0,min(100,int(p['cancel_late_fee_percent'] or 0)))
    same=max(0,min(100,int(p['cancel_same_day_fee_percent'] or 0)))
    after=max(0,min(100,int(p['cancel_after_start_fee_percent'] or 0)))
    return f'대여 {d}일 전까지 무료 취소 · 대여 1~{max(1,d-1)}일 전 {late}% · 당일 {same}% · 시작 후 {after}% (상품 대여료 기준)'


def queue_cancel_refund(conn, order, requested_by='고객'):
    cancel_pending_adjustments(conn, order['id'], '주문 취소로 기존 미처리 조정건 취소')
    quote=cancellation_quote(conn,order)
    amount=int(quote['refund'] or 0)
    paid=int(quote['paid'] or 0)
    if amount > 0:
        adjustment_type='전액환불' if amount==paid else '부분환불'
        reason=f"예약 취소 환불 · {quote['policy']} · 취소수수료 {quote['fee']:,}원"
        create_payment_adjustment(conn,order['id'],adjustment_type,amount,reason,requested_by)
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    # 이미 계좌입금된 보증금은 카드 환불과 섞지 않고 별도 보증금 환급대기로 전환합니다.
    deposit_received=max(0,int(order['deposit_received'] or 0))
    if deposit_received>0 and (order['deposit_payment_status'] or '')=='입금확인':
        deposit_refund=max(0,deposit_received-int(order['damage_deduction'] or 0))
        conn.execute("""UPDATE reservations SET refund_amount=?,refund_status=CASE WHEN ? > 0 THEN '환급예정' ELSE refund_status END,
                     settlement_note=CASE WHEN COALESCE(settlement_note,'')='' THEN '주문 취소에 따른 보증금 환급대기' ELSE settlement_note END WHERE id=?""",
                     (deposit_refund,deposit_refund,order['id']))
    conn.execute('''UPDATE reservations SET cancellation_fee=?,cancellation_refund_amount=?,cancellation_rate=?,cancellation_policy=?,cancelled_at=? WHERE id=?''',
                 (quote['fee'],amount,quote['rate'],quote['policy'],now,order['id']))
    return quote


def complete_payment_adjustment(conn, adjustment, method='', memo='', provider='', provider_payment_key='', provider_order_id='', provider_status='', provider_data=None):
    if not adjustment or adjustment['status']!='처리대기':
        return False
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    amount=int(adjustment['amount'] or 0)
    if adjustment['adjustment_type']=='추가결제':
        conn.execute('UPDATE reservations SET order_extra_paid_total=COALESCE(order_extra_paid_total,0)+? WHERE id=?',(amount,adjustment['reservation_id']))
        result='추가결제'
    else:
        conn.execute('UPDATE reservations SET order_refund_total=COALESCE(order_refund_total,0)+? WHERE id=?',(amount,adjustment['reservation_id']))
        result=adjustment['adjustment_type']
    conn.execute("UPDATE payment_adjustments SET status='처리완료',payment_method=?,completed_at=?,memo=? WHERE id=?",(method or '',now,memo or adjustment['memo'] or '',adjustment['id']))
    log_payment_transaction(conn,adjustment['reservation_id'],method,amount,result,adjustment['reason'] or '',provider=provider,provider_payment_key=provider_payment_key,provider_order_id=provider_order_id,provider_status=provider_status,provider_data=provider_data)
    return True


def expire_payment_if_needed(conn, order):
    if not order or order['payment_status']!='결제대기' or not order['payment_expires_at']:
        return order
    now_text=datetime.now().strftime('%Y-%m-%d %H:%M')
    if order['payment_expires_at'] < now_text:
        conn.execute("UPDATE reservations SET payment_status='결제실패',payment_failure_reason='결제시간 만료',payment_updated_at=? WHERE id=?",(now_text,order['id']))
        log_payment_transaction(conn,order['id'],order['payment_method'],card_payment_amount(order),'실패','결제시간 만료')
        conn.commit()
        return conn.execute('SELECT * FROM reservations WHERE id=?',(order['id'],)).fetchone()
    return order


@app.before_request
def expire_stale_payment_holds():
    if request.endpoint == 'static':
        return
    conn=db(); now_text=datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        rows=conn.execute("SELECT * FROM reservations WHERE payment_status='결제대기' AND COALESCE(payment_expires_at,'')<>'' AND payment_expires_at<?",(now_text,)).fetchall()
    except sqlite3.OperationalError:
        conn.close(); return
    for row in rows:
        conn.execute("UPDATE reservations SET payment_status='결제실패',payment_failure_reason='결제시간 만료',payment_updated_at=? WHERE id=?",(now_text,row['id']))
        log_payment_transaction(conn,row['id'],row['payment_method'],card_payment_amount(row),'실패','결제시간 만료')
    if rows:
        conn.commit()
    conn.close()

def inventory_summary(conn,size_id):
    tr=conn.execute('SELECT stock FROM product_sizes WHERE id=?',(size_id,)).fetchone(); total=tr['stock'] if tr else 0
    now_text=datetime.now().strftime('%Y-%m-%d %H:%M')
    def q(st):
        if st in ('예약','대여중'):
            return conn.execute("""SELECT COALESCE(SUM(ri.qty),0) q FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id
                WHERE ri.size_id=? AND r.status=? AND (r.payment_status='결제완료' OR (r.payment_status='결제대기' AND (COALESCE(r.payment_expires_at,'')='' OR r.payment_expires_at>=?)))""",(size_id,st,now_text)).fetchone()['q']
        return conn.execute('SELECT COALESCE(SUM(ri.qty),0) q FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id WHERE ri.size_id=? AND r.status=?',(size_id,st)).fetchone()['q']
    reserved,renting,cleaning=q('예약'),q('대여중'),q('세탁/검수중')
    return {'total':total,'reserved':reserved,'renting':renting,'cleaning':cleaning,'physical_available':max(0,total-renting-cleaning)}


def rental_days(start, end):
    a = datetime.strptime(start, '%Y-%m-%d').date()
    b = datetime.strptime(end, '%Y-%m-%d').date()
    return (b - a).days + 1

def rental_line_price(base_price, extra_daily_price, days, qty=1):
    days = max(1, int(days))
    return (int(base_price or 0) + int(extra_daily_price or 0) * (days - 1)) * max(1, int(qty))


def save_image(file):
    if not file or not file.filename or '.' not in file.filename:
        return ''
    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return ''
    safe = secure_filename(file.filename)
    stem = Path(safe).stem[:50] or 'image'
    filename = f'{stem}_{uuid.uuid4().hex[:10]}.{ext}'
    file.save(UPLOAD_DIR / filename)
    return filename


CARRIERS = ['CJ대한통운','한진택배','롯데택배','우체국택배','로젠택배','기타']

def tracking_url(carrier, tracking_no):
    from urllib.parse import quote_plus
    no = ''.join(ch for ch in (tracking_no or '') if ch.isalnum())
    if not no:
        return ''
    if carrier == 'CJ대한통운':
        return 'https://www.cjlogistics.com/ko/tool/parcel/tracking?gnbInvcNo=' + quote_plus(no)
    if carrier == '한진택배':
        return 'https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillSch.do?mCode=MN020'
    if carrier == '롯데택배':
        return 'https://www.lotteglogis.com/home/reservation/tracking/index'
    if carrier == '우체국택배':
        return 'https://service.epost.go.kr/trace.RetrieveDomRigiTraceList.comm'
    if carrier == '로젠택배':
        return 'https://www.ilogen.com/web/personal/trace'
    return 'https://search.naver.com/search.naver?query=' + quote_plus(f'{carrier} {no} 배송조회')

app.jinja_env.globals['tracking_url'] = tracking_url
app.jinja_env.globals['CARRIERS'] = CARRIERS


def load_notification_secrets():
    env_api_key=os.environ.get('SOLAPI_API_KEY','').strip()
    env_api_secret=os.environ.get('SOLAPI_API_SECRET','').strip()
    env_webhook=os.environ.get('SOLAPI_WEBHOOK_SECRET','').strip()
    if env_api_key or env_api_secret or env_webhook:
        return {'api_key':env_api_key, 'api_secret':env_api_secret, 'webhook_secret':env_webhook}
    """API Key/Secret은 DB가 아니라 로컬 비밀설정 파일에서만 읽습니다."""
    if not NOTIFICATION_SECRET_FILE.exists():
        return {'api_key':'', 'api_secret':'', 'webhook_secret':''}
    try:
        data=json.loads(NOTIFICATION_SECRET_FILE.read_text(encoding='utf-8'))
        return {
            'api_key':str(data.get('api_key','')).strip(),
            'api_secret':str(data.get('api_secret','')).strip(),
            'webhook_secret':str(data.get('webhook_secret','')).strip(),
        }
    except (OSError, ValueError, TypeError):
        return {'api_key':'', 'api_secret':'', 'webhook_secret':''}


def save_notification_secrets(api_key, api_secret, webhook_secret=None):
    current={'webhook_secret':''}
    if NOTIFICATION_SECRET_FILE.exists():
        try:
            current=json.loads(NOTIFICATION_SECRET_FILE.read_text(encoding='utf-8'))
        except Exception:
            current={'webhook_secret':''}
    data={'api_key':(api_key or '').strip(), 'api_secret':(api_secret or '').strip(),
          'webhook_secret': str(current.get('webhook_secret','') if webhook_secret is None else webhook_secret).strip()}
    NOTIFICATION_SECRET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(NOTIFICATION_SECRET_FILE, 0o600)
    except OSError:
        pass


def masked_api_key(value):
    value=(value or '').strip()
    if not value:
        return '미설정'
    if len(value) <= 8:
        return '*' * max(4, len(value))
    return value[:4] + '*' * (len(value)-8) + value[-4:]



def load_payment_secrets():
    env_client=os.environ.get('TOSS_CLIENT_KEY','').strip()
    env_secret=os.environ.get('TOSS_SECRET_KEY','').strip()
    if env_client or env_secret:
        return {'client_key':env_client, 'secret_key':env_secret}
    """토스 클라이언트/시크릿 키를 DB가 아닌 로컬 파일에서 읽습니다."""
    if not PAYMENT_SECRET_FILE.exists():
        return {'client_key':'', 'secret_key':''}
    try:
        data=json.loads(PAYMENT_SECRET_FILE.read_text(encoding='utf-8'))
        return {
            'client_key':str(data.get('client_key','')).strip(),
            'secret_key':str(data.get('secret_key','')).strip(),
        }
    except (OSError, ValueError, TypeError):
        return {'client_key':'', 'secret_key':''}


def save_payment_secrets(client_key, secret_key):
    data={'client_key':(client_key or '').strip(), 'secret_key':(secret_key or '').strip()}
    PAYMENT_SECRET_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(PAYMENT_SECRET_FILE, 0o600)
    except OSError:
        pass


def payment_provider_config(conn):
    sec=load_payment_secrets()
    mode=get_text_setting(conn,'payment_provider_mode','MOCK').upper().strip() or 'MOCK'
    if mode not in {'MOCK','TOSS_TEST'}:
        mode='MOCK'
    client_key=sec.get('client_key','')
    secret_key=sec.get('secret_key','')
    return {
        'mode':mode,
        'client_key':client_key,
        'secret_key':secret_key,
        'ready': mode=='TOSS_TEST' and bool(client_key and secret_key),
        'client_masked':masked_api_key(client_key),
        'secret_masked':masked_api_key(secret_key),
    }


def toss_basic_auth(secret_key):
    raw=((secret_key or '').strip()+':').encode('utf-8')
    return 'Basic '+base64.b64encode(raw).decode('ascii')


def toss_api_post(url, secret_key, payload, idempotency_key=''):
    headers={'Authorization':toss_basic_auth(secret_key),'Content-Type':'application/json'}
    if idempotency_key:
        headers['Idempotency-Key']=idempotency_key
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers=headers,method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw=response.read().decode('utf-8','replace')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw=e.read().decode('utf-8','replace')
        try:
            detail=json.loads(raw)
            code=detail.get('code') or f'HTTP_{e.code}'
            message=detail.get('message') or raw
        except Exception:
            code=f'HTTP_{e.code}'; message=raw or str(e)
        raise RuntimeError(f'{code}: {str(message)[:500]}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'TOSS_NETWORK: {e.reason}')


def toss_confirm_payment(secret_key, payment_key, order_id, amount, idempotency_key):
    return toss_api_post(TOSS_CONFIRM_URL,secret_key,{
        'paymentKey':payment_key,'orderId':order_id,'amount':int(amount)
    },idempotency_key)


def toss_cancel_payment(secret_key, payment_key, cancel_reason, cancel_amount, idempotency_key):
    url=TOSS_CANCEL_URL.format(payment_key=urllib.parse.quote(payment_key,safe=''))
    payload={'cancelReason':(cancel_reason or '주문 환불')[:200]}
    if cancel_amount is not None:
        payload['cancelAmount']=int(cancel_amount)
    return toss_api_post(url,secret_key,payload,idempotency_key)


def toss_receipt_url(data):
    receipt=(data or {}).get('receipt') or {}
    return str(receipt.get('url') or '') if isinstance(receipt,dict) else ''


def make_toss_order_id(prefix, object_id, order_no=''):
    # 토스 orderId 제약(6~64자, 영숫자/-/_/=)에 맞춘 서버 생성 고유값
    safe=''.join(ch for ch in (order_no or '') if ch.isalnum() or ch in '-_')[:28]
    base=f'{prefix}-{safe or object_id}-{object_id}-{secrets.token_hex(4)}'
    return base[:64]


def member_toss_customer_key(conn, member_id):
    if not member_id:
        return 'ANONYMOUS'
    row=conn.execute('SELECT toss_customer_key FROM member_accounts WHERE id=?',(member_id,)).fetchone()
    if not row:
        return 'ANONYMOUS'
    key=(row['toss_customer_key'] or '').strip()
    if not key:
        key='member_'+uuid.uuid4().hex
        conn.execute('UPDATE member_accounts SET toss_customer_key=? WHERE id=?',(key,member_id))
    return key


def order_name_for_toss(items):
    if not items:
        return '코스튬 대여 주문'
    first=str(items[0]['name'] or '코스튬 대여')
    return (first if len(items)==1 else f'{first} 외 {len(items)-1}건')[:100]


def normalize_mobile(value):
    return ''.join(ch for ch in str(value or '') if ch.isdigit())[:15]


def provider_payment_method(data):
    method=str((data or {}).get('method') or '').strip()
    easy=(data or {}).get('easyPay') or {}
    if isinstance(easy,dict) and easy.get('provider'):
        return f'{method}/{easy.get("provider")}' if method else str(easy.get('provider'))
    return method or '토스페이먼츠'


def provider_raw(data):
    try:
        return json.dumps(data,ensure_ascii=False)[:20000]
    except Exception:
        return ''

def notification_provider_config(conn):
    return {
        'provider': get_text_setting(conn,'notification_provider','테스트'),
        'sender_phone': get_text_setting(conn,'notification_sender_phone',''),
        'kakao_pf_id': get_text_setting(conn,'notification_kakao_pf_id',''),
        'sms_fallback': get_setting(conn,'notification_sms_fallback',1),
        'auto_send': get_setting(conn,'notification_auto_send',0),
        'auto_result_sync': get_setting(conn,'notification_auto_result_sync',0),
    }


def solapi_auth_header(api_key, api_secret):
    dt=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    salt=secrets.token_hex(16)
    signature=hmac.new(api_secret.encode('utf-8'), (dt+salt).encode('utf-8'), hashlib.sha256).hexdigest()
    return f'HMAC-SHA256 apiKey={api_key}, date={dt}, salt={salt}, signature={signature}'


def solapi_send_payload(api_key, api_secret, payload):
    req=urllib.request.Request(
        SOLAPI_SEND_URL,
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
        headers={'Authorization':solapi_auth_header(api_key,api_secret),'Content-Type':'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw=response.read().decode('utf-8','replace')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw=e.read().decode('utf-8','replace')
        try:
            detail=json.loads(raw)
            message=detail.get('message') or detail.get('errorMessage') or detail.get('statusMessage') or raw
        except Exception:
            message=raw or str(e)
        raise RuntimeError(f'SOLAPI HTTP {e.code}: {message[:500]}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'SOLAPI 연결 실패: {e.reason}')


def solapi_get_json(api_key, api_secret, url):
    req = urllib.request.Request(
        url,
        headers={'Authorization': solapi_auth_header(api_key, api_secret), 'Content-Type': 'application/json'},
        method='GET'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode('utf-8', 'replace')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            detail = json.loads(raw)
            message = detail.get('message') or detail.get('errorMessage') or detail.get('statusMessage') or raw
        except Exception:
            message = raw or str(e)
        raise RuntimeError(f'SOLAPI 결과조회 HTTP {e.code}: {message[:500]}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'SOLAPI 결과조회 연결 실패: {e.reason}')


def solapi_message_from_result(result, message_id=''):
    message_list = result.get('messageList') or {}
    if isinstance(message_list, dict):
        if message_id and message_id in message_list:
            return message_list[message_id]
        values = list(message_list.values())
        return values[0] if values else None
    if isinstance(message_list, list):
        if message_id:
            for item in message_list:
                if item.get('messageId') == message_id:
                    return item
        return message_list[0] if message_list else None
    return None


def solapi_queue_summary(message):
    parts = []
    for q in (message.get('queues') or []):
        name = str(q.get('name') or '').strip()
        code = str(q.get('statusCode') or '').strip()
        if name or code:
            parts.append(f'{name or "queue"}:{code or "-"}')
    return ' → '.join(parts)


def normalize_solapi_result(message):
    code = str(message.get('statusCode') or '').strip()
    provider_status = str(message.get('status') or '').upper().strip()
    reason = str(message.get('reason') or message.get('statusMessage') or '').strip()
    if code == '4000':
        local_status = '발송성공'
    elif code == '5000' or (provider_status == 'COMPLETE' and code and code != '4000'):
        local_status = '발송실패'
    elif provider_status in ('PENDING', 'SENDING', 'PROCESSING') or code in ('2000', '3000') or (code and code.startswith(('2', '3'))):
        local_status = '발송중'
    elif provider_status == 'COMPLETE':
        local_status = '발송성공' if not code else '발송실패'
    else:
        local_status = '발송중'

    replacement = 1 if message.get('replacement') else 0
    if message.get('replacements'):
        replacement = 1
    queues = message.get('queues') or []
    has_sms = any(str(q.get('name') or '').lower() in ('sms', 'lms', 'mms') for q in queues)
    has_kakao = any('kakao' in str(q.get('name') or '').lower() for q in queues)
    if has_sms and has_kakao:
        replacement = 1

    return {
        'status': local_status,
        'code': code,
        'reason': reason,
        'type': str(message.get('type') or ''),
        'replacement': replacement,
        'queue_summary': solapi_queue_summary(message),
        'date_processed': str(message.get('dateProcessed') or ''),
        'date_reported': str(message.get('dateReported') or ''),
        'date_received': str(message.get('dateReceived') or ''),
        'message_id': str(message.get('messageId') or ''),
        'group_id': str(message.get('groupId') or ''),
    }


def apply_notification_provider_result(conn, row_id, message, raw_result=None):
    info = normalize_solapi_result(message)
    checked = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    error = '' if info['status'] != '발송실패' else (info['reason'] or f'SOLAPI 상태코드 {info["code"] or "-"}')
    sent_at = ''
    if info['status'] == '발송성공':
        sent_at = info['date_received'] or info['date_reported'] or info['date_processed'] or checked
    response_text = json.dumps(raw_result, ensure_ascii=False)[:10000] if raw_result is not None else ''
    has_route_detail = 1 if any(k in message for k in ('replacement','replacements','queues')) else 0
    conn.execute(
        """UPDATE notification_queue SET
           status=?, error_message=?, provider_status_code=?, provider_status_reason=?,
           provider_message_type=?,
           provider_replacement=CASE WHEN ?=1 THEN ? ELSE provider_replacement END,
           provider_queue_summary=CASE WHEN ?=1 THEN ? ELSE provider_queue_summary END,
           provider_date_processed=?, provider_date_reported=?, provider_date_received=?, result_checked_at=?,
           provider_message_id=CASE WHEN ?<>'' THEN ? ELSE provider_message_id END,
           provider_group_id=CASE WHEN ?<>'' THEN ? ELSE provider_group_id END,
           provider_response=CASE WHEN ?<>'' THEN ? ELSE provider_response END,
           sent_at=CASE WHEN ?<>'' THEN ? ELSE sent_at END
           WHERE id=?""",
        (
            info['status'], error, info['code'], info['reason'], info['type'],
            has_route_detail, info['replacement'], has_route_detail, info['queue_summary'],
            info['date_processed'], info['date_reported'], info['date_received'], checked,
            info['message_id'], info['message_id'], info['group_id'], info['group_id'],
            response_text, response_text, sent_at, sent_at, row_id
        )
    )
    return info


def sync_notification_result(nid):
    conn = db()
    row = conn.execute('SELECT * FROM notification_queue WHERE id=?', (nid,)).fetchone()
    if not row:
        conn.close()
        return False, '알림 기록을 찾을 수 없습니다.'
    secret = load_notification_secrets()
    if not secret['api_key'] or not secret['api_secret']:
        conn.close()
        return False, 'SOLAPI API Key/Secret을 먼저 저장해주세요.'
    group_id = (row['provider_group_id'] or '').strip()
    if not group_id:
        conn.close()
        return False, 'SOLAPI 그룹ID가 없어 결과를 조회할 수 없습니다.'
    try:
        url = SOLAPI_GROUP_MESSAGES_URL.format(group_id=urllib.parse.quote(group_id, safe=''))
        result = solapi_get_json(secret['api_key'], secret['api_secret'], url)
        message = solapi_message_from_result(result, (row['provider_message_id'] or '').strip())
        if not message:
            raise RuntimeError('SOLAPI 응답에서 해당 메시지를 찾지 못했습니다.')
        info = apply_notification_provider_result(conn, nid, message, result)
        conn.commit()
        conn.close()
        extra = ' · 대체문자 사용' if info['replacement'] else ''
        return True, f"결과 확인: {info['status']} · 코드 {info['code'] or '-'}{extra}"
    except Exception as e:
        checked = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            'UPDATE notification_queue SET result_checked_at=?,error_message=? WHERE id=?',
            (checked, '결과조회 실패: ' + str(e)[:900], nid)
        )
        conn.commit()
        conn.close()
        return False, str(e)


def sync_pending_notification_results(limit=50):
    conn = db()
    config = notification_provider_config(conn)
    if config['provider'] != 'SOLAPI':
        conn.close()
        return (0, 0)
    ids = [r['id'] for r in conn.execute(
        """SELECT id FROM notification_queue
           WHERE status IN ('발송접수','발송중') AND provider_group_id<>''
           ORDER BY id LIMIT ?""",
        (max(1, min(int(limit), 100)),)
    ).fetchall()]
    conn.close()
    ok = fail = 0
    for nid in ids:
        success, _ = sync_notification_result(nid)
        if success:
            ok += 1
        else:
            fail += 1
    return ok, fail


def build_solapi_message(row, template, config):
    to=normalize_phone(row['recipient_phone'])
    sender=normalize_phone(config['sender_phone'])
    if not to:
        raise ValueError('수신번호가 없습니다.')
    channel=row['channel'] or '알림톡'
    if channel in ('SMS','문자'):
        if not sender:
            raise ValueError('SOLAPI 발신번호를 먼저 설정해주세요.')
        return {'to':to,'from':sender,'text':row['message'],'autoTypeDetect':True}
    if channel == '알림톡':
        pf_id=(config['kakao_pf_id'] or '').strip()
        template_id=(template['provider_template_id'] or '').strip() if template else ''
        if not pf_id:
            raise ValueError('카카오 채널 ID(pfId)를 먼저 설정해주세요.')
        if not template_id:
            raise ValueError(f'{row["event_type"]} 알림톡의 SOLAPI 템플릿 ID를 먼저 입력해주세요.')
        msg={'to':to,'text':row['message'],'kakaoOptions':{
            'pfId':pf_id,'templateId':template_id,
            'disableSms':False if config['sms_fallback'] else True,
        }}
        if sender:
            msg['from']=sender
        elif config['sms_fallback']:
            raise ValueError('알림톡 실패 시 문자 대체발송을 사용하려면 발신번호가 필요합니다.')
        return msg
    raise ValueError(f'실제 발송을 지원하지 않는 채널입니다: {channel}')


def send_notification_now(nid, allow_test_order=False):
    conn=db()
    row=conn.execute("""SELECT n.*,r.order_no FROM notification_queue n
                        JOIN reservations r ON r.id=n.reservation_id WHERE n.id=?""",(nid,)).fetchone()
    if not row:
        conn.close(); return False,'알림 기록을 찾을 수 없습니다.'
    config=notification_provider_config(conn)
    secret=load_notification_secrets()
    if config['provider'] != 'SOLAPI':
        conn.close(); return False,'실제 발송 제공업체가 SOLAPI로 설정되어 있지 않습니다.'
    if not secret['api_key'] or not secret['api_secret']:
        conn.close(); return False,'SOLAPI API Key/Secret을 먼저 저장해주세요.'
    if (row['order_no'] or '').startswith('TEST-') and not allow_test_order:
        conn.close(); return False,'TEST 주문은 실제 고객에게 잘못 발송될 수 있어 실제 발송을 차단했습니다.'
    template=conn.execute('SELECT * FROM notification_templates WHERE event_type=?',(row['event_type'],)).fetchone()
    now=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        message=build_solapi_message(row,template,config)
        result=solapi_send_payload(secret['api_key'],secret['api_secret'],{'messages':[message],'strict':True,'showMessageList':True})
        failed=result.get('failedMessageList') or []
        group=result.get('groupInfo') or {}
        initial_message=solapi_message_from_result(result) or {}
        registered_success=int((group.get('count') or {}).get('registeredSuccess', 0) or 0)
        if failed and registered_success <= 0:
            first_failed=failed[0] if isinstance(failed,list) and failed else {}
            reason=first_failed.get('statusMessage') or first_failed.get('reason') or '발송 접수 실패'
            raise RuntimeError(reason)
        group_id=group.get('groupId') or group.get('_id') or initial_message.get('groupId') or ''
        message_id=initial_message.get('messageId') or ''
        conn.execute("""UPDATE notification_queue SET status='발송접수', sent_at=?, error_message='',
                       provider_group_id=?,provider_message_id=?,provider_response=?,last_attempt_at=?,attempt_count=COALESCE(attempt_count,0)+1 WHERE id=?""",
                     (now,group_id,message_id,json.dumps(result,ensure_ascii=False)[:10000],now,nid))
        conn.commit(); conn.close()
        return True, f'SOLAPI 발송 접수 완료' + (f' · 그룹 {group_id}' if group_id else '')
    except Exception as e:
        conn.execute("""UPDATE notification_queue SET status='발송실패',error_message=?,last_attempt_at=?,attempt_count=COALESCE(attempt_count,0)+1 WHERE id=?""",
                     (str(e)[:1000],now,nid))
        conn.commit(); conn.close()
        return False,str(e)


def process_pending_notifications(limit=10):
    conn=db(); config=notification_provider_config(conn)
    if config['provider'] != 'SOLAPI' or not config['auto_send']:
        conn.close(); return (0,0)
    ids=[r['id'] for r in conn.execute("""SELECT n.id FROM notification_queue n JOIN reservations r ON r.id=n.reservation_id
        WHERE n.status='발송대기' AND r.order_no NOT LIKE 'TEST-%' ORDER BY n.id LIMIT ?""",(max(1,min(int(limit),50)),)).fetchall()]
    conn.close()
    ok=fail=0
    for nid in ids:
        success,_=send_notification_now(nid)
        ok += 1 if success else 0
        fail += 0 if success else 1
    return ok,fail


def solapi_connection_test(phone):
    conn=db(); config=notification_provider_config(conn); conn.close()
    secret=load_notification_secrets()
    if config['provider'] != 'SOLAPI':
        raise ValueError('제공업체를 SOLAPI로 설정해주세요.')
    if not secret['api_key'] or not secret['api_secret']:
        raise ValueError('API Key/Secret을 먼저 저장해주세요.')
    sender=normalize_phone(config['sender_phone']); to=normalize_phone(phone)
    if not sender or not to:
        raise ValueError('등록 발신번호와 테스트 수신번호를 확인해주세요.')
    return solapi_send_payload(secret['api_key'],secret['api_secret'],{
        'messages':[{'to':to,'from':sender,'text':'[코스튬 대여몰] SOLAPI 문자 연동 테스트입니다.','autoTypeDetect':True}],
        'strict':True,'showMessageList':True
    })

NOTIFICATION_EVENT_LABELS = {
    '주문접수':'주문접수', '결제완료':'결제완료', '발송완료':'발송완료',
    '회수예정':'회수예정', '반납완료':'반납완료', '주문취소':'주문취소'
}


def notification_message(template, order):
    values = {
        'customer_name': order['customer_name'] or '',
        'order_no': order['order_no'] or '',
        'start_date': order['start_date'] or '',
        'end_date': order['end_date'] or '',
        'dispatch_date': order['dispatch_date'] or '-',
        'pickup_date': order['pickup_date'] or '-',
        'final_amount': f"{int(order['final_amount'] or 0):,}",
        'card_amount': f"{card_payment_amount(order):,}",
        'deposit_amount': f"{int(order['deposit_total'] or 0):,}",
        'carrier': order['outbound_carrier'] or '',
        'tracking_no': order['outbound_tracking_no'] or '',
    }
    try:
        return (template or '').format(**values)
    except (KeyError, ValueError):
        return template or ''


def queue_notification(conn, reservation_id, event_type, event_key=None):
    tpl=conn.execute('SELECT * FROM notification_templates WHERE event_type=?',(event_type,)).fetchone()
    if not tpl or not tpl['is_active']:
        return None
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(reservation_id,)).fetchone()
    if not order or not (order['phone'] or '').strip():
        return None
    key=event_key or f'{reservation_id}:{event_type}'
    message=notification_message(tpl['message_template'],order)
    cur=conn.execute('INSERT OR IGNORE INTO notification_queue (reservation_id,event_type,event_key,channel,recipient_name,recipient_phone,message,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (reservation_id,event_type,key,tpl['channel'],order['customer_name'],order['phone'],message,'발송대기',datetime.now().strftime('%Y-%m-%d %H:%M')))
    return cur.lastrowid if cur.rowcount else None


def queue_due_pickup_notifications(conn):
    # 별도 스케줄러가 없는 로컬 버전에서는 앱 요청이 들어올 때 회수 하루 전/당일 알림을 대기열에 생성합니다.
    today=date.today()
    targets=((today+timedelta(days=1)).isoformat(), today.isoformat())
    rows=conn.execute("SELECT id,pickup_date FROM reservations WHERE pickup_date IN (?,?) AND payment_status='결제완료' AND status IN ('예약','대여중') AND return_status<>'회수완료'",targets).fetchall()
    created=0
    for row in rows:
        if queue_notification(conn,row['id'],'회수예정',f"{row['id']}:회수예정:{row['pickup_date']}"):
            created+=1
    return created


def normalize_phone(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def current_member():
    member_id = session.get('member_user_id')
    if not member_id:
        return None
    conn = db()
    row = conn.execute('SELECT id,name,phone,phone_key,email,zipcode,address1,address2,is_active,last_login_at FROM member_accounts WHERE id=?', (member_id,)).fetchone()
    conn.close()
    if not row or not row['is_active']:
        session.pop('member_user_id', None)
        return None
    return row


def member_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_member():
            flash('로그인이 필요한 메뉴입니다.')
            return redirect(url_for('member_login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def safe_member_next_url(value):
    value = (value or '').strip()
    if value.startswith('/') and not value.startswith('//') and not value.startswith('/admin'):
        return value
    return url_for('mypage')


def current_admin():
    admin_id = session.get('admin_user_id')
    if not admin_id:
        return None
    conn = db()
    row = conn.execute('SELECT id,username,display_name,must_change_password,is_active,last_login_at FROM admin_users WHERE id=?', (admin_id,)).fetchone()
    conn.close()
    if not row or not row['is_active']:
        session.pop('admin_user_id', None)
        return None
    return row


@app.before_request
def generate_due_notification_queue():
    if request.endpoint == 'static':
        return
    conn=db()
    try:
        created=queue_due_pickup_notifications(conn)
        if created:
            conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


_LAST_NOTIFICATION_PUMP = None

@app.before_request
def auto_send_notification_queue():
    global _LAST_NOTIFICATION_PUMP
    if request.endpoint in {'static','solapi_message_webhook'}:
        return
    now=datetime.now()
    if _LAST_NOTIFICATION_PUMP and (now-_LAST_NOTIFICATION_PUMP).total_seconds() < 60:
        return
    _LAST_NOTIFICATION_PUMP=now
    try:
        process_pending_notifications(limit=5)
    except Exception:
        # 고객 요청 자체가 메시지업체 장애 때문에 실패하지 않도록 알림 발송 오류는 큐에만 기록합니다.
        pass


_LAST_RESULT_SYNC = None

@app.before_request
def auto_sync_notification_results():
    global _LAST_RESULT_SYNC
    # 결과조회는 외부 API 호출이므로 고객 화면을 느리게 하지 않도록 관리자 페이지 요청에서만 실행합니다.
    if request.endpoint in {'static','solapi_message_webhook','admin_login'} or not request.path.startswith('/admin'):
        return
    conn = db()
    config = notification_provider_config(conn)
    conn.close()
    if config['provider'] != 'SOLAPI' or not config.get('auto_result_sync'):
        return
    now = datetime.now()
    if _LAST_RESULT_SYNC and (now - _LAST_RESULT_SYNC).total_seconds() < 120:
        return
    _LAST_RESULT_SYNC = now
    try:
        sync_pending_notification_results(limit=5)
    except Exception:
        pass


@app.before_request
def protect_admin_area():
    # /admin/login 과 정적 리소스는 인증 없이 접근할 수 있습니다.
    if request.path.startswith('/admin') and request.endpoint not in {'admin_login', 'admin_logout', 'static'}:
        admin = current_admin()
        if not admin:
            next_url = request.full_path if request.query_string else request.path
            return redirect(url_for('admin_login', next=next_url))
        g.admin_user = admin
        if admin['must_change_password'] and request.endpoint != 'admin_password':
            flash('보안을 위해 최초 비밀번호를 먼저 변경해주세요.')
            return redirect(url_for('admin_password'))


@app.context_processor
def inject_user_context():
    admin = getattr(g, 'admin_user', None) or current_admin()
    member = current_member()
    conn=db()
    deposit_bank={
        'bank_name': get_text_setting(conn,'deposit_bank_name',''),
        'account_number': get_text_setting(conn,'deposit_account_number',''),
        'account_holder': get_text_setting(conn,'deposit_account_holder',''),
    }
    conn.close()
    return {'admin_user': admin, 'member_user': member, 'deposit_bank': deposit_bank, 'card_payment_amount': card_payment_amount}


def safe_next_url(value):
    value = (value or '').strip()
    if value.startswith('/admin') and not value.startswith('//'):
        return value
    return url_for('dashboard')


@app.route('/policy/<doc_type>')
def legal_policy(doc_type):
    if doc_type not in {'terms','privacy','rental'}:
        return '문서를 찾을 수 없습니다.',404
    conn=db(); doc=get_legal_document(conn,doc_type)
    cancellation=cancellation_policy_summary(conn) if doc_type=='rental' else ''
    conn.close()
    if not doc:
        return '문서를 찾을 수 없습니다.',404
    return render_template('legal_policy.html',doc=doc,doc_type=doc_type,cancellation_policy=cancellation)


@app.route('/admin/legal-documents', methods=['GET','POST'])
def admin_legal_documents():
    conn=db(); valid={'terms','privacy','rental'}
    if request.method=='POST':
        doc_type=request.form.get('doc_type','').strip()
        title=request.form.get('title','').strip(); version=request.form.get('version','').strip(); content=request.form.get('content','').strip()
        if doc_type not in valid or not title or not version or not content:
            conn.close(); flash('문서 종류, 제목, 버전, 내용을 모두 입력해주세요.'); return redirect(url_for('admin_legal_documents'))
        conn.execute('UPDATE legal_documents SET title=?,version=?,content=?,updated_at=? WHERE doc_type=?',
                     (title,version,content,datetime.now().strftime('%Y-%m-%d %H:%M'),doc_type))
        conn.commit(); conn.close(); flash('약관 문서를 저장했습니다. 이후 신규 동의부터 새 버전이 기록됩니다.')
        return redirect(url_for('admin_legal_documents',edit=doc_type))
    docs=legal_documents_map(conn)
    counts={r['consent_type']:r['c'] for r in conn.execute('SELECT consent_type,COUNT(*) c FROM consent_records GROUP BY consent_type').fetchall()}
    edit=request.args.get('edit','terms')
    if edit not in valid: edit='terms'
    conn.close()
    return render_template('legal_admin.html',docs=docs,counts=counts,edit=edit)


@app.route('/member/signup', methods=['GET','POST'])
def member_signup():
    if current_member():
        return redirect(url_for('mypage'))
    if request.method == 'POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); phone_key=normalize_phone(phone)
        email=request.form.get('email','').strip(); password=request.form.get('password',''); confirm=request.form.get('confirm_password','')
        zipcode=request.form.get('zipcode','').strip(); address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip()
        agree_terms=request.form.get('agree_terms')=='1'; agree_privacy=request.form.get('agree_privacy')=='1'
        if not agree_terms or not agree_privacy:
            flash('회원가입을 위해 이용약관과 개인정보 수집·이용에 모두 동의해주세요.'); return render_template('member_signup.html', form=request.form)
        if not name or len(phone_key) < 8:
            flash('이름과 연락처를 확인해주세요.'); return render_template('member_signup.html', form=request.form)
        if len(password) < 8:
            flash('비밀번호는 8자 이상으로 입력해주세요.'); return render_template('member_signup.html', form=request.form)
        if password != confirm:
            flash('비밀번호 확인이 일치하지 않습니다.'); return render_template('member_signup.html', form=request.form)
        conn=db()
        if conn.execute('SELECT id FROM member_accounts WHERE phone_key=?',(phone_key,)).fetchone():
            conn.close(); flash('이미 가입된 연락처입니다. 로그인해주세요.'); return redirect(url_for('member_login'))
        created=datetime.now().strftime('%Y-%m-%d %H:%M')
        cur=conn.execute('''INSERT INTO member_accounts(name,phone,phone_key,email,password_hash,zipcode,address1,address2,is_active,created_at)
                            VALUES(?,?,?,?,?,?,?,?,1,?)''',(name,phone,phone_key,email,generate_password_hash(password),zipcode,address1,address2,created))
        member_id=cur.lastrowid
        docs=legal_documents_map(conn)
        record_consent(conn,'회원가입_이용약관',docs.get('terms'),member_id=member_id,phone=phone)
        record_consent(conn,'회원가입_개인정보',docs.get('privacy'),member_id=member_id,phone=phone)
        before=conn.total_changes
        conn.execute("UPDATE reservations SET member_id=? WHERE member_id IS NULL AND REPLACE(REPLACE(phone,'-',''),' ','')=?",(member_id,phone_key))
        linked=conn.total_changes-before
        conn.commit(); conn.close()
        session['member_user_id']=member_id; session.permanent=True
        flash(f'회원가입이 완료되었습니다. 기존 주문 {linked}건도 마이페이지에 연결했습니다.')
        return redirect(url_for('mypage'))
    return render_template('member_signup.html', form={})


@app.route('/member/login', methods=['GET','POST'])
def member_login():
    if current_member():
        return redirect(url_for('mypage'))
    next_url=request.values.get('next','')
    if request.method == 'POST':
        phone=request.form.get('phone','').strip(); password=request.form.get('password',''); phone_key=normalize_phone(phone)
        conn=db(); member=conn.execute('SELECT * FROM member_accounts WHERE phone_key=? AND is_active=1',(phone_key,)).fetchone()
        if not member or not check_password_hash(member['password_hash'],password):
            conn.close(); flash('연락처 또는 비밀번호가 올바르지 않습니다.'); return render_template('member_login.html', phone=phone, next_url=next_url)
        login_at=datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.execute('UPDATE member_accounts SET last_login_at=? WHERE id=?',(login_at,member['id']))
        conn.execute("UPDATE reservations SET member_id=? WHERE member_id IS NULL AND REPLACE(REPLACE(phone,'-',''),' ','')=?",(member['id'],phone_key))
        conn.commit(); conn.close()
        session['member_user_id']=member['id']; session.permanent=True
        flash(f'{member["name"]}님, 로그인했습니다.')
        return redirect(safe_member_next_url(next_url))
    return render_template('member_login.html', phone='', next_url=next_url)


@app.route('/member/logout', methods=['POST'])
def member_logout():
    session.pop('member_user_id',None)
    flash('로그아웃되었습니다.')
    return redirect(url_for('home'))


@app.route('/mypage')
@member_required
def mypage():
    member=current_member(); conn=db()
    orders=conn.execute('''SELECT r.*,
        (SELECT p.name FROM reservation_items ri JOIN products p ON p.id=ri.product_id WHERE ri.reservation_id=r.id ORDER BY ri.id LIMIT 1) first_product,
        (SELECT COUNT(*) FROM reservation_items ri WHERE ri.reservation_id=r.id) item_count,
        (SELECT COALESCE(SUM(ri.qty),0) FROM reservation_items ri WHERE ri.reservation_id=r.id) item_qty
        FROM reservations r WHERE r.member_id=? ORDER BY r.id DESC''',(member['id'],)).fetchall()
    summary=conn.execute('''SELECT COUNT(*) order_count,
        COALESCE(SUM(CASE WHEN status IN ('예약','대여중','세탁/검수중') THEN 1 ELSE 0 END),0) active_count,
        COALESCE(SUM(CASE WHEN status='반납완료' THEN 1 ELSE 0 END),0) completed_count,
        COALESCE(SUM(CASE WHEN refund_status IN ('환급예정','부분환급') THEN refund_amount ELSE 0 END),0) refund_pending
        FROM reservations WHERE member_id=?''',(member['id'],)).fetchone()
    conn.close()
    return render_template('mypage.html',member=member,orders=orders,summary=summary,today=date.today().isoformat())


@app.route('/mypage/profile', methods=['GET','POST'])
@member_required
def mypage_profile():
    member=current_member()
    if request.method == 'POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); phone_key=normalize_phone(phone)
        email=request.form.get('email','').strip(); zipcode=request.form.get('zipcode','').strip(); address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip()
        current_password=request.form.get('current_password',''); new_password=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if not name or len(phone_key)<8:
            flash('이름과 연락처를 확인해주세요.'); return redirect(request.url)
        conn=db(); full=conn.execute('SELECT * FROM member_accounts WHERE id=?',(member['id'],)).fetchone()
        duplicate=conn.execute('SELECT id FROM member_accounts WHERE phone_key=? AND id<>?',(phone_key,member['id'])).fetchone()
        if duplicate:
            conn.close(); flash('다른 회원이 사용 중인 연락처입니다.'); return redirect(request.url)
        if new_password:
            if not current_password or not check_password_hash(full['password_hash'],current_password):
                conn.close(); flash('비밀번호 변경을 위해 현재 비밀번호를 확인해주세요.'); return redirect(request.url)
            if len(new_password)<8 or new_password!=confirm:
                conn.close(); flash('새 비밀번호는 8자 이상이며 확인값과 같아야 합니다.'); return redirect(request.url)
            conn.execute('UPDATE member_accounts SET password_hash=? WHERE id=?',(generate_password_hash(new_password),member['id']))
        conn.execute('UPDATE member_accounts SET name=?,phone=?,phone_key=?,email=?,zipcode=?,address1=?,address2=? WHERE id=?',(name,phone,phone_key,email,zipcode,address1,address2,member['id']))
        conn.execute("UPDATE reservations SET member_id=? WHERE member_id IS NULL AND REPLACE(REPLACE(phone,'-',''),' ','')=?",(member['id'],phone_key))
        conn.commit(); conn.close(); flash('회원정보를 수정했습니다.'); return redirect(url_for('mypage_profile'))
    return render_template('mypage_profile.html',member=member)


@app.route('/mypage/order/<int:rid>')
@member_required
def mypage_order(rid):
    member=current_member(); conn=db()
    order=conn.execute('SELECT * FROM reservations WHERE id=? AND member_id=?',(rid,member['id'])).fetchone()
    if not order:
        conn.close(); return '주문을 찾을 수 없습니다.',404
    order=expire_payment_if_needed(conn,order)
    items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
    history=conn.execute('SELECT * FROM payment_transactions WHERE reservation_id=? ORDER BY id DESC LIMIT 5',(rid,)).fetchall()
    adjustments=conn.execute('SELECT * FROM payment_adjustments WHERE reservation_id=? ORDER BY id DESC',(rid,)).fetchall()
    cancel_info=cancellation_quote(conn,order) if order['status']!='취소' else None
    cancel_policy_summary=cancellation_policy_summary(conn)
    conn.close(); return render_template('mypage_order.html',order=order,items=items,history=history,adjustments=adjustments,cancel_info=cancel_info,cancel_policy_summary=cancel_policy_summary)


@app.route('/mypage/order/<int:rid>/cancel', methods=['POST'])
@member_required
def mypage_order_cancel(rid):
    member=current_member(); conn=db(); order=conn.execute('SELECT * FROM reservations WHERE id=? AND member_id=?',(rid,member['id'])).fetchone()
    if not order:
        conn.close(); flash('주문을 찾을 수 없습니다.'); return redirect(url_for('mypage'))
    if order['status']!='예약' or order['shipping_status']!='발송전':
        conn.close(); flash('발송 전 예약 상태의 주문만 직접 취소할 수 있습니다.'); return redirect(url_for('mypage_order',rid=rid))
    quote=None
    if order['payment_status']=='결제완료':
        quote=queue_cancel_refund(conn,order,'회원고객')
        conn.execute("UPDATE reservations SET status='취소' WHERE id=?",(rid,))
    else:
        conn.execute("UPDATE reservations SET status='취소',payment_status='결제취소',payment_expires_at='',cancelled_at=? WHERE id=?",(datetime.now().strftime('%Y-%m-%d %H:%M'),rid))
    queue_notification(conn,rid,'주문취소')
    conn.commit(); conn.close()
    if quote is not None:
        flash(f"예약을 취소했습니다. 취소수수료 {quote['fee']:,}원 공제 후 {quote['refund']:,}원이 환불 대기로 등록되었습니다.")
    else:
        flash('결제 전 예약을 취소했습니다. 점유 재고도 즉시 해제되었습니다.')
    return redirect(url_for('mypage_order',rid=rid))


@app.route('/mypage/order/<int:rid>/change', methods=['GET','POST'])
@member_required
def mypage_order_change(rid):
    member=current_member(); conn=db(); order=conn.execute('SELECT * FROM reservations WHERE id=? AND member_id=?',(rid,member['id'])).fetchone()
    if not order:
        conn.close(); return '주문을 찾을 수 없습니다.',404
    items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
    if order['status']!='예약' or order['shipping_status']!='발송전':
        conn.close(); flash('발송 전 예약 상태의 주문만 변경할 수 있습니다.'); return redirect(url_for('mypage_order',rid=rid))
    if request.method=='POST':
        start=request.form.get('start_date','').strip()
        try: days=max(1,int(request.form.get('rental_days','1') or 1)); start_d=datetime.strptime(start,'%Y-%m-%d').date(); end=(start_d+timedelta(days=days-1)).isoformat()
        except (ValueError,TypeError):
            conn.close(); flash('대여 시작일과 기간을 확인해주세요.'); return redirect(request.url)
        try: conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            conn.close(); flash('다른 주문이 처리 중입니다. 잠시 후 다시 시도해주세요.'); return redirect(request.url)
        order=conn.execute('SELECT * FROM reservations WHERE id=? AND member_id=?',(rid,member['id'])).fetchone()
        items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
        total=0
        for item in items:
            avail=available_stock(conn,item['size_id'],start,end,exclude_reservation_id=rid)
            if item['qty']>avail:
                conn.rollback(); conn.close(); flash(f'{item["name"]} / {item["size_name"]}는 선택 기간에 {avail}벌만 가능합니다.'); return redirect(request.url)
            line=rental_line_price(item['daily_price'],item['extra_daily_price'],days,item['qty']); total+=line
            conn.execute('UPDATE reservation_items SET rental_days=?,total_price=? WHERE id=?',(days,line,item['id']))
        dispatch,pickup=auto_logistics_dates(conn,start,end); final=total+order['shipping_fee']+order['return_shipping_fee']+order['deposit_total']
        adjustment_type=''; adjustment_amount=0
        if order['payment_status']=='결제완료':
            adjustment_type,adjustment_amount=queue_amount_adjustment(conn,order,max(0,final-int(order['deposit_total'] or 0)),'회원 대여기간 변경','회원고객')
        conn.execute('UPDATE reservations SET start_date=?,end_date=?,dispatch_date=?,pickup_date=?,rental_days=?,total_price=?,final_amount=? WHERE id=?',(start,end,dispatch,pickup,days,total,final,rid))
        conn.commit(); conn.close()
        if adjustment_type=='추가결제': flash(f'대여기간을 변경했습니다. 추가결제 {adjustment_amount:,}원이 필요합니다.')
        elif adjustment_type=='부분환불': flash(f'대여기간을 변경했습니다. 부분환불 {adjustment_amount:,}원이 등록되었습니다.')
        else: flash('대여기간을 변경했습니다.')
        return redirect(url_for('mypage_order',rid=rid))
    conn.close(); return render_template('mypage_order_change.html',order=order,items=items,today=date.today().isoformat())


@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if current_admin():
        return redirect(url_for('dashboard'))
    next_url = request.values.get('next','')
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        conn = db()
        user = conn.execute('SELECT * FROM admin_users WHERE username=? AND is_active=1', (username,)).fetchone()
        if not user or not check_password_hash(user['password_hash'], password):
            conn.close()
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return render_template('admin_login.html', next_url=next_url, username=username)
        login_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.execute('UPDATE admin_users SET last_login_at=? WHERE id=?', (login_at, user['id']))
        conn.commit(); conn.close()
        session['admin_user_id'] = user['id']
        session.permanent = True
        if user['must_change_password']:
            flash('최초 로그인입니다. 새 비밀번호로 변경해주세요.')
            return redirect(url_for('admin_password'))
        flash(f'{user["display_name"] or user["username"]}님, 로그인했습니다.')
        return redirect(safe_next_url(next_url))
    return render_template('admin_login.html', next_url=next_url, username='')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_user_id', None)
    flash('관리자 로그아웃되었습니다.')
    return redirect(url_for('admin_login'))


@app.route('/admin/password', methods=['GET','POST'])
def admin_password():
    admin = current_admin()
    if not admin:
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        current_password = request.form.get('current_password','')
        new_password = request.form.get('new_password','')
        confirm_password = request.form.get('confirm_password','')
        conn = db()
        full = conn.execute('SELECT * FROM admin_users WHERE id=?', (admin['id'],)).fetchone()
        if not full or not check_password_hash(full['password_hash'], current_password):
            conn.close(); flash('현재 비밀번호가 올바르지 않습니다.'); return redirect(url_for('admin_password'))
        if len(new_password) < 8:
            conn.close(); flash('새 비밀번호는 8자 이상으로 입력해주세요.'); return redirect(url_for('admin_password'))
        if new_password != confirm_password:
            conn.close(); flash('새 비밀번호 확인이 일치하지 않습니다.'); return redirect(url_for('admin_password'))
        if check_password_hash(full['password_hash'], new_password):
            conn.close(); flash('현재 비밀번호와 다른 비밀번호를 사용해주세요.'); return redirect(url_for('admin_password'))
        conn.execute('UPDATE admin_users SET password_hash=?, must_change_password=0 WHERE id=?',
                     (generate_password_hash(new_password), admin['id']))
        conn.commit(); conn.close()
        flash('관리자 비밀번호를 변경했습니다.')
        return redirect(url_for('dashboard'))
    return render_template('admin_password.html', admin=admin)

@app.route('/')
def home():
    q = request.args.get('q','').strip()
    category = request.args.get('category','').strip()
    sort = request.args.get('sort','newest').strip()
    try: page = max(1, int(request.args.get('page','1')))
    except ValueError: page = 1
    per_page = 12
    sort_sql = {
        'newest':'p.id DESC', 'name':'p.name ASC',
        'price_low':'p.daily_price ASC, p.id DESC',
        'price_high':'p.daily_price DESC, p.id DESC'
    }.get(sort, 'p.id DESC')
    conn = db()
    where = ['p.is_active=1']
    params = []
    if q:
        where.append('(p.name LIKE ? OR p.description LIKE ?)')
        params.extend([f'%{q}%', f'%{q}%'])
    if category:
        where.append('p.category=?')
        params.append(category)
    where_sql=' AND '.join(where)
    total = conn.execute(f'SELECT COUNT(*) c FROM products p WHERE {where_sql}', params).fetchone()['c']
    total_pages = max(1, (total + per_page - 1)//per_page)
    page = min(page, total_pages)
    products = conn.execute(f'''
        SELECT p.*, COALESCE(SUM(ps.stock),0) total_stock,
               GROUP_CONCAT(ps.size_name, ', ') sizes_text
        FROM products p LEFT JOIN product_sizes ps ON ps.product_id=p.id
        WHERE {where_sql}
        GROUP BY p.id ORDER BY {sort_sql}
        LIMIT ? OFFSET ?
    ''', params + [per_page, (page-1)*per_page]).fetchall()
    categories = [r['category'] for r in conn.execute("SELECT DISTINCT category FROM products WHERE is_active=1 AND category<>'' ORDER BY category").fetchall()]
    conn.close()
    return render_template('home.html', products=products, categories=categories, q=q, selected_category=category, sort=sort, page=page, total=total, total_pages=total_pages)



@app.route('/product/<int:product_id>')
def product_detail(product_id):
    conn=db(); product,sizes=get_product(conn,product_id)
    if not product or not product['is_active']:
        conn.close(); return '현재 볼 수 없는 상품입니다.',404
    images=conn.execute('SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order,id',(product_id,)).fetchall()
    conn.close()
    return render_template('product_detail.html',product=product,sizes=sizes,images=images,today=date.today().isoformat())


@app.route('/product/<int:product_id>/quick-rent', methods=['POST'])
def product_quick_rent(product_id):
    conn=db(); product,sizes=get_product(conn,product_id)
    if not product or not product['is_active']:
        conn.close(); return '현재 대여할 수 없는 상품입니다.',404
    try:
        size_id=int(request.form.get('size_id','0') or 0)
        qty=max(1,int(request.form.get('qty','1') or 1))
    except ValueError:
        conn.close(); flash('사이즈와 수량을 확인해주세요.'); return redirect(url_for('product_detail',product_id=product_id))
    start=request.form.get('start_date','').strip()
    try:
        days=max(1,int(request.form.get('rental_days','1') or 1))
    except ValueError:
        days=1
    try:
        end=(datetime.strptime(start,'%Y-%m-%d').date()+timedelta(days=days-1)).isoformat() if start else ''
    except ValueError:
        end=''
    action=request.form.get('action','cart')
    ps=conn.execute('SELECT * FROM product_sizes WHERE id=? AND product_id=?',(size_id,product_id)).fetchone()
    if not ps or not start or not end or start>end:
        conn.close(); flash('대여기간과 사이즈를 확인해주세요.'); return redirect(url_for('product_detail',product_id=product_id))
    try:
        days=rental_days(start,end)
    except ValueError:
        conn.close(); flash('대여 날짜를 확인해주세요.'); return redirect(url_for('product_detail',product_id=product_id))
    avail=available_stock(conn,size_id,start,end)
    conn.close()
    if qty>avail:
        flash(f'선택한 기간의 {ps["size_name"]} 사이즈 예약 가능 수량은 {avail}벌입니다.')
        return redirect(url_for('product_detail',product_id=product_id))
    item={'product_id':product_id,'size_id':size_id,'qty':qty,'start_date':start,'end_date':end,'rental_days':days}
    session['rental_start']=start; session['rental_end']=end
    if action=='direct':
        session['direct_checkout_item']=item
        session.modified=True
        return redirect(url_for('checkout',mode='direct'))
    cart=session.get('cart',[])
    if cart:
        cstart=cart[0].get('start_date') or session.get('rental_start','')
        cend=cart[0].get('end_date') or session.get('rental_end','')
        if cstart and (cstart != start or cend != end):
            flash(f'장바구니 상품은 같은 대여기간으로 주문합니다. 현재 장바구니 기간은 {cstart} ~ {cend}입니다.')
            return redirect(url_for('product_detail',product_id=product_id))
    for x in cart:
        if x['product_id']==product_id and x['size_id']==size_id:
            combined=x['qty']+qty
            conn2=db(); total_av=available_stock(conn2,size_id,start,end); conn2.close()
            if combined>total_av:
                flash(f'선택한 기간에는 최대 {total_av}벌까지 대여할 수 있어 추가할 수 없습니다.')
                return redirect(url_for('product_detail',product_id=product_id))
            x['qty']=combined; x.update({'start_date':start,'end_date':end,'rental_days':days})
            break
    else:
        cart.append(item)
    session['cart']=cart; session.modified=True
    flash(f'대여기간 {start} ~ {end} ({days}일) 상품을 장바구니에 담았습니다.')
    return redirect(url_for('cart_view'))


@app.route('/reserve/<int:product_id>', methods=['GET','POST'])
def reserve(product_id):
    conn = db()
    product, sizes = get_product(conn, product_id)
    if not product or not product['is_active']:
        conn.close()
        return '현재 대여할 수 없는 상품입니다.', 404
    if request.method == 'GET':
        conn.close()
        return redirect(url_for('product_detail',product_id=product_id))
    if request.method == 'POST':
        customer = request.form.get('customer_name','').strip()
        phone = request.form.get('phone','').strip()
        start = request.form.get('start_date','')
        end = request.form.get('end_date','')
        size_id = int(request.form.get('size_id','0') or 0)
        qty = int(request.form.get('qty','1') or 1)
        memo = request.form.get('memo','').strip()
        recipient_name = request.form.get('recipient_name','').strip() or customer
        zipcode = request.form.get('zipcode','').strip()
        address1 = request.form.get('address1','').strip()
        address2 = request.form.get('address2','').strip()
        size_row = conn.execute('SELECT * FROM product_sizes WHERE id=? AND product_id=?', (size_id, product_id)).fetchone()
        if not customer or not phone or not start or not end or start > end or not size_row or not recipient_name or not address1:
            flash('입력 내용을 확인해주세요.')
            conn.close(); return redirect(request.url)
        try:
            days = rental_days(start, end)
        except ValueError:
            flash('대여 날짜를 확인해주세요.')
            conn.close(); return redirect(request.url)
        avail = available_stock(conn, size_id, start, end)
        if qty < 1 or qty > avail:
            flash(f'해당 기간 {size_row["size_name"]} 사이즈 예약 가능 수량은 {avail}벌입니다.')
            conn.close(); return redirect(request.url)
        total = rental_line_price(product['daily_price'], product['extra_daily_price'], days, qty)
        fees = get_fees(conn)
        shipping_fee = fees['outbound_shipping_fee']
        return_shipping_fee = fees['return_shipping_fee']
        deposit_total = product['deposit'] * qty
        final_amount = total + shipping_fee + return_shipping_fee + deposit_total
        created = datetime.now().strftime('%Y-%m-%d %H:%M')
        start_d = datetime.strptime(start,'%Y-%m-%d').date(); end_d = datetime.strptime(end,'%Y-%m-%d').date()
        dispatch_date, pickup_date = auto_logistics_dates(conn, start, end)
        cur = conn.execute('''INSERT INTO reservations
            (product_id,size_id,order_no,customer_name,phone,start_date,end_date,dispatch_date,pickup_date,qty,rental_days,total_price,status,shipping_status,return_status,memo,created_at,recipient_name,zipcode,address1,address2,shipping_fee,return_shipping_fee,deposit_total,final_amount)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (product_id,size_id,'',customer,phone,start,end,dispatch_date,pickup_date,qty,days,total,'예약','발송전','회수전',memo,created,recipient_name,zipcode,address1,address2,shipping_fee,return_shipping_fee,deposit_total,final_amount))
        rid = cur.lastrowid
        conn.execute('INSERT INTO reservation_items(reservation_id,product_id,size_id,qty,daily_price,extra_daily_price,rental_days,total_price,deposit_total) VALUES(?,?,?,?,?,?,?,?,?)',(rid,product_id,size_id,qty,product['daily_price'],product['extra_daily_price'],days,total,deposit_total))
        ono = order_no(rid, created)
        payment_token=secrets.token_urlsafe(24); payment_expires_at=(datetime.now()+timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        conn.execute("UPDATE reservations SET order_no=?,payment_status='결제대기',order_source='온라인',deposit_received=0,refund_amount=0,payment_token=?,payment_updated_at=?,payment_expires_at=? WHERE id=?", (ono,payment_token,created,payment_expires_at,rid))
        queue_notification(conn,rid,'주문접수')
        conn.commit(); conn.close()
        flash(f'예약이 등록되었습니다. 주문번호 {ono} / 카드결제 {total + shipping_fee + return_shipping_fee:,}원 / 보증금 계좌입금 {deposit_total:,}원')
        return redirect(url_for('payment_page',token=payment_token))
    today = date.today().isoformat()
    fees = get_fees(conn)
    conn.close()
    return render_template('reserve.html', product=product, sizes=sizes, today=today, outbound_fee=fees['outbound_shipping_fee'], return_fee=fees['return_shipping_fee'])


@app.route('/availability/<int:size_id>')
def availability(size_id):
    start, end = request.args.get('start'), request.args.get('end')
    if not start or not end: return jsonify({'available': 0})
    conn = db(); a = available_stock(conn, size_id, start, end); conn.close()
    return jsonify({'available': a})


@app.route('/availability-calendar/<int:size_id>')
def availability_calendar(size_id):
    days = min(max(int(request.args.get('days', '60')), 7), 120)
    start_q = request.args.get('start','').strip()
    try:
        first = datetime.strptime(start_q,'%Y-%m-%d').date() if start_q else date.today()
    except ValueError:
        first = date.today()
    conn = db()
    s = conn.execute('SELECT * FROM product_sizes WHERE id=?', (size_id,)).fetchone()
    if not s:
        conn.close(); return jsonify([])
    result = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        avail = available_stock(conn, size_id, d, d)
        result.append({'date': d, 'available': avail, 'sold_out': avail <= 0})
    conn.close(); return jsonify(result)


@app.route('/cart-availability-calendar')
def cart_availability_calendar():
    cart=session.get('cart',[])
    days=min(max(int(request.args.get('days','42')),7),120)
    start_q=request.args.get('start','').strip()
    try:
        first=datetime.strptime(start_q,'%Y-%m-%d').date() if start_q else date.today()
    except ValueError:
        first=date.today()
    conn=db(); result=[]
    valid=[]
    for x in cart:
        ps=conn.execute('SELECT ps.*,p.name FROM product_sizes ps JOIN products p ON p.id=ps.product_id WHERE ps.id=? AND p.is_active=1',(x['size_id'],)).fetchone()
        if ps: valid.append((x,ps))
    for i in range(days):
        d=(first+timedelta(days=i)).isoformat(); shortages=[]; min_spare=None
        for x,ps in valid:
            av=available_stock(conn,ps['id'],d,d); spare=av-int(x['qty'])
            min_spare=spare if min_spare is None else min(min_spare,spare)
            if av<int(x['qty']): shortages.append({'name':ps['name'],'size':ps['size_name'],'need':int(x['qty']),'available':av})
        result.append({'date':d,'available':len(shortages)==0 and len(valid)>0,'shortages':shortages,'min_spare':min_spare if min_spare is not None else 0})
    conn.close(); return jsonify(result)


@app.route('/cart')
def cart_view():
    cart=session.get('cart',[]); conn=db(); rows=[]
    period = {'start': cart[0].get('start_date','') if cart else '', 'end': cart[0].get('end_date','') if cart else '', 'days': cart[0].get('rental_days',1) if cart else 1}
    rental_total=0; deposit_total=0
    for i,item in enumerate(cart):
        p=conn.execute('SELECT * FROM products WHERE id=? AND is_active=1',(item['product_id'],)).fetchone(); ps=conn.execute('SELECT * FROM product_sizes WHERE id=?',(item['size_id'],)).fetchone()
        if p and ps:
            qty=int(item.get('qty',1)); days=int(item.get('rental_days') or period['days'] or 1)
            line_total=rental_line_price(p['daily_price'],p['extra_daily_price'],days,qty)
            line_deposit=int(p['deposit'] or 0)*qty
            rental_total+=line_total; deposit_total+=line_deposit
            rows.append({'index':i,'product':p,'size':ps,'qty':qty,'line_total':line_total,'deposit_total':line_deposit})
    fees=get_fees(conn); shipping_total=fees['outbound_shipping_fee']+fees['return_shipping_fee'] if rows else 0
    summary={'rental_total':rental_total,'deposit_total':deposit_total,'shipping_total':shipping_total,'final_total':rental_total+deposit_total+shipping_total}
    conn.close(); return render_template('cart.html',rows=rows,period=period,summary=summary)

@app.route('/cart/add/<int:product_id>',methods=['GET','POST'])
def cart_add(product_id):
    flash('대여 시작일과 대여일수를 먼저 선택해주세요.')
    return redirect(url_for('product_detail', product_id=product_id))

@app.route('/cart/update', methods=['POST'])
def cart_update():
    cart=session.get('cart',[])
    conn=db()
    for i,item in enumerate(cart):
        try: qty=max(1,int(request.form.get(f'qty_{i}',item.get('qty',1))))
        except ValueError: qty=item.get('qty',1)
        ps=conn.execute('SELECT stock FROM product_sizes WHERE id=?',(item['size_id'],)).fetchone()
        if ps:
            st=item.get('start_date') or session.get('rental_start',''); en=item.get('end_date') or session.get('rental_end','')
            av=available_stock(conn,item['size_id'],st,en) if st and en else ps['stock']
            item['qty']=min(qty,max(1,av))
    conn.close(); session['cart']=cart; session.modified=True; flash('장바구니 수량을 수정했습니다.'); return redirect(url_for('cart_view'))

@app.route('/cart/remove/<int:index>',methods=['POST'])
def cart_remove(index):
    cart=session.get('cart',[])
    if 0<=index<len(cart): cart.pop(index)
    session['cart']=cart; session.modified=True; return redirect(url_for('cart_view'))

@app.route('/checkout',methods=['GET','POST'])
def checkout():
    member = current_member()
    direct_mode = request.args.get('mode') == 'direct' or request.form.get('mode') == 'direct'
    if direct_mode:
        direct_item=session.get('direct_checkout_item')
        cart=[direct_item] if direct_item else []
    else:
        cart=session.get('cart',[])
    if not cart:
        flash('예약할 상품이 없습니다.')
        return redirect(url_for('home'))
    conn=db(); rows=[]
    for x in cart:
        p=conn.execute('SELECT * FROM products WHERE id=? AND is_active=1',(x['product_id'],)).fetchone()
        ps=conn.execute('SELECT * FROM product_sizes WHERE id=? AND product_id=?',(x['size_id'],x['product_id'])).fetchone()
        if p and ps:
            rows.append((x,p,ps))
    if len(rows) != len(cart):
        conn.close(); flash('장바구니에 현재 대여할 수 없는 상품이 포함되어 있습니다.')
        return redirect(url_for('cart_view'))
    if request.method=='POST':
        customer=request.form.get('customer_name','').strip(); phone=request.form.get('phone','').strip()
        start=request.form.get('start_date',''); end=request.form.get('end_date','')
        recipient=request.form.get('recipient_name','').strip() or customer; zipcode=request.form.get('zipcode','').strip()
        address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip(); memo=request.form.get('memo','').strip()
        agree_terms=request.form.get('agree_terms')=='1'; agree_privacy=request.form.get('agree_privacy')=='1'; agree_rental=request.form.get('agree_rental')=='1'
        if not (agree_terms and agree_privacy and agree_rental):
            conn.close(); flash('주문을 위해 이용약관, 개인정보 처리, 대여·파손·분실 규정에 모두 동의해주세요.'); return redirect(request.url)
        if not customer or not phone or not start or not end or start>end or not address1:
            conn.close(); flash('주문 정보를 확인해주세요.'); return redirect(request.url)
        expected_start=cart[0].get('start_date','') if cart else ''; expected_end=cart[0].get('end_date','') if cart else ''
        if expected_start and (start!=expected_start or end!=expected_end):
            conn.close(); flash('장바구니에서 확정한 대여기간과 주문기간이 다릅니다.'); return redirect(url_for('cart_view'))
        try:
            days=rental_days(start,end)
            conn.execute('BEGIN IMMEDIATE')
        except ValueError:
            conn.close(); flash('대여 날짜를 확인해주세요.'); return redirect(request.url)
        except sqlite3.OperationalError:
            conn.close(); flash('다른 주문이 처리 중입니다. 잠시 후 다시 주문해주세요.'); return redirect(url_for('cart_view'))
        rt=dt=0; vals=[]
        for x,p,ps in rows:
            av=available_stock(conn,ps['id'],start,end)
            if int(x['qty'])>av:
                conn.rollback(); conn.close()
                flash(f'{p["name"]} / {ps["size_name"]} 재고가 방금 소진되었습니다. 현재 예약 가능 수량은 {av}벌입니다.')
                return redirect(url_for('cart_view'))
            line=rental_line_price(p['daily_price'],p['extra_daily_price'],days,x['qty'])
            dep=p['deposit']*int(x['qty']); rt+=line; dt+=dep; vals.append((x,p,ps,line,dep))
        fees=get_fees(conn); sf=fees['outbound_shipping_fee']; rf=fees['return_shipping_fee']; final=rt+dt+sf+rf
        created=datetime.now().strftime('%Y-%m-%d %H:%M'); dispatch,pickup=auto_logistics_dates(conn,start,end)
        x0,p0,ps0,_,_=vals[0]
        payment_token=secrets.token_urlsafe(24); payment_expires_at=(datetime.now()+timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        cur=conn.execute('''INSERT INTO reservations(product_id,size_id,order_no,customer_name,phone,start_date,end_date,dispatch_date,pickup_date,qty,rental_days,total_price,status,shipping_status,return_status,memo,created_at,recipient_name,zipcode,address1,address2,shipping_fee,return_shipping_fee,deposit_total,final_amount,payment_status,payment_method,paid_at,order_source,deposit_received,refund_amount,member_id,payment_token,payment_updated_at,payment_expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (p0['id'],ps0['id'],'',customer,phone,start,end,dispatch,pickup,x0['qty'],days,rt,'예약','발송전','회수전',memo,created,recipient,zipcode,address1,address2,sf,rf,dt,final,'결제대기','','','온라인',0,0,(member['id'] if member else None),payment_token,created,payment_expires_at))
        rid=cur.lastrowid; ono=order_no(rid,created); conn.execute('UPDATE reservations SET order_no=? WHERE id=?',(ono,rid))
        for x,p,ps,line,dep in vals:
            conn.execute('INSERT INTO reservation_items(reservation_id,product_id,size_id,qty,daily_price,extra_daily_price,rental_days,total_price,deposit_total) VALUES(?,?,?,?,?,?,?,?,?)',(rid,p['id'],ps['id'],x['qty'],p['daily_price'],p['extra_daily_price'],days,line,dep))
        docs=legal_documents_map(conn); mid=(member['id'] if member else None)
        record_consent(conn,'주문_이용약관',docs.get('terms'),member_id=mid,reservation_id=rid,phone=phone)
        record_consent(conn,'주문_개인정보',docs.get('privacy'),member_id=mid,reservation_id=rid,phone=phone)
        rental_snapshot=(docs.get('rental')['content'] if docs.get('rental') else '') + '\n\n[주문 당시 취소·환불 규정]\n' + cancellation_policy_summary(conn)
        record_consent(conn,'주문_대여규정',docs.get('rental'),member_id=mid,reservation_id=rid,phone=phone,content_override=rental_snapshot)
        queue_notification(conn,rid,'주문접수')
        conn.commit(); conn.close()
        if direct_mode: session.pop('direct_checkout_item',None)
        else: session['cart']=[]
        session.pop('rental_start',None); session.pop('rental_end',None); session.modified=True
        return redirect(url_for('payment_page',token=payment_token))
    fees=get_fees(conn); selected_start=(cart[0].get('start_date','') if cart else '') or session.get('rental_start','')
    selected_end=(cart[0].get('end_date','') if cart else '') or session.get('rental_end','')
    selected_days=(cart[0].get('rental_days',1) if cart else 1)
    rental_total=sum(rental_line_price(p['daily_price'],p['extra_daily_price'],selected_days,x['qty']) for x,p,ps in rows)
    deposit_total=sum(p['deposit']*int(x['qty']) for x,p,ps in rows)
    expected_total=rental_total+deposit_total+fees['outbound_shipping_fee']+fees['return_shipping_fee']
    cancel_policy_summary=cancellation_policy_summary(conn)
    docs=legal_documents_map(conn)
    conn.close()
    return render_template('checkout.html',rows=rows,today=date.today().isoformat(),fees=fees,selected_start=selected_start,selected_end=selected_end,selected_days=selected_days,direct_mode=direct_mode,rental_total=rental_total,deposit_total=deposit_total,expected_total=expected_total,member=member,cancel_policy_summary=cancel_policy_summary,docs=docs)


PAYMENT_METHODS = ['신용/체크카드','계좌이체','카카오페이','네이버페이','토스페이']


@app.route('/payment/<token>')
def payment_page(token):
    conn=db(); order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return '결제 주문을 찾을 수 없습니다.',404
    order=expire_payment_if_needed(conn,order)
    items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(order['id'],)).fetchall()
    history=conn.execute('SELECT * FROM payment_transactions WHERE reservation_id=? ORDER BY id DESC LIMIT 8',(order['id'],)).fetchall()
    pay_config=payment_provider_config(conn)
    customer_key=member_toss_customer_key(conn,order['member_id']) if pay_config['mode']=='TOSS_TEST' else 'ANONYMOUS'
    conn.commit(); conn.close()
    return render_template('payment.html',order=order,items=items,methods=PAYMENT_METHODS,history=history,pay_config=pay_config,toss_customer_key=customer_key)


@app.route('/payment/<token>/process', methods=['POST'])
def payment_process(token):
    """MOCK 모드용 기존 결제 시뮬레이션. TOSS_TEST에서는 SDK/승인 API만 사용합니다."""
    action=request.form.get('action','success'); method=request.form.get('payment_method','').strip()
    conn=db(); config=payment_provider_config(conn)
    if config['mode']!='MOCK':
        conn.close(); flash('토스 테스트 모드에서는 토스 결제창의 결제하기 버튼을 이용해주세요.'); return redirect(url_for('payment_page',token=token))
    if method not in PAYMENT_METHODS:
        conn.close(); flash('결제수단을 선택해주세요.'); return redirect(url_for('payment_page',token=token))
    order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return '결제 주문을 찾을 수 없습니다.',404
    order=expire_payment_if_needed(conn,order)
    if order['status']=='취소' or order['payment_status']=='결제취소':
        conn.close(); flash('이미 취소된 주문입니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    if order['payment_status']=='결제완료':
        conn.close(); flash('이미 결제가 완료된 주문입니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    if action=='fail':
        now=datetime.now().strftime('%Y-%m-%d %H:%M')
        reason='시뮬레이션 결제 실패'
        conn.execute("UPDATE reservations SET payment_status='결제실패',payment_method=?,payment_failure_reason=?,payment_updated_at=?,payment_expires_at='' WHERE id=?",(method,reason,now,order['id']))
        log_payment_transaction(conn,order['id'],method,card_payment_amount(order),'실패',reason,provider='MOCK')
        conn.commit(); conn.close(); flash('시뮬레이션 결제를 실패 상태로 처리했습니다. 재고 점유는 해제됩니다.')
        return redirect(url_for('payment_page',token=token))
    try:
        conn.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError:
        conn.close(); flash('다른 주문이 처리 중입니다. 다시 시도해주세요.'); return redirect(url_for('payment_page',token=token))
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(order['id'],)).fetchone()
    items=conn.execute('SELECT * FROM reservation_items WHERE reservation_id=?',(order['id'],)).fetchall()
    for item in items:
        av=available_stock(conn,item['size_id'],order['start_date'],order['end_date'],exclude_reservation_id=order['id'])
        if item['qty']>av:
            conn.rollback(); conn.close(); flash('결제 직전 재고가 소진된 품목이 있어 결제를 완료할 수 없습니다. 다른 날짜를 선택해주세요.')
            return redirect(url_for('payment_page',token=token))
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    pay_amount=card_payment_amount(order)
    conn.execute("""UPDATE reservations SET payment_status='결제완료',payment_method=?,paid_at=?,payment_failure_reason='',payment_updated_at=?,payment_expires_at='',paid_order_amount=? WHERE id=?""",
                 (method,now,now,pay_amount,order['id']))
    log_payment_transaction(conn,order['id'],method,pay_amount,'성공','시뮬레이션 결제완료 (보증금 별도 계좌입금)',provider='MOCK')
    queue_notification(conn,order['id'],'결제완료')
    conn.commit(); conn.close(); flash('시뮬레이션 결제가 완료되었습니다.')
    return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))


@app.route('/payment/<token>/toss/prepare', methods=['POST'])
def toss_payment_prepare(token):
    conn=db(); config=payment_provider_config(conn)
    if not config['ready']:
        conn.close(); return jsonify({'ok':False,'message':'관리자에서 토스 테스트 키와 결제모드를 먼저 설정해주세요.'}),400
    order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return jsonify({'ok':False,'message':'주문을 찾을 수 없습니다.'}),404
    if order['status']=='취소' or order['payment_status']=='결제취소':
        conn.close(); return jsonify({'ok':False,'message':'취소된 주문입니다.'}),400
    if order['payment_status']=='결제완료':
        conn.close(); return jsonify({'ok':False,'message':'이미 결제가 완료된 주문입니다.'}),400
    if order['toss_payment_key'] and order['toss_payment_status']=='CONFIRM_ERROR':
        conn.close(); return jsonify({'ok':False,'message':'이전 토스 결제의 승인 결과 확인이 필요합니다. 중복결제 방지를 위해 새 결제를 시작하지 않습니다.'}),409
    try:
        conn.execute('BEGIN IMMEDIATE')
        order=conn.execute('SELECT * FROM reservations WHERE id=?',(order['id'],)).fetchone()
        items=conn.execute("SELECT ri.*,p.name FROM reservation_items ri JOIN products p ON p.id=ri.product_id WHERE ri.reservation_id=? ORDER BY ri.id",(order['id'],)).fetchall()
        for item in items:
            av=available_stock(conn,item['size_id'],order['start_date'],order['end_date'],exclude_reservation_id=order['id'])
            if int(item['qty'])>av:
                conn.rollback(); conn.close(); return jsonify({'ok':False,'message':f"{item['name']} 재고가 부족해 결제를 시작할 수 없습니다."}),409
        now=datetime.now(); expires=(now+timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M')
        toss_oid=make_toss_order_id('TOSS',order['id'],order['order_no'])
        idem=str(uuid.uuid4())
        conn.execute("""UPDATE reservations SET payment_status='결제대기',payment_expires_at=?,payment_updated_at=?,payment_failure_reason='',
                      toss_order_id=?,toss_payment_key='',toss_payment_status='READY',toss_last_error='',toss_confirm_idempotency_key=? WHERE id=?""",
                     (expires,now.strftime('%Y-%m-%d %H:%M'),toss_oid,idem,order['id']))
        customer_key=member_toss_customer_key(conn,order['member_id'])
        member_email=''
        if order['member_id']:
            mr=conn.execute('SELECT email FROM member_accounts WHERE id=?',(order['member_id'],)).fetchone()
            member_email=(mr['email'] or '').strip() if mr else ''
        conn.commit()
        result={
            'ok':True,'orderId':toss_oid,'amount':card_payment_amount(order),'orderName':order_name_for_toss(items),
            'customerKey':customer_key,'customerName':order['customer_name'],'customerEmail':member_email,
            'customerMobilePhone':normalize_mobile(order['phone']),
            'successUrl':url_for('toss_payment_success',token=token,_external=True),
            'failUrl':url_for('toss_payment_fail',token=token,_external=True),
        }
        conn.close(); return jsonify(result)
    except sqlite3.OperationalError:
        try: conn.rollback()
        except Exception: pass
        conn.close(); return jsonify({'ok':False,'message':'다른 주문이 처리 중입니다. 잠시 후 다시 시도해주세요.'}),409


@app.route('/payments/toss/success')
def toss_payment_success():
    token=request.args.get('token','').strip(); payment_key=request.args.get('paymentKey','').strip(); toss_oid=request.args.get('orderId','').strip()
    try: amount=int(request.args.get('amount','0') or 0)
    except ValueError: amount=0
    conn=db(); config=payment_provider_config(conn); order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return '결제 주문을 찾을 수 없습니다.',404
    if not config['ready']:
        conn.close(); flash('토스 결제 설정을 확인해주세요.'); return redirect(url_for('payment_page',token=token))
    if order['payment_status']=='결제완료' and order['toss_payment_key']==payment_key:
        conn.close(); flash('이미 승인된 결제입니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    if not payment_key or not toss_oid or toss_oid!=(order['toss_order_id'] or '') or amount!=card_payment_amount(order):
        reason='결제 승인정보 검증 실패: 주문번호 또는 금액이 일치하지 않습니다.'
        conn.execute("UPDATE reservations SET payment_status='결제실패',payment_failure_reason=?,toss_last_error=?,payment_expires_at='' WHERE id=?",(reason,reason,order['id']))
        log_payment_transaction(conn,order['id'],'토스페이먼츠',amount,'실패',reason,provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid)
        conn.commit(); conn.close(); flash(reason); return redirect(url_for('payment_page',token=token))
    try:
        conn.execute('BEGIN IMMEDIATE')
        order=conn.execute('SELECT * FROM reservations WHERE id=?',(order['id'],)).fetchone()
        if order['status']=='취소':
            conn.rollback(); conn.close(); flash('취소된 주문은 결제를 승인할 수 없습니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
        items=conn.execute('SELECT * FROM reservation_items WHERE reservation_id=?',(order['id'],)).fetchall()
        for item in items:
            av=available_stock(conn,item['size_id'],order['start_date'],order['end_date'],exclude_reservation_id=order['id'])
            if int(item['qty'])>av:
                reason='결제 승인 직전 재고가 부족해 승인을 중단했습니다.'
                conn.execute("UPDATE reservations SET payment_status='결제실패',payment_failure_reason=?,toss_last_error=?,payment_expires_at='' WHERE id=?",(reason,reason,order['id']))
                log_payment_transaction(conn,order['id'],'토스페이먼츠',amount,'실패',reason,provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid)
                conn.commit(); conn.close(); flash(reason); return redirect(url_for('payment_page',token=token))
        idem=(order['toss_confirm_idempotency_key'] or '').strip() or str(uuid.uuid4())
        data=toss_confirm_payment(config['secret_key'],payment_key,toss_oid,amount,idem)
        method=provider_payment_method(data); status=str(data.get('status') or 'DONE'); approved=str(data.get('approvedAt') or '')
        now=datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.execute("""UPDATE reservations SET payment_status='결제완료',payment_method=?,paid_at=?,payment_failure_reason='',payment_updated_at=?,payment_expires_at='',
                     paid_order_amount=?,toss_payment_key=?,toss_payment_status=?,toss_method=?,toss_approved_at=?,toss_receipt_url=?,toss_last_error='',toss_raw_json=? WHERE id=?""",
                     (f'토스/{method}',now,now,amount,payment_key,status,method,approved,toss_receipt_url(data),provider_raw(data),order['id']))
        log_payment_transaction(conn,order['id'],f'토스/{method}',amount,'성공','토스페이먼츠 승인완료',provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid,provider_status=status,provider_data=data)
        queue_notification(conn,order['id'],'결제완료')
        conn.commit(); conn.close(); flash('토스페이먼츠 테스트 결제가 승인되었습니다.')
        return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    except RuntimeError as e:
        try: conn.rollback()
        except Exception: pass
        msg=str(e); network=msg.startswith('TOSS_NETWORK:')
        conn.close(); conn=db(); now=datetime.now().strftime('%Y-%m-%d %H:%M')
        new_status='결제대기' if network else '결제실패'
        expires=(datetime.now()+timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M') if network else ''
        conn.execute("""UPDATE reservations SET payment_status=?,payment_failure_reason=?,payment_updated_at=?,payment_expires_at=?,toss_payment_key=?,toss_payment_status='CONFIRM_ERROR',toss_last_error=? WHERE id=?""",
                     (new_status,msg,now,expires,payment_key,msg,order['id']))
        log_payment_transaction(conn,order['id'],'토스페이먼츠',amount,'승인확인필요' if network else '실패',msg,provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid,provider_status='CONFIRM_ERROR')
        conn.commit(); conn.close(); flash('토스 결제 승인 처리 중 오류가 발생했습니다: '+msg[:180]); return redirect(url_for('payment_page',token=token))
    except sqlite3.OperationalError:
        try: conn.rollback()
        except Exception: pass
        conn.close(); flash('다른 주문이 처리 중입니다. 승인 페이지를 새로고침해 다시 확인해주세요.'); return redirect(request.url)


@app.route('/payments/toss/fail')
def toss_payment_fail():
    token=request.args.get('token','').strip(); code=request.args.get('code','PAYMENT_FAILED').strip(); message=request.args.get('message','결제가 완료되지 않았습니다.').strip(); toss_oid=request.args.get('orderId','').strip()
    conn=db(); order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return '결제 주문을 찾을 수 없습니다.',404
    # 과거 결제시도의 콜백이 늦게 온 경우 현재 새 시도를 실패시키지 않습니다.
    if toss_oid and order['toss_order_id'] and toss_oid!=order['toss_order_id']:
        conn.close(); flash('이전 결제 시도의 실패 응답입니다. 현재 주문은 그대로 유지됩니다.'); return redirect(url_for('payment_page',token=token))
    reason=f'{code}: {message}'[:500]; now=datetime.now().strftime('%Y-%m-%d %H:%M')
    conn.execute("UPDATE reservations SET payment_status='결제실패',payment_failure_reason=?,payment_updated_at=?,payment_expires_at='',toss_payment_status='AUTH_FAILED',toss_last_error=? WHERE id=?",(reason,now,reason,order['id']))
    log_payment_transaction(conn,order['id'],'토스페이먼츠',card_payment_amount(order),'실패',reason,provider='TOSS',provider_order_id=toss_oid or order['toss_order_id'],provider_status='AUTH_FAILED')
    conn.commit(); conn.close(); flash('결제가 완료되지 않았습니다. '+message[:150]); return redirect(url_for('payment_page',token=token))


@app.route('/payment/<token>/cancel', methods=['POST'])
def payment_cancel(token):
    conn=db(); order=conn.execute('SELECT * FROM reservations WHERE payment_token=?',(token,)).fetchone()
    if not order:
        conn.close(); return '결제 주문을 찾을 수 없습니다.',404
    if order['payment_status']=='결제완료':
        conn.close(); flash('결제완료 주문은 결제화면에서 취소할 수 없습니다. 주문 취소 후 환불 절차로 처리해주세요.')
        return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    if order['status']=='예약' and order['shipping_status']=='발송전':
        now=datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.execute("UPDATE reservations SET status='취소',payment_status='결제취소',payment_updated_at=?,payment_expires_at='' WHERE id=?",(now,order['id']))
        log_payment_transaction(conn,order['id'],order['payment_method'],card_payment_amount(order),'취소','고객이 결제 전 주문 취소')
        queue_notification(conn,order['id'],'주문취소')
        conn.commit(); conn.close(); flash('주문을 취소했습니다. 점유 재고가 즉시 해제됩니다.')
    else:
        conn.close(); flash('현재 상태에서는 결제 취소가 불가능합니다.')
    return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))


@app.route('/payment-adjustment/<token>', methods=['GET','POST'])
def payment_adjustment_page(token):
    conn=db()
    adj=conn.execute("SELECT * FROM payment_adjustments WHERE token=? AND adjustment_type='추가결제'",(token,)).fetchone()
    if not adj:
        conn.close(); return '추가결제 건을 찾을 수 없습니다.',404
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(adj['reservation_id'],)).fetchone()
    if not order:
        conn.close(); return '주문을 찾을 수 없습니다.',404
    config=payment_provider_config(conn)
    if request.method=='POST':
        # MOCK 모드에서는 기존 시뮬레이션 결제를 유지합니다.
        if config['mode']!='MOCK':
            conn.close(); flash('토스 테스트 모드에서는 토스 결제 UI의 결제하기 버튼을 이용해주세요.'); return redirect(url_for('payment_adjustment_page',token=token))
        method=request.form.get('payment_method','').strip(); action=request.form.get('action','success')
        if adj['status']!='처리대기':
            conn.close(); flash('이미 처리된 추가결제 건입니다.'); return redirect(url_for('payment_adjustment_page',token=token))
        if order['status']=='취소':
            conn.close(); flash('취소된 주문에는 추가결제를 진행할 수 없습니다.'); return redirect(url_for('payment_adjustment_page',token=token))
        if method not in PAYMENT_METHODS:
            conn.close(); flash('결제수단을 선택해주세요.'); return redirect(url_for('payment_adjustment_page',token=token))
        if action=='fail':
            log_payment_transaction(conn,order['id'],method,adj['amount'],'추가결제실패',adj['reason'] or '시뮬레이션 추가결제 실패',provider='MOCK')
            conn.commit(); conn.close(); flash('시뮬레이션 추가결제를 실패 상태로 기록했습니다. 다시 시도할 수 있습니다.')
            return redirect(url_for('payment_adjustment_page',token=token))
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            conn.close(); flash('다른 결제가 처리 중입니다. 다시 시도해주세요.'); return redirect(url_for('payment_adjustment_page',token=token))
        adj=conn.execute('SELECT * FROM payment_adjustments WHERE id=?',(adj['id'],)).fetchone()
        if not adj or adj['status']!='처리대기':
            conn.rollback(); conn.close(); flash('이미 처리된 추가결제 건입니다.'); return redirect(url_for('payment_adjustment_page',token=token))
        complete_payment_adjustment(conn,adj,method,'시뮬레이션 추가결제 완료',provider='MOCK')
        conn.commit(); conn.close(); flash('추가결제가 완료되었습니다.')
        return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(order['id'],)).fetchall()
    customer_key=member_toss_customer_key(conn,order['member_id']) if config['mode']=='TOSS_TEST' else 'ANONYMOUS'
    conn.commit(); conn.close()
    return render_template('payment_adjustment.html',order=order,adj=adj,items=items,methods=PAYMENT_METHODS,pay_config=config,toss_customer_key=customer_key)


@app.route('/payment-adjustment/<token>/toss/prepare', methods=['POST'])
def toss_adjustment_prepare(token):
    conn=db(); config=payment_provider_config(conn)
    if not config['ready']:
        conn.close(); return jsonify({'ok':False,'message':'관리자에서 토스 테스트 키와 결제모드를 먼저 설정해주세요.'}),400
    adj=conn.execute("SELECT * FROM payment_adjustments WHERE token=? AND adjustment_type='추가결제'",(token,)).fetchone()
    if not adj:
        conn.close(); return jsonify({'ok':False,'message':'추가결제 건을 찾을 수 없습니다.'}),404
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(adj['reservation_id'],)).fetchone()
    if not order or order['status']=='취소' or adj['status']!='처리대기':
        conn.close(); return jsonify({'ok':False,'message':'현재 추가결제를 진행할 수 없습니다.'}),400
    if adj['toss_payment_key'] and adj['toss_payment_status']=='CONFIRM_ERROR':
        conn.close(); return jsonify({'ok':False,'message':'이전 추가결제의 승인 결과 확인이 필요해 새 결제를 시작할 수 없습니다.'}),409
    toss_oid=make_toss_order_id('ADJ',adj['id'],order['order_no'])
    conn.execute("UPDATE payment_adjustments SET toss_order_id=?,toss_payment_key='',toss_payment_status='READY',toss_raw_json='' WHERE id=?",(toss_oid,adj['id']))
    customer_key=member_toss_customer_key(conn,order['member_id'])
    member_email=''
    if order['member_id']:
        mr=conn.execute('SELECT email FROM member_accounts WHERE id=?',(order['member_id'],)).fetchone()
        member_email=(mr['email'] or '').strip() if mr else ''
    conn.commit(); conn.close()
    return jsonify({
        'ok':True,'orderId':toss_oid,'amount':int(adj['amount']),'orderName':f'{order["order_no"]} 추가대여 결제'[:100],
        'customerKey':customer_key,'customerName':order['customer_name'],'customerEmail':member_email,'customerMobilePhone':normalize_mobile(order['phone']),
        'successUrl':url_for('toss_adjustment_success',token=token,_external=True),'failUrl':url_for('toss_adjustment_fail',token=token,_external=True)
    })


@app.route('/payments/toss/adjustment/success')
def toss_adjustment_success():
    token=request.args.get('token','').strip(); payment_key=request.args.get('paymentKey','').strip(); toss_oid=request.args.get('orderId','').strip()
    try: amount=int(request.args.get('amount','0') or 0)
    except ValueError: amount=0
    conn=db(); config=payment_provider_config(conn)
    adj=conn.execute("SELECT * FROM payment_adjustments WHERE token=? AND adjustment_type='추가결제'",(token,)).fetchone()
    if not adj:
        conn.close(); return '추가결제 건을 찾을 수 없습니다.',404
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(adj['reservation_id'],)).fetchone()
    if not config['ready']:
        conn.close(); flash('토스 결제 설정을 확인해주세요.'); return redirect(url_for('payment_adjustment_page',token=token))
    if adj['status']=='처리완료' and adj['toss_payment_key']==payment_key:
        conn.close(); flash('이미 승인된 추가결제입니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    if not payment_key or toss_oid!=(adj['toss_order_id'] or '') or amount!=int(adj['amount'] or 0):
        reason='추가결제 승인정보 검증 실패'
        conn.execute("UPDATE payment_adjustments SET toss_payment_status='VERIFY_FAILED',toss_raw_json=? WHERE id=?",(reason,adj['id']))
        log_payment_transaction(conn,order['id'],'토스페이먼츠',amount,'추가결제실패',reason,provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid)
        conn.commit(); conn.close(); flash(reason); return redirect(url_for('payment_adjustment_page',token=token))
    try:
        conn.execute('BEGIN IMMEDIATE')
        adj=conn.execute('SELECT * FROM payment_adjustments WHERE id=?',(adj['id'],)).fetchone()
        if not adj or adj['status']!='처리대기':
            conn.rollback(); conn.close(); flash('이미 처리된 추가결제입니다.'); return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
        idem=str(uuid.uuid5(uuid.NAMESPACE_URL,f'toss-adjustment-confirm-{adj["id"]}-{toss_oid}'))
        data=toss_confirm_payment(config['secret_key'],payment_key,toss_oid,amount,idem)
        method=provider_payment_method(data); status=str(data.get('status') or 'DONE')
        conn.execute("UPDATE payment_adjustments SET toss_payment_key=?,toss_payment_status=?,toss_raw_json=? WHERE id=?",(payment_key,status,provider_raw(data),adj['id']))
        complete_payment_adjustment(conn,adj,f'토스/{method}','토스페이먼츠 추가결제 승인완료',provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid,provider_status=status,provider_data=data)
        conn.commit(); conn.close(); flash('토스페이먼츠 테스트 추가결제가 승인되었습니다.')
        return redirect(url_for('order_complete',order_no=order['order_no'],phone=order['phone']))
    except RuntimeError as e:
        try: conn.rollback()
        except Exception: pass
        conn.close(); conn=db(); msg=str(e)
        conn.execute("UPDATE payment_adjustments SET toss_payment_key=?,toss_payment_status='CONFIRM_ERROR',toss_raw_json=? WHERE id=?",(payment_key,msg,adj['id']))
        log_payment_transaction(conn,order['id'],'토스페이먼츠',amount,'추가결제실패',msg,provider='TOSS',provider_payment_key=payment_key,provider_order_id=toss_oid,provider_status='CONFIRM_ERROR')
        conn.commit(); conn.close(); flash('추가결제 승인 오류: '+msg[:180]); return redirect(url_for('payment_adjustment_page',token=token))


@app.route('/payments/toss/adjustment/fail')
def toss_adjustment_fail():
    token=request.args.get('token','').strip(); code=request.args.get('code','PAYMENT_FAILED').strip(); message=request.args.get('message','추가결제가 완료되지 않았습니다.').strip(); toss_oid=request.args.get('orderId','').strip()
    conn=db(); adj=conn.execute("SELECT * FROM payment_adjustments WHERE token=? AND adjustment_type='추가결제'",(token,)).fetchone()
    if not adj:
        conn.close(); return '추가결제 건을 찾을 수 없습니다.',404
    order=conn.execute('SELECT * FROM reservations WHERE id=?',(adj['reservation_id'],)).fetchone(); reason=f'{code}: {message}'[:500]
    if not toss_oid or not adj['toss_order_id'] or toss_oid==adj['toss_order_id']:
        conn.execute("UPDATE payment_adjustments SET toss_payment_status='AUTH_FAILED',toss_raw_json=? WHERE id=?",(reason,adj['id']))
        log_payment_transaction(conn,order['id'],'토스페이먼츠',adj['amount'],'추가결제실패',reason,provider='TOSS',provider_order_id=toss_oid or adj['toss_order_id'],provider_status='AUTH_FAILED')
        conn.commit()
    conn.close(); flash('추가결제가 완료되지 않았습니다. '+message[:150]); return redirect(url_for('payment_adjustment_page',token=token))



def toss_refund_adjustment(conn, adjustment, config):
    """환불 조정액을 실제 토스 승인 건들에 나누어 부분취소합니다. 중간 실패 시 이미 성공한 금액은 DB에 남아 재시도에서 제외합니다."""
    if not adjustment or adjustment['adjustment_type'] not in ('부분환불','전액환불'):
        raise RuntimeError('환불 조정 건이 아닙니다.')
    if not config.get('ready'):
        raise RuntimeError('토스 테스트 결제 설정이 완료되지 않았습니다.')
    target=int(adjustment['amount'] or 0)
    already=int(adjustment['provider_refunded_amount'] or 0)
    remaining=max(0,target-already)
    if remaining<=0:
        return 0
    txs=conn.execute("""SELECT * FROM payment_transactions
        WHERE reservation_id=? AND provider='TOSS' AND COALESCE(provider_payment_key,'')<>''
          AND result IN ('성공','추가결제') AND amount>COALESCE(refunded_amount,0)
        ORDER BY id DESC""",(adjustment['reservation_id'],)).fetchall()
    if not txs:
        raise RuntimeError('환불 가능한 토스 결제 승인 건이 없습니다.')
    refunded_now=0
    last_raw=''
    for tx in txs:
        if remaining<=0: break
        available=max(0,int(tx['amount'] or 0)-int(tx['refunded_amount'] or 0))
        if available<=0: continue
        cancel_amount=min(remaining,available)
        idem=str(uuid.uuid5(uuid.NAMESPACE_URL,f'toss-refund-{adjustment["id"]}-{tx["id"]}-{tx["refunded_amount"]}-{cancel_amount}'))
        data=toss_cancel_payment(config['secret_key'],tx['provider_payment_key'],adjustment['reason'] or '주문 환불',cancel_amount,idem)
        status=str(data.get('status') or 'CANCELED')
        conn.execute("UPDATE payment_transactions SET refunded_amount=COALESCE(refunded_amount,0)+?,provider_status=?,provider_raw_json=? WHERE id=?",
                     (cancel_amount,status,provider_raw(data),tx['id']))
        refunded_now += cancel_amount; remaining -= cancel_amount; already += cancel_amount; last_raw=provider_raw(data)
        conn.execute("UPDATE payment_adjustments SET provider_refunded_amount=?,toss_payment_status=?,toss_raw_json=? WHERE id=?",
                     (already,'REFUNDING' if remaining>0 else 'REFUNDED',last_raw,adjustment['id']))
        conn.commit()
    if remaining>0:
        raise RuntimeError(f'토스 환불 가능 잔액이 부족합니다. 미처리 {remaining:,}원')
    return refunded_now


@app.route('/admin/payment-adjustments')
def payment_adjustments_admin():
    status=request.args.get('status','pending').strip(); kind=request.args.get('type','').strip(); q=request.args.get('q','').strip()
    conn=db(); where=['1=1']; params=[]
    if status=='pending': where.append("a.status='처리대기'")
    elif status=='done': where.append("a.status='처리완료'")
    elif status=='cancel': where.append("a.status='취소'")
    if kind in ('추가결제','부분환불','전액환불'):
        where.append('a.adjustment_type=?'); params.append(kind)
    if q:
        where.append('(r.order_no LIKE ? OR r.customer_name LIKE ? OR r.phone LIKE ?)'); params.extend([f'%{q}%']*3)
    rows=conn.execute(f"""SELECT a.*,r.order_no,r.customer_name,r.phone,r.status order_status,r.final_amount,
        (SELECT COUNT(*) FROM payment_transactions pt WHERE pt.reservation_id=a.reservation_id AND pt.provider='TOSS' AND COALESCE(pt.provider_payment_key,'')<>'') toss_charge_count
        FROM payment_adjustments a JOIN reservations r ON r.id=a.reservation_id
        WHERE {' AND '.join(where)} ORDER BY CASE a.status WHEN '처리대기' THEN 0 ELSE 1 END,a.id DESC""",params).fetchall()
    summary=conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN status='처리대기' AND adjustment_type='추가결제' THEN amount ELSE 0 END),0) extra_pending,
        COALESCE(SUM(CASE WHEN status='처리대기' AND adjustment_type IN ('부분환불','전액환불') THEN amount ELSE 0 END),0) refund_pending,
        COALESCE(SUM(CASE WHEN status='처리완료' AND adjustment_type='추가결제' THEN amount ELSE 0 END),0) extra_done,
        COALESCE(SUM(CASE WHEN status='처리완료' AND adjustment_type IN ('부분환불','전액환불') THEN amount ELSE 0 END),0) refund_done
        FROM payment_adjustments""").fetchone()
    pay_config=payment_provider_config(conn)
    conn.close(); return render_template('payment_adjustments.html',rows=rows,summary=summary,status=status,kind=kind,q=q,pay_config=pay_config)


@app.route('/admin/payment-adjustments/<int:aid>/complete', methods=['POST'])
def payment_adjustment_complete_admin(aid):
    conn=db(); adj=conn.execute('SELECT * FROM payment_adjustments WHERE id=?',(aid,)).fetchone()
    if not adj:
        conn.close(); return '결제 조정 건이 없습니다.',404
    if adj['status']!='처리대기':
        conn.close(); flash('이미 처리된 건입니다.'); return redirect(request.referrer or url_for('payment_adjustments_admin'))
    config=payment_provider_config(conn); memo=request.form.get('memo','').strip()
    if adj['adjustment_type'] in ('부분환불','전액환불'):
        toss_count=conn.execute("SELECT COUNT(*) c FROM payment_transactions WHERE reservation_id=? AND provider='TOSS' AND COALESCE(provider_payment_key,'')<>''",(adj['reservation_id'],)).fetchone()['c']
        if config['ready'] and toss_count:
            try:
                toss_refund_adjustment(conn,adj,config)
                adj=conn.execute('SELECT * FROM payment_adjustments WHERE id=?',(aid,)).fetchone()
                complete_payment_adjustment(conn,adj,'토스페이먼츠 환불',memo or '토스페이먼츠 테스트 취소 API 처리완료',provider='TOSS',provider_status='REFUNDED')
                conn.commit(); conn.close(); flash(f'{adj["adjustment_type"]} {int(adj["amount"]):,}원을 토스 테스트 결제에서 실제 취소 처리했습니다.')
                return redirect(request.referrer or url_for('payment_adjustments_admin'))
            except RuntimeError as e:
                # toss_refund_adjustment는 성공한 부분취소를 건별 커밋하므로 재시도하면 남은 금액만 처리합니다.
                conn.close(); flash('토스 환불 처리 오류: '+str(e)[:220]); return redirect(request.referrer or url_for('payment_adjustments_admin'))
    method=request.form.get('payment_method','관리자처리').strip() or '관리자처리'
    complete_payment_adjustment(conn,adj,method,memo or '관리자 수동 처리완료',provider='MOCK' if config['mode']=='MOCK' else 'MANUAL')
    conn.commit(); conn.close(); flash(f'{adj["adjustment_type"]} {int(adj["amount"]):,}원을 처리완료했습니다.')
    return redirect(request.referrer or url_for('payment_adjustments_admin'))


@app.route('/admin/payment-adjustments/<int:aid>/cancel', methods=['POST'])
def payment_adjustment_cancel_admin(aid):
    conn=db(); adj=conn.execute('SELECT * FROM payment_adjustments WHERE id=?',(aid,)).fetchone()
    if not adj:
        conn.close(); return '결제 조정 건이 없습니다.',404
    if adj['status']=='처리대기':
        conn.execute("UPDATE payment_adjustments SET status='취소',memo=? WHERE id=?",(request.form.get('memo','관리자 취소').strip() or '관리자 취소',aid)); conn.commit(); flash('미처리 결제 조정 건을 취소했습니다.')
    else:
        flash('처리대기 상태만 취소할 수 있습니다.')
    conn.close(); return redirect(request.referrer or url_for('payment_adjustments_admin'))


@app.route('/order-complete')
def order_complete():
    ono=request.args.get('order_no','').strip(); phone=request.args.get('phone','').strip()
    conn=db()
    order=conn.execute("SELECT * FROM reservations WHERE UPPER(order_no)=UPPER(?) AND REPLACE(REPLACE(phone,'-',''),' ','')=REPLACE(REPLACE(?,'-',''),' ','')",(ono,phone)).fetchone()
    items=[]; history=[]
    if order:
        order=expire_payment_if_needed(conn,order)
        items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(order['id'],)).fetchall()
        history=conn.execute('SELECT * FROM payment_transactions WHERE reservation_id=? ORDER BY id DESC LIMIT 5',(order['id'],)).fetchall()
        adjustments=conn.execute('SELECT * FROM payment_adjustments WHERE reservation_id=? ORDER BY id DESC',(order['id'],)).fetchall()
    else:
        adjustments=[]
    conn.close()
    if not order: return '주문을 찾을 수 없습니다.',404
    return render_template('order_complete.html',order=order,items=items,history=history,adjustments=adjustments)


@app.route('/order-lookup', methods=['GET','POST'])
def order_lookup():
    order = None
    order_no_q = request.values.get('order_no','').strip()
    phone_q = request.values.get('phone','').strip()
    if order_no_q and phone_q:
        conn = db()
        order = conn.execute("""SELECT r.*,p.name,p.image_filename,p.daily_price,p.deposit,
                       COALESCE(ps.size_name,p.size,'FREE') size_name
                FROM reservations r JOIN products p ON p.id=r.product_id
                LEFT JOIN product_sizes ps ON ps.id=r.size_id
                WHERE UPPER(r.order_no)=UPPER(?) AND REPLACE(REPLACE(r.phone,'-',''),' ','')=REPLACE(REPLACE(?,'-',''),' ','')""",
                (order_no_q, phone_q)).fetchone()
        items=[]; adjustments=[]
        cancel_info=None; cancel_policy_summary=''
        if order:
            order=expire_payment_if_needed(conn,order)
            items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(order['id'],)).fetchall()
            adjustments=conn.execute('SELECT * FROM payment_adjustments WHERE reservation_id=? ORDER BY id DESC',(order['id'],)).fetchall()
            cancel_info=cancellation_quote(conn,order) if order['status']!='취소' else None
            cancel_policy_summary=cancellation_policy_summary(conn)
        conn.close()
        if not order:
            flash('주문번호와 연락처가 일치하는 주문을 찾지 못했습니다.')
    else: items=[]; adjustments=[]; cancel_info=None; cancel_policy_summary=''
    return render_template('order_lookup.html', order=order, items=items, adjustments=adjustments, order_no_q=order_no_q, phone_q=phone_q, cancel_info=cancel_info, cancel_policy_summary=cancel_policy_summary)


@app.route('/order-cancel', methods=['POST'])
def order_cancel():
    ono = request.form.get('order_no','').strip()
    phone = request.form.get('phone','').strip()
    conn = db()
    row = conn.execute("""SELECT * FROM reservations WHERE UPPER(order_no)=UPPER(?)
        AND REPLACE(REPLACE(phone,'-',''),' ','')=REPLACE(REPLACE(?,'-',''),' ','')""",(ono,phone)).fetchone()
    if not row:
        conn.close(); flash('주문을 찾지 못했습니다.'); return redirect(url_for('order_lookup'))
    if row['status'] != '예약' or row['shipping_status'] != '발송전':
        conn.close(); flash('이미 출고가 진행되었거나 대여가 시작된 주문은 고객 화면에서 취소할 수 없습니다.'); return redirect(url_for('order_lookup',order_no=ono,phone=phone))
    quote=None
    if row['payment_status']=='결제완료':
        quote=queue_cancel_refund(conn,row,'비회원고객')
        conn.execute("UPDATE reservations SET status='취소' WHERE id=?",(row['id'],))
    else:
        conn.execute("UPDATE reservations SET status='취소',payment_status='결제취소',payment_expires_at='',cancelled_at=? WHERE id=?",(datetime.now().strftime('%Y-%m-%d %H:%M'),row['id']))
    queue_notification(conn,row['id'],'주문취소')
    conn.commit(); conn.close()
    if quote is not None:
        flash(f"예약을 취소했습니다. 취소수수료 {quote['fee']:,}원 공제 후 {quote['refund']:,}원이 환불 대기로 등록되었습니다.")
    else:
        flash('결제 전 예약을 취소했습니다. 점유되었던 재고도 즉시 해제됩니다.')
    return redirect(url_for('order_lookup',order_no=ono,phone=phone))


@app.route('/admin/products')
def products_admin():
    q = request.args.get('q','').strip()
    category = request.args.get('category','').strip()
    status = request.args.get('status','').strip()
    sort = request.args.get('sort','newest').strip()
    try: page = max(1, int(request.args.get('page','1')))
    except ValueError: page = 1
    per_page = 15
    sort_sql = {
        'newest':'p.id DESC', 'oldest':'p.id ASC', 'name':'p.name ASC',
        'stock_low':'total_stock ASC, p.id DESC', 'stock_high':'total_stock DESC, p.id DESC',
        'price_low':'p.daily_price ASC, p.id DESC', 'price_high':'p.daily_price DESC, p.id DESC'
    }.get(sort, 'p.id DESC')
    conn = db()
    where = ['1=1']
    params = []
    if q:
        where.append('(p.name LIKE ? OR p.description LIKE ?)')
        params.extend([f'%{q}%', f'%{q}%'])
    if category:
        where.append('p.category=?')
        params.append(category)
    if status == 'active': where.append('p.is_active=1')
    elif status == 'inactive': where.append('p.is_active=0')
    where_sql=' AND '.join(where)
    total = conn.execute(f'SELECT COUNT(*) c FROM products p WHERE {where_sql}', params).fetchone()['c']
    total_pages=max(1,(total+per_page-1)//per_page); page=min(page,total_pages)
    products = conn.execute(f'''
        SELECT p.*, COALESCE(SUM(ps.stock),0) total_stock,
               GROUP_CONCAT(ps.size_name || ' ' || ps.stock || '벌', ', ') size_stock_text,
               (SELECT COUNT(*) FROM reservations r WHERE r.product_id=p.id) reservation_count,
               COALESCE((SELECT SUM(r.qty) FROM reservations r WHERE r.product_id=p.id AND r.status='예약'),0) reserved_qty,
               COALESCE((SELECT SUM(r.qty) FROM reservations r WHERE r.product_id=p.id AND r.status='대여중'),0) renting_qty,
               COALESCE((SELECT SUM(r.qty) FROM reservations r WHERE r.product_id=p.id AND r.status='세탁/검수중'),0) cleaning_qty
        FROM products p LEFT JOIN product_sizes ps ON ps.product_id=p.id
        WHERE {where_sql}
        GROUP BY p.id ORDER BY {sort_sql}
        LIMIT ? OFFSET ?
    ''', params + [per_page,(page-1)*per_page]).fetchall()
    categories = [r['category'] for r in conn.execute("SELECT DISTINCT category FROM products WHERE category<>'' ORDER BY category").fetchall()]
    conn.close()
    return render_template('products.html', products=products, categories=categories, q=q, selected_category=category, selected_status=status, sort=sort, page=page, total=total, total_pages=total_pages)


@app.route('/admin/products/new', methods=['GET','POST'])
def new_product():
    conn = db()
    categories = [r['category'] for r in conn.execute("SELECT DISTINCT category FROM products WHERE category<>'' ORDER BY category").fetchall()]
    if request.method == 'POST':
        name = request.form.get('name','').strip(); category = request.form.get('category','').strip()
        try:
            daily_price = max(0, int(request.form.get('daily_price','0') or 0))
            extra_daily_price = max(0, int(request.form.get('extra_daily_price','0') or 0))
            deposit = max(0, int(request.form.get('deposit', str(get_setting(conn,'default_deposit',DEFAULT_DEPOSIT))) or 0))
        except ValueError:
            flash('가격을 숫자로 입력해주세요.'); conn.close(); return redirect(request.url)
        description = request.form.get('description','').strip(); sizes_raw = request.form.get('sizes','').strip()
        components=request.form.get('components','').strip(); size_guide=request.form.get('size_guide','').strip(); rental_notes=request.form.get('rental_notes','').strip()
        image_filename = save_image(request.files.get('image'))
        if not name or not category:
            flash('상품명과 카테고리를 입력해주세요.'); conn.close(); return redirect(request.url)
        cur = conn.execute('''INSERT INTO products(name,category,size,stock,daily_price,extra_daily_price,deposit,description,image_filename,is_active,components,size_guide,rental_notes)
                              VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)''', (name,category,'',0,daily_price,extra_daily_price,deposit,description,image_filename,components,size_guide,rental_notes))
        pid = cur.lastrowid
        parsed = []
        for item in sizes_raw.split(','):
            item = item.strip()
            if not item: continue
            if ':' in item:
                size_name, stock_text = item.split(':', 1)
                try: stock = max(0, int(stock_text.strip()))
                except ValueError: stock = 0
            else:
                size_name, stock = item, 1
            if size_name.strip(): parsed.append((pid, size_name.strip().upper(), stock))
        if not parsed: parsed = [(pid, 'FREE', 1)]
        conn.executemany('INSERT OR IGNORE INTO product_sizes(product_id,size_name,stock) VALUES(?,?,?)', parsed)
        for order,file in enumerate(request.files.getlist('gallery_images')):
            fn=save_image(file)
            if fn: conn.execute('INSERT INTO product_images(product_id,image_filename,sort_order,created_at) VALUES(?,?,?,?)',(pid,fn,order,datetime.now().strftime('%Y-%m-%d %H:%M')))
        conn.commit(); conn.close(); flash('상품이 등록되었습니다.')
        return redirect(url_for('products_admin'))
    default_deposit = get_setting(conn,'default_deposit',DEFAULT_DEPOSIT)
    conn.close()
    return render_template('product_new.html', categories=categories, default_deposit=default_deposit)


@app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
def edit_product(product_id):
    conn = db(); product, sizes = get_product(conn, product_id)
    if not product: conn.close(); return '상품이 없습니다.', 404
    if request.method == 'POST':
        name = request.form.get('name','').strip(); category=request.form.get('category','').strip()
        if not name or not category:
            flash('상품명과 카테고리는 필수입니다.'); conn.close(); return redirect(request.url)
        try:
            price=max(0,int(request.form.get('daily_price','0') or 0)); extra_price=max(0,int(request.form.get('extra_daily_price','0') or 0)); deposit=max(0,int(request.form.get('deposit','0') or 0))
        except ValueError:
            flash('가격을 숫자로 입력해주세요.'); conn.close(); return redirect(request.url)
        conn.execute('UPDATE products SET name=?,category=?,daily_price=?,extra_daily_price=?,deposit=?,description=?,components=?,size_guide=?,rental_notes=?,is_active=? WHERE id=?',
                     (name,category,price,extra_price,deposit,request.form.get('description','').strip(),request.form.get('components','').strip(),request.form.get('size_guide','').strip(),request.form.get('rental_notes','').strip(),1 if request.form.get('is_active')=='1' else 0,product_id))
        filename = save_image(request.files.get('image'))
        if filename:
            old = product['image_filename']; conn.execute('UPDATE products SET image_filename=? WHERE id=?',(filename,product_id))
            if old:
                try: (UPLOAD_DIR/old).unlink(missing_ok=True)
                except OSError: pass
        max_order=conn.execute('SELECT COALESCE(MAX(sort_order),-1) m FROM product_images WHERE product_id=?',(product_id,)).fetchone()['m']
        for idx,file in enumerate(request.files.getlist('gallery_images'),start=max_order+1):
            fn=save_image(file)
            if fn: conn.execute('INSERT INTO product_images(product_id,image_filename,sort_order,created_at) VALUES(?,?,?,?)',(product_id,fn,idx,datetime.now().strftime('%Y-%m-%d %H:%M')))
        conn.commit(); conn.close(); flash('상품 정보를 수정했습니다.'); return redirect(url_for('edit_product', product_id=product_id))
    images=conn.execute('SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order,id',(product_id,)).fetchall(); conn.close(); return render_template('product_edit.html', product=product, sizes=sizes, images=images)



@app.route('/admin/product-images/<int:image_id>/delete',methods=['POST'])
def delete_product_image(image_id):
    conn=db(); img=conn.execute('SELECT * FROM product_images WHERE id=?',(image_id,)).fetchone()
    if img:
        conn.execute('DELETE FROM product_images WHERE id=?',(image_id,)); conn.commit()
        try:(UPLOAD_DIR/img['image_filename']).unlink(missing_ok=True)
        except OSError:pass
        pid=img['product_id']
    else: pid=None
    conn.close(); flash('추가 이미지를 삭제했습니다.')
    return redirect(url_for('edit_product',product_id=pid)) if pid else redirect(url_for('products_admin'))


@app.route('/admin/products/<int:product_id>/sizes', methods=['POST'])
def update_sizes(product_id):
    conn = db(); rows = conn.execute('SELECT * FROM product_sizes WHERE product_id=?', (product_id,)).fetchall()
    for r in rows:
        try:
            if f'stock_{r["id"]}' in request.form:
                conn.execute('UPDATE product_sizes SET stock=? WHERE id=?',(max(0,int(request.form[f'stock_{r["id"]}'])),r['id']))
        except ValueError: pass
    new_size = request.form.get('new_size','').strip().upper()
    if new_size:
        try: ns=max(0,int(request.form.get('new_stock','0') or 0)); conn.execute('INSERT OR IGNORE INTO product_sizes(product_id,size_name,stock) VALUES(?,?,?)',(product_id,new_size,ns))
        except ValueError: pass
    conn.commit(); conn.close(); flash('사이즈별 재고를 수정했습니다.'); return redirect(url_for('edit_product', product_id=product_id))


@app.route('/admin/sizes/<int:size_id>/delete', methods=['POST'])
def delete_size(size_id):
    conn=db(); used=conn.execute('SELECT COUNT(*) c FROM reservations WHERE size_id=?',(size_id,)).fetchone()['c']
    if used: flash('예약 이력이 있는 사이즈는 삭제할 수 없습니다. 재고를 0으로 변경해주세요.')
    else: conn.execute('DELETE FROM product_sizes WHERE id=?',(size_id,)); conn.commit(); flash('사이즈를 삭제했습니다.')
    conn.close(); return redirect(request.referrer or url_for('products_admin'))


@app.route('/admin/products/<int:product_id>/toggle', methods=['POST'])
def toggle_product(product_id):
    conn=db(); p=conn.execute('SELECT is_active FROM products WHERE id=?',(product_id,)).fetchone()
    if p: conn.execute('UPDATE products SET is_active=? WHERE id=?',(0 if p['is_active'] else 1,product_id)); conn.commit()
    conn.close(); return redirect(url_for('products_admin'))


@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
def delete_product(product_id):
    conn=db(); count=conn.execute('SELECT COUNT(*) c FROM reservations WHERE product_id=?',(product_id,)).fetchone()['c']
    if count:
        conn.execute('UPDATE products SET is_active=0 WHERE id=?',(product_id,)); conn.commit(); flash('예약 이력이 있어 삭제 대신 대여중지 처리했습니다.')
    else:
        p=conn.execute('SELECT image_filename FROM products WHERE id=?',(product_id,)).fetchone()
        gallery=conn.execute('SELECT image_filename FROM product_images WHERE product_id=?',(product_id,)).fetchall()
        conn.execute('DELETE FROM product_images WHERE product_id=?',(product_id,)); conn.execute('DELETE FROM product_sizes WHERE product_id=?',(product_id,)); conn.execute('DELETE FROM products WHERE id=?',(product_id,)); conn.commit()
        files=[p['image_filename']] if p and p['image_filename'] else []
        files += [g['image_filename'] for g in gallery]
        for fn in files:
            try:(UPLOAD_DIR/fn).unlink(missing_ok=True)
            except OSError:pass
        flash('상품을 삭제했습니다.')
    conn.close(); return redirect(url_for('products_admin'))


@app.route('/admin/reservations/new', methods=['GET','POST'])
def reservation_new_admin():
    conn=db()
    options=conn.execute("SELECT ps.id size_id,ps.size_name,ps.stock,p.id product_id,p.name,p.daily_price,p.extra_daily_price,p.deposit FROM product_sizes ps JOIN products p ON p.id=ps.product_id WHERE p.is_active=1 ORDER BY p.name,ps.id").fetchall()
    if request.method=='POST':
        customer=request.form.get('customer_name','').strip(); phone=request.form.get('phone','').strip()
        recipient=request.form.get('recipient_name','').strip() or customer; zipcode=request.form.get('zipcode','').strip()
        address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip(); memo=request.form.get('memo','').strip()
        start=request.form.get('start_date','').strip(); payment_status=request.form.get('payment_status','결제대기'); payment_method=request.form.get('payment_method','').strip()
        try:
            days=max(1,int(request.form.get('rental_days','1') or 1)); start_d=datetime.strptime(start,'%Y-%m-%d').date(); end=(start_d+timedelta(days=days-1)).isoformat()
        except (ValueError,TypeError):
            conn.close(); flash('대여 시작일과 대여일수를 확인해주세요.'); return redirect(request.url)
        if not customer or not phone or not address1:
            conn.close(); flash('고객명, 연락처, 주소를 입력해주세요.'); return redirect(request.url)
        if payment_status not in ['결제대기','결제완료']: payment_status='결제대기'
        requested={}
        for i in range(5):
            raw=request.form.get(f'size_id_{i}','').strip()
            if not raw: continue
            try: sid=int(raw); qty=max(1,int(request.form.get(f'qty_{i}','1') or 1))
            except ValueError: continue
            requested[sid]=requested.get(sid,0)+qty
        if not requested:
            conn.close(); flash('대여 상품을 한 개 이상 선택해주세요.'); return redirect(request.url)
        try: conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            conn.close(); flash('다른 주문이 처리 중입니다. 잠시 후 다시 시도해주세요.'); return redirect(request.url)
        vals=[]; rt=dt=0
        for sid,qty in requested.items():
            row=conn.execute("SELECT ps.*,p.name,p.daily_price,p.extra_daily_price,p.deposit,p.is_active,p.id product_id FROM product_sizes ps JOIN products p ON p.id=ps.product_id WHERE ps.id=?",(sid,)).fetchone()
            if not row or not row['is_active']:
                conn.rollback(); conn.close(); flash('선택한 상품 중 현재 대여할 수 없는 상품이 있습니다.'); return redirect(request.url)
            av=available_stock(conn,sid,start,end)
            if qty>av:
                conn.rollback(); conn.close(); flash(f'{row["name"]} / {row["size_name"]} 예약 가능 수량은 {av}벌입니다.'); return redirect(request.url)
            line=rental_line_price(row['daily_price'],row['extra_daily_price'],days,qty); dep=row['deposit']*qty
            rt+=line; dt+=dep; vals.append((row,qty,line,dep))
        fees=get_fees(conn); sf=fees['outbound_shipping_fee']; rf=fees['return_shipping_fee']; final=rt+dt+sf+rf
        dispatch,pickup=auto_logistics_dates(conn,start,end); created=datetime.now().strftime('%Y-%m-%d %H:%M'); first,first_qty,_,_=vals[0]
        paid_at=created if payment_status=='결제완료' else ''; received=0; refund=0
        cur=conn.execute('''INSERT INTO reservations(product_id,size_id,order_no,customer_name,phone,start_date,end_date,dispatch_date,pickup_date,qty,rental_days,total_price,status,shipping_status,return_status,memo,created_at,recipient_name,zipcode,address1,address2,shipping_fee,return_shipping_fee,deposit_total,final_amount,payment_status,payment_method,paid_at,order_source,deposit_received,refund_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (first['product_id'],first['id'],'',customer,phone,start,end,dispatch,pickup,first_qty,days,rt,'예약','발송전','회수전',memo,created,recipient,zipcode,address1,address2,sf,rf,dt,final,payment_status,payment_method,paid_at,'관리자수동',received,refund))
        rid=cur.lastrowid; ono=order_no(rid,created); conn.execute('UPDATE reservations SET order_no=? WHERE id=?',(ono,rid))
        for row,qty,line,dep in vals:
            conn.execute('INSERT INTO reservation_items(reservation_id,product_id,size_id,qty,daily_price,extra_daily_price,rental_days,total_price,deposit_total) VALUES(?,?,?,?,?,?,?,?,?)',(rid,row['product_id'],row['id'],qty,row['daily_price'],row['extra_daily_price'],days,line,dep))
        if payment_status=='결제완료':
            conn.execute('UPDATE reservations SET paid_order_amount=MAX(0,final_amount-deposit_total) WHERE id=?',(rid,))
        queue_notification(conn,rid,'주문접수')
        if payment_status=='결제완료':
            queue_notification(conn,rid,'결제완료')
        conn.commit(); conn.close(); flash(f'관리자 수동 주문 {ono}를 등록했습니다.')
        return redirect(url_for('reservation_detail',rid=rid))
    conn.close()
    return render_template('reservation_new.html',options=options,today=date.today().isoformat())


@app.route('/admin/reservations')
def reservations():
    status=request.args.get('status','').strip(); shipping=request.args.get('shipping','').strip(); payment=request.args.get('payment','').strip(); q=request.args.get('q','').strip(); task=request.args.get('task','').strip()
    conn=db(); where=['1=1']; params=[]; today=date.today().isoformat(); task_label=''
    if task == 'today_ship':
        where.append("r.dispatch_date=? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령')")
        params.append(today); task_label='오늘 발송할 주문'
    elif task == 'missed_ship':
        where.append("r.dispatch_date<? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령')")
        params.append(today); task_label='발송 지연 주문'
    elif task == 'today_return':
        where.append("r.pickup_date=? AND r.status IN ('예약','대여중') AND r.return_status NOT IN ('회수완료')")
        params.append(today); task_label='오늘 회수 예정 주문'
    elif task == 'overdue':
        where.append("r.pickup_date<? AND r.status IN ('예약','대여중') AND r.return_status NOT IN ('회수완료')")
        params.append(today); task_label='회수 지연 주문'
    elif task == 'cleaning':
        where.append("r.status='세탁/검수중'")
        task_label='세탁/검수중 주문'
    elif task == 'payment_pending':
        where.append("r.payment_status='결제대기' AND r.status='예약'")
        task_label='결제 확인대기 주문'
    if status: where.append('r.status=?'); params.append(status)
    if shipping: where.append('r.shipping_status=?'); params.append(shipping)
    if payment: where.append('r.payment_status=?'); params.append(payment)
    if q:
        where.append('(r.order_no LIKE ? OR r.customer_name LIKE ? OR r.phone LIKE ? OR p.name LIKE ?)'); params.extend([f'%{q}%']*4)
    clause=' AND '.join(where)
    rows=conn.execute(f"""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name,
        (SELECT COUNT(*) FROM reservation_items ri WHERE ri.reservation_id=r.id) item_count,
        (SELECT COALESCE(SUM(ri.qty),0) FROM reservation_items ri WHERE ri.reservation_id=r.id) item_qty_total
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE {clause} ORDER BY r.start_date,r.id DESC""",params).fetchall()
    conn.close(); return render_template('reservations.html',rows=rows,status=status,shipping=shipping,payment=payment,q=q,task=task,task_label=task_label)


@app.route('/admin/reservations/<int:rid>')
def reservation_detail(rid):
    conn=db()
    r=conn.execute("""SELECT r.*,p.name,p.category,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE r.id=?""",(rid,)).fetchone()
    if not r:
        conn.close(); return '주문이 없습니다.',404
    damage_items=conn.execute('SELECT * FROM damage_items WHERE reservation_id=? ORDER BY id DESC',(rid,)).fetchall()
    items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
    payment_history=conn.execute('SELECT * FROM payment_transactions WHERE reservation_id=? ORDER BY id DESC',(rid,)).fetchall()
    adjustments=conn.execute('SELECT * FROM payment_adjustments WHERE reservation_id=? ORDER BY id DESC',(rid,)).fetchall()
    cancel_info=cancellation_quote(conn,r) if r['status']!='취소' else None
    cancel_policy_summary=cancellation_policy_summary(conn)
    consents=conn.execute('SELECT * FROM consent_records WHERE reservation_id=? ORDER BY id',(rid,)).fetchall()
    notifications=conn.execute('SELECT * FROM notification_queue WHERE reservation_id=? ORDER BY id DESC',(rid,)).fetchall()
    conn.close()
    return render_template('reservation_detail.html', r=r, damage_items=damage_items, items=items, payment_history=payment_history, adjustments=adjustments, cancel_info=cancel_info, cancel_policy_summary=cancel_policy_summary, consents=consents, notifications=notifications)


@app.route('/admin/reservations/<int:rid>/update', methods=['POST'])
def reservation_update(rid):
    status=request.form.get('status','예약'); shipping=request.form.get('shipping_status','발송전'); ret=request.form.get('return_status','회수전'); payment_status=request.form.get('payment_status','결제대기'); memo=request.form.get('memo','').strip()
    outbound_carrier=request.form.get('outbound_carrier','').strip(); outbound_tracking_no=request.form.get('outbound_tracking_no','').strip()
    return_carrier=request.form.get('return_carrier','').strip(); return_tracking_no=request.form.get('return_tracking_no','').strip()
    if status not in ['예약','대여중','세탁/검수중','반납완료','취소']: status='예약'
    if shipping not in ['발송전','포장완료','발송완료','고객수령']: shipping='발송전'
    if ret not in ['회수전','회수요청','회수중','회수완료','파손/분실확인']: ret='회수전'
    if payment_status not in ['결제대기','결제완료','결제실패','결제취소']: payment_status='결제대기'
    if outbound_carrier and outbound_carrier not in CARRIERS: outbound_carrier='기타'
    if return_carrier and return_carrier not in CARRIERS: return_carrier='기타'
    if ret=='회수완료' and status not in ['취소','반납완료']:
        status='세탁/검수중'
    conn=db(); current=conn.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
    paid_at=current['paid_at'] if current else ''
    deposit_received=current['deposit_received'] if current else 0
    refund_amount=current['refund_amount'] if current else 0
    paid_order_amount=current['paid_order_amount'] if current else 0
    if payment_status=='결제완료':
        if not paid_at: paid_at=datetime.now().strftime('%Y-%m-%d %H:%M')
        if current and not paid_order_amount: paid_order_amount=card_payment_amount(current)
    elif payment_status=='결제취소' and shipping=='발송전':
        status='취소'
    if current and status=='취소' and current['status']!='취소' and current['payment_status']=='결제완료':
        queue_cancel_refund(conn,current,'관리자')
        payment_status='결제완료'
    conn.execute("""UPDATE reservations SET status=?,shipping_status=?,return_status=?,payment_status=?,paid_at=?,paid_order_amount=?,deposit_received=?,refund_amount=?,memo=?,
        outbound_carrier=?,outbound_tracking_no=?,return_carrier=?,return_tracking_no=? WHERE id=?""",
        (status,shipping,ret,payment_status,paid_at,paid_order_amount,deposit_received,refund_amount,memo,outbound_carrier,outbound_tracking_no,return_carrier,return_tracking_no,rid))
    if current:
        if payment_status=='결제완료' and current['payment_status']!='결제완료':
            queue_notification(conn,rid,'결제완료')
        if shipping=='발송완료' and current['shipping_status']!='발송완료':
            queue_notification(conn,rid,'발송완료')
        if status=='반납완료' and current['status']!='반납완료':
            queue_notification(conn,rid,'반납완료')
        if status=='취소' and current['status']!='취소':
            queue_notification(conn,rid,'주문취소')
    conn.commit(); conn.close()
    flash('주문/송장 정보를 저장했습니다.'); return redirect(request.referrer or url_for('reservations'))


@app.route('/admin/reservations/<int:rid>/deposit-payment', methods=['POST'])
def reservation_deposit_payment(rid):
    conn=db(); r=conn.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r:
        conn.close(); return '주문이 없습니다.',404
    status=request.form.get('deposit_payment_status','입금대기').strip()
    if int(r['deposit_total'] or 0)<=0:
        status='해당없음'
    elif status not in ['입금대기','입금확인']:
        status='입금대기'
    note=request.form.get('deposit_payment_note','').strip()
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    if status=='입금확인':
        received=int(r['deposit_total'] or 0)
        refund=max(0,received-int(r['damage_deduction'] or 0))
        received_at=(r['deposit_received_at'] or '').strip() or now
        conn.execute("""UPDATE reservations SET deposit_payment_status='입금확인',deposit_received=?,deposit_received_at=?,deposit_payment_note=?,refund_amount=? WHERE id=?""",
                     (received,received_at,note,refund,rid))
        msg=f'보증금 {received:,}원 입금을 확인했습니다.'
    elif status=='해당없음':
        conn.execute("UPDATE reservations SET deposit_payment_status='해당없음',deposit_received=0,deposit_received_at='',deposit_payment_note=? WHERE id=?",(note,rid))
        msg='이 주문은 보증금이 없습니다.'
    else:
        if (r['refund_status'] or '') in ['부분환급','환급완료','보증금전액차감']:
            conn.close(); flash('이미 보증금 정산이 진행된 주문은 입금확인을 취소할 수 없습니다.'); return redirect(url_for('reservation_detail',rid=rid))
        conn.execute("UPDATE reservations SET deposit_payment_status='입금대기',deposit_received=0,deposit_received_at='',deposit_payment_note=?,refund_amount=0 WHERE id=?",(note,rid))
        msg='보증금을 입금대기 상태로 변경했습니다.'
    conn.commit(); conn.close(); flash(msg)
    return redirect(url_for('reservation_detail',rid=rid))


@app.route('/admin/reservations/<int:rid>/settlement', methods=['POST'])
def reservation_settlement(rid):
    conn=db()
    r=conn.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r:
        conn.close(); return '주문이 없습니다.',404
    try:
        # 수납액은 '보증금 입금 확인'에서만 변경합니다. 정산화면에서는 읽기 전용입니다.
        received=max(0,int(r['deposit_received'] or 0))
        deduction=max(0,int(request.form.get('damage_deduction','0') or 0))
        additional=max(0,int(request.form.get('additional_charge','0') or 0))
    except ValueError:
        conn.close(); flash('정산 금액은 숫자로 입력해주세요.'); return redirect(url_for('reservation_detail',rid=rid))
    refund=max(0, received-deduction)
    refund_status=request.form.get('refund_status','미정산')
    if refund_status not in ['미정산','환급예정','부분환급','환급완료','보증금전액차감']:
        refund_status='미정산'
    if deduction >= received and received > 0 and refund_status in ['미정산','환급예정','부분환급']:
        refund_status='보증금전액차감'
    note=request.form.get('settlement_note','').strip()
    refund_date=request.form.get('refund_date','').strip()
    conn.execute('''UPDATE reservations SET deposit_received=?,damage_deduction=?,additional_charge=?,refund_amount=?,refund_status=?,settlement_note=?,refund_date=? WHERE id=?''',
                 (received,deduction,additional,refund,refund_status,note,refund_date,rid))
    conn.commit(); conn.close(); flash('보증금 정산정보를 저장했습니다.')
    return redirect(url_for('reservation_detail',rid=rid))


@app.route('/admin/settlements')
def settlements_admin():
    status=request.args.get('status','pending').strip()
    q=request.args.get('q','').strip()
    conn=db(); where=["r.status<>'취소'"]; params=[]
    if status == 'pending':
        where.append("r.refund_status IN ('미정산','환급예정','부분환급')")
    elif status == 'refund':
        where.append("r.refund_status IN ('환급예정','부분환급')")
    elif status == 'charge':
        where.append("r.additional_charge>0")
    elif status == 'deducted':
        where.append("r.refund_status='보증금전액차감'")
    elif status == 'done':
        where.append("r.refund_status='환급완료'")
    if q:
        where.append('(r.order_no LIKE ? OR r.customer_name LIKE ? OR r.phone LIKE ? OR p.name LIKE ?)')
        params.extend([f'%{q}%']*4)
    clause=' AND '.join(where)
    rows=conn.execute(f"""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name,
        COALESCE((SELECT COUNT(*) FROM damage_items di WHERE di.reservation_id=r.id),0) damage_item_count
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE {clause}
        ORDER BY CASE r.refund_status WHEN '미정산' THEN 0 WHEN '환급예정' THEN 1 WHEN '부분환급' THEN 2 ELSE 3 END,
                 r.end_date DESC,r.id DESC""",params).fetchall()
    summary=conn.execute("""SELECT
        COALESCE(SUM(CASE WHEN status<>'취소' AND refund_status IN ('미정산','환급예정','부분환급') THEN 1 ELSE 0 END),0) pending_count,
        COALESCE(SUM(CASE WHEN status<>'취소' AND refund_status IN ('환급예정','부분환급') THEN refund_amount ELSE 0 END),0) refund_due,
        COALESCE(SUM(CASE WHEN status<>'취소' THEN additional_charge ELSE 0 END),0) additional_due,
        COALESCE(SUM(CASE WHEN status<>'취소' THEN damage_deduction ELSE 0 END),0) deduction_total
        FROM reservations""").fetchone()
    conn.close()
    return render_template('settlements.html',rows=rows,status=status,q=q,summary=summary)


def sync_damage_totals(conn, rid):
    r=conn.execute('SELECT deposit_received,deposit_total,refund_status FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r: return
    total=conn.execute('SELECT COALESCE(SUM(amount),0) total FROM damage_items WHERE reservation_id=?',(rid,)).fetchone()['total']
    received=r['deposit_received'] if r['deposit_received'] else r['deposit_total']
    refund=max(0,received-total)
    refund_status=r['refund_status'] or '미정산'
    if total >= received and received > 0 and refund_status not in ('환급완료',):
        refund_status='보증금전액차감'
    elif refund_status=='보증금전액차감' and total < received:
        refund_status='미정산'
    conn.execute('UPDATE reservations SET damage_deduction=?,refund_amount=?,refund_status=? WHERE id=?',(total,refund,refund_status,rid))


@app.route('/admin/reservations/<int:rid>/damage-items/add', methods=['POST'])
def damage_item_add(rid):
    conn=db(); r=conn.execute('SELECT id FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r:
        conn.close(); return '주문이 없습니다.',404
    item_name=request.form.get('item_name','').strip()
    issue_type=request.form.get('issue_type','파손').strip()
    if issue_type not in ['파손','분실','오염','기타']: issue_type='기타'
    try:
        qty=max(1,int(request.form.get('qty','1') or 1))
        amount=max(0,int(request.form.get('amount','0') or 0))
    except ValueError:
        conn.close(); flash('수량과 차감금액은 숫자로 입력해주세요.'); return redirect(url_for('reservation_detail',rid=rid))
    if not item_name:
        conn.close(); flash('파손·분실 품목명을 입력해주세요.'); return redirect(url_for('reservation_detail',rid=rid))
    note=request.form.get('note','').strip()
    conn.execute('INSERT INTO damage_items(reservation_id,item_name,issue_type,qty,amount,note,created_at) VALUES(?,?,?,?,?,?,?)',
                 (rid,item_name,issue_type,qty,amount,note,datetime.now().strftime('%Y-%m-%d %H:%M')))
    sync_damage_totals(conn,rid); conn.commit(); conn.close()
    flash('파손·분실 품목을 추가했습니다. 차감액과 환급액도 다시 계산했습니다.')
    return redirect(url_for('reservation_detail',rid=rid))


@app.route('/admin/reservations/<int:rid>/damage-items/<int:item_id>/delete', methods=['POST'])
def damage_item_delete(rid,item_id):
    conn=db(); conn.execute('DELETE FROM damage_items WHERE id=? AND reservation_id=?',(item_id,rid))
    sync_damage_totals(conn,rid); conn.commit(); conn.close()
    flash('품목 기록을 삭제하고 정산금액을 다시 계산했습니다.')
    return redirect(url_for('reservation_detail',rid=rid))


@app.route('/admin/customers')
def customers_admin():
    q = request.args.get('q','').strip()
    caution = request.args.get('caution','').strip()
    conn = db()
    where = ["TRIM(COALESCE(r.phone,''))<>''"]
    params = []
    if q:
        where.append("(r.customer_name LIKE ? OR r.phone LIKE ?)")
        params.extend([f'%{q}%', f'%{q}%'])
    if caution == '1':
        where.append("COALESCE(cp.caution,0)=1")
    clause=' AND '.join(where)
    sql = """SELECT r.phone,
            MAX(r.customer_name) customer_name,
            COUNT(*) order_count,
            COALESCE(SUM(CASE WHEN r.status <> '취소' THEN r.qty ELSE 0 END),0) total_qty,
            COALESCE(SUM(CASE WHEN r.status='반납완료' THEN 1 ELSE 0 END),0) completed_count,
            MAX(r.created_at) last_order_at,
            COALESCE(cp.caution,0) caution,
            COALESCE(cp.customer_note,'') customer_note,
            COALESCE(cp.damage_loss_note,'') damage_loss_note
        FROM reservations r LEFT JOIN customer_profiles cp ON cp.phone=r.phone
        WHERE """ + clause + """
        GROUP BY r.phone
        ORDER BY caution DESC, last_order_at DESC"""
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template('customers.html', rows=rows, q=q, caution=caution)


@app.route('/admin/customers/<path:phone>', methods=['GET','POST'])
def customer_detail(phone):
    conn = db()
    if request.method == 'POST':
        customer_note=request.form.get('customer_note','').strip()
        damage_loss_note=request.form.get('damage_loss_note','').strip()
        caution=1 if request.form.get('caution')=='1' else 0
        conn.execute("""INSERT INTO customer_profiles(phone,customer_note,damage_loss_note,caution,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET
            customer_note=excluded.customer_note, damage_loss_note=excluded.damage_loss_note,
            caution=excluded.caution, updated_at=excluded.updated_at""",
            (phone,customer_note,damage_loss_note,caution,datetime.now().strftime('%Y-%m-%d %H:%M')))
        conn.commit(); conn.close(); flash('고객 관리정보를 저장했습니다.')
        return redirect(url_for('customer_detail', phone=phone))
    orders=conn.execute("""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE r.phone=? ORDER BY r.created_at DESC,r.id DESC""",(phone,)).fetchall()
    if not orders:
        conn.close(); return '고객 주문이 없습니다.',404
    profile=conn.execute('SELECT * FROM customer_profiles WHERE phone=?',(phone,)).fetchone()
    summary=conn.execute("""SELECT COUNT(*) order_count,
        COALESCE(SUM(CASE WHEN status<>'취소' THEN qty ELSE 0 END),0) total_qty,
        COALESCE(SUM(CASE WHEN status='반납완료' THEN 1 ELSE 0 END),0) completed_count,
        COALESCE(SUM(CASE WHEN status<>'취소' THEN total_price ELSE 0 END),0) rental_total,
        COALESCE(SUM(damage_deduction),0) damage_total,
        COALESCE(SUM(additional_charge),0) additional_total,
        COALESCE(SUM(CASE WHEN refund_status='환급완료' THEN refund_amount ELSE 0 END),0) refunded_total
        FROM reservations WHERE phone=?""",(phone,)).fetchone()
    customer_name=orders[0]['customer_name']
    conn.close()
    return render_template('customer_detail.html',phone=phone,customer_name=customer_name,profile=profile,summary=summary,orders=orders)


@app.route('/admin/inventory')
def inventory_admin():
    conn = db(); now_text=datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = conn.execute('''
        SELECT ps.id size_id, ps.size_name, ps.stock total_stock,
               p.id product_id, p.name, p.category, p.is_active,
               COALESCE((SELECT SUM(ri.qty) FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id
                         WHERE ri.size_id=ps.id AND r.status='예약'
                           AND (r.payment_status='결제완료' OR (r.payment_status='결제대기' AND (COALESCE(r.payment_expires_at,'')='' OR r.payment_expires_at>=?)))),0) reserved_qty,
               COALESCE((SELECT SUM(ri.qty) FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id
                         WHERE ri.size_id=ps.id AND r.status='대여중' AND r.payment_status='결제완료'),0) renting_qty,
               COALESCE((SELECT SUM(ri.qty) FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id
                         WHERE ri.size_id=ps.id AND r.status='세탁/검수중'),0) cleaning_qty
        FROM product_sizes ps JOIN products p ON p.id=ps.product_id
        ORDER BY p.name, ps.id
    ''',(now_text,)).fetchall()
    data=[]
    for r in rows:
        d=dict(r)
        d['physical_available']=max(0, r['total_stock']-r['renting_qty']-r['cleaning_qty'])
        data.append(d)
    conn.close()
    return render_template('inventory.html', rows=data)


@app.route('/admin/dashboard')
def dashboard():
    conn=db(); today=date.today().isoformat()
    def agg(where, params=()):
        return conn.execute(f"SELECT COUNT(*) orders, COALESCE(SUM(qty),0) qty FROM reservations WHERE {where}", params).fetchone()
    today_ship=agg("dispatch_date=? AND status='예약' AND payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=reservations.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND shipping_status NOT IN ('발송완료','고객수령')", (today,))
    missed_ship=agg("dispatch_date<? AND status='예약' AND payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=reservations.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND shipping_status NOT IN ('발송완료','고객수령')", (today,))
    today_return=agg("pickup_date=? AND status IN ('예약','대여중') AND payment_status='결제완료' AND return_status NOT IN ('회수완료')", (today,))
    overdue=agg("pickup_date<? AND status IN ('예약','대여중') AND payment_status='결제완료' AND return_status NOT IN ('회수완료')", (today,))
    cleaning=agg("status='세탁/검수중'")
    payment_pending=agg("payment_status='결제대기' AND status='예약'")
    payment_failed=agg("payment_status='결제실패' AND status='예약'")
    adjustment_pending=conn.execute("SELECT COUNT(*) orders,COALESCE(SUM(amount),0) amount FROM payment_adjustments WHERE status='처리대기'").fetchone()
    notification_pending=conn.execute("SELECT COUNT(*) orders FROM notification_queue WHERE status='발송대기'").fetchone()
    stats={
        'active_products':conn.execute('SELECT COUNT(*) c FROM products WHERE is_active=1').fetchone()['c'],
        'reserved':conn.execute("SELECT COUNT(*) c FROM reservations WHERE status='예약' AND payment_status='결제완료'").fetchone()['c'],
        'renting':conn.execute("SELECT COUNT(*) c FROM reservations WHERE status='대여중'").fetchone()['c'],
        'cleaning':cleaning['orders'],
    }
    tasks={'today_ship':dict(today_ship),'missed_ship':dict(missed_ship),'today_return':dict(today_return),'overdue':dict(overdue),'cleaning':dict(cleaning),'payment_pending':dict(payment_pending),'payment_failed':dict(payment_failed),'adjustment_pending':dict(adjustment_pending),'notification_pending':dict(notification_pending)}
    urgent=conn.execute("""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name,
        CASE WHEN r.dispatch_date < ? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령') THEN '발송 지연'
             WHEN r.pickup_date < ? AND r.status IN ('예약','대여중') AND r.payment_status='결제완료' AND r.return_status NOT IN ('회수완료') THEN '회수 지연'
             WHEN r.dispatch_date = ? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령') THEN '오늘 발송'
             WHEN r.pickup_date = ? AND r.status IN ('예약','대여중') AND r.payment_status='결제완료' AND r.return_status NOT IN ('회수완료') THEN '오늘 회수'
             WHEN r.status='세탁/검수중' THEN '세탁/검수' ELSE '' END task_type
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE (r.dispatch_date < ? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령'))
           OR (r.pickup_date < ? AND r.status IN ('예약','대여중') AND r.payment_status='결제완료' AND r.return_status NOT IN ('회수완료'))
           OR (r.dispatch_date = ? AND r.status='예약' AND r.payment_status='결제완료' AND NOT EXISTS (SELECT 1 FROM payment_adjustments a WHERE a.reservation_id=r.id AND a.status='처리대기' AND a.adjustment_type='추가결제') AND r.shipping_status NOT IN ('발송완료','고객수령'))
           OR (r.pickup_date = ? AND r.status IN ('예약','대여중') AND r.payment_status='결제완료' AND r.return_status NOT IN ('회수완료'))
           OR r.status='세탁/검수중'
        ORDER BY CASE task_type WHEN '발송 지연' THEN 1 WHEN '회수 지연' THEN 2 WHEN '오늘 발송' THEN 3 WHEN '오늘 회수' THEN 4 ELSE 5 END, r.start_date, r.id
        LIMIT 20""",(today,today,today,today,today,today,today,today)).fetchall()
    conn.close(); return render_template('dashboard.html',stats=stats,tasks=tasks,urgent=urgent,today=today)


@app.route('/admin/notifications')
def notifications_admin():
    status=request.args.get('status','').strip(); event_type=request.args.get('event_type','').strip(); q=request.args.get('q','').strip()
    conn=db(); queue_due_pickup_notifications(conn); conn.commit()
    where=['1=1']; params=[]
    if status:
        where.append('n.status=?'); params.append(status)
    if event_type:
        where.append('n.event_type=?'); params.append(event_type)
    if q:
        where.append('(r.order_no LIKE ? OR n.recipient_name LIKE ? OR n.recipient_phone LIKE ? OR n.message LIKE ?)'); params.extend([f'%{q}%']*4)
    clause=' AND '.join(where)
    rows=conn.execute(f'''SELECT n.*,r.order_no,r.start_date,r.end_date,r.pickup_date,r.status order_status
        FROM notification_queue n JOIN reservations r ON r.id=n.reservation_id
        WHERE {clause} ORDER BY CASE n.status WHEN '발송대기' THEN 1 WHEN '발송접수' THEN 2 WHEN '발송중' THEN 3 WHEN '발송실패' THEN 4 WHEN '발송성공' THEN 5 ELSE 6 END,n.id DESC LIMIT 300''',params).fetchall()
    counts={x['status']:x['c'] for x in conn.execute('SELECT status,COUNT(*) c FROM notification_queue GROUP BY status').fetchall()}
    templates=conn.execute('SELECT * FROM notification_templates ORDER BY rowid').fetchall()
    provider_config=notification_provider_config(conn)
    conn.close()
    secret=load_notification_secrets()
    secret_status={'configured':bool(secret['api_key'] and secret['api_secret']),'api_key_mask':masked_api_key(secret['api_key']),'webhook_configured':bool(secret.get('webhook_secret'))}
    return render_template('notifications.html',rows=rows,counts=counts,templates=templates,status=status,event_type=event_type,q=q,event_labels=NOTIFICATION_EVENT_LABELS,provider_config=provider_config,secret_status=secret_status)


@app.route('/admin/notifications/<int:nid>/test-send', methods=['POST'])
def notification_test_send(nid):
    result=request.form.get('result','success')
    conn=db(); row=conn.execute('SELECT * FROM notification_queue WHERE id=?',(nid,)).fetchone()
    if not row:
        conn.close(); flash('알림 기록을 찾을 수 없습니다.'); return redirect(url_for('notifications_admin'))
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    if result=='fail':
        conn.execute("UPDATE notification_queue SET status='발송실패',sent_at='',error_message=? WHERE id=?",('테스트 발송 실패',nid)); flash('테스트 발송실패로 처리했습니다.')
    else:
        conn.execute("UPDATE notification_queue SET status='발송완료',sent_at=?,error_message='' WHERE id=?",(now,nid)); flash('테스트 발송완료로 처리했습니다.')
    conn.commit(); conn.close(); return redirect(request.referrer or url_for('notifications_admin'))


@app.route('/admin/notifications/<int:nid>/retry', methods=['POST'])
def notification_retry(nid):
    conn=db(); conn.execute("UPDATE notification_queue SET status='발송대기',sent_at='',error_message='',provider_group_id='',provider_message_id='',provider_response='',provider_status_code='',provider_status_reason='',provider_message_type='',provider_replacement=0,provider_queue_summary='',provider_date_processed='',provider_date_reported='',provider_date_received='',result_checked_at='' WHERE id=?",(nid,)); conn.commit(); conn.close()
    flash('알림을 다시 발송대기로 돌렸습니다.'); return redirect(request.referrer or url_for('notifications_admin'))


@app.route('/admin/notifications/test-send-pending', methods=['POST'])
def notification_test_send_pending():
    conn=db(); now=datetime.now().strftime('%Y-%m-%d %H:%M')
    cur=conn.execute("UPDATE notification_queue SET status='발송완료',sent_at=?,error_message='' WHERE status='발송대기'",(now,))
    count=cur.rowcount; conn.commit(); conn.close(); flash(f'발송대기 알림 {count}건을 테스트 발송완료 처리했습니다.')
    return redirect(url_for('notifications_admin'))


@app.route('/admin/notification-templates', methods=['POST'])
def notification_templates_update():
    event_type=request.form.get('event_type','').strip(); title=request.form.get('title','').strip(); channel=request.form.get('channel','알림톡').strip(); message=request.form.get('message_template','').strip(); provider_template_id=request.form.get('provider_template_id','').strip(); active=1 if request.form.get('is_active')=='1' else 0
    if event_type not in NOTIFICATION_EVENT_LABELS or not title or not message:
        flash('알림 종류, 제목, 메시지 내용을 확인해주세요.'); return redirect(url_for('notifications_admin'))
    if channel not in ['알림톡','SMS','문자','미연동']:
        channel='알림톡'
    conn=db(); conn.execute('UPDATE notification_templates SET title=?,channel=?,message_template=?,provider_template_id=?,is_active=?,updated_at=? WHERE event_type=?',(title,channel,message,provider_template_id,active,datetime.now().strftime('%Y-%m-%d %H:%M'),event_type)); conn.commit(); conn.close()
    flash('알림 템플릿을 저장했습니다. 이후 새로 생성되는 알림부터 적용됩니다.'); return redirect(url_for('notifications_admin'))


@app.route('/admin/notifications/provider-settings', methods=['POST'])
def notification_provider_settings_update():
    provider=request.form.get('notification_provider','테스트').strip()
    if provider not in ('테스트','SOLAPI'):
        provider='테스트'
    sender=normalize_phone(request.form.get('notification_sender_phone',''))
    pf_id=request.form.get('notification_kakao_pf_id','').strip()
    sms_fallback=1 if request.form.get('notification_sms_fallback')=='1' else 0
    auto_send=1 if request.form.get('notification_auto_send')=='1' else 0
    auto_result_sync=1 if request.form.get('notification_auto_result_sync')=='1' else 0
    api_key=request.form.get('solapi_api_key','').strip()
    api_secret=request.form.get('solapi_api_secret','').strip()
    webhook_secret=request.form.get('solapi_webhook_secret','').strip()
    clear_secret=request.form.get('clear_solapi_secret')=='1'
    conn=db()
    set_setting(conn,'notification_provider',provider)
    set_setting(conn,'notification_sender_phone',sender)
    set_setting(conn,'notification_kakao_pf_id',pf_id)
    set_setting(conn,'notification_sms_fallback',sms_fallback)
    set_setting(conn,'notification_auto_send',auto_send)
    set_setting(conn,'notification_auto_result_sync',auto_result_sync)
    conn.commit(); conn.close()
    old=load_notification_secrets()
    if clear_secret:
        save_notification_secrets('','','')
    elif api_key or api_secret or webhook_secret:
        save_notification_secrets(api_key or old['api_key'], api_secret or old['api_secret'], webhook_secret or old.get('webhook_secret',''))
    flash('알림 발송 연동 설정을 저장했습니다. API Secret은 DB가 아닌 로컬 비밀설정 파일에 저장됩니다.')
    return redirect(url_for('notifications_admin'))


@app.route('/admin/notifications/<int:nid>/send-real', methods=['POST'])
def notification_real_send(nid):
    ok,message=send_notification_now(nid)
    flash(message if ok else '실제 발송 실패: '+message)
    return redirect(request.referrer or url_for('notifications_admin'))


@app.route('/admin/notifications/send-pending-real', methods=['POST'])
def notification_real_send_pending():
    conn=db(); ids=[r['id'] for r in conn.execute("""SELECT n.id FROM notification_queue n JOIN reservations r ON r.id=n.reservation_id
        WHERE n.status='발송대기' AND r.order_no NOT LIKE 'TEST-%' ORDER BY n.id LIMIT 100""").fetchall()]; conn.close()
    ok=fail=0
    for nid in ids:
        success,_=send_notification_now(nid)
        if success: ok+=1
        else: fail+=1
    flash(f'실제 발송 처리: 접수 {ok}건 / 실패 {fail}건. TEST 주문은 안전을 위해 제외했습니다.')
    return redirect(url_for('notifications_admin'))


@app.route('/admin/notifications/connection-test', methods=['POST'])
def notification_connection_test():
    phone=request.form.get('test_phone','').strip()
    try:
        result=solapi_connection_test(phone)
        group=(result.get('groupInfo') or {}).get('groupId','')
        flash('SOLAPI 문자 연동 테스트가 접수되었습니다.' + (f' 그룹ID: {group}' if group else ''))
    except Exception as e:
        flash('SOLAPI 연동 테스트 실패: '+str(e))
    return redirect(url_for('notifications_admin'))

@app.route('/admin/notifications/<int:nid>/sync-result', methods=['POST'])
def notification_sync_result(nid):
    ok, message = sync_notification_result(nid)
    flash(message if ok else '발송결과 조회 실패: ' + message)
    return redirect(request.referrer or url_for('notifications_admin'))


@app.route('/admin/notifications/sync-results', methods=['POST'])
def notification_sync_results():
    ok, fail = sync_pending_notification_results(limit=100)
    flash(f'SOLAPI 발송결과 조회: {ok}건 확인 / {fail}건 조회실패')
    return redirect(url_for('notifications_admin'))


@app.route('/webhooks/solapi/message', methods=['POST'])
def solapi_message_webhook():
    secret = load_notification_secrets().get('webhook_secret', '')
    if not secret:
        return 'Webhook disabled', 403
    expected = hashlib.sha1(secret.encode('utf-8')).hexdigest()
    received = (request.headers.get('X-Solapi-Secret') or '').strip()
    if not hmac.compare_digest(received, expected):
        return 'Unauthorized', 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return 'OK', 200
    conn = db()
    try:
        for event in payload:
            if not isinstance(event, dict):
                continue
            message_id = str(event.get('messageId') or '').strip()
            group_id = str(event.get('groupId') or '').strip()
            row = None
            if message_id:
                row = conn.execute(
                    'SELECT id FROM notification_queue WHERE provider_message_id=? ORDER BY id DESC LIMIT 1',
                    (message_id,)
                ).fetchone()
            if not row and group_id:
                row = conn.execute(
                    'SELECT id FROM notification_queue WHERE provider_group_id=? ORDER BY id DESC LIMIT 1',
                    (group_id,)
                ).fetchone()
            if row:
                apply_notification_provider_result(conn, row['id'], event, None)
        conn.commit()
    finally:
        conn.close()
    return 'OK', 200


@app.route('/admin/payment-settings', methods=['GET','POST'])
def admin_payment_settings():
    conn=db(); current=load_payment_secrets(); mode=get_text_setting(conn,'payment_provider_mode','MOCK').upper().strip() or 'MOCK'
    if request.method=='POST':
        new_mode=request.form.get('payment_provider_mode','MOCK').upper().strip()
        if new_mode not in {'MOCK','TOSS_TEST'}:
            new_mode='MOCK'
        clear=request.form.get('clear_keys')=='1'
        client_in=request.form.get('toss_client_key','').strip(); secret_in=request.form.get('toss_secret_key','').strip()
        client='' if clear else (client_in or current.get('client_key',''))
        secret='' if clear else (secret_in or current.get('secret_key',''))
        if new_mode=='TOSS_TEST':
            if not client or not secret:
                conn.close(); flash('토스 테스트 모드를 사용하려면 테스트 클라이언트 키와 테스트 시크릿 키가 모두 필요합니다.'); return redirect(url_for('admin_payment_settings'))
            # V3.0은 테스트 결제 전용입니다. 라이브 키 오입력으로 실제 결제가 생기는 것을 차단합니다.
            if not client.startswith('test_') or not secret.startswith('test_'):
                conn.close(); flash('V3.3에서는 test_ 로 시작하는 토스 테스트 키만 저장할 수 있습니다. 라이브 키는 사용할 수 없습니다.'); return redirect(url_for('admin_payment_settings'))
        save_payment_secrets(client,secret)
        set_setting(conn,'payment_provider_mode',new_mode)
        conn.commit(); conn.close(); flash('결제 연동 설정을 저장했습니다.' if new_mode=='MOCK' else '토스페이먼츠 테스트 결제 설정을 저장했습니다.')
        return redirect(url_for('admin_payment_settings'))
    cfg=payment_provider_config(conn); conn.close()
    return render_template('payment_settings.html',config=cfg)


@app.route('/admin/settings', methods=['GET','POST'])
def admin_settings():
    conn = db()
    if request.method == 'POST':
        try:
            outbound = max(0, int(request.form.get('outbound_shipping_fee','0') or 0))
            ret = max(0, int(request.form.get('return_shipping_fee','0') or 0))
            default_deposit = max(0, int(request.form.get('default_deposit','0') or 0))
            dispatch_lead = max(0, int(request.form.get('dispatch_lead_business_days','1') or 1))
            cancel_free_days = max(1, int(request.form.get('cancel_free_days','3') or 3))
            cancel_late_fee = max(0, min(100, int(request.form.get('cancel_late_fee_percent','20') or 0)))
            cancel_same_day_fee = max(0, min(100, int(request.form.get('cancel_same_day_fee_percent','50') or 0)))
            cancel_after_start_fee = max(0, min(100, int(request.form.get('cancel_after_start_fee_percent','100') or 0)))
        except ValueError:
            conn.close(); flash('설정 금액과 취소수수료는 숫자로 입력해주세요.'); return redirect(request.url)
        set_setting(conn,'outbound_shipping_fee',outbound)
        set_setting(conn,'return_shipping_fee',ret)
        set_setting(conn,'default_deposit',default_deposit)
        set_setting(conn,'deposit_bank_name',request.form.get('deposit_bank_name','').strip())
        set_setting(conn,'deposit_account_number',request.form.get('deposit_account_number','').strip())
        set_setting(conn,'deposit_account_holder',request.form.get('deposit_account_holder','').strip())
        set_setting(conn,'dispatch_lead_business_days',dispatch_lead)
        set_setting(conn,'cancel_free_days',cancel_free_days)
        set_setting(conn,'cancel_late_fee_percent',cancel_late_fee)
        set_setting(conn,'cancel_same_day_fee_percent',cancel_same_day_fee)
        set_setting(conn,'cancel_after_start_fee_percent',cancel_after_start_fee)
        raw_holidays = request.form.get('holiday_dates','').strip()
        valid_holidays = sorted(parse_holidays(raw_holidays))
        set_setting(conn,'holiday_dates','\n'.join(valid_holidays))
        conn.commit(); conn.close(); flash('운영 설정을 저장했습니다. 취소수수료 규정은 저장 즉시 이후 취소 건부터 적용됩니다.')
        return redirect(url_for('admin_settings'))
    settings = get_fees(conn); conn.close()
    return render_template('settings.html', settings=settings)


@app.route('/admin/calendar')
def admin_calendar():
    month_q = request.args.get('month','').strip()
    try:
        first = datetime.strptime(month_q + '-01','%Y-%m-%d').date() if month_q else date.today().replace(day=1)
    except ValueError:
        first = date.today().replace(day=1)
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = next_month - timedelta(days=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    conn=db()
    rows=conn.execute("""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE r.status NOT IN ('취소','반납완료') AND r.payment_status IN ('결제완료','결제대기') AND r.start_date<=? AND r.end_date>=?
        ORDER BY r.start_date,r.id""",(last.isoformat(),first.isoformat())).fetchall()
    conn.close()
    summary_by_day={}
    for r in rows:
        a=max(first, datetime.strptime(r['start_date'],'%Y-%m-%d').date())
        b=min(last, datetime.strptime(r['end_date'],'%Y-%m-%d').date())
        d=a
        while d<=b:
            day=summary_by_day.setdefault(d.isoformat(),{})
            item=day.setdefault(r['status'], {'orders':0,'qty':0})
            item['orders'] += 1
            item['qty'] += r['qty']
            d+=timedelta(days=1)
    cells=[None]*first.weekday()+[date(first.year,first.month,d) for d in range(1,last.day+1)]
    while len(cells)%7: cells.append(None)
    weeks=[cells[i:i+7] for i in range(0,len(cells),7)]
    return render_template('calendar.html', first=first, prev_month=prev_month, next_month=next_month, weeks=weeks, summary_by_day=summary_by_day)


@app.route('/admin/calendar/day')
def calendar_day():
    day=request.args.get('date','').strip(); status=request.args.get('status','').strip()
    try:
        datetime.strptime(day,'%Y-%m-%d')
    except ValueError:
        return '날짜 형식이 올바르지 않습니다.',400
    conn=db(); where=["r.status NOT IN ('취소','반납완료')", 'r.start_date<=?', 'r.end_date>=?']; params=[day,day]
    if status:
        where.append('r.status=?'); params.append(status)
    clause=' AND '.join(where)
    rows=conn.execute(f"""SELECT r.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name
        FROM reservations r JOIN products p ON p.id=r.product_id LEFT JOIN product_sizes ps ON ps.id=r.size_id
        WHERE {clause} ORDER BY r.status,r.start_date,r.id""",params).fetchall()
    conn.close()
    return render_template('calendar_day.html', day=day, status=status, rows=rows)


@app.route('/admin/reservations/<int:rid>/edit', methods=['GET','POST'])
def reservation_edit(rid):
    conn=db()
    r=conn.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r:
        conn.close(); return '주문이 없습니다.',404
    items=conn.execute("SELECT ri.*,p.name,p.image_filename,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
    if request.method=='POST':
        customer=request.form.get('customer_name','').strip(); phone=request.form.get('phone','').strip()
        recipient=request.form.get('recipient_name','').strip() or customer
        zipcode=request.form.get('zipcode','').strip(); address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip()
        start=request.form.get('start_date',''); end=request.form.get('end_date','')
        dispatch_date=request.form.get('dispatch_date','').strip(); pickup_date=request.form.get('pickup_date','').strip()
        if not customer or not phone or not address1 or not start or not end or start>end:
            conn.close(); flash('주문 정보를 확인해주세요.'); return redirect(request.url)
        try:
            days=rental_days(start,end)
            if dispatch_date: datetime.strptime(dispatch_date,'%Y-%m-%d')
            if pickup_date: datetime.strptime(pickup_date,'%Y-%m-%d')
        except ValueError:
            conn.close(); flash('날짜 정보를 확인해주세요.'); return redirect(request.url)
        auto_dispatch,auto_pickup=auto_logistics_dates(conn,start,end)
        dispatch_date=dispatch_date or auto_dispatch; pickup_date=pickup_date or auto_pickup
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            conn.close(); flash('다른 주문이 처리 중입니다. 잠시 후 다시 시도해주세요.'); return redirect(request.url)
        r=conn.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
        items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(rid,)).fetchall()
        rental_total=0; deposit_total=0
        for item in items:
            if r['status'] in ('예약','대여중'):
                avail=available_stock(conn,item['size_id'],start,end,exclude_reservation_id=rid)
                if int(item['qty'])>avail:
                    conn.rollback(); conn.close(); flash(f'{item["name"]} / {item["size_name"]}는 변경 기간에 {avail}벌만 가능합니다.'); return redirect(request.url)
            line=rental_line_price(item['daily_price'],item['extra_daily_price'],days,item['qty'])
            rental_total+=line; deposit_total+=int(item['deposit_total'] or 0)
            conn.execute('UPDATE reservation_items SET rental_days=?,total_price=? WHERE id=?',(days,line,item['id']))
        final_amount=rental_total+int(r['shipping_fee'] or 0)+int(r['return_shipping_fee'] or 0)+deposit_total
        memo=request.form.get('memo','').strip()
        adjustment_type=''; adjustment_amount=0
        if r['payment_status']=='결제완료':
            adjustment_type,adjustment_amount=queue_amount_adjustment(conn,r,max(0,final_amount-deposit_total),'대여기간/주문금액 변경','관리자')
        conn.execute("""UPDATE reservations SET customer_name=?,phone=?,recipient_name=?,zipcode=?,address1=?,address2=?,start_date=?,end_date=?,dispatch_date=?,pickup_date=?,rental_days=?,total_price=?,deposit_total=?,final_amount=?,memo=? WHERE id=?""",
                     (customer,phone,recipient,zipcode,address1,address2,start,end,dispatch_date,pickup_date,days,rental_total,deposit_total,final_amount,memo,rid))
        conn.commit(); conn.close()
        if adjustment_type:
            flash(f'주문을 수정했습니다. 금액 차이 {adjustment_amount:,}원이 {adjustment_type} 대기로 등록되었습니다.')
        else:
            flash('주문 정보를 수정했습니다. 결제금액 조정은 없습니다.')
        return redirect(url_for('reservation_detail',rid=rid))
    conn.close(); return render_template('reservation_edit.html',r=r,items=items)


@app.route('/order-change', methods=['GET','POST'])
def order_change():
    ono=request.values.get('order_no','').strip(); phone=request.values.get('phone','').strip()
    conn=db(); r=conn.execute("""SELECT * FROM reservations WHERE UPPER(order_no)=UPPER(?) AND REPLACE(REPLACE(phone,'-',''),' ','')=REPLACE(REPLACE(?,'-',''),' ','')""",(ono,phone)).fetchone()
    if not r:
        conn.close(); flash('주문을 찾지 못했습니다.'); return redirect(url_for('order_lookup'))
    items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(r['id'],)).fetchall()
    if r['status']!='예약' or r['shipping_status']!='발송전':
        conn.close(); flash('발송 전 예약 상태의 주문만 고객이 직접 변경할 수 있습니다.'); return redirect(url_for('order_lookup',order_no=ono,phone=phone))
    if request.method=='POST':
        start=request.form.get('start_date','').strip(); recipient=request.form.get('recipient_name','').strip() or r['customer_name']
        zipcode=request.form.get('zipcode','').strip(); address1=request.form.get('address1','').strip(); address2=request.form.get('address2','').strip()
        try: days=max(1,int(request.form.get('rental_days','1') or 1)); start_d=datetime.strptime(start,'%Y-%m-%d').date(); end=(start_d+timedelta(days=days-1)).isoformat()
        except (ValueError,TypeError):
            conn.close(); flash('대여 시작일과 기간을 확인해주세요.'); return redirect(request.url)
        if not address1:
            conn.close(); flash('배송주소를 확인해주세요.'); return redirect(request.url)
        try: conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            conn.close(); flash('다른 주문이 처리 중입니다. 잠시 후 다시 시도해주세요.'); return redirect(request.url)
        r=conn.execute('SELECT * FROM reservations WHERE id=?',(r['id'],)).fetchone()
        items=conn.execute("SELECT ri.*,p.name,COALESCE(ps.size_name,p.size,'FREE') size_name FROM reservation_items ri JOIN products p ON p.id=ri.product_id LEFT JOIN product_sizes ps ON ps.id=ri.size_id WHERE ri.reservation_id=? ORDER BY ri.id",(r['id'],)).fetchall()
        total=0
        for item in items:
            avail=available_stock(conn,item['size_id'],start,end,exclude_reservation_id=r['id'])
            if item['qty']>avail:
                conn.rollback(); conn.close(); flash(f'{item["name"]} / {item["size_name"]}는 선택 기간에 {avail}벌만 가능합니다.'); return redirect(request.url)
            line=rental_line_price(item['daily_price'],item['extra_daily_price'],days,item['qty']); total+=line
            conn.execute('UPDATE reservation_items SET rental_days=?,total_price=? WHERE id=?',(days,line,item['id']))
        dispatch,pickup=auto_logistics_dates(conn,start,end); final=total+r['shipping_fee']+r['return_shipping_fee']+r['deposit_total']
        adjustment_type=''; adjustment_amount=0
        if r['payment_status']=='결제완료': adjustment_type,adjustment_amount=queue_amount_adjustment(conn,r,max(0,final-int(r['deposit_total'] or 0)),'비회원 대여기간 변경','비회원고객')
        conn.execute("""UPDATE reservations SET start_date=?,end_date=?,dispatch_date=?,pickup_date=?,rental_days=?,recipient_name=?,zipcode=?,address1=?,address2=?,total_price=?,final_amount=? WHERE id=?""",
                     (start,end,dispatch,pickup,days,recipient,zipcode,address1,address2,total,final,r['id']))
        conn.commit(); conn.close()
        if adjustment_type=='추가결제': flash(f'예약을 변경했습니다. 추가결제 {adjustment_amount:,}원이 필요합니다.')
        elif adjustment_type=='부분환불': flash(f'예약을 변경했습니다. 부분환불 {adjustment_amount:,}원이 등록되었습니다.')
        else: flash('예약 내용을 변경했습니다.')
        return redirect(url_for('order_lookup',order_no=ono,phone=phone))
    conn.close(); return render_template('order_change.html',r=r,items=items,today=date.today().isoformat())

# Gunicorn imports the module instead of executing __main__, so initialize on import.
seed_persistent_storage()
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host=os.environ.get('HOST', '127.0.0.1'), port=port, debug=False)
