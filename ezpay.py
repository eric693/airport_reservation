import time
import binascii
import requests
from urllib.parse import urlencode
from Crypto.Cipher import AES

MID      = "338919792"
HASH_KEY = "9s7eUA9G6nlu8dOUgELrLCDOaAs6KXeA"
HASH_IV  = "P3nVery9iVgdOopC"

def pkcs7_pad_32(text: str) -> bytes:
    data = text.encode("utf-8")
    pad_len = 32 - (len(data) % 32)
    return data + bytes([pad_len]) * pad_len

params = [
    ("RespondType",    "JSON"),
    ("Version",        "1.5"),
    ("TimeStamp",      str(int(time.time()))),
    ("TransNum",       ""),
    ("MerchantOrderNo", f"INV{int(time.time())}"),
    ("BuyerName",      "測試客人"),
    ("BuyerEmail",     ""),
    ("Category",       "B2C"),
    ("TaxType",        "1"),
    ("TaxRate",        "5"),
    ("Amt",            "300"),
    ("TaxAmt",         "15"),
    ("TotalAmt",       "315"),
    ("CarrierType",    ""),
    ("CarrierNum",     ""),
    ("LoveCode",       ""),
    ("PrintFlag",      "Y"),
    ("ItemName",       "機場接送定金"),
    ("ItemCount",      "1"),
    ("ItemUnit",       "筆"),
    ("ItemPrice",      "315"),
    ("ItemAmt",        "315"),
    ("Comment",        ""),
    ("CreateStatusTime", ""),
    ("Status",         "1"),
]

query = urlencode(params)
cipher = AES.new(HASH_KEY.encode("utf-8"), AES.MODE_CBC, HASH_IV.encode("utf-8"))
postdata = binascii.hexlify(cipher.encrypt(pkcs7_pad_32(query))).decode("ascii")

print("query =", query[:120])
print("PostData_ prefix =", postdata[:60])

resp = requests.post(
    "https://inv.ezpay.com.tw/Api/invoice_issue",
    data={"MerchantID_": MID, "PostData_": postdata},
    timeout=30,
)
print("status_code =", resp.status_code)
print("response =", resp.text)