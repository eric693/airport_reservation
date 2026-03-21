"""
ezPay 電子發票測試腳本
執行方式：python test_ezpay.py
"""

import os
import base64
import urllib.parse
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ── 填入你的 ezPay 設定 ──────────────────────────────────────────────
EZPAY_MERCHANT_ID = os.environ.get('EZPAY_MERCHANT_ID', '338919792')
EZPAY_HASH_KEY    = os.environ.get('EZPAY_HASH_KEY',    'uXbTWrmBjLArC0Ln93CZEqC20eY5jBE0')
EZPAY_HASH_IV     = os.environ.get('EZPAY_HASH_IV',     'PjvPgMj6OJppH8vC')
EZPAY_MODE        = 'test'  # test 或 prod

# ── 測試發票資料 ─────────────────────────────────────────────────────
TEST_ORDER_ID   = 9999
TEST_NAME       = '測試客人'
TEST_EMAIL      = ''         # 可留空
TEST_INV_TYPE   = 'personal' # personal / company / donate
TEST_CARRIER    = ''         # 手機條碼，例：/ABC1234，留空則開雲端發票

# ── API 網址 ─────────────────────────────────────────────────────────
if EZPAY_MODE == 'prod':
    API_URL = 'https://inv.ezpay.com.tw/Api/invoice_issue'
else:
    API_URL = 'https://cinv.ezpay.com.tw/Api/invoice_issue'

def encrypt(data_str):
    key = EZPAY_HASH_KEY.encode('utf-8')
    iv  = EZPAY_HASH_IV.encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(data_str.encode('utf-8'), AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')

def test_invoice():
    from datetime import datetime
    timestamp = int(datetime.now().timestamp())

    amt        = 315
    tax_amt    = round(amt - amt / 1.05)
    amt_excl   = amt - tax_amt

    # 買受人設定
    if TEST_INV_TYPE == 'company':
        carrier_type = ''
        carrier_num  = ''
        print_flag   = '1'
        buyer_uni_no = '50978670'  # 測試用統編
        love_code    = ''
        category     = 'B2B'
        buyer_name   = '樂高小客車租賃有限公司'
    elif TEST_INV_TYPE == 'personal' and TEST_CARRIER:
        carrier_type = '0'
        carrier_num  = TEST_CARRIER
        print_flag   = '0'
        buyer_uni_no = ''
        love_code    = ''
        category     = 'B2C'
        buyer_name   = TEST_NAME
    else:
        carrier_type = ''
        carrier_num  = ''
        print_flag   = '0'
        buyer_uni_no = ''
        love_code    = ''
        category     = 'B2C'
        buyer_name   = TEST_NAME

    params = {
        'RespondType':     'JSON',
        'Version':         '1.4',
        'TimeStamp':       timestamp,
        'MerchantOrderNo': f'INV{TEST_ORDER_ID}',
        'Status':          '1',
        'Category':        category,
        'BuyerName':       buyer_name,
        'BuyerEmail':      TEST_EMAIL,
        'BuyerUBN':        buyer_uni_no,
        'CarrierType':     carrier_type,
        'CarrierNum':      carrier_num,
        'PrintFlag':       print_flag,
        'TaxType':         '1',
        'TaxRate':         '5',
        'Amt':             amt_excl,
        'TaxAmt':          tax_amt,
        'TotalAmt':        amt,
        'ItemName':        f'機場接送定金（訂單#{TEST_ORDER_ID}）',
        'ItemCount':       '1',
        'ItemUnit':        '筆',
        'ItemAmt':         amt_excl,
        'ItemTaxAmt':      tax_amt,
        'Comment':         '',
        'LoveCode':        love_code,
    }

    print('=' * 50)
    print('ezPay 電子發票測試')
    print(f'環境：{"正式" if EZPAY_MODE == "prod" else "測試"}')
    print(f'API：{API_URL}')
    print(f'商店代號：{EZPAY_MERCHANT_ID}')
    print(f'HashKey：{EZPAY_HASH_KEY[:8]}...')
    print(f'HashIV：{EZPAY_HASH_IV[:4]}...')
    print('=' * 50)
    print(f'發票類型：{TEST_INV_TYPE}')
    print(f'訂單編號：INV{TEST_ORDER_ID}')
    print(f'金額：NT${amt}（未稅 {amt_excl}，稅 {tax_amt}）')
    print('=' * 50)

    post_data_str = urllib.parse.urlencode(params)
    post_data_enc = encrypt(post_data_str)

    print('正在呼叫 API...')
    try:
        resp = requests.post(API_URL, data={
            'MerchantID_': EZPAY_MERCHANT_ID,
            'PostData_':   post_data_enc,
        }, timeout=15)

        print(f'HTTP 狀態碼：{resp.status_code}')
        result = resp.json()
        print(f'API 回傳：{result}')
        print('=' * 50)

        if result.get('Status') == 'SUCCESS':
            inv_data = result.get('Result', {})
            inv_no   = inv_data.get('InvoiceNumber', '')
            inv_date = inv_data.get('InvoiceDate', '')
            print(f'✅ 發票開立成功！')
            print(f'發票號碼：{inv_no}')
            print(f'開立日期：{inv_date}')
        else:
            print(f'❌ 發票開立失敗')
            print(f'錯誤訊息：{result.get("Message", "未知錯誤")}')
            print(f'完整回傳：{result}')

    except Exception as e:
        print(f'❌ 發生例外錯誤：{e}')

if __name__ == '__main__':
    test_invoice()